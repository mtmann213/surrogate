# RESUME - Surrogate Datalink System

## Session Status: **Stuck — RF-timed FHSS architecture has hit a wall. Proposing baseband digital FHSS as the next move.**

### 2026-04-28 (fourth pass) — Test results after switching to `command` message port

Channel-arg fix + msg-port retunes eliminated the loud failures (no more `RX channel <huge int>` errors, no more `cmd time errors` storm, no more `LLLLL` underflow river). But the link still does not decode a single frame, and three new symptoms surfaced that point at **fundamental limits of the RF-timed-retune approach on a single B210 with two gr-uhd handles**:

1. **TX/RX freq mismatch is persistent.** Across `[HW-STATE]` reads in the latest test:
   ```
   13:28:07.426  TX=915.700  RX=915.700   ← match
   13:28:09.542  TX=915.700  RX=914.300   ← mismatch
   13:28:11.699  TX=914.300  RX=914.300   ← match
   13:28:14.068  TX=915.700  RX=915.000   ← mismatch
   13:28:16.274  TX=914.300  RX=915.000   ← mismatch
   ```
   3 of 5 reads mismatch. With a 5 ms inter-call gap inside a 79 ms hop period, sampling-artifact would predict ~6 % mismatch — observed ~60 %. The two radios are genuinely on different schedules. Same PMT command goes to both via msg-port, but `usrp_sink` and `usrp_source` have separate command-handler threads and separate underlying command queues; commands are not arriving at the FPGA simultaneously.

2. **TX chip queue persistently full.** FrameFeeder produces frames at ~1 fps instead of the expected ~14.7 fps. Math: chip_src should drain a 17 k-chip frame in ~68 ms (250 kchip/s), so steady-state should be ~14.7 fps. Observed is ~12× slower. Either the GR scheduler is throttled (sink back-pressure) or the chain is producing zeros most of the time.

3. **`gr::uhd::rfnoc_block::general_work` aborts.** After ~12 s, a `terminate reached from thread id` traceback in the GR scheduler kills the process. The crash site is inside gr-uhd's general_work for the rfnoc_block — the scheduler hit an unhandled C++ exception bubbling up from UHD. Likely the same `Radio ctrl (0) packet parse error` we saw on the previous run, but this time fatal.

### Why the RF-timed approach is structurally fragile here

- Two `multi_usrp` handles to the same B210 (one in `usrp_sink`, one in `usrp_source`) compete for the device's command queue. Even via the canonical msg-port, the two queues drain into the FPGA via the same USB control endpoint.
- ~25 timed commands/sec (12.5 hops × 2 sides) puts steady pressure on the B210 control bus. The `radio_ctrl` packet-parse assertions are the device telling us we're saturating it.
- `set_start_time(T0)` on `usrp_sink` is unverifiable: gr-uhd's docstring says it tags the first packet, but the bench evidence (chip queue backing up at startup, freq mismatches) suggests the TX sample-0 → T0 mapping is not actually being honored in this UHD/gr-uhd combination.

### Proposed next move — baseband digital FHSS

Stop hopping the radios. Keep TX and RX on a single fixed center freq (e.g. 915 MHz). Implement hopping in baseband:

- TX path: complex-multiply the burst signal by `exp(+j 2π Δf_k t)` for hop k.
- RX path: complex-multiply the captured signal by `exp(-j 2π Δf_k t)` for hop k.
- Hop index = `sample_count // period_samples`. One shared sample counter on each side, derived from `set_start_time(T0)` on a single sink/source pair (so both sides count from the same T0).
- No timed commands, no command-queue traffic, no PLL settling, no USB control flood.

**Constraint:** Δf range must fit in baseband BW. User's hop set (914.3 / 915 / 915.7 MHz) = ±0.7 MHz spread — needs `sample_rate ≥ 2 Msps` (currently 1 Msps). B210 over USB 3 handles 2 Msps trivially.

