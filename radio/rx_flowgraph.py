"""
RX Flowgraph.

Signal chain:
  UHD Source -> DC Blocker -> AGC -> RRC Matched Filter
  -> Timing Recovery (M&M) -> Costas Loop -> FrameSink

FrameSink handles the complete frame pipeline internally:
  preamble detection -> chip collection -> despreading -> callback
"""
from __future__ import annotations

import logging
import numpy as np
from typing import Callable, Optional

from gnuradio import gr, blocks, filter as gr_filter, analog, digital, zeromq
from gnuradio import uhd as gr_uhd
import pmt

from core.config_manager import SurrogateConfig
from core.hop_scheduler import HopScheduler
from core.frame_generator import FrameGenerator
from core.fec_codec import FECCodec
from core.spreading_codes import get_code
from radio.blocks.frame_sink import FrameSink
from radio.blocks.hop_controller import HopController
from radio.blocks.baseband_hopper import BasebandHopper
from radio.runtime_modulation import validate_runtime_modulation

log = logging.getLogger(__name__)


class RXFlowgraph(gr.top_block):

    def __init__(self, cfg: SurrogateConfig, hop_scheduler: HopScheduler,
                 frame_gen: FrameGenerator, fec_codec: FECCodec,
                 frame_callback: Optional[Callable] = None,
                 baseband_hopper: Optional[BasebandHopper] = None):
        gr.top_block.__init__(self, "Surrogate RX")

        self._cfg             = cfg
        self._hop_scheduler   = hop_scheduler
        self._frame_gen       = frame_gen
        self._fec_codec       = fec_codec
        self._frame_callback  = frame_callback
        self._baseband_hopper = baseband_hopper

        rf      = cfg.rf
        mod     = cfg.modulation
        timing  = cfg.timing
        frame   = cfg.frame

        validate_runtime_modulation(mod.type)

        sample_rate = rf.sample_rate
        chip_rate   = mod.chip_rate_sps
        sps         = sample_rate / chip_rate
        self._sps   = sps
        self._mod_type = mod.type
        burst_s     = timing.burst_duration_ms / 1000.0
        guard_s     = timing.transition_time_ms / 1000.0

        spread_code = get_code(mod.spreading)
        code_len    = mod.spreading.code_length

        data_info_bits = (frame.payload_bits +
                          (frame.invariant.length_bits if frame.invariant.enabled else 0))
        if fec_codec.enabled:
            aligned_bits = ((data_info_bits + 7) // 8) * 8
            coded_bits   = fec_codec.coded_length(aligned_bits)
        else:
            coded_bits = data_info_bits

        # ---- UHD Source / Simulation Source ----
        if cfg.rf.simulation:
            self._uhd_src = zeromq.sub_source(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:5555", 100, False, -1)
            log.info("RX Simulation mode: ZMQ SUB at tcp://127.0.0.1:5555")
        else:
            stream_args = gr_uhd.stream_args("fc32", "sc16")
            stream_args.channels = [rf.rx_channel]
            stream_args.args = "num_recv_frames=64"
            rx_device = rf.msl_device if cfg.rf.hw_mode != "single_b210" else rf.gnd_device

            initial_freq = hop_scheduler.frequency_at(0)

            self._uhd_src = gr_uhd.usrp_source(rx_device, stream_args)
            self._uhd_src.set_samp_rate(sample_rate)
            self._uhd_src.set_center_freq(initial_freq, 0)
            self._uhd_src.set_gain(rf.rx_gain, 0)
            self._uhd_src.set_antenna(rf.rx_antenna, 0)
            self._uhd_src.set_bandwidth(rf.rx_bandwidth, 0)
            if cfg.rf.hw_mode == "dual_b210":
                slave_ref = "external" if rf.b210_ref_source == "internal" else "internal"
                self._uhd_src.set_clock_source(slave_ref)

        # ---- DC Blocker ----
        self._dc_blocker = gr_filter.dc_blocker_cc(32, True)

        # ---- AGC ----
        self._agc = analog.agc_cc(1e-4, 1.0, 1.0)
        self._agc.set_max_gain(65536)

        # ---- RRC Matched Filter ----
        ps     = mod.pulse_shaping
        n_taps = ps.span_symbols * int(sps) + 1
        rrc_taps = gr_filter.firdes.root_raised_cosine(
            1.0, sample_rate, chip_rate, ps.rolloff, n_taps
        )
        self._rrc_mf = gr_filter.fir_filter_ccf(1, rrc_taps)

        # ---- M&M Timing Recovery (chip-rate complex output) ----
        self._timing_recovery = digital.clock_recovery_mm_cc(
            float(sps), 0.25 * 0.175 ** 2, 0.5, 0.175, 0.005,
        )

        # ---- Costas Loop (BPSK carrier phase recovery) ----
        self._costas = digital.costas_loop_cc(2 * np.pi / 500.0, 2)

        # ---- Frame Sink (complex64 input, preamble detect + despread + callback) ----
        preamble_bits = frame_gen.preamble_bits
        self._frame_sink = FrameSink(
            preamble_bits   = preamble_bits,
            frame_coded_bits= coded_bits,
            spreading_code  = spread_code,
            code_length     = code_len,
            callback        = self._on_frame_received,
            diag_dir        = "recordings" if cfg.anomaly.enabled else None,
        )

        # ---- Hop Controller ----
        self._hop_ctrl = HopController(
            hop_scheduler, sample_rate, burst_s, guard_s, rf.rx_channel,
            mode='rx', uhd_src=self._uhd_src,
        )

        # ---- IQ Recording tap ----
        self._rx_record_valve = blocks.copy(gr.sizeof_gr_complex)
        self._rx_record_sink  = blocks.null_sink(gr.sizeof_gr_complex)
        self._rx_record_valve.set_enabled(False)
        self._rx_iq_connected = False

        self._connect()

    def _connect(self):
        # UHD -> DC blocker -> AGC
        self.connect(self._uhd_src, self._dc_blocker)
        self.connect(self._dc_blocker, self._agc)

        last = self._agc

        if self._baseband_hopper is not None:
            self.connect(last, self._baseband_hopper)
            last = self._baseband_hopper

        if self._cfg.hopping.enabled:
            self.connect(last, self._hop_ctrl)
            last = self._hop_ctrl

        # RRC -> M&M -> Costas -> FrameSink
        self.connect(last, self._rrc_mf)
        self.connect(self._rrc_mf, self._timing_recovery)
        self.connect(self._timing_recovery, self._costas)
        self.connect(self._costas, self._frame_sink)

    def _on_frame_received(self, coded_bits: np.ndarray,
                           timestamp: float, snr: float) -> None:
        payload, fec_ok = self._frame_gen.parse_frame(coded_bits)
        if self._frame_callback:
            self._frame_callback(payload, timestamp, snr, fec_ok)

    def set_rx_gain(self, gain_db: float) -> None:
        if hasattr(self._uhd_src, "set_gain"):
            self._uhd_src.set_gain(gain_db)

    def set_frame_callback(self, fn: Callable) -> None:
        self._frame_callback = fn

    def enable_iq_recording(self, file_sink) -> None:
        self._rx_record_sink = file_sink
        self._rx_record_valve.set_enabled(True)
        self.lock()
        self.connect(self._agc, self._rx_record_valve)
        self.connect(self._rx_record_valve, self._rx_record_sink)
        self._rx_iq_connected = True
        self.unlock()

    def disable_iq_recording(self) -> None:
        if self._rx_iq_connected:
            self.lock()
            self.disconnect(self._agc, self._rx_record_valve)
            self.disconnect(self._rx_record_valve, self._rx_record_sink)
            self._rx_iq_connected = False
            self.unlock()
            self._rx_record_valve.set_enabled(False)

    def get_snr(self) -> float:
        return self._frame_sink.last_snr

    def get_frame_count(self) -> int:
        return self._frame_sink.frames_received

    def get_agc_gain(self) -> float:
        try:
            return float(self._agc.gain())
        except Exception:
            return 0.0
