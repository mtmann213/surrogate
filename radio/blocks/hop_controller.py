"""
Hop Controller — GNU Radio block.

Two modes:

  - 'tx' (master): sample-driven. At each burst-period boundary it calls
    HopScheduler.next_frequency() to advance the shared sequence index, then
    emits a 'tx_command' stream tag at the exact sample offset of the next
    burst's first sample. The USRP sink consumes this tag for hardware-timed
    retuning. This block must sit in the TX signal path between the heartbeat
    adder and the UHD sink.

  - 'rx' (slave): preamble-driven. The block IS in the signal path (directly
    downstream of the UHD source) and reads `rx_time` tags from its input so
    it can convert any sample offset to hardware antenna time. A
    'preamble_detected' message from FrameSink advances the local hop index
    and emits a UHD *timed* tune_cmd PMT (with a `time` field) scheduled for
    the start of the current burst's guard interval — i.e. roughly
    preamble_antenna_time + burst_duration + 1ms. The UHD FPGA executes the
    retune at exactly that hardware time regardless of host pipeline lag, so
    the PLL settles inside the guard and the next burst's preamble lands on a
    stable frequency. If preambles are missed, elapsed-time is rounded to
    integer hops so the index re-anchors automatically.

Input/Output: complex64 pass-through (both modes).
"""
from __future__ import annotations