**Replacement scope:**
- Delete `radio/hop_timing.py` (or keep as `_legacy_rf_hopping.py` reference).
- New `radio/blocks/baseband_hopper.py` (~80 lines) — gr.sync_block that maintains a phase accumulator + reads hop schedule + applies complex rotation per sample.
- Insert one BasebandHopper at the head of TX (before BurstGate) and one at the head of RX (after AGC/DC-blocker, before demod).
- `FlowgraphManager._build_flowgraphs` no longer instantiates HopTimingController; just bumps `cfg.rf.sample_rate` to ≥ 2 Msps when hopping is enabled.

**Pros:** TX/RX synchronized by construction; no `cmd time errors`; no `radio_ctrl` crashes; faster hop rates possible; works identically on dual_b205 (no shared TCXO needed because hopping is digital).

**Cons:** Hop range capped at half the sample rate; slightly higher CPU for the DDS rotation.

### What stays
- HopScheduler (the freq-list and seed-driven permutation) is unchanged.
- BurstGate / FrameFeeder / FrameSink / FEC / DSSS — all unchanged.
- The shared-T0 design from `radio/hop_timing.py` is reused: still call `set_time_now(0)` and `set_start_time(T0)` on RX source so RX sample 0 = T0. TX side uses `set_start_time(T0)` for the same reason.

### 2026-04-28 (third pass) — `command` message port for timed retunes (NOT ENOUGH)

Channel-arg fix landed cleanly (no more `RX channel <huge>` errors, hop freqs rotate). But the bench run still showed a `usrp_sink :error: cmd time errors` storm (~1000/sec) plus `LLLLL...` underflow rivers and zero RX. Two suspicious clues in the log:

- A 0.55 sec gap between queueing idx=2 and idx=3, even though the initial `_refill_queue()` should have filled 8 hops in one pass. Smells like the direct API path (set_command_time → set_center_freq → clear_command_time) blocked or got pre-empted mid-sequence.
- `usrp_source :warning: ERROR_CODE_LATE_COMMAND` arrives BEFORE the flowgraph even starts streaming — the timed commands are entering the queue with a state that the FPGA is unhappy about.

The direct-API chain has a real failure mode: if a Python interrupt or context switch hits between `set_command_time` and `clear_command_time`, the device handle is left with an armed command time that bleeds into the next unrelated UHD call. The fix is to switch to the **canonical** way of doing timed retunes in gr-uhd: post a PMT command dict to the block's `command` message port. gr-uhd runs its own `command_msg_handler` on a dedicated thread and applies `time` + `freq` atomically.

**Applied** in `radio/hop_timing.py:_issue_timed_retune`:
```python
cmd = pmt.make_dict()
cmd = pmt.dict_add(cmd, pmt.intern("freq"), pmt.from_double(float(freq)))
cmd = pmt.dict_add(cmd, pmt.intern("chan"), pmt.from_long(0))
cmd = pmt.dict_add(cmd, pmt.intern("time"),
                   pmt.cons(pmt.from_uint64(int(t_fpga)),
                            pmt.from_double(t_fpga - int(t_fpga))))
u.to_basic_block()._post(pmt.intern("command"), cmd)
```

Also synced `config/default_config.yaml` to enable hopping with the gemini hop frequencies (per user request — `python3 main.py` should now run the hopping path by default).

**Open if the next test still misbehaves:**
- TX sample-0 → T0 pinning. If retunes fire correctly but bursts cross hop boundaries, `usrp_sink.set_start_time(T0)` may not actually be tagging the first packet on this gr-uhd version. Fallback: explicit `tx_sob` / `tx_time` head-of-chain block.
- Underflows. If they persist after the cmd-time-storm clears, host CPU/USB pipeline can't sustain 1 Msps — would investigate FrameFeeder allocation overhead and chip queue draining rate.

### 2026-04-28 (second pass) — block-vs-device channel mismatch (RESOLVED)

