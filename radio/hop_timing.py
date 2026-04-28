"""
HopTimingController — strict, FPGA-timed FHSS retunes for both TX and RX.

Architecture
------------
A single shared epoch T0 (FPGA seconds, on the B210's TCXO) is the authoritative
clock for the hop sequence. Both `usrp_sink` (TX) and `usrp_source` (RX) receive
timed retune commands via gr-uhd's `command` message port at

    T_retune(N) = T0 + N*period_s + burst_s + guard_lead_s

i.e. ~1 ms into the guard interval that follows burst N. UHD's command queue is
processed entirely on the FPGA, so the retune fires at exactly that hardware
time on both the TX and RX RFICs regardless of host pipeline lag, GIL stalls,
or USB jitter.

We use the message port (PMT dict with "freq", "chan", "time") rather than
direct `set_command_time` / `set_center_freq` / `clear_command_time` API
calls. The direct-API chain is fragile: a Python-thread interruption between
the three calls leaks an armed command time and produces a cmd-time-error
storm. The msg-port handler runs on gr-uhd's own thread and applies time
+ freq atomically.

This replaces the older reactive design where RX retunes only happened after a
preamble correlation succeeded — a chicken-and-egg gate that prevented the link
from bootstrapping after the very first burst.

Bootstrap
---------
- `set_time_now(0)` on the device pins the FPGA time origin.
- `T0 = now + start_offset_s` (default 0.5 s) is far enough in the future for
  UHD's stream pipeline to settle and for the first batch of retunes to be
  queued before any of them are due.
- `usrp_sink.set_start_time(T0)` and `usrp_source.set_start_time(T0)` make
  the first emitted/captured sample correspond to FPGA time T0. Burst 0 then
  occupies antenna time `[T0, T0 + burst_s]`.
- Initial center frequency is `freq[0]`, set during flowgraph construction;
  the first timed command is for `freq[1]` and fires in guard 0.

A small background thread keeps the UHD command queue filled with retunes
covering the next ~`queue_horizon_s` seconds of FPGA time.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Optional

import pmt
from gnuradio import uhd

from core.hop_scheduler import HopScheduler

log = logging.getLogger(__name__)


def _make_time_pair(t_fpga: float):
    """gr-uhd 'command' port wants time as a pair(uint64 secs, double frac)."""
    secs_int = int(math.floor(t_fpga))
    secs_frac = t_fpga - secs_int
    return pmt.cons(pmt.from_uint64(secs_int), pmt.from_double(secs_frac))


def _build_freq_command(freq: float, chan: int, t_fpga: float):
    cmd = pmt.make_dict()
    cmd = pmt.dict_add(cmd, pmt.intern("freq"), pmt.from_double(float(freq)))
    cmd = pmt.dict_add(cmd, pmt.intern("chan"), pmt.from_long(int(chan)))
    cmd = pmt.dict_add(cmd, pmt.intern("time"), _make_time_pair(t_fpga))
    return cmd


class HopTimingController:

    def __init__(
        self,
        scheduler: HopScheduler,
        sample_rate: float,
        burst_duration_s: float,
        guard_duration_s: float,
        tx_channel: int = 0,
        rx_channel: int = 0,
        guard_lead_s: float = 0.001,
        start_offset_s: float = 0.5,
        queue_horizon_s: float = 1.0,
        refill_period_s: float = 0.2,
    ):
        self._scheduler = scheduler
        self._sample_rate = sample_rate
        self._burst_s = burst_duration_s
        self._guard_s = guard_duration_s
        self._period_s = burst_duration_s + guard_duration_s
        # The device-channel arguments (rx_channel=1 = port B on B210) are
        # only used to *select* which device frontend is mapped during
        # stream_args setup. By the time the gr-uhd block exists, that
        # frontend has been remapped to block-channel 0, so all per-channel
        # API calls (set_center_freq, set_gain, ...) must use BLOCK-relative
        # indices. We therefore deliberately ignore tx_channel/rx_channel
        # here and always pass 0 to set_center_freq.
        self._tx_channel = tx_channel  # kept for logging only
        self._rx_channel = rx_channel  # kept for logging only
        self._guard_lead_s = guard_lead_s
        self._start_offset_s = start_offset_s
        self._queue_horizon_s = queue_horizon_s
        self._refill_period_s = refill_period_s

        self._tx_uhd = None
        self._rx_uhd = None
        self._t0: Optional[float] = None
        self._next_hop_index = 1
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_logged_index = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach(self, tx_uhd=None, rx_uhd=None) -> None:
        """Attach to UHD sink / source objects. Either may be None (simulation)."""
        self._tx_uhd = tx_uhd
        self._rx_uhd = rx_uhd

    def configure_epoch(self) -> Optional[float]:
        """
        Reset the device clock and pin the shared epoch T0. Must be called
        AFTER constructing the flowgraphs but BEFORE starting them, so
        set_start_time() takes effect on the first emitted sample.

        Returns T0 (FPGA seconds) or None if no UHD device is attached.
        """
        ref = self._tx_uhd or self._rx_uhd
        if ref is None:
            log.info("HopTimingController: no UHD device attached (simulation?)")
            return None

        # Pin the FPGA clock origin. set_time_now operates on the underlying
        # motherboard, so a single B210 with separate sink+source handles
        # still observes one shared time base afterwards.
        ref.set_time_now(uhd.time_spec_t(0.0))

        now = ref.get_time_now().get_real_secs()
        self._t0 = now + self._start_offset_s

        # Pin the start-of-stream times so sample 0 on each side maps to T0
        # in FPGA-time. UHD waits for its FPGA clock to reach T0 before
        # emitting / capturing sample 0.
        for label, u in (("tx", self._tx_uhd), ("rx", self._rx_uhd)):
            if u is None:
                continue
            try:
                u.set_start_time(uhd.time_spec_t(self._t0))
            except Exception as exc:
                log.warning("set_start_time failed on %s side: %s", label, exc)

        log.info(
            "HopTiming: T0=%.6f s (FPGA), period=%.3f ms (burst=%.3f, guard=%.3f), "
            "guard_lead=%.3f ms",
            self._t0, self._period_s * 1000.0,
            self._burst_s * 1000.0, self._guard_s * 1000.0,
            self._guard_lead_s * 1000.0,
        )
        return self._t0

    def start(self) -> None:
        """Pre-queue the first batch of retunes and start the refill thread."""
        if self._t0 is None:
            return
        self._next_hop_index = 1
        self._refill_queue()

        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._refill_worker, name="HopRefill", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        for u in (self._tx_uhd, self._rx_uhd):
            if u is None:
                continue
            try:
                u.clear_command_time()
            except Exception:
                pass
        self._t0 = None

    # ------------------------------------------------------------------
    # Status (for FlowgraphManager.get_stats)
    # ------------------------------------------------------------------

    def current_hop_index(self) -> int:
        """Best-effort estimate of the hop index TX is currently transmitting."""
        if self._t0 is None:
            return 0
        ref = self._tx_uhd or self._rx_uhd
        if ref is None:
            return 0
        try:
            now = ref.get_time_now().get_real_secs()
        except Exception:
            return 0
        if now < self._t0:
            return 0
        return int((now - self._t0) / self._period_s)

    def current_hop_freq(self) -> float:
        return self._scheduler.frequency_at(self.current_hop_index())

    # ------------------------------------------------------------------
    # Queue refill
    # ------------------------------------------------------------------

    def _refill_worker(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._refill_queue()
            except Exception as exc:
                log.error("HopTiming refill error: %s", exc, exc_info=True)
            self._stop_evt.wait(self._refill_period_s)

    def _refill_queue(self) -> None:
        if self._t0 is None:
            return
        ref = self._tx_uhd or self._rx_uhd
        if ref is None:
            return
        try:
            now = ref.get_time_now().get_real_secs()
        except Exception as exc:
            log.debug("get_time_now failed: %s", exc)
            return

        target_t = now + self._queue_horizon_s
        max_index = int((target_t - self._t0) / self._period_s) + 2

        with self._lock:
            n = self._next_hop_index
            while n <= max_index:
                t_retune = (
                    self._t0
                    + (n - 1) * self._period_s
                    + self._burst_s
                    + self._guard_lead_s
                )
                if t_retune > now + 0.005:
                    freq = self._scheduler.frequency_at(n)
                    self._issue_timed_retune(t_retune, freq, n)
                self._next_hop_index = n + 1
                n += 1

    def _issue_timed_retune(self, t_fpga: float, freq: float,
                            hop_index: int) -> None:
        # Use gr-uhd's "command" message port instead of the direct
        # set_command_time / set_center_freq / clear_command_time chain.
        # The msg port is the documented canonical way to do timed retunes:
        # gr-uhd's command_msg_handler runs on its own thread and applies
        # `time` + `freq` atomically on the FPGA queue. The direct-API chain
        # is fragile — a Python-thread interruption between the three calls
        # leaks an armed command time and produces the cmd-time-error storm
        # we saw in 2026-04-28 bench tests.
        cmd = _build_freq_command(freq, chan=0, t_fpga=t_fpga)
        for label, u in (
            ("tx", self._tx_uhd),
            ("rx", self._rx_uhd),
        ):
            if u is None:
                continue
            try:
                u.to_basic_block()._post(pmt.intern("command"), cmd)
            except Exception as exc:
                log.error("Timed retune (%s) failed: %s", label, exc)

        # Verbose only for the first few and then every 32nd, to keep the
        # log readable on long runs.
        if hop_index <= 4 or hop_index % 32 == 0:
            log.info(
                "HopTiming queued idx=%d freq=%.3f MHz at hw_t=%.6f",
                hop_index, freq / 1e6, t_fpga,
            )
