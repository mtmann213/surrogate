"""
DSSS Despreader — GNU Radio block.

Operates in two phases:
  1. ACQUISITION: slide the reference code across the soft-symbol stream,
     searching for a peak correlation that indicates chip alignment.
  2. TRACKING: once aligned, sum code_length consecutive chips per bit
     (integrate-and-dump) to produce despread soft bits.

Input:  float32 soft symbols (from BPSK demod, ±1 range)
Output: float32 despread soft bits (one per coded bit)

The block emits a 'burst_start' tag when acquisition lock is achieved,
which downstream frame-sync uses to align the bit stream.
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr
import pmt

_ACQ_THRESHOLD_RATIO = 0.6   # Peak must be this fraction of max possible
_ACQ_SEARCH_CHIPS = 2048     # Search window in chips before declaring no-lock


class DSSSDespreader(gr.basic_block):

    def __init__(self, spreading_code: np.ndarray, code_length: int):
        gr.basic_block.__init__(
            self,
            name="DSSS Despreader",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )
        self._code = (1 - 2 * spreading_code.astype(np.float32))  # bipolar
        self._code_len = code_length
        self._locked = False
        self._lock_offset = 0
        self._sample_count = 0
        self._acq_buffer = np.zeros(_ACQ_SEARCH_CHIPS, dtype=np.float32)
        self._acq_idx = 0

        self.message_port_register_in(pmt.intern("code_update"))
        self.set_msg_handler(pmt.intern("code_update"), self._handle_code_update)
        self.message_port_register_out(pmt.intern("lock_status"))

    def _handle_code_update(self, msg):
        if pmt.is_f32vector(msg):
            code = np.array(pmt.f32vector_elements(msg), dtype=np.float32)
            self._code = 1 - 2 * ((code > 0).astype(np.float32))
            self._code_len = len(code)
            self._locked = False

    def forecast(self, noutput_items, ninputs):
        return [noutput_items * self._code_len]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]

        if not self._locked:
            return self._acquire(in0, out)
        else:
            return self._track(in0, out)

    def _acquire(self, in0, out):
        n = len(in0)
        # Fill acquisition buffer
        space = len(self._acq_buffer) - self._acq_idx
        take = min(n, space)
        self._acq_buffer[self._acq_idx:self._acq_idx + take] = in0[:take]
        self._acq_idx += take

        if self._acq_idx < len(self._acq_buffer):
            self.consume(0, take)
            return 0

        # Run sliding correlation
        buf = self._acq_buffer
        cl = self._code_len
        ref = self._code[:cl]
        max_corr = 0.0
        best_offset = 0

        for offset in range(min(cl, len(buf) - cl)):
            seg = buf[offset:offset + cl]
            corr = float(np.abs(np.dot(seg, ref)))
            if corr > max_corr:
                max_corr = corr
                best_offset = offset

        threshold = _ACQ_THRESHOLD_RATIO * cl
        if max_corr >= threshold:
            self._locked = True
            self._lock_offset = best_offset
            # Emit lock tag
            self.add_item_tag(0, self.nitems_written(0),
                              pmt.intern("burst_start"), pmt.PMT_T)
            lock_msg = pmt.cons(pmt.intern("locked"),
                                pmt.from_double(float(max_corr)))
            self.message_port_pub(pmt.intern("lock_status"), lock_msg)

        # Consume the search window and reset
        self._acq_idx = 0
        self.consume(0, min(take, len(buf)))
        return 0

    def _track(self, in0, out):
        n_in = len(in0)
        n_out = len(out)
        cl = self._code_len

        # Align to lock offset on first tracking call
        start = self._lock_offset
        self._lock_offset = 0  # Only apply once

        n_bits = min((n_in - start) // cl, n_out)
        if n_bits <= 0:
            self.consume(0, min(n_in, start + 1))
            return 0

        ref = self._code[:cl]
        for i in range(n_bits):
            seg = in0[start + i * cl: start + i * cl + cl]
            out[i] = float(np.dot(seg, ref))

        consumed = start + n_bits * cl
        self.consume(0, consumed)
        return n_bits