### 2026-04-28 (later) — Channel-arg bug actually was BLOCK-vs-DEVICE channel
The `tune_request_t` wrap by itself didn't help: the user's second test run produced exactly the same `RX channel <huge int> out of range` error, just with a different garbage number (509409152 vs the previous 126008099355424). Both are memory-pointer-shaped.

Re-reading `radio/rx_flowgraph.py:89`:
```python
self._uhd_src.set_center_freq(initial_freq, 0)   # ← chan=0
```
even though `stream_args.channels = [rf.rx_channel] = [1]`. This is the convention: gr-uhd remaps the selected device frontend to *block-relative* channel index 0. Per-channel API calls (`set_center_freq`, `set_gain`, `set_antenna`) take the BLOCK index, not the device index.

`HopTimingController._issue_timed_retune` was passing `chan=rf.rx_channel=1` (the device index), which is out-of-range from the block's perspective. The garbage-pointer-looking number is pybind's stack-frame mangling once it can't find a matching overload (passing an int that's larger than the block's channel count drives it into the wrong dispatch path).

**Fix applied** (`radio/hop_timing.py`):
```python
u.set_command_time(ts)
u.set_center_freq(float(freq), 0)   # block-relative channel 0
u.clear_command_time()
```
Reverted the `tune_request_t` wrap — the simpler `(double, 0)` form matches the same overload that the flowgraph init code uses successfully. `tx_channel` / `rx_channel` constructor args are kept for logging but are no longer passed to UHD.

**Why this should also kill the cmd-time error storm:**
Previously `set_center_freq` raised before `clear_command_time` could run → the device handle was left with a stale armed command time. Once the channel arg is correct, the call succeeds and `clear_command_time` runs every cycle. Each retune is a clean arm/fire/clear sequence with no leaked state.

**Still pending if the next test still misbehaves:**
- TX sample-0 → T0 pinning. `usrp_sink.set_start_time(T0)` *should* tag the first packet with `tx_time=T0`, but the behaviour depends on gr-uhd version. Fallback: add a `TxTimeTagger` block at the head of TX emitting `tx_sob=True` + `tx_time=T0` on sample 0.
- TX chip queue backpressure (`TX chip queue full`) should clear once UHD stops error-spamming.

### 2026-04-28 — RX-channel argument bug + LATE_COMMAND clog (RESOLVED above)
First test run of the shared-epoch design: **timed-command queue is firing on the FPGA but is being mis-called.** Symptoms:

- TX log lines now correctly rotate through 913/915/917 MHz (HopTimingController-derived index works).
- `[HW-STATE]` shows TX & RX getting close but desynced (e.g. TX=913 / RX=915).
- Floods of `Timed retune (rx ch=1) failed: LookupError: IndexError: multi_usrp: RX channel 126008099355424 out of range`. The huge number is *garbage in the channel slot of set_center_freq* — almost certainly because gr-uhd's `set_center_freq` overload expects `(tune_request_t, chan)`, not `(double freq, chan)`. Passing `freq` as the first positional arg is being type-coerced to a tune_request whose internals are read as the channel id.
- TX side appears to accept `set_center_freq(freq, chan)` happily (no TX errors), but several `usrp_sink :error: In the last X ms, N cmd time errors occurred` indicate timed commands arriving with a hardware time already past — i.e. the refill thread is queueing retunes whose `T_retune` is ≤ current FPGA time. Suggests `set_start_time` did not actually pin the TX sink's first sample to T0, OR pin works but the `Restarting flowgraphs` path never re-applied it after the GUI Start button.
- `usrp_source :warning: ERROR_CODE_LATE_COMMAND` appears once early — same problem on RX.
- TX feeder occasionally hits `chip queue full — dropping frame` because the actual sample-emission rate doesn't match `chip_queue` drain rate (LLL = late underruns spam).

