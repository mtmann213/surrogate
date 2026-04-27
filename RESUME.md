# RESUME - Surrogate Datalink System

## Session Status: **In Progress — Fixing FHSS Hop Sync**
BPSK without hopping ≈ 80% FEC=OK (chain healthy). With hopping enabled: still 0% FEC=OK after multiple attempted fixes. Root cause not yet found. Last test (2026-04-27): user log shows no "Hop-Tag" lines and no preamble detections — TX hops may not even be firing, OR they fire but the RX side never sees a valid burst.

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
