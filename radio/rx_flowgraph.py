"""
RX Flowgraph.

Signal chain:
  UHD Source → DC Blocker → AGC → RRC Matched Filter
  → Timing Recovery (M&M) → Costas Loop → complex_to_real → FrameSink

FrameSink handles the complete frame pipeline internally:
  preamble detection → integrate-and-dump despreading → callback
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
from radio.blocks.interleave import Interleave

log = logging.getLogger(__name__)


class RXFlowgraph(gr.top_block):

    def __init__(self, cfg: SurrogateConfig, hop_scheduler: HopScheduler,
                 frame_gen: FrameGenerator, fec_codec: FECCodec,
                 frame_callback: Optional[Callable] = None):
        gr.top_block.__init__(self, "Surrogate RX")

        self._cfg             = cfg
        self._hop_scheduler   = hop_scheduler
        self._frame_gen       = frame_gen
        self._fec_codec       = fec_codec
        self._frame_callback  = frame_callback

        rf      = cfg.rf
        mod     = cfg.modulation
        timing  = cfg.timing
        frame   = cfg.frame

        sample_rate = rf.sample_rate
        chip_rate   = mod.chip_rate_sps
        sps         = sample_rate / chip_rate
        self._sps   = sps
        burst_s     = timing.burst_duration_ms / 1000.0
        guard_s     = timing.transition_time_ms / 1000.0

        spread_code = get_code(mod.spreading)
        code_len    = mod.spreading.code_length

        # Coded bits per frame — must match tx_flowgraph._frame_feeder exactly.
        # build_frame pads data_bits to byte boundary before FEC, so we must
        # byte-align here too; fec_codec.coded_length() works on raw bits and
        # would give the wrong (smaller) value without this alignment.
        data_info_bits = (frame.payload_bits +
                          (frame.invariant.length_bits if frame.invariant.enabled else 0))
        if fec_codec.enabled:
            aligned_bits = ((data_info_bits + 7) // 8) * 8
            coded_bits   = fec_codec.coded_length(aligned_bits)
        else:
            coded_bits = data_info_bits

        # ---- UHD Source / Simulation Source ----
        if cfg.rf.simulation:
            # Use ZMQ SUB source to receive I/Q samples from simulated TX
            self._uhd_src = zeromq.sub_source(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:5555", 100, False, -1)
            log.info("RX Simulation mode: ZMQ SUB at tcp://127.0.0.1:5555")
        else:
            stream_args = gr_uhd.stream_args("fc32", "sc16")
            stream_args.channels = [rf.rx_channel]
            rx_device = rf.msl_device if cfg.rf.hw_mode != "single_b210" else rf.gnd_device

            # Initial freq is hop[0] so the very first transmitted burst
            # (which TX also starts at hop[0]) lands inside the RX passband.
            initial_freq = hop_scheduler.frequency_at(0)

            self._uhd_src = gr_uhd.usrp_source(rx_device, stream_args)
            self._uhd_src.set_samp_rate(sample_rate)
            self._uhd_src.set_center_freq(initial_freq, 0)
            self._uhd_src.set_gain(rf.rx_gain, 0)
            self._uhd_src.set_antenna(rf.rx_antenna, 0)
            self._uhd_src.set_bandwidth(rf.rx_bandwidth, 0)
            if cfg.rf.hw_mode == "dual_b210":
                # Slave B210 uses external reference from master
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

        # ---- M&M Timing Recovery ----
        # sps should be 4–16 for reliable convergence; use float sps
        self._timing_recovery = digital.clock_recovery_mm_cc(
            float(sps),
            0.25 * 0.175 ** 2,
            0.5,
            0.175,
            0.005,
        )

        # ---- Costas Loop (BPSK carrier phase recovery) ----
        # Without this, the cable's electrical length puts the down-converted
        # signal at an arbitrary phase offset.  The Costas loop rotates the
        # constellation so the data lands on the I axis before complex_to_real.
        # Order=2 for BPSK; loop_bw = 2π/500 ≈ 0.012 rad/sample is slow enough
        # to ride out the M&M transient yet fast enough to track a few-kHz
        # residual offset on a shared-oscillator loopback.
        self._costas = digital.costas_loop_cc(2 * np.pi / 500.0, 2)

       # ---- MSK / GMSK Demodulator ----
        if mod.type in ("msk", "gmsk"):
            self._msk_demod = digital.gmsk_demod(int(sps), None, 0.5, 0.005, 0.0, False, False)
        else:
            self._msk_demod = None

        # ---- Complex → Real (extract BPSK I component) ----
        self._complex_to_real = blocks.complex_to_real()

        # ---- OQPSK: interleave block to recombine I/Q chips ----
        if mod.type == "oqpsk":
            self._oqpsk_interleave = Interleave()
        else:
            self._oqpsk_interleave = None

        # ---- Frame Sink (preamble detect + despread + callback) ----
        preamble_bits = frame_gen.preamble_bits
        self._frame_sink = FrameSink(
            preamble_bits   = preamble_bits,
            frame_coded_bits= coded_bits,
            spreading_code  = spread_code,
            code_length     = code_len,
            callback        = self._on_frame_received,
        )

        # ---- Hop Controller (no-op pass-through) ----
        # Retained as a topology placeholder; hopping is driven on the FPGA
        # by HopTimingController via UHD timed commands.
        self._hop_ctrl = HopController(
            hop_scheduler, sample_rate, burst_s, guard_s, rf.rx_channel,
            mode='rx',
            uhd_src=self._uhd_src,
        )

        # ---- IQ Recording tap ----
        self._rx_record_valve = blocks.copy(gr.sizeof_gr_complex)
        self._rx_record_sink  = blocks.null_sink(gr.sizeof_gr_complex)
        self._rx_record_valve.set_enabled(False)
        self._rx_iq_connected = False

        self._connect()

    def _connect(self):
        # UHD → DC blocker → AGC
        self.connect(self._uhd_src, self._dc_blocker)
        self.connect(self._dc_blocker, self._agc)

        last = self._agc

        # IQ recording tap is added dynamically via enable_iq_recording() / lock+unlock.
        # Do NOT connect the valve here — blocks.copy with set_enabled(False) calls
        # consume_each(0) which stalls the fan-out and starves the main RX path.

        # Hop controller is a pure pass-through in the new design; UHD timed
        # commands queued by HopTimingController do the actual retuning.
        if self._cfg.hopping.enabled:
            self.connect(last, self._hop_ctrl)
            last = self._hop_ctrl

        if self._cfg.modulation.type in ("msk", "gmsk"):
            # MSK/GMSK: frequency-discriminator demod → hard bits → float ±1 → FrameSink
            self._msk_to_float = blocks.char_to_float(1, 1.0)
            self._msk_scale = blocks.multiply_const_ff(-2.0)
            self._msk_offset = blocks.add_const_ff(1.0)

            self.connect(last, self._msk_demod)
            self.connect(self._msk_demod, self._msk_to_float)
            self.connect(self._msk_to_float, self._msk_scale)
            self.connect(self._msk_scale, self._msk_offset)
            self.connect(self._msk_offset, self._frame_sink)
        elif self._cfg.modulation.type == "oqpsk":
            # OQPSK: RRC → single M&M timing recovery → extract I/Q → hard decision → interleave.
            # Single M&M ensures I/Q are aligned to same timing point.
            # For OQPSK, I chips are on real axis, Q chips on imag axis of the complex baseband signal.
            from radio.blocks.interleave import HardDecision
            self._oqpsk_mm = digital.clock_recovery_mm_cc(
                float(self._sps),
                0.25 * 0.175 ** 2,
                0.5,
                0.175,
                0.005,
            )
            # I channel: M&M output real part → hard decision
            self._oqpsk_mm_c2r_i = blocks.complex_to_real()
            self._oqpsk_hd_i = HardDecision()
            self._oqpsk_c2f_i = blocks.char_to_float(1, 1.0)
            self._oqpsk_i_scale = blocks.multiply_const_ff(-2.0)
            self._oqpsk_i_offset = blocks.add_const_ff(1.0)
            # Q channel: M&M output imag part → hard decision
            self._oqpsk_mm_c2r_q = blocks.complex_to_imag()
            self._oqpsk_hd_q = HardDecision()
            self._oqpsk_c2f_q = blocks.char_to_float(1, 1.0)
            self._oqpsk_q_scale = blocks.multiply_const_ff(-2.0)
            self._oqpsk_q_offset = blocks.add_const_ff(1.0)

            self.connect(last, self._rrc_mf)
            self.connect(self._rrc_mf, self._oqpsk_mm)
            # I channel: M&M → complex_to_real → hard decision → ±1
            self.connect(self._oqpsk_mm, self._oqpsk_mm_c2r_i)
            self.connect(self._oqpsk_mm_c2r_i, self._oqpsk_hd_i)
            self.connect(self._oqpsk_hd_i, self._oqpsk_c2f_i)
            self.connect(self._oqpsk_c2f_i, self._oqpsk_i_scale)
            self.connect(self._oqpsk_i_scale, self._oqpsk_i_offset)
            # Q channel: M&M → complex_to_imag → hard decision → ±1
            self.connect(self._oqpsk_mm, self._oqpsk_mm_c2r_q)
            self.connect(self._oqpsk_mm_c2r_q, self._oqpsk_hd_q)
            self.connect(self._oqpsk_hd_q, self._oqpsk_c2f_q)
            self.connect(self._oqpsk_c2f_q, self._oqpsk_q_scale)
            self.connect(self._oqpsk_q_scale, self._oqpsk_q_offset)
            # Interleave I and Q chips back into chip stream
            self.connect(self._oqpsk_i_offset, (self._oqpsk_interleave, 0))
            self.connect(self._oqpsk_q_offset, (self._oqpsk_interleave, 1))
            self.connect(self._oqpsk_interleave, self._frame_sink)
        else:
            # BPSK / QPSK: RRC matched filter → M&M timing recovery → Costas loop.
            # Costas loop rotates constellation so data lands on I axis before complex_to_real.
            self.connect(last, self._rrc_mf)
            self.connect(self._rrc_mf, self._timing_recovery)
            self.connect(self._timing_recovery, self._costas)
            self.connect(self._costas, self._complex_to_real)
            self.connect(self._complex_to_real, self._frame_sink)

    # ------------------------------------------------------------------
    # Frame callback (called from FrameSink in GR scheduler thread)
    # ------------------------------------------------------------------

    def _on_frame_received(self, coded_bits: np.ndarray,
                           timestamp: float, snr: float) -> None:
        payload, fec_ok = self._frame_gen.parse_frame(coded_bits)
        if self._frame_callback:
            self._frame_callback(payload, timestamp, snr, fec_ok)

    # ------------------------------------------------------------------
    # Runtime control
    # ------------------------------------------------------------------

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