**Diagnosis:**
1. **set_center_freq signature.** Need to wrap freq in `uhd.tune_request_t(freq)` before calling `set_center_freq`. Without this, gr-uhd's pybind reads the second-arg slot from elsewhere on the stack/object and produces a junk channel index.
2. **set_start_time on a sink** does not pin sample-0 to T0 the way it does on a source. T0 is now in the past relative to actual TX antenna time → refilled commands repeatedly miss their hardware deadline and produce the LATE_COMMAND storm.
3. **Wider sample rate (note: log shows period=79 ms now, not 22 ms).** burst=69 ms, guard=10 ms — config has been changed since last session. period_s is fine; it just means more time per hop.

**Next moves (queued, not yet applied):**
1. In `HopTimingController._issue_timed_retune`, change to `u.set_center_freq(uhd.tune_request_t(freq), chan)`. Should kill the channel-index error storm on the RX side.
2. Make TX timing alignment explicit: tag sample 0 of the TX path with `tx_time = T0` (and `tx_sob = True`). Either via a small helper block placed at the head of the TX chain, or by using `usrp_sink.set_start_time` *and* verifying the resulting timing. If the explicit tag is needed, add a `TxTimeTagger` block.
3. Investigate why TX feeder backs up (`chip queue full`) — probably because `BurstGate` is running at its own sample-counter rate and not matching what the sink actually consumes after the alignment fix lands.

### 2026-04-28 — Strict shared-epoch architecture (`HopTimingController`) — awaiting test
After repeated failure of reactive RX retunes, the architecture was rewritten so both TX and RX hop on the **same FPGA clock**, no preamble dependency. New module `radio/hop_timing.py:HopTimingController` is now the sole authority on hop timing.