import threading
import time
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
        self._sample_rate = sample_rate
        self._channel = channel  # Physical USRP channel (0=A, 1=B)
        self._burst_duration_s = burst_duration_s
        self._guard_duration_s = guard_duration_s
        self._burst_samples = int(burst_duration_s * sample_rate)
        self._guard_samples = int(guard_duration_s * sample_rate)
        self._period_samples = self._burst_samples + self._guard_samples
        self._period_s = self._period_samples / float(sample_rate)
        self._sample_count = 0
        self._current_freq = 0.0
        self._lock = threading.Lock()

        # RX-mode state: local hop index (advances on preamble msg).
        # Initialised to -1 so the first preamble lands on index 0.
        self._rx_hop_index = -1
        self._rx_last_preamble_t = 0.0
        self._uhd_src = uhd_src

        # rx_time tag anchor: (sample_offset, hw_time_seconds). Updated each
        # time the UHD source emits an rx_time tag (start of stream + after
        # any overflow). Used to convert HopController sample positions to
        # FPGA hardware antenna times for timed tune commands.
        self._rx_anchor_sample = 0
        self._rx_anchor_hw = 0.0
        self._rx_anchor_set = False

        self.message_port_register_out(pmt.intern("tune_cmd"))
        self.message_port_register_out(pmt.intern("hop_event"))

        if mode == 'rx':
            self.message_port_register_in(pmt.intern("preamble_detected"))
            self.set_msg_handler(pmt.intern("preamble_detected"), self._on_preamble)

        if mode == 'tx':
            # UHD sink is initialized at frequency_at(0) for burst 0. The TX
            # work() loop emits a tag at the start of each burst's guard
            # interval to schedule the retune for the *next* burst's frequency.
            # Pre-advance the scheduler once so the first guard-start tag
            # carries freq[1], matching burst 1.
            self._scheduler.next_frequency()

    def update_timing(self, burst_s: float, guard_s: float) -> None:
        with self._lock:
            self._burst_duration_s = burst_s
            self._guard_duration_s = guard_s
            self._burst_samples = int(burst_s * self._sample_rate)
            self._guard_samples = int(guard_s * self._sample_rate)
            self._period_samples = self._burst_samples + self._guard_samples
            self._period_s = self._period_samples / float(self._sample_rate)

    def work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]
        out[:] = in0

        n = len(in0)

        # RX mode: pure pass-through, but scan input for rx_time tags so the
        # message handler can convert sample positions to hardware times.
        # Wrapped in try/except so any GR/PMT API quirk can't break the
        # signal pass-through and silently kill preamble detection downstream.
        if self._mode == 'rx':
            try:
                n_read = self.nitems_read(0)
                tags = self.get_tags_in_range(0, n_read, n_read + n,
                                              pmt.intern("rx_time"))
                for tag in tags:
                    try:
                        val = tag.value
                        if pmt.is_tuple(val):
                            whole = pmt.to_uint64(pmt.tuple_ref(val, 0))
                            frac = pmt.to_double(pmt.tuple_ref(val, 1))
                        elif pmt.is_pair(val):
                            whole = pmt.to_uint64(pmt.car(val))
                            frac = pmt.to_double(pmt.cdr(val))
                        else:
                            continue
                        was_set = self._rx_anchor_set
                        with self._lock:
                            self._rx_anchor_sample = int(tag.offset)
                            self._rx_anchor_hw = float(whole) + float(frac)
                            self._rx_anchor_set = True
                        if not was_set:
                            log.info("RX rx_time anchor set: sample=%d hw_t=%.6f",
                                     int(tag.offset),
                                     float(whole) + float(frac))
                    except Exception as exc:
                        log.debug("rx_time tag parse failed: %s", exc)
            except Exception as exc:
                log.debug("rx_time tag scan failed: %s", exc)
            return n

        with self._lock:
            period = self._period_samples
            burst_samples = self._burst_samples
            cnt = self._sample_count

        abs_offset = self.nitems_written(0)
        in_period = cnt % period
        # Schedule retune at the start of the GUARD interval (immediately
        # after the burst ends), not at the period boundary (start of the
        # next burst). Placing it at the period boundary causes UHD to retune
        # exactly when the next burst begins, so the PLL settles during data
        # and corrupts every frame. Guard-start placement gives the PLL the
        # full guard interval to settle before the next burst's preamble.
        guard_offset = (burst_samples - in_period) % period

        if guard_offset < n:
            freq = self._scheduler.next_frequency()

            # 1. Emit GUI event
            event = pmt.make_dict()
            event = pmt.dict_add(event, pmt.intern("freq"), pmt.from_double(freq))
            event = pmt.dict_add(event, pmt.intern("hop_index"), pmt.from_long(self._scheduler.current_index()))
            self.message_port_pub(pmt.intern("hop_event"), event)

            # 2. Stream tag for UHD sink (sample-precise timed tune).
            if freq != self._current_freq:
                tag_cmd = pmt.cons(pmt.intern("freq"), pmt.from_double(freq))
                self.add_item_tag(0, abs_offset + guard_offset,
                                  pmt.intern("tx_command"), tag_cmd)
                self._current_freq = freq
                log.info("TX Ch %d Hop-Tag → %.3f MHz (offset=%d, guard-start)",
                         self._channel, freq / 1e6, abs_offset + guard_offset)

        with self._lock:
            self._sample_count = (self._sample_count + n) % period

        return n

    # ------------------------------------------------------------------
    # RX mode: preamble-anchored hopping
    # ------------------------------------------------------------------

    def _on_preamble(self, msg) -> None:
        """
        Called by the GR msg passing system when FrameSink detects a preamble.

        Advances the local hop index (skipping ahead by elapsed-time/period to
        absorb missed/false-positive preambles) and emits a UHD *timed*
        tune_cmd targeted at the current burst's guard interval. The FPGA
        executes the retune precisely at that hw_time so the PLL settles in
        the guard, leaving the next burst's preamble on a stable frequency.
        """
        now = time.time()
        if self._rx_last_preamble_t > 0.0 and self._period_s > 0.0:
            elapsed = now - self._rx_last_preamble_t
            # Drop a duplicate/false-positive preamble that arrives well
            # inside the current burst — a real preamble cannot be closer
            # than one full burst-period to the previous one.
            if elapsed < self._period_s * 0.5:
                return
            n_advance = max(1, int(round(elapsed / self._period_s)))
        else:
            n_advance = 1
        self._rx_last_preamble_t = now

        self._rx_hop_index += n_advance
        next_freq = self._scheduler.frequency_at(self._rx_hop_index + 1)

        # Always emit a hop_event for GUI/stats, even when no retune is needed.
        event = pmt.make_dict()
        event = pmt.dict_add(event, pmt.intern("freq"), pmt.from_double(next_freq))
        event = pmt.dict_add(event, pmt.intern("hop_index"), pmt.from_long(self._rx_hop_index))
        self.message_port_pub(pmt.intern("hop_event"), event)

        if next_freq == self._current_freq:
            return

        # Compute the FPGA hardware time at which the retune should fire.
        # Approach: take HopController's most recent input-sample antenna time
        # (rx_time anchor + samples_since_anchor / sample_rate) as our best
        # estimate of "now" at the antenna, then schedule the retune
        # `burst_duration + 1ms` later — i.e. just inside the guard interval
        # of the burst whose preamble just fired us. UHD will execute it on
        # the FPGA at that exact hardware time, regardless of host lag.
        target_hw = None
        with self._lock:
            anchor_set = self._rx_anchor_set
            anchor_sample = self._rx_anchor_sample
            anchor_hw = self._rx_anchor_hw
            sample_rate = self._sample_rate
            burst_s = self._burst_duration_s
        if anchor_set and self._uhd_src is not None:
            sample_now = int(self.nitems_read(0))
            antenna_now_hw = anchor_hw + (sample_now - anchor_sample) / sample_rate
            target_hw = antenna_now_hw + burst_s + 0.001
        self._do_rx_tune(next_freq, self._rx_hop_index, target_hw, n_advance)

    def _do_rx_tune(self, freq: float, hop_index: int,
                    target_hw: "float | None", n_advance: int) -> None:
        """Publish the tune_cmd PMT (timed if target_hw is provided)."""
        msg_cmd = pmt.make_dict()
        msg_cmd = pmt.dict_add(msg_cmd, pmt.intern("freq"),
                               pmt.from_double(freq))
        if target_hw is not None:
            whole = int(target_hw)
            frac = target_hw - whole
            # gr-uhd's command port parses "time" as a pair (cons cell) of
            # (uint64 secs, double frac_secs) — NOT a tuple. Using a tuple
            # silently makes the source ignore the timed-command request.
            time_pmt = pmt.cons(pmt.from_uint64(whole),
                                pmt.from_double(frac))
            msg_cmd = pmt.dict_add(msg_cmd, pmt.intern("time"), time_pmt)
        self.message_port_pub(pmt.intern("tune_cmd"), msg_cmd)
        self._current_freq = freq

        if target_hw is not None:
            log.info("RX Ch %d preamble@idx=%d → tune to %.3f MHz @ hw_t=%.6f (advance=%d)",
                     self._channel, hop_index, freq / 1e6, target_hw, n_advance)
        else:
            log.info("RX Ch %d preamble@idx=%d → tune to %.3f MHz (immediate, no rx_time anchor) (advance=%d)",
                     self._channel, hop_index, freq / 1e6, n_advance)
