"""
Preamble Inserter — GNU Radio block.

Consumes a stream of coded chips (uint8 0/1) representing one frame of data.
Prepends the preamble bit sequence (also as chips, not spread) at the start
of each burst window, then outputs the concatenated chip stream.

This block tracks burst boundaries via sample count and inserts the preamble
automatically at the start of each burst period.

Input:  uint8 (coded+spread data chips)
Output: uint8 (preamble chips | data chips)
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr
import pmt


class PreambleInserter(gr.basic_block):

    def __init__(self, preamble_bits: np.ndarray,
                 burst_data_chips: int):
        """
        preamble_bits:    binary (0/1) preamble sequence
        burst_data_chips: number of data chips per burst (after preamble)
        """
        gr.basic_block.__init__(
            self,
            name="Preamble Inserter",
            in_sig=[np.uint8],
            out_sig=[np.uint8],
        )
        self._preamble = preamble_bits.astype(np.uint8)
        self._pre_len = len(preamble_bits)
        self._data_chips = burst_data_chips
        self._burst_chips = self._pre_len + burst_data_chips
        self._phase = 0  # 0..pre_len-1 = preamble, pre_len..burst_chips-1 = data

        self.message_port_register_in(pmt.intern("preamble_update"))
        self.set_msg_handler(pmt.intern("preamble_update"), self._handle_update)

    def _handle_update(self, msg):
        if pmt.is_u8vector(msg):
            self._preamble = np.array(pmt.u8vector_elements(msg), dtype=np.uint8)
            self._pre_len = len(self._preamble)
            self._burst_chips = self._pre_len + self._data_chips

    def forecast(self, noutput_items, ninputs):
        # Worst case: all noutput_items are preamble, needing 0 input
        # Best case: all are data. Request proportionally.
        ratio = self._data_chips / max(1, self._burst_chips)
        return [max(1, int(noutput_items * ratio))]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]
        n_in = len(in0)
        n_out = len(out)
        in_idx = 0
        out_idx = 0

        while out_idx < n_out:
            phase = self._phase
            if phase < self._pre_len:
                # Output preamble chip
                out[out_idx] = self._preamble[phase]
                out_idx += 1
                self._phase += 1
            elif phase < self._burst_chips:
                # Output data chip from input
                if in_idx >= n_in:
                    break
                out[out_idx] = in0[in_idx]
                out_idx += 1
                in_idx += 1
                self._phase += 1
            else:
                self._phase = 0  # Start next burst

        self.consume(0, in_idx)
        return out_idx
