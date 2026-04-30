"""
Frame Sink -- GNU Radio block.

Performs the complete RX frame pipeline in one block:
  1. Preamble detection via sliding bipolar correlation (unspread symbols)
  2. Chip collection for one full frame after the preamble
  3. Despreading (vectorised, one soft bit per code_length chips)
  4. Hard-decision and callback dispatch

Input:  float32 soft BPSK symbols (one per chip, after complex_to_real)
Output: none (sink)
"""
from __future__ import annotations

import logging
import os
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
                 callback: Optional[Callable] = None,
                 diag_dir: Optional[str] = None):
        gr.sync_block.__init__(self, "Frame Sink",
                               in_sig=[np.float32], out_sig=[])

        self.message_port_register_out(pmt.intern("preamble_detected"))
        self.message_port_register_out(pmt.intern("frame_complete"))

        self._preamble = (1 - 2 * preamble_bits.astype(np.float32))
        self._pre_len = len(preamble_bits)
        self._code = (1 - 2 * spreading_code[:code_length].astype(np.float32))
        self._code_len = code_length
        self._coded_bits = frame_coded_bits
        self._data_chips = frame_coded_bits * code_length
        self._callback = callback

        self._buf: np.ndarray = _EMPTY.copy()
        self._new_samples = 0
        self._state = "SEARCHING"
        self._collect_buf = np.empty(self._data_chips, dtype=np.float32)
        self._collect_idx = 0
        self._polarity = 1.0

        self.frames_received = 0
        self.last_snr = 0.0
        self.last_peak = 0.0
        self._search_count = 0
        self._last_debug_t = time.time()
        self._diag_dir = diag_dir
        self._diag_save_idx = 0
        self._peak_history = []
        self._last_near_miss_t = 0.0
        self._preamble_raw = preamble_bits.copy()
        self._threshold = 0.55 * len(preamble_bits)

        preamble_hex = ''.join(str(b) for b in preamble_bits[:16])
        log.info("[RX-INIT] preamble=%s..., threshold=%.2f  code_len=%d  data_chips=%d",
                 preamble_hex, self._threshold, code_length, frame_coded_bits * code_length)

        self._dispatch_queue: queue.Queue = queue.Queue(maxsize=2)
        self._decode_thread = threading.Thread(
            target=self._decode_worker, name="FrameSink-decode", daemon=True
        )
        self._decode_thread.start()

    def set_callback(self, fn: Callable) -> None:
        self._callback = fn

    def work(self, input_items, output_items):
        in0 = input_items[0]
        if self._state == "SEARCHING":
            if len(in0) > 0:
                self._buf = np.concatenate((self._buf, in0))
                self._new_samples += len(in0)
                if self._new_samples >= self._pre_len:
                    self._search()
                    self._new_samples = 0
        else:
            self._fill_collect(in0)
        return len(input_items[0])

    def _search(self) -> None:
        buf = self._buf
        n = len(buf)
        pl = self._pre_len
        if n < pl:
            return

        # Direct correlation with bipolar preamble reference.
        ref = self._preamble
        corr = np.correlate(buf, ref, mode='valid')
        peak_idx = int(np.argmax(np.abs(corr)))
        peak_val = float(np.abs(corr[peak_idx]))
        threshold = self._threshold
        self.last_peak = peak_val
        self._search_count += 1

        chip_region = buf[peak_idx:peak_idx + pl]
        chip_mean = float(np.mean(chip_region))
        chip_std = float(np.std(chip_region))
        chip_abs_mean = float(np.mean(np.abs(chip_region)))
        chip_corr = float(np.sum(chip_region * ref))

        now = time.time()
        if now - self._last_debug_t >= 2.0:
            mean_amp = float(np.mean(np.abs(buf)))
            max_peak = max(self._peak_history[-100:]) if self._peak_history else 0.0
            est_signal_amp = max_peak / pl if pl > 0 else 0.0
            log.info(
                "[RX-DIAG] corr peak=%.2f  threshold=%.2f  mean_amp=%.3f  "
                "max_peak=%.2f  est_sig_amp=%.3f  buf_chips=%d  searches=%d "
                "chip_mean=%.3f  chip_std=%.3f  chip_abs=%.3f  raw_corr=%.2f",
                peak_val, threshold, mean_amp, max_peak, est_signal_amp,
                n, self._search_count, chip_mean, chip_std, chip_abs_mean, chip_corr,
            )
            self._last_debug_t = now
            self._search_count = 0

        self._peak_history.append(peak_val)
        if len(self._peak_history) > 100:
            self._peak_history.pop(0)

        near_miss_thresh = 0.30 * threshold
        if peak_val >= near_miss_thresh and peak_val < threshold:
            now = time.time()
            if now - self._last_near_miss_t >= 5.0:
                self._last_near_miss_t = now
                peak_region = buf[max(0, peak_idx - 2):peak_idx + pl + 2]
                log.warning(
                    "[RX-NEAR-MISS] peak=%.2f  thresh=%.2f  ratio=%.1f%%  "
                    "chip_abs=%.3f  peak_region=%s",
                    peak_val, threshold, 100 * peak_val / threshold, chip_abs_mean,
                    ' '.join(f'{x:+.2f}' for x in peak_region[:min(36, len(peak_region))]),
                )
                if self._diag_dir and self._diag_save_idx < 3:
                    save_path = os.path.join(
                        self._diag_dir, f"near_miss_{self._diag_save_idx}.npy")
                    np.save(save_path, buf[peak_idx:peak_idx + pl])
                    log.warning("[RX-NEAR-MISS] saved chip window to %s", save_path)
                    self._diag_save_idx += 1

        if peak_val >= threshold:
            exclude_start = max(0, peak_idx - pl)
            exclude_end = min(len(corr), peak_idx + pl)
            noise_samples = np.concatenate([corr[:exclude_start], corr[exclude_end:]])
            noise_floor = float(np.std(np.abs(noise_samples))) + 1e-9
            snr_val = 20.0 * np.log10(peak_val / (noise_floor * np.sqrt(pl)))

            self.last_snr = snr_val
            self._polarity = float(np.sign(corr[peak_idx]))

            log.info(
                "[RX-PREAMBLE] peak=%.2f  threshold=%.2f  polarity=%+.0f  "
                "snr=%.1f dB  frames_rx=%d",
                peak_val, threshold, self._polarity,
                self.last_snr, self.frames_received,
            )

            pmsg = pmt.make_dict()
            pmsg = pmt.dict_add(pmsg, pmt.intern("polarity"),
                                pmt.from_double(self._polarity))
            pmsg = pmt.dict_add(pmsg, pmt.intern("snr"),
                                pmt.from_double(self.last_snr))
            self.message_port_pub(pmt.intern("preamble_detected"), pmsg)

            data_start = peak_idx + pl
            leftover = buf[data_start:]
            self._buf = _EMPTY.copy()
            self._collect_idx = 0
            self._state = "COLLECTING"
            self._fill_collect(leftover)
        else:
            self._buf = buf[-(pl - 1):].copy()

    def _fill_collect(self, chips: np.ndarray) -> None:
        remaining = self._data_chips - self._collect_idx
        take = min(len(chips), remaining)
        self._collect_buf[self._collect_idx:self._collect_idx + take] = chips[:take]
        self._collect_idx += take
        if self._collect_idx >= self._data_chips:
            self._dispatch()
            leftover = chips[take:]
            self._buf = leftover.copy() if len(leftover) > 0 else _EMPTY.copy()
            self._state = "SEARCHING"
            self._collect_idx = 0

    def _dispatch(self) -> None:
        chips = self._collect_buf[:self._data_chips]
        soft_bits = self._despread(chips)
        hard_bits = (soft_bits < 0).astype(np.uint8)
        ts = time.time()
        snr = self.last_snr
        self.frames_received += 1

        mean_abs_sb = float(np.mean(np.abs(soft_bits)))
        min_sb = float(np.min(soft_bits))
        max_sb = float(np.max(soft_bits))
        sb_std = float(np.std(soft_bits))
        log.info(
            "[RX-DIAG2] frame=%d  mean_abs_soft=%.1f  range=[%.1f, %.1f]  "
            "std=%.1f  code_len=%d",
            self.frames_received, mean_abs_sb, min_sb, max_sb, sb_std, self._code_len,
        )

        self.message_port_pub(pmt.intern("frame_complete"), pmt.PMT_T)
        if self._callback:
            try:
                self._dispatch_queue.put_nowait((hard_bits, ts, snr))
            except queue.Full:
                pass

    def _decode_worker(self) -> None:
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
                    log.error("[RX-DECODE] callback exception: %s", exc,
                              exc_info=True)

    def _despread(self, chips: np.ndarray) -> np.ndarray:
        cl = self._code_len
        n_bits = len(chips) // cl
        matrix = chips[:n_bits * cl].reshape(n_bits, cl)
        return (matrix @ self._code) * self._polarity
