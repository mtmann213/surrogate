"""
TX Flowgraph.

Signal chain:
  FrameSource (Python thread → queue) → FEC+DSSS (Python pre-processing)
  → PreambleInserter → BurstGate → BPSK/QPSK Modulator → RRC Filter
  → AnomalyBlock → [InterferenceAdder] → HopController → UHD Sink

The frame bytes are generated in a separate thread (FrameFeeder), encoded
and spread in Python, then fed into the GNU Radio stream via a Vector Source
updated via a ZMQ or queue-based source block.
"""
from __future__ import annotations

import queue
import threading
import time
import logging
import numpy as np
from typing import Optional

from gnuradio import gr, blocks, filter as gr_filter, analog, digital, zeromq
from gnuradio import uhd as gr_uhd
import pmt

from core.config_manager import SurrogateConfig
from core.hop_scheduler import HopScheduler
from core.frame_generator import FrameGenerator
from core.fec_codec import FECCodec
from core.spreading_codes import get_code
from core.anomaly_injector import AnomalyInjector
from radio.blocks.burst_gate import BurstGate
from radio.blocks.hop_controller import HopController
from radio.blocks.preamble_inserter import PreambleInserter
from radio.blocks.anomaly_block import AnomalyBlock
from radio.blocks.interleave import Deinterleave

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: Bit-stream source (feeds pre-built frame chips into GR stream)
# ---------------------------------------------------------------------------

