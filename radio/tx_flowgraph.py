"""
TX Flowgraph — simplified static path.

Signal chain (static, no hopping):
  FrameSource (Python thread → queue) → FEC+DSSS (pre-processing)
  → FrameChipSource → BPSK Modulator → RRC Filter
  → [BasebandHopper] → UHD Sink

No heartbeat, no BurstGate, no adder, no HopController.
The frame feeder paces itself via sleep + queue blocking.
"""
from __future__ import annotations

import queue
import threading
import time
import logging
import numpy as np
from typing import Callable, Optional

from gnuradio import gr, blocks, filter as gr_filter, analog, zeromq
from gnuradio import uhd as gr_uhd
import pmt

from core.config_manager import SurrogateConfig
from core.hop_scheduler import HopScheduler
from core.frame_generator import FrameGenerator
from core.fec_codec import FECCodec
from core.spreading_codes import get_code
from core.anomaly_injector import AnomalyInjector
from radio.blocks.anomaly_block import AnomalyBlock
from radio.blocks.baseband_hopper import BasebandHopper
from radio.runtime_modulation import validate_runtime_modulation

log = logging.getLogger(__name__)


class FrameChipSource(gr.sync_block):
    """Pulls chip arrays from a queue, outputs uint8 stream.
    Outputs 0 (which maps to +1.0 in BPSK) when queue is empty."""
    def __init__(self, chip_queue: queue.Queue):
        gr.sync_block.__init__(self, "Frame Chip Source",
                               in_sig=[], out_sig=[np.uint8])
        self._queue = chip_queue
        self._current: Optional[np.ndarray] = None
        self._offset = 0

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        idx = 0
        while idx < n:
            if self._current is None or self._offset >= len(self._current):
                try:
                    self._current = self._queue.get_nowait()
                    self._offset = 0
                except queue.Empty:
                    out[idx:] = 0
                    return n
            take = min(n - idx, len(self._current) - self._offset)
            out[idx:idx + take] = self._current[self._offset:self._offset + take]
            idx += take
            self._offset += take
        return n


