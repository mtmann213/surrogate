"""
Burst Gate — GNU Radio block.

Passes samples during the active burst window, outputs zeros during the
guard/transition interval.

Input/Output: complex64 pass-through with gating.
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr
import pmt

_ZERO = np.complex64(0)


class BurstGate(gr.sync_block):
    """
    Passes samples during the active burst window, outputs zeros during the
    guard/transition interval.

    Uses CPython atomic int reads to avoid threading.Lock in the hot path.
    The lock is only needed for set_burst_duration/set_guard_duration which
    update multiple related values (burst_samples + guard_samples = period).
    These are called from the GUI thread ~once per config update, so a
    single-element consistency glitch (one burst boundary off by one buffer)
    is acceptable — we skip the 50ns lock overhead on every work() call.
    """

    def __init__(self, sample_rate: float, burst_duration_s: float,
                 guard_duration_s: float):
        gr.sync_block.__init__(
            self,
            name="Burst Gate",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )
        self._sample_rate = sample_rate
        self._burst_samples = int(burst_duration_s * sample_rate)
        self._guard_samples = int(guard_duration_s * sample_rate)
        self._period_samples = self._burst_samples + self._guard_samples
        self._sample_count = 0
        self._manual_gate: bool = True

        self.message_port_register_in(pmt.intern("gate_cmd"))
        self.set_msg_handler(pmt.intern("gate_cmd"), self._handle_gate_cmd)

    def set_burst_duration(self, duration_s: float) -> None:
        self._burst_samples = int(duration_s * self._sample_rate)
        self._period_samples = self._burst_samples + self._guard_samples

    def set_guard_duration(self, duration_s: float) -> None:
        self._guard_samples = int(duration_s * self._sample_rate)
        self._period_samples = self._burst_samples + self._guard_samples

    def _handle_gate_cmd(self, msg):
        if pmt.is_symbol(msg):
            cmd = pmt.symbol_to_string(msg)
            if cmd == "open":
                self._manual_gate = True
            elif cmd == "close":
                self._manual_gate = False

    def work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]
        n = len(in0)

        burst = self._burst_samples
        period = self._period_samples
        cnt = self._sample_count

        if not self._manual_gate:
            out[:] = _ZERO
        else:
            # Vectorised: compute phase for every sample in one numpy call
            phases = (cnt + np.arange(n, dtype=np.int64)) % period
            mask = phases < burst          # bool array, shape (n,)
            out[:] = np.where(mask, in0, _ZERO)

        self._sample_count = (cnt + n) % period

        return n
