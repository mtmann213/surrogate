"""
Frame Sink — GNU Radio block.

Performs the complete RX frame pipeline in one block:
  1. Preamble detection via sliding bipolar correlation (unspread symbols)
  2. Chip collection for one full frame after the preamble
  3. Integrate-and-dump despreading (vectorised, one soft bit per code_length chips)
  4. Hard-decision and callback dispatch

Input:  float32 soft BPSK symbols (one per chip, output of complex_to_real)
Output: none (sink)

The preamble section of each burst is NOT spread (raw BPSK), so the correlator
works directly on the soft chip stream. The data chips that follow are spread
and are despread here before calling the frame callback.

PERFORMANCE NOTE
----------------
work() must return in microseconds to avoid starving the GR scheduler of the
Python GIL.  All buffer management uses numpy operations (which release the GIL
during C execution) rather than Python-level deque iteration (which holds the
GIL ~150 µs per 1024-chip call and caused 400+ UHD underflows/sec).

THREADING NOTE
--------------
The Viterbi FEC decoder is dispatched to a background thread via a bounded
queue so work() never blocks on decode.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import numpy as np
from typing import Callable, Optional

from gnuradio import gr
import pmt

log = logging.getLogger(__name__)

_EMPTY = np.empty(0, dtype=np.float32)


class FrameSink(gr.sync_block):

    def __init__(self,
                 preamble_bits: np.ndarray,
                 frame_coded_bits: int,
                 spreading_code: np.ndarray,
                 code_length: int,
                 callback: Optional[Callable] = None):
        """
        preamble_bits:    binary (0/1) preamble, NOT spread
        frame_coded_bits: coded data bits per frame (after FEC, before spreading)
        spreading_code:   binary (0/1) spreading code used on data chips
        code_length:      chips per coded bit (== len(spreading_code) for one period)
        callback:         fn(coded_bits: np.ndarray, timestamp: float, snr: float)
        """
        gr.sync_block.__init__(self, "Frame Sink",
                               in_sig=[np.float32], out_sig=[])

        # Output message port: published on each preamble detection so the
        # RX HopController (and any other listener) can advance hop state.
        self.message_port_register_out(pmt.intern("preamble_detected"))
        self.message_port_register_out(pmt.intern("frame_complete"))

        self._preamble   = (1 - 2 * preamble_bits.astype(np.float32))   # bipolar ±1
        self._pre_len    = len(preamble_bits)
        self._code       = (1 - 2 * spreading_code[:code_length].astype(np.float32))
        self._code_len   = code_length
        self._coded_bits = frame_coded_bits
        self._data_chips = frame_coded_bits * code_length
        self._callback   = callback

        # Search buffer: a plain numpy array that stays small (trimmed to
        # 2×pre_len after each failed search).  np.concatenate releases the
        # GIL during its C copy, so work() stays GIL-friendly.
        self._np_buf: np.ndarray = _EMPTY.copy()
        self._new_samples = 0

        # State machine
        self._state       = "SEARCHING"   # SEARCHING | COLLECTING
        self._collect_buf = np.empty(self._data_chips, dtype=np.float32)
        self._collect_idx = 0
        self._polarity    = 1.0

        # Statistics
        self.frames_received = 0
        self.last_snr        = 0.0
        self.last_peak       = 0.0
        self._search_count   = 0
        self._last_debug_t   = time.time()

        # Background decode thread
        self._dispatch_queue: queue.Queue = queue.Queue(maxsize=2)
        self._decode_thread = threading.Thread(
            target=self._decode_worker, name="FrameSink-decode", daemon=True
        )
        self._decode_thread.start()

    def set_callback(self, fn: Callable) -> None:
        self._callback = fn

    # ------------------------------------------------------------------
    # GNU Radio work()
    # ------------------------------------------------------------------

    def work(self, input_items, output_items):
        in0 = input_items[0]

        if self._state == "SEARCHING":
            if len(in0) > 0:
                self._np_buf = np.concatenate((self._np_buf, in0))
                self._new_samples += len(in0)
                # Only search if we have enough new samples to make it worthwhile
                if self._new_samples >= self._pre_len:
                    self._search()
                    self._new_samples = 0
        else:
            self._fill_collect(in0)

        return len(input_items[0])

    # ------------------------------------------------------------------
    # State: SEARCHING
    # ------------------------------------------------------------------

    def _search(self) -> None:
        buf = self._np_buf
        n   = len(buf)
        pl  = self._pre_len

        if n < pl:
            return

        ref  = self._preamble
        corr = np.correlate(buf, ref, mode='valid')
        peak_idx = int(np.argmax(np.abs(corr)))
        peak_val = float(np.abs(corr[peak_idx]))

        threshold = 0.55 * pl
        self.last_peak = peak_val
        self._search_count += 1

        # Chip-level diagnostics around correlation peak
        chip_region = buf[peak_idx:peak_idx+pl]
        chip_mean = float(np.mean(chip_region))
        chip_std = float(np.std(chip_region))
        chip_abs_mean = float(np.mean(np.abs(chip_region)))
        chip_corr = float(np.sum(chip_region * ref))

        now = time.time()
        if now - self._last_debug_t >= 2.0:
            mean_amp = float(np.mean(np.abs(buf)))
            log.info(
                "[RX-DIAG] corr peak=%.2f  threshold=%.2f  mean_amp=%.3f  buf_chips=%d  searches=%d "
                "chip_mean=%.3f  chip_std=%.3f  chip_abs=%.3f  raw_corr=%.2f",
                peak_val, threshold, mean_amp, n, self._search_count,
                chip_mean, chip_std, chip_abs_mean, chip_corr,
            )
            self._last_debug_t = now
            self._search_count = 0

        if peak_val >= threshold:
            noise = float(np.std(np.abs(corr))) + 1e-9
            snr_val = 20.0 * np.log10(peak_val / (noise * np.sqrt(pl)))
            
            if snr_val < 5.0:
                # False positive due to high noise or adjacent channel bleed
                self._np_buf = buf[-(pl - 1):].copy()
                return

            self.last_snr = snr_val
            self._polarity = float(np.sign(corr[peak_idx]))

            log.info(
                "[RX-PREAMBLE] peak=%.2f  threshold=%.2f  polarity=%+.0f  snr=%.1f dB  frames_rx=%d",
                peak_val, threshold, self._polarity, self.last_snr, self.frames_received,
            )

            # Notify hop-controller (and any other listener) that a preamble
            # was detected. Used by the RX HopController to advance its hop
            # index and retune the UHD source for the next burst.
            pmsg = pmt.make_dict()
            pmsg = pmt.dict_add(pmsg, pmt.intern("polarity"),
                                pmt.from_double(self._polarity))
            pmsg = pmt.dict_add(pmsg, pmt.intern("snr"),
                                pmt.from_double(self.last_snr))
            self.message_port_pub(pmt.intern("preamble_detected"), pmsg)

            data_start = peak_idx + pl
            leftover = buf[data_start:]
            self._np_buf = _EMPTY.copy()
            self._collect_idx = 0
            self._state = "COLLECTING"
            self._fill_collect(leftover)
        else:
            # Keep only the tail needed for the next sliding correlation overlap.
            # (pl - 1) chips ensures that if the preamble started at the very
            # end of the current buffer, it will be completed in the next one.
            self._np_buf = buf[-(pl - 1):].copy()

    # ------------------------------------------------------------------
    # State: COLLECTING
    # ------------------------------------------------------------------

    def _fill_collect(self, chips: np.ndarray) -> None:
        remaining = self._data_chips - self._collect_idx
        take      = min(len(chips), remaining)

        self._collect_buf[self._collect_idx:self._collect_idx + take] = chips[:take]
        self._collect_idx += take

        if self._collect_idx >= self._data_chips:
            self._dispatch()
            leftover = chips[take:]
            # numpy assignment — no Python-level iteration
            self._np_buf = leftover.copy() if len(leftover) > 0 else _EMPTY.copy()
            self._state = "SEARCHING"
            self._collect_idx = 0

    # ------------------------------------------------------------------
    # Despread + dispatch
    # ------------------------------------------------------------------

    def _dispatch(self) -> None:
        chips     = self._collect_buf[:self._data_chips]
        soft_bits = self._despread(chips)
        hard_bits = (soft_bits < 0).astype(np.uint8)
        ts        = time.time()
        snr       = self.last_snr

        self.frames_received += 1

        # Diagnostics: despreading quality
        # If despreading works, mean |soft_bit| ≈ code_length (31).
        # Values near zero indicate chip misalignment, phase error, or wrong code.
        mean_abs_sb = float(np.mean(np.abs(soft_bits)))
        min_sb = float(np.min(soft_bits))
        max_sb = float(np.max(soft_bits))
        sb_std = float(np.std(soft_bits))
        log.info(
            "[RX-DIAG2] frame=%d  mean_abs_soft=%.1f  range=[%.1f, %.1f]  std=%.1f  code_len=%d",
            self.frames_received, mean_abs_sb, min_sb, max_sb, sb_std, self._code_len,
        )

        self.message_port_pub(pmt.intern("frame_complete"), pmt.PMT_T)

        if self._callback:
            try:
                self._dispatch_queue.put_nowait((hard_bits, ts, snr))
            except queue.Full:
                pass

    def _decode_worker(self) -> None:
        """Background thread: drains dispatch queue and invokes callback."""
        while True:
            try:
                item = self._dispatch_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            hard_bits, ts, snr = item
            if self._callback:
                try:
                    self._callback(hard_bits, ts, snr)
                except Exception as exc:
                    log.error("[RX-DECODE] callback exception: %s", exc, exc_info=True)

    def _despread(self, chips: np.ndarray) -> np.ndarray:
        """
        Integrate-and-dump: reshape chips into (n_bits, code_length) and
        dot each row with the bipolar spreading code → one soft bit per row.
        """
        cl     = self._code_len
        n_bits = len(chips) // cl
        matrix = chips[:n_bits * cl].reshape(n_bits, cl)
        return (matrix @ self._code) * self._polarity