class TXFlowgraph(gr.top_block):

    def __init__(self, cfg: SurrogateConfig, hop_scheduler: HopScheduler,
                 frame_gen: FrameGenerator, fec_codec: FECCodec,
                 anomaly_injector: AnomalyInjector,
                 frame_callback: Optional[Callable] = None,
                 baseband_hopper: Optional[BasebandHopper] = None,
                 start_delay_s: float = 0.0):
        gr.top_block.__init__(self, "Surrogate TX")

        self._cfg = cfg
        self._hop_scheduler = hop_scheduler
        self._frame_gen = frame_gen
        self._fec_codec = fec_codec
        self._anomaly = anomaly_injector
        self._chip_queue: queue.Queue = queue.Queue(maxsize=2)
        self._feeder_thread: Optional[threading.Thread] = None
        self._running = False
        self.frame_count = 0
        self._frame_callback = frame_callback
        self._baseband_hopper = baseband_hopper
        self._start_delay_s = max(0.0, float(start_delay_s))

        rf = cfg.rf
        mod = cfg.modulation
        timing = cfg.timing
        frame = cfg.frame

        self._spread_code = get_code(mod.spreading)
        self._code_len = mod.spreading.code_length

        # ---------- UHD Sink ----------
        if cfg.rf.simulation:
            self._throttle = blocks.throttle(gr.sizeof_gr_complex, rf.sample_rate)
            self._zmq_sink = zeromq.pub_sink(gr.sizeof_gr_complex, 1,
                                             "tcp://127.0.0.1:5555", 100, False, -1)
            self._uhd_sink = None
        else:
            stream_args = gr_uhd.stream_args("fc32", "sc16")
            stream_args.channels = [rf.tx_channel]
            stream_args.args = "num_send_frames=64"
            self._uhd_sink = gr_uhd.usrp_sink(rf.gnd_device, stream_args)
            self._uhd_sink.set_samp_rate(rf.sample_rate)
            self._uhd_sink.set_center_freq(rf.center_frequency, 0)
            self._uhd_sink.set_gain(rf.tx_gain, 0)
            self._uhd_sink.set_antenna(rf.tx_antenna, 0)
            self._uhd_sink.set_bandwidth(rf.tx_bandwidth, 0)
            if cfg.rf.hw_mode == "dual_b210":
                self._uhd_sink.set_clock_source(rf.b210_ref_source)
            self._zmq_sink = None
            self._throttle = None

        # ---------- Modulation chain ----------
        validate_runtime_modulation(mod.type)

        self._chip_src = FrameChipSource(self._chip_queue)
        self._mod_type = mod.type

        self._bit_to_float = blocks.char_to_float(1, 1.0)
        self._scale = blocks.multiply_const_ff(-2.0)
        self._offset_add = blocks.add_const_ff(1.0)
        self._float_to_complex = blocks.float_to_complex()
        self._null_src = blocks.null_source(gr.sizeof_float)

        # RRC pulse shaping filter at chip_rate for the current BPSK chip stream.
        sps = rf.sample_rate / mod.chip_rate_sps
        ps = mod.pulse_shaping
        n_taps = ps.span_symbols * int(sps) + 1
        rrc_taps = gr_filter.firdes.root_raised_cosine(
            1.0, rf.sample_rate, mod.chip_rate_sps, ps.rolloff, n_taps
        )
        self._rrc_filter = gr_filter.interp_fir_filter_ccf(int(sps), rrc_taps)

        # Anomaly block (bypassed when disabled)
        self._anomaly_block = AnomalyBlock(anomaly_injector, rf.sample_rate)

        # IQ recording tap
        self._tx_record_valve = blocks.copy(gr.sizeof_gr_complex)
        self._tx_record_sink = blocks.null_sink(gr.sizeof_gr_complex)
        self._tx_record_valve.set_enabled(False)
        self._tx_iq_connected = False

        self._connect()

    def _connect(self):
        # Chips -> BPSK -> RRC
        self.connect(self._chip_src, self._bit_to_float)
        self.connect(self._bit_to_float, self._scale)
        self.connect(self._scale, self._offset_add)
        self.connect(self._offset_add, (self._float_to_complex, 0))
        self.connect(self._null_src, (self._float_to_complex, 1))
        self.connect(self._float_to_complex, self._rrc_filter)
        last = self._rrc_filter

        # Anomaly (if enabled)
        if self._cfg.anomaly.enabled or self._cfg.anomaly.interference.enabled:
            self.connect(last, self._anomaly_block)
            last = self._anomaly_block

        # Baseband hopper (if hopping enabled)
        if self._baseband_hopper is not None:
            self.connect(last, self._baseband_hopper)
            last = self._baseband_hopper

        # Sink
        if self._cfg.rf.simulation:
            self.connect(last, self._throttle)
            self.connect(self._throttle, self._zmq_sink)
        else:
            self.connect(last, self._uhd_sink)

    # ------------------------------------------------------------------
    # Runtime control
    # ------------------------------------------------------------------

    def set_tx_gain(self, gain_db: float) -> None:
        if self._uhd_sink:
            self._uhd_sink.set_gain(gain_db)

    def enable_iq_recording(self, file_sink) -> None:
        self._tx_record_sink = file_sink
        self._tx_record_valve.set_enabled(True)
        self.lock()
        self.connect(self._rrc_filter, self._tx_record_valve)
        self.connect(self._tx_record_valve, self._tx_record_sink)
        self._tx_iq_connected = True
        self.unlock()

    def disable_iq_recording(self) -> None:
        if self._tx_iq_connected:
            self.lock()
            self.disconnect(self._rrc_filter, self._tx_record_valve)
            self.disconnect(self._tx_record_valve, self._tx_record_sink)
            self._tx_iq_connected = False
            self.unlock()
            self._tx_record_valve.set_enabled(False)

    # ------------------------------------------------------------------
    # Frame feeder (separate thread)
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        super().start()
        self._feeder_thread = threading.Thread(
            target=self._frame_feeder, name="FrameFeeder", daemon=True
        )
        self._feeder_thread.start()

    def stop(self):
        self._running = False
        super().stop()

    def _frame_feeder(self):
        burst_s = self._cfg.timing.burst_duration_ms / 1000.0
        frame_id = 0

        if self._start_delay_s > 0:
            log.info("TX feeder delaying %.3f s for RX startup settle", self._start_delay_s)
            deadline = time.monotonic() + self._start_delay_s
            while self._running and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))

        while self._running:
            if self._anomaly.should_drop_burst():
                time.sleep(burst_s)
                continue

            if self._cfg.frame.payload_hex:
                try:
                    payload = bytes.fromhex(self._cfg.frame.payload_hex)
                except ValueError:
                    payload = bytes(range(256)) * 4
            else:
                payload = bytes(range(256)) * 4

            payload_bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))
            payload_bits = self._anomaly.apply_ber(payload_bits)
            payload = np.packbits(payload_bits).tobytes()

            frame_bits = self._frame_gen.build_frame(payload, frame_id)

            pre_len = self._cfg.frame.preamble.length_bits
            preamble_chips = frame_bits[:pre_len]
            data_bits = frame_bits[pre_len:]

            cl = self._code_len
            ref_code = self._spread_code[:cl].reshape(1, cl)
            data_chips = (data_bits.reshape(-1, 1) ^ ref_code).reshape(-1).astype(np.uint8)

            all_chips = np.concatenate([preamble_chips, data_chips]).astype(np.uint8)

            if self._frame_callback:
                try:
                    self._frame_callback(frame_id, payload, time.time())
                except Exception:
                    pass

            frame_id += 1
            self.frame_count += 1

            try:
                self._chip_queue.put(all_chips, timeout=burst_s * 4)
            except queue.Full:
                log.warning("TX chip queue full -- dropping frame")
