"""
DSSS Spreader — GNU Radio block.

Input:  uint8 bits (0/1), one item per bit
Output: uint8 chips (0/1), code_length items per bit

This is a 1:code_length interpolating block implemented as a basic_block.
The spreading code is stored as a flat array and tiled across all input bits.
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr
import pmt


class DSSSSpreader(gr.basic_block):

    def __init__(self, spreading_code: np.ndarray):
        gr.basic_block.__init__(
            self,
            name="DSSS Spreader",
            in_sig=[np.uint8],
            out_sig=[np.uint8],
        )
        self._code = spreading_code.astype(np.uint8)
        self._code_len = len(spreading_code)
        self._code_offset = 0  # position within code for current bit

        # Message port to update the spreading code live
        self.message_port_register_in(pmt.intern("code_update"))
        self.set_msg_handler(pmt.intern("code_update"), self._handle_code_update)

    def update_code(self, new_code: np.ndarray) -> None:
        self._code = new_code.astype(np.uint8)
        self._code_len = len(new_code)
        self._code_offset = 0

    def _handle_code_update(self, msg):
        if pmt.is_u8vector(msg):
            code = np.array(pmt.u8vector_elements(msg), dtype=np.uint8)
            self.update_code(code)

    def forecast(self, noutput_items, ninputs):
        # Each output chip came from 1 input bit; we produce code_len chips per bit
        return [max(1, noutput_items // self._code_len)]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]

        n_in = len(in0)
        n_out = len(out)
        # Number of bits we can process
        n_bits = min(n_in, n_out // self._code_len)
        if n_bits == 0:
            return 0

        chips_per_bit = self._code_len
        out_idx = 0
        for i in range(n_bits):
            bit = in0[i]
            chip_start = (self._code_offset * chips_per_bit) % len(self._code)
            # DSSS: XOR bit with each chip of the spreading code
            chips = bit ^ self._code[chip_start:chip_start + chips_per_bit]
            if len(chips) < chips_per_bit:
                # Wrap around
                chips = np.concatenate([chips,
                    bit ^ self._code[:(chips_per_bit - len(chips))]])
            out[out_idx:out_idx + chips_per_bit] = chips
            out_idx += chips_per_bit
            self._code_offset = (self._code_offset + 1) % (len(self._code) // chips_per_bit + 1)

        self.consume(0, n_bits)
        return out_idx
