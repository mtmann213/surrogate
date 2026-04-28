"""
Hop Controller — GNU Radio block.

This block is now a **pure complex pass-through** in both 'tx' and 'rx' modes.

Frequency hopping is driven entirely by `radio/hop_timing.py:HopTimingController`,
which queues UHD timed `set_center_freq` commands directly on the sink and
source FPGAs against a shared epoch T0. There is no longer any sample-driven
tag insertion (TX) nor preamble-reactive retune logic (RX).

The block is retained in both flowgraphs for two reasons:

  - On TX, `enable_iq_recording()` taps the post-hop_ctrl stream — keeping the
    block as a pass-through preserves that connection point.
  - The constructor signature is referenced by tx/rx_flowgraph; making it a
    no-op is a less invasive change than ripping it out of the topology.

Input/Output: complex64 pass-through.
"""
from __future__ import annotations

import logging
import numpy as np
from gnuradio import gr
import pmt

from core.hop_scheduler import HopScheduler

log = logging.getLogger(__name__)

class HopController(gr.sync_block):

    def __init__(self, hop_scheduler: HopScheduler,
                 sample_rate: float,
                 burst_duration_s: float,
                 guard_duration_s: float,
                 channel: int = 0,
                 mode: str = 'tx',
                 uhd_src=None):
        gr.sync_block.__init__(
            self,
            name="Hop Controller",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )
        if mode not in ('tx', 'rx'):
            raise ValueError(f"HopController mode must be 'tx' or 'rx', got {mode!r}")
        self._mode = mode
        self._scheduler = hop_scheduler
        self._channel = channel
        # Hopping is now driven by HopTimingController; no msg ports needed.
        # Output ports are kept registered (but unused) so any GUI subscribers
        # that connected to "hop_event" don't error out — left as a no-op.
        self.message_port_register_out(pmt.intern("hop_event"))

    def work(self, input_items, output_items):
        # Pure pass-through. All hop scheduling is handled out-of-band by
        # HopTimingController via UHD timed commands.
        output_items[0][:] = input_items[0]
        return len(input_items[0])