class FrameChipSource(gr.sync_block):
    """
    Pulls pre-encoded, pre-spread chip arrays from a queue and outputs them
    as a uint8 stream. Outputs silence (zeros) when the queue is empty.
    """
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
                 frame_callback: Optional[Callable] = None):
        gr.top_block.__init__(self, "Surrogate TX")

        self._cfg = cfg
        self._hop_scheduler = hop_scheduler
        self._frame_gen = frame_gen
        self._fec_codec = fec_codec
        self._anomaly = anomaly_injector
        self._chip_queue: queue.Queue = queue.Queue(maxsize=16)
        self._feeder_thread: Optional[threading.Thread] = None
        self._running = False
        self.frame_count = 0
        self._frame_callback = frame_callback

        rf = cfg.rf
        mod = cfg.modulation
        timing = cfg.timing
        frame = cfg.frame

        # Pre-compute spreading code
        self._spread_code = get_code(mod.spreading)
        self._code_len = mod.spreading.code_length

        # Burst parameters
        burst_s = timing.burst_duration_ms / 1000.0
        guard_s = timing.transition_time_ms / 1000.0
        preamble_s = timing.preamble_duration_ms / 1000.0
        sample_rate = rf.sample_rate
        chip_rate = mod.chip_rate_sps
        sps = sample_rate / chip_rate  # samples per chip

        # Compute chips per burst
        preamble_chips = frame.preamble.length_bits
        data_bits = (frame.payload_bits +
                     (frame.invariant.length_bits if frame.invariant.enabled else 0))
        coded_bits = fec_codec.coded_length(data_bits) if fec_codec.enabled else data_bits
        data_chips = coded_bits * self._code_len
        self._total_data_chips = data_chips

        # ---- UHD Sink / Simulation Sink ----
        if cfg.rf.simulation:
            # Use ZMQ PUB sink to broadcast I/Q samples to simulated RX
            # Throttle is required when no hardware sink is present to pace the flowgraph
            self._throttle = blocks.throttle(gr.sizeof_gr_complex, sample_rate)
            self._zmq_sink = zeromq.pub_sink(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:5555", 100, False, -1)
            self._uhd_sink = None
            log.info("TX Simulation mode: ZMQ PUB at tcp://127.0.0.1:5555")
        else:
            device_addr = rf.gnd_device
            stream_args = gr_uhd.stream_args("fc32", "sc16")
            stream_args.channels = [rf.tx_channel]
            self._uhd_sink = gr_uhd.usrp_sink(
                device_addr,
                stream_args,
            )
            # Initial freq is hop[0] so the first burst the HopController
            # gates is on the same frequency RX is initially listening on.
            initial_freq = hop_scheduler.frequency_at(0)

            self._uhd_sink.set_samp_rate(sample_rate)
            self._uhd_sink.set_center_freq(initial_freq, 0)
            self._uhd_sink.set_gain(rf.tx_gain, 0)
            self._uhd_sink.set_antenna(rf.tx_antenna, 0)
            self._uhd_sink.set_bandwidth(rf.tx_bandwidth, 0)
            if cfg.rf.hw_mode == "dual_b210":
                self._uhd_sink.set_clock_source(rf.b210_ref_source)
            self._zmq_sink = None
            self._throttle = None

        # ---- Heartbeat Source ----
        self._heartbeat = analog.noise_source_c(analog.GR_GAUSSIAN, 1e-6)

        # ---- Frame chip source ----
        self._chip_src = FrameChipSource(self._chip_queue)

        # ---- BPSK/QPSK symbol mapper ----
        # Map 0 → +1.0, 1 → -1.0
        self._bit_to_float = blocks.char_to_float(1, 1.0)
        # 0→+1, 1→-1 via: float = 1.0 - 2.0*bit
        self._scale = blocks.multiply_const_ff(-2.0)
        self._offset_add = blocks.add_const_ff(1.0)

        # ---- Float to complex (BPSK: I=data, Q=0; QPSK: interleave pairs) ----
        self._float_to_complex = blocks.float_to_complex()
        if mod.type == "bpsk":
            # Q channel must be a proper zero source — sig_source_f(0,...) has
            # undefined behaviour; null_source outputs zeros on demand at any rate.
            self._null_src = blocks.null_source(gr.sizeof_float)
        else:
            self._null_src = blocks.null_source(gr.sizeof_float)

        # ---- MSK / GMSK Modulator ----
        if mod.type == "msk":
            self._msk_mod = digital.gmsk_mod(int(sps), 4.0, 1) # bt=4.0 is approx MSK
        elif mod.type == "gmsk":
            self._msk_mod = digital.gmsk_mod(int(sps), 0.35, 1)
        else:
            self._msk_mod = None

        # ---- OQPSK components (parallel I/Q processing) ----
        # Standard OQPSK: even chips → I channel, odd chips → Q channel (delayed by T/2).
        # The deinterleave block splits the chip stream into I/Q, each goes through
        # its own interpolating RRC filter, Q gets a T/2 delay, then they combine.
        if mod.type == "oqpsk":
            sps_int = int(sps)
            ps_oq = mod.pulse_shaping
            n_taps_oq = ps_oq.span_symbols * sps_int + 1
            rrc_taps_oq = gr_filter.firdes.root_raised_cosine(
                1.0, sample_rate, chip_rate, ps_oq.rolloff, n_taps_oq
            )
            self._rrc_i = gr_filter.interp_fir_filter_fff(sps_int, rrc_taps_oq)
            self._rrc_q = gr_filter.interp_fir_filter_fff(sps_int, rrc_taps_oq)
            # Q delay applied AFTER interpolation: sps/2 samples at sample_rate = T/2 chip period.
            self._q_delay = blocks.delay(gr.sizeof_float, sps_int // 2)
            self._deinterleave = Deinterleave()
        else:
            self._rrc_i = None
            self._rrc_q = None
            self._q_delay = None
            self._deinterleave = None

        # ---- RRC Pulse Shaping Filter ----
        ps = mod.pulse_shaping
        n_taps = ps.span_symbols * int(sps) + 1
        rrc_taps = gr_filter.firdes.root_raised_cosine(
            1.0, sample_rate, chip_rate, ps.rolloff, n_taps
        )
        # Interpolating FIR: upsample by sps
        self._rrc_filter = gr_filter.interp_fir_filter_ccf(int(sps), rrc_taps)

        # ---- Anomaly Block ----
        self._anomaly_block = AnomalyBlock(anomaly_injector, sample_rate)

        # ---- Interference source ----
        self._interference_enabled = cfg.anomaly.interference.enabled
        intr = cfg.anomaly.interference
        self._interference_src = self._build_interference_source(intr, sample_rate)
        self._interference_gain = blocks.multiply_const_cc(
            10 ** (intr.relative_power_db / 20.0) if self._interference_enabled else 0.0
        )
        self._interference_adder = blocks.add_cc()

        # ---- Burst Gate ----
        self._burst_gate = BurstGate(sample_rate, burst_s, guard_s)

        # ---- Adder ----
        self._adder = blocks.add_cc()

        # ---- Hop Controller ----
        self._hop_ctrl = HopController(hop_scheduler, sample_rate, burst_s, guard_s, rf.tx_channel)

        # ---- IQ Recorder tap ----
        self._tx_record_valve = blocks.copy(gr.sizeof_gr_complex)
        self._tx_record_sink = blocks.null_sink(gr.sizeof_gr_complex)
        self._tx_record_valve.set_enabled(False)
        self._tx_iq_connected = False

        # ---- Connect flowgraph ----
        self._connect()

    def _build_interference_source(self, intr_cfg, sample_rate: float):
        """Build the appropriate interference source block."""
        from gnuradio import analog, blocks as gr_blocks
        src_type = intr_cfg.source_type

        if not intr_cfg.enabled:
            return analog.sig_source_c(sample_rate, analog.GR_CONST_WAVE, 0, 0, 0)

        if src_type == "tone":
            return analog.sig_source_c(
                sample_rate, analog.GR_COS_WAVE,
                intr_cfg.tone_freq_hz, 1.0, 0
            )
        elif src_type == "awgn":
            return analog.noise_source_c(analog.GR_GAUSSIAN, 1.0, 42)
        elif src_type == "file":
            return gr_blocks.file_source(
                gr.sizeof_gr_complex, intr_cfg.file_path, repeat=True
            )
        else:
            return analog.sig_source_c(sample_rate, analog.GR_CONST_WAVE, 0, 0, 0)

    def _connect(self):
        # 1. Continuous timing path (The Heartbeat)
        # This keeps the HopController's item counter perfectly in sync with the hardware clock.
        self.connect(self._heartbeat, (self._adder, 0))
        self.connect(self._adder, self._hop_ctrl)
        
        last_hop = self._hop_ctrl

        # 2. Bursty data path
        if self._cfg.modulation.type == "msk":
            self.connect(self._chip_src, self._msk_mod)
            last_shaped = self._msk_mod
        elif self._cfg.modulation.type == "oqpsk":
            self.connect(self._chip_src, self._bit_to_float)
            self.connect(self._bit_to_float, self._scale)
            self.connect(self._scale, self._offset_add)
            # OQPSK: split chips into I (even) and Q (odd) channels.
            # Each channel gets its own interpolating RRC filter.
            # Q is delayed by T/2 (sps//2 samples at sample_rate) before combining.
            self.connect(self._offset_add, self._deinterleave)
            self.connect((self._deinterleave, 0), self._rrc_i)
            self.connect((self._deinterleave, 1), self._rrc_q)
            self.connect(self._rrc_q, self._q_delay)
            self.connect(self._rrc_i, (self._float_to_complex, 0))
            self.connect(self._q_delay, (self._float_to_complex, 1))
            last_shaped = self._float_to_complex
        else:
            self.connect(self._chip_src, self._bit_to_float)
            self.connect(self._bit_to_float, self._scale)
            self.connect(self._scale, self._offset_add)
            if self._cfg.modulation.type == "bpsk":
                self.connect(self._offset_add, (self._float_to_complex, 0))
                self.connect(self._null_src, (self._float_to_complex, 1))
            else:
                self.connect(self._offset_add, (self._float_to_complex, 0))
                self.connect(self._offset_add, (self._float_to_complex, 1))
            self.connect(self._float_to_complex, self._rrc_filter)
            last_shaped = self._rrc_filter

        last = last_shaped

        if self._cfg.anomaly.enabled or self._interference_enabled:
            self.connect(last, self._anomaly_block)
            self.connect(self._anomaly_block, (self._interference_adder, 0))
            self.connect(self._interference_src, self._interference_gain)
            self.connect(self._interference_gain, (self._interference_adder, 1))
            last = self._interference_adder

        # Gate the bursty signal
        self.connect(last, self._burst_gate)
        # Inject the gated bursts into the continuous timing stream
        self.connect(self._burst_gate, (self._adder, 1))

        # Output from the hopped heartbeat
        last = last_hop

        # IQ recording tap is added dynamically via enable_iq_recording() / lock+unlock.

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

    def set_interference_power(self, db: float) -> None:
        self._interference_gain.set_k(10 ** (db / 20.0))

    def set_interference_enabled(self, enabled: bool) -> None:
        self._interference_gain.set_k(
            10 ** (self._cfg.anomaly.interference.relative_power_db / 20.0)
            if enabled else 0.0
        )

    def enable_iq_recording(self, file_sink) -> None:
        self._tx_record_sink = file_sink
        self._tx_record_valve.set_enabled(True)
        self.lock()
        self.connect(self._hop_ctrl, self._tx_record_valve)
        self.connect(self._tx_record_valve, self._tx_record_sink)
        self._tx_iq_connected = True
        self.unlock()

    def disable_iq_recording(self) -> None:
        if self._tx_iq_connected:
            self.lock()
            self.disconnect(self._hop_ctrl, self._tx_record_valve)
            self.disconnect(self._tx_record_valve, self._tx_record_sink)
            self._tx_iq_connected = False
            self.unlock()
            self._tx_record_valve.set_enabled(False)

    # ------------------------------------------------------------------
    # Frame feeder (runs in separate thread)
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        self._feeder_thread = threading.Thread(
            target=self._frame_feeder, name="FrameFeeder", daemon=True
        )
        self._feeder_thread.start()
        super().start()

    def stop(self):
        self._running = False
        super().stop()

    def _frame_feeder(self):
        """
        Continuously builds frames, encodes, spreads, and pushes chip arrays
        into the queue for FrameChipSource to consume.
        """
        burst_s = self._cfg.timing.burst_duration_ms / 1000.0
        mod = self._cfg.modulation
        frame_id = 0

        while self._running:
            # Check for burst dropout
            if self._anomaly.should_drop_burst():
                time.sleep(burst_s)
                continue

            # Build payload
            if self._cfg.frame.payload_hex:
                try:
                    payload = bytes.fromhex(self._cfg.frame.payload_hex)
                except ValueError:
                    log.error("Invalid payload_hex: %s", self._cfg.frame.payload_hex)
                    payload = bytes(range(256)) * 4
            else:
                payload = bytes(range(256)) * 4  # repeating pattern

            # Apply BER at bit level
            payload_bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))
            payload_bits = self._anomaly.apply_ber(payload_bits)
            payload = np.packbits(payload_bits).tobytes()

            # Build frame bits (preamble | FEC(invariant|payload))
            frame_bits = self._frame_gen.build_frame(payload, frame_id)

            # Preamble section: no spreading
            pre_len = self._cfg.frame.preamble.length_bits
            preamble_chips = frame_bits[:pre_len]

            # Data section: 1-to-N DSSS spreading.
            # Each coded bit produces code_len chips: chip[j] = bit XOR code[j].
            # Vectorised: reshape to (n_bits, 1) XOR (1, code_len) → (n_bits, code_len)
            # then flatten to 1-D chip stream.
            data_bits = frame_bits[pre_len:]
            cl = self._code_len
            ref_code = self._spread_code[:cl].reshape(1, cl)             # (1, cl)
            data_chips = (data_bits.reshape(-1, 1) ^ ref_code).reshape(-1).astype(np.uint8)

            # Assemble chips: [preamble chips | data chips | guard padding]
            # The BurstGate runs an independent period counter of
            # (burst_samples + guard_samples).  Without padding, burst N+1's
            # preamble starts streaming into the gate exactly when it closes for
            # the guard interval, so every burst after the first is transmitted
            # without a preamble and the RX can never detect them.
            #
            # We pad with random chips instead of zeros to keep the RX Timing
            # Recovery and Costas Loop locked during the inter-frame gap.
            guard_chips = int(
                self._cfg.timing.transition_time_ms / 1000.0
                * mod.chip_rate_sps
            )
            guard_padding = np.random.randint(0, 2, guard_chips, dtype=np.uint8)
            all_chips = np.concatenate(
                [preamble_chips, data_chips, guard_padding]
            ).astype(np.uint8)

            # Notify callback
            if self._frame_callback:
                try:
                    self._frame_callback(frame_id, payload, time.time())
                except Exception:
                    pass

            frame_id += 1
            self.frame_count += 1

            # Push to queue — BLOCKING so the hardware sample clock paces the
            # feeder. Once the queue is full (4 frames buffered), each put()
            # blocks until the chip source drains one frame (~17.768 ms), which
            # is exactly the right inter-frame gap.  No separate sleep needed.
            jitter_us = (self._cfg.timing.timing_jitter_us +
                         self._anomaly.state.extra_timing_jitter_us)
            if jitter_us > 0:
                time.sleep(abs(np.random.normal(0, jitter_us * 1e-6)))
            try:
                self._chip_queue.put(all_chips, timeout=burst_s * 8)
            except queue.Full:
                log.warning("TX chip queue full — dropping frame")
