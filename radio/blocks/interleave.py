"""
Interleave / Deinterleave / HardDecision blocks.

Used for OQPSK parallel I/Q processing:
- Deinterleave: 1 float32 input (chip stream) → 2 outputs (I chips, Q chips).
  Even-indexed chips → I, odd-indexed chips → Q.
- Interleave: 2 float32 inputs (I chips, Q chips) → 1 output.
  Alternates: I[0], Q[0], I[1], Q[1], ...
- HardDecision: 1 float32 input (complex symbol real/imag) → 1 output (char 0/1).
  Threshold at 0: >= 0 → 1, < 0 → 0.
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr


class HardDecision(gr.basic_block):
    """Hard decision: float32 ±1 symbols → char 0/1. Threshold at 0."""

    def __init__(self):
        gr.basic_block.__init__(
            self,
            name="HardDecision",
            in_sig=[np.float32],
            out_sig=[np.uint8],
        )

    def forecast(self, noutput_items, ninputs):
        return [noutput_items]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]
        n = len(out0)
        for i in range(n):
            out0[i] = 0 if in0[i] >= 0.0 else 1
        self.consume(0, n)
        return n


class Deinterleave(gr.basic_block):
    """Split a chip stream into I (even) and Q (odd) channels."""

    def __init__(self):
        gr.basic_block.__init__(
            self,
            name="Deinterleave",
            in_sig=[np.float32],
            out_sig=[np.float32, np.float32],
        )

    def forecast(self, noutput_items, ninputs):
        # noutput_items is max output items (single int in new GR).
        # 1 input port, need 2x input items for 2 output channels.
        return [2 * noutput_items]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]
        out0 = output_items[0]  # I (even chips)
        out1 = output_items[1]  # Q (odd chips)
        n_in = len(in0)

        n_out0 = len(out0)
        n_out1 = len(out1)
        n_out = min(n_out0, n_out1)

        # Even indices → I, odd indices → Q
        for i in range(n_out):
            out0[i] = in0[2 * i]
            out1[i] = in0[2 * i + 1]

        consumed = 2 * n_out
        self.consume(0, consumed)
        return n_out


class Interleave(gr.basic_block):
    """Interleave two chip streams back into one (I[0], Q[0], I[1], Q[1], ...)."""

    def __init__(self):
        gr.basic_block.__init__(
            self,
            name="Interleave",
            in_sig=[np.float32, np.float32],
            out_sig=[np.float32],
        )

    def forecast(self, noutput_items, ninputs):
        # noutput_items is max output items.
        # 2 input ports, need noutput_items//2 items from each.
        return [noutput_items // 2 + 1, noutput_items // 2 + 1]

    def general_work(self, input_items, output_items):
        in0 = input_items[0]  # I chips
        in1 = input_items[1]  # Q chips
        out0 = output_items[0]
        n_out = len(out0)
        n_in0 = len(in0)
        n_in1 = len(in1)

        # Max pairs we can produce
        n_pairs = min(n_in0, n_in1, n_out // 2)

        for i in range(n_pairs):
            out0[2 * i] = in0[i]
            out0[2 * i + 1] = in1[i]

        consumed = n_pairs
        self.consume(0, consumed)
        self.consume(1, consumed)
        return 2 * n_pairs