**Design:**
- At flowgraph build time, FlowgraphManager calls `usrp_sink.set_time_now(0)` to pin the device clock origin, then picks epoch `T0 = now + 0.5 s`.
- `usrp_sink.set_start_time(T0)` and `usrp_source.set_start_time(T0)` make sample-0 on each side correspond to FPGA time T0. Burst N then occupies antenna time `[T0 + N·period_s, T0 + N·period_s + burst_s]` automatically (BurstGate's sample counter starts at 0 = T0).
- `HopTimingController` queues UHD timed commands on **both** sink and source: at FPGA time `T0 + (N-1)·period_s + burst_s + 1 ms`, both RFICs retune to `freq[N]`. Background thread refills the queue every 200 ms, keeping ~1 s of retunes ahead.
- Single B210 = single TCXO, so TX-sink and RX-source see identical FPGA time. TX and RX retune on the exact same hardware instant; there is no race.

**What was removed:**
- TX `HopController` no longer emits `tx_command` stream tags. Block is a pure complex pass-through (kept in topology only as the IQ-recording tap point).
- RX `HopController` no longer reads `rx_time` tags, no longer issues `tune_cmd` PMTs, no longer has `advance_and_tune()`. Block is a pure pass-through.
- `FlowgraphManager._dispatch_frame` no longer calls `advance_and_tune()`. Frame decode and hop scheduling are completely decoupled.
- `rx_flowgraph._connect` no longer wires `tune_cmd → uhd_src.command`.

**Files touched this turn:**
- `radio/hop_timing.py` — NEW. Owns T0, queues timed retune commands.
- `radio/flowgraph_manager.py` — instantiates HopTimingController between flowgraph build and start; removed advance_and_tune() call from `_dispatch_frame`; stats now read hop index/freq from HopTimingController.
- `radio/blocks/hop_controller.py` — gutted to pure pass-through.
- `radio/rx_flowgraph.py` — removed `tune_cmd` msg_connect and stale comments.

**Expected behavior on next test:**
- Log line: `HopTiming: T0=… s (FPGA), period=… ms (burst=…, guard=…)`.
- Log lines: `HopTiming queued idx=N freq=… MHz at hw_t=…` for N=1..4 immediately, then every 32nd hop.
- TX log line: `[TX] Frame #N | freq=…` should rotate through hop frequencies (read from HopTimingController, not config center).
- RX should produce FEC=OK frames steadily once AGC settles, without the 1-frame-then-silence pattern.

**If it still fails:**
1. Check whether `set_start_time` actually pins TX sink's first-sample time. If gr-uhd's `usrp_sink.set_start_time` doesn't add a `tx_time` tag, TX may stream as soon as fed and T0 is wrong relative to actual burst antenna times. Fix: add an explicit `tx_time`/`tx_sob` tagger block at the head of TX.
2. Verify B210 underflows aren't crashing the timed-command queue. UHD has a finite command queue — refill thread must keep up.
3. dual_b210 / dual_b205 modes share NO TCXO without external 10 MHz reference — this design assumes single_b210 or properly externally-referenced dual configs.

### Earlier sessions
Prior context (BPSK regression, OQPSK work, reactive RX hop attempts) preserved below. Sections marked "FHSS Hop Sync" describe the failed reactive design that was replaced this session.

## Earlier Session Status: **In Progress — Fixing FHSS Hop Sync**
BPSK without hopping ≈ 80% FEC=OK (chain healthy). With hopping enabled: still 0% FEC=OK after multiple attempted fixes. Root cause not yet found. Last test (2026-04-27): user log shows no "Hop-Tag" lines and no preamble detections — TX hops may not even be firing, OR they fire but the RX side never sees a valid burst.

### 2026-04-27 — Chicken-and-egg + wrong target_hw formula (fixed, awaiting test)
Re-read repo after local LLM continued work. Two compounding bugs in the RX hop path:

**Bug A — Chicken-and-egg gating on FEC=OK.** `FlowgraphManager._dispatch_frame` only called `HopController.advance_and_tune()` when `fec_ok=True`. But after burst 0, FEC=OK requires the retune to fire on the previous burst, and the retune fires only on FEC=OK → permanent deadlock the moment any burst FEC-fails. Also: `_time_anchored_set` is initialised inside `advance_and_tune()`, so the standalone time-anchored watchdog in `work()` was likewise never armed. **Fix:** `_dispatch_frame` now calls `advance_and_tune()` on every preamble-confirmed frame, before the FEC-error early return. A successful preamble correlation is itself proof we were tuned correctly for the burst that just arrived — that is the right gate, not FEC.

**Bug B — `target_hw` math points into the burst, not the guard.** The previous formula `(period+1)*period_s - burst_s - 0.002` simplifies to `anchor + period*period_s + guard_s - 0.002` — i.e. only `guard_s − 2ms` past the burst's *start*. With burst_s=17.27 ms and guard_s=5 ms that lands the retune ~3 ms into the burst we just received, so the PLL settles during the next burst's data section. Replaced both occurrences (advance_and_tune and the work() watchdog) with `anchor + period*period_s + burst_s + 0.001` — 1 ms into the guard immediately *after* burst N. UHD's FPGA-side timed command executes regardless of host pipeline lag, so the settle window is the full guard minus 1 ms.

**Files touched this turn:**
- `radio/flowgraph_manager.py` — moved `advance_and_tune()` ahead of the `if not fec_ok: return`.
- `radio/blocks/hop_controller.py` — corrected target_hw formula in `advance_and_tune()` and the `work()` watchdog.

**Open concerns (not yet acted on):**
- The time anchor is set from the decode-worker thread reading `nitems_read(0)`, which is several ms ahead of the actual frame. So `_time_anchored_hw` is offset by the decode latency L. Combined target time becomes `burst_0_end + L + 1ms`. If L > guard_s ≈ 5 ms, UHD sees the timed command in the past and executes it immediately — landing inside burst N+1. The clean fix is to anchor from the GR scheduler thread via FrameSink's `preamble_detected` msg port (which the local LLM removed). If this run still shows late retunes, restore that wiring or bump `transition_time_ms` to ≥10 ms.
- TX side previously logged zero `Hop-Tag` lines on the failing run. The TX work() loop and tag-emission code look correct on paper (idx=0 → emit seq[1] tag at first guard-start, _current_freq starts at 0 so first tag always fires). If the next test still shows no Hop-Tag lines, suspect: (a) UHD underflows starving the flowgraph before guard-start is reached, (b) HopController not actually wired into TX path post-restart.

### 2026-04-26 — Opus session resumes
A local model (Qwen3 35B) made changes between the last Opus session and now: added `radio/blocks/interleave.py` (Deinterleave, Interleave, HardDecision), wired Deinterleave into the TX OQPSK path. BPSK regression was introduced during that work. OQPSK RX still routes through the BPSK path (no Interleave/HardDecision yet on RX). Suspected OQPSK TX bug: `_rrc_i`/`_rrc_q` use `interp_fir_filter_fff(sps_int, ...)` but inputs are at `chip_rate/2`, so output is `sample_rate/2` — wrong rate; should be `sps_int * 2` for chip_rate/2 → sample_rate. Q delay `sps_int // 2` at sample_rate/2 = full chip period, not T/2. These bugs are queued for the OQPSK fix.

### 2026-04-26 — FHSS Hop Sync Investigation

**Done:**
- Verified BPSK chain works without hopping (~80% FEC=OK).
- Collapsed dual `HopScheduler` instances into one shared instance — TX advances, RX reads via `frequency_at()`.
- Added `mode='tx'|'rx'` to `HopController`. RX mode is preamble-anchored: `FrameSink.preamble_detected` msg → HopController advances local hop index → retune via UHD source's `command` port.
- TX and RX UHD blocks both initialized at `frequency_at(0)` so first burst aligns.
- Tried IMMEDIATE retune on preamble → 0% FEC=OK (data corrupted by mid-burst PLL).
- Tried DEFERRED retune via Python Timer at `burst_duration + 1ms` → 0% FEC=OK (delay overshoots guard, lands in burst N+1's preamble window).

**Root cause:** Software preamble-detect time is offset from antenna time by host pipeline lag L (USB DMA + GR queue depth). To put hardware retune in the 5ms guard, we need delay ∈ (17.27 − L, 22.27 − L − PLL_settle) ms. With typical L ≈ 5–10ms, NEITHER immediate (delay=0) nor 18ms-deferred works.

**Next step (in progress):** Pass `uhd_src` to RX `HopController`. Read `rx_time` tags from upstream so we know the antenna hardware time of HopController's input samples. In `_on_preamble`, compute target hw_time = (preamble antenna time) + `burst_duration_s` + 1ms and issue a *timed* PMT tune_cmd (`time` field). UHD source executes it on the FPGA at exactly that hw_time, putting the retune in the guard regardless of L.

**Bug found in first attempt (2026-04-27):** `time` PMT was built with `pmt.make_tuple` — gr-uhd's command port parses it as a **pair** (cons cell) and silently ignores tuples. With a malformed timed command, RX never retuned past freq[0], so after burst 0 no further preambles were detected. Fixed: use `pmt.cons(uint64, double)`. Also wrapped the upstream tag scan in try/except so any PMT API quirk can't break the signal pass-through.

**Actual root cause found (2026-04-26, after RX fix):** Even with timed-command RX retunes firing at correct hw_t inside the guard, RX still got only 1 FEC=OK frame. Re-read user's TX log: `TX Ch 0 Hop-Tag → 915.000 MHz (offset=0)`, `→ 914.000 MHz (offset=44536)`, `→ 916.000 MHz (offset=89072)` — all at multiples of period_samples=44536, i.e. at the **start of each burst**, not in the guard. TX HopController was placing `tx_command` tags at the period boundary, so UHD retuned exactly when the next burst's first sample went out the antenna. PLL settled during the data section of every burst → every frame corrupted at the source. RX-side fixes alone could never help.

**Fix attempt (in `radio/blocks/hop_controller.py`):**
1. TX work() now places the tag at `(burst_samples - in_period) % period` = start of the guard interval, giving the PLL the full guard to settle before the next burst's preamble.
2. TX-mode constructor pre-advances the scheduler once. UHD sink is initialized at `frequency_at(0)` for burst 0, so the first guard-start tag (which schedules burst 1's frequency) must return freq[1].

**Result (2026-04-27 user test):** Did NOT fix it. Run output:
- No `TX Ch 0 Hop-Tag → ...` lines anywhere in the log (previous run had them at offsets 0/44536/89072).
- No RX preamble-detected log lines.
- Many UHD underflows ("U" chars) and at least one `usrp_sink :error: ... 1 underflows occurred`.
- A `Restarting flowgraphs` happened mid-run; after restart the log is silent except for "[TX] Frame #N | freq=915.000 MHz" lines (these come from FrameFeeder's own log of the *configured* center freq — they do NOT reflect actual hop state).

**Hypotheses to investigate next session (not yet acted on, per user instruction):**
1. The pre-advance plus the `freq != self._current_freq` gate may be skipping the FIRST tag entirely if `_current_freq` initialization races with the scheduler state. Verify the first tag actually fires.
2. The RX HopController's input queue may not be receiving rx_time tags in this configuration — anchor was set ONCE at start (sample=0, hw_t=2.548645) but no "preamble@idx=" lines means `_on_preamble` is never being called by FrameSink. RX preamble detection itself may be the failure, not the retune.
3. UHD underflows mean TX is starved during bursts — could be corrupting more than just the late samples. Check if hopping enabled changes the GR scheduler's timing budget.
4. With pre-advance, the scheduler is now at index 1 when work() first runs. If the first guard-start fires `next_frequency()` at burst 0's guard, that returns freq[2], not freq[1]. UHD's initial freq is freq[0], so burst 1 should be on freq[1] — but UHD will be tuned to freq[2]. Off-by-one in the pre-advance logic likely.
5. Verify TX HopController is actually wired into the flowgraph (was it disconnected during a recent edit?). The total absence of Hop-Tag log lines is suspicious.

**Other items found in `llm-handover.md`** (queued, not yet acted on):
- OQPSK `HardDecision` block has inverted polarity vs TX bit mapping (TX `0 → +1.0`, RX `>=0 → 1`) — likely masked by `parse_frame()`'s phase-inversion correction but worth flipping for clarity.
- OQPSK TX `_rrc_i`/`_rrc_q` interp factor is `sps_int` but should be `2 * sps_int` (input is at `chip_rate/2`); Q delay should be `sps_int` samples at the corrected output rate, not `sps_int // 2`.

## Key Accomplishments

### 1. Robust Frequency Hopping
- **Heartbeat Architecture**: Redesigned the TX path to use a continuous noise heartbeat. This ensures the sample counters in the `HopController` and `BurstGate` never stall, keeping TX/RX perfectly aligned even during USB/CPU underflows.
- **Tag-based Tuning**: Switched from asynchronous PMT messages to high-precision Stream Tags (`tx_command`). The USRP hardware now executes frequency hops at the exact nanosecond specified, eliminating drift.
- **Port Mapping**: Fixed a Segfault by ensuring all tuning commands use `chan=0` within the flowgraph context, regardless of physical mapping.

### 2. Receiver Reliability
- **Continuous Sliding Window**: Removed a bug in `FrameSink` that was discarding data chunks. Detection is now gapless.
- **Invariant Sync & Phase Correction**: Added a 16-bit invariant (0x1234) check to every frame. The receiver now automatically detects and corrects 180° phase inversions (bit flips), ensuring `FEC=OK` even with complex antenna setups.
- **Junk Filtering**: Real-time terminal logs and GUI stats now only count valid frames, filtering out false-positive triggers from noise.

### 3. New Features
- **Modulation**: Added **OQPSK**, **MSK**, and **GMSK** support.
- **Simulation Mode**: Integrated a ZMQ-based simulation mode for dev/test without USRP hardware.
- **GUI Controls**:
  - Direct toolbar **Record IQ** button.
  - Custom **Hex Payload** input.
  - Hardware port and antenna selection (e.g., Port A/B, TX/RX, RX2).
- **Hardware Monitor**: A background thread now logs the *actual* frequency reported by the USRP B210 hardware driver every 2 seconds.

### 4. Performance Fixes
- **GIL Starvation**: Disabled Python blocks that were stalling the GR scheduler, eliminating 400+ TX underflows/sec.
- **FrameSink Optimization**: Replaced numpy deque with `np.concatenate` (589µs → 0.9µs per call).
- **Viterbi Decoder**: Vectorized ACS table computation, releasing the GIL during decode.
- **IQ Recording**: Fixed 0-byte files by using dynamic valve connection with lock/unlock pattern.

### 5. OQPSK Implementation (In Progress)
- **Deinterleave Block** (`radio/blocks/interleave.py`): Splits chip stream into I/Q channels (even→I, odd→Q).
- **Interleave Block**: Recombines I/Q chips back into stream (I[0], Q[0], I[1], Q[1]...).
- **HardDecision Block**: Converts float32 ±1 symbols to uint8 0/1 bits.
- **TX OQPSK Path**: Deinterleave → I/Q interpolating RRC → Q delay (T/2) → combine complex.
- **RX OQPSK Path**: RRC → M&M timing recovery → extract I/Q → hard decision → interleave.
- **Forecast API Fix**: Updated `forecast()` to return list instead of modifying array (newer GR gateway).
- **Status**: BPSK regression discovered — all frames now show FEC=ERR. Investigation ongoing.

## Current Setup Guide
- **Hardware**: USRP B210
- **Cabling**: RF0 TX/RX (Port A) → RF1 RX2 (Port B) with 60 dB attenuation.
- **Optimal Settings**:
  - Sample Rate: **2.0 MHz**
  - Transition Time: **5.0 ms**
  - TX/RX Gain: **50.0 dB**

## Known Issues
1. **FHSS Hop Sync (active)**: 0% FEC=OK with hopping enabled. RX retune fires either mid-burst (immediate) or past the guard (Timer-deferred). Switching to UHD timed-command using rx_time-tag-derived antenna time.
2. **OQPSK Not Working**: RX SNR is very low (0-2 dB) with separate M&M timing recovery per channel. Single M&M approach also shows low SNR. I/Q alignment after timing recovery is uncertain. Queued behind hopping fix.
3. **BPSK without hopping**: Occasional FEC=ERR at low SNR with bit-flipped invariant payload (fffefdfc...). UHD underflows under load. "TX chip queue full" warnings under sustained load.

## Technical Debts / Future Work
- **Automatic Burst Alignment**: Currently relies on sample counting from start. Adding a "Sync Search" mode to the RX `HopController` would allow it to join an already-running hop sequence.
- **FEC Performance**: The vectorized Viterbi is fast, but K=9 support would improve coding gain further.
- **Hardware Agnostic IDs**: Some logic still assumes USRP-specific property names; could be generalized for other SDRs.
- **OQPSK Timing Recovery**: The M&M timing recovery may not be converging properly for OQPSK's I/Q structure. Consider: (a) processing only one channel through M&M and extracting the other, (b) using soft-decision Viterbi instead of hard decision, (c) verifying chip timing with inspectrum.

## Quick Start for Next Session
```bash
python3 main.py
# 1. Start on RF Tab
# 2. Check Antenna (TX/RX and RX2) and Channels (0 and 1)
# 3. Enable Hopping on Hopping Tab
# 4. Click Start
```

## Debugging Notes (Current Session)
- BPSK was producing FEC=OK frames before OQPSK work began.
- After adding interleave.py blocks and modifying rx_flowgraph.py, BPSK now shows FEC=ERR.
- The BPSK code paths in `_connect()` should be unaffected by OQPSK changes (they're in separate `elif` branches).
- Investigating: check if `forecast()` signature change affected other blocks, verify spreading code consistency between TX/RX, confirm no subtle import or initialization issues.
