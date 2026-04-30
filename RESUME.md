# RESUME - Surrogate Datalink System

## Session Status: **Baseband digital FHSS implemented. RF-timed retune architecture eliminated.**

### 2026-04-28 — Baseband digital FHSS replaces broken RF-timed architecture

The RF-timed FHSS approach (`HopTimingController`) has been fully replaced with baseband digital FHSS (`BasebandHopper`). The old approach posted timed retune commands to both UHD sink and source via the gr-uhd `command` message port ~25 times/sec, which saturated the B210 USB control bus, caused TX/RX freq mismatches ~60% of the time, and crashed after ~10-12 s with `radio_ctrl` packet parse errors.

**What changed:**
- **New** `radio/blocks/baseband_hopper.py` — `gr.sync_block` implementing per-sample complex rotation:
  - TX: `out = in * exp(+j * 2π * Δf_k * t)` — shifts baseband to hop frequency
  - RX: `out = in * exp(-j * 2π * Δf_k * t)` — cancels TX rotation back to baseband
  - Phase-accumulator based for phase continuity across hop boundaries
  - Short-circuits (pure pass-through) when only one frequency in hop sequence
- `radio/hop_timing.py` (`HopTimingController`) — no longer imported or instantiated. Left as reference.
- `radio/flowgraph_manager.py` — creates separate TX/RX BasebandHopper instances when `hopping.enabled`; no longer creates `HopTimingController`
- `radio/tx_flowgraph.py` — `BasebandHopper` inserted between `_hop_ctrl` and `_uhd_sink`
- `radio/rx_flowgraph.py` — `BasebandHopper` inserted after AGC, before demod chain
- `config/default_config.yaml` — `sample_rate` bumped to **2.0 Msps** (was 1.0 Msps) to satisfy Nyquist for ±700 kHz hop range

**Config parameters (active):**
- `sample_rate: 2.0 Msps`, `chip_rate: 250 kchip/s`, `SPS = 8`
- `hopping.enabled: true`, `hop_frequencies: [915.0, 914.3, 915.7] MHz`
- `burst_duration_ms: 69.072 ms`, `transition_time_ms: 10.0 ms`
- Analog bandwidth: `1.0 MHz` (auto-derived from sample_rate/2)

**To test static path (no hopping):** set `hopping.enabled: false` in config.

## Key Accomplishments

### 1. Baseband Digital FHSS
- **Zero RF retunes** — both TX and RX stay on a single fixed center frequency; hopping is a complex rotation in the GR chain.
- **TX/RX synchronized by construction** — both sides share the same hop schedule and sample-counter timing (anchored to T0 via `set_start_time`). No command bus traffic, no PLL settling, no `cmd time errors`.
- **No USB control bus saturation** — the old ~25 commands/sec timed-retune traffic that caused B210 crashes is eliminated.
- **Works on dual_b205** — no shared TCXO needed because hopping is digital.

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
- **Status**: Needs verification after baseband FHSS landing.

## Current Setup Guide
- **Hardware**: USRP B210
- **Cabling**: RF0 TX/RX (Port A) → RF1 RX2 (Port B) with 60 dB attenuation.
- **Optimal Settings**:
  - Sample Rate: **2.0 MHz**
  - Transition Time: **10.0 ms**
  - TX/RX Gain: **50.0 dB**

## Known Issues
1. **Baseband FHSS (in test)**: Implemented but not yet verified on hardware. Should work where RF-timed approach failed.
2. **OQPSK Not Working**: RX SNR is very low (0-2 dB) with separate M&M timing recovery per channel. Single M&M approach also shows low SNR. I/Q alignment after timing recovery is uncertain.
3. **BPSK without hopping**: Occasional FEC=ERR at low SNR with bit-flipped invariant payload (fffefdfc...). UHD underflows under load. "TX chip queue full" warnings under sustained load.

## Technical Debts / Future Work
- **Automatic Burst Alignment**: Currently relies on sample counting from start. Adding a "Sync Search" mode to the RX `BasebandHopper` would allow it to join an already-running hop sequence.
- **BasebandHopper sample counter**: Currently uses local sample count (starts at 0 when flowgraph runs). In principle TX and RX should align via T0, but a shared absolute-sample-count mechanism (e.g. reading FPGA time in work()) would be more robust.
- **FEC Performance**: The vectorized Viterbi is fast, but K=9 support would improve coding gain further.
- **Hardware Agnostic IDs**: Some logic still assumes USRP-specific property names; could be generalized for other SDRs.
- **OQPSK Timing Recovery**: The M&M timing recovery may not be converging properly for OQPSK's I/Q structure. Consider: (a) processing only one channel through M&M and extracting the other, (b) using soft-decision Viterbi instead of hard decision, (c) verifying chip timing with inspectrum.
- **Legacy hop_timing.py**: `radio/hop_timing.py` is no longer imported anywhere but kept as reference. Can be deleted or renamed to `_legacy_rf_hopping.py` once baseband FHSS is verified.

## Quick Start for Next Session
```bash
python3 main.py
# 1. Start on RF Tab
# 2. Check Antenna (TX/RX and RX2) and Channels (0 and 1)
# 3. Enable Hopping on Hopping Tab (now baseband digital FHSS)
# 4. Click Start
```

## Debugging Notes (Current Session)
- Baseband FHSS is now the hopping implementation. The `BasebandHopper` block is a `gr.sync_block` that applies `exp(±j * 2π * Δf_k * t)` per sample.
- TX applies +Δf_k, RX applies -Δf_k. Both are driven by the shared `HopScheduler` and independent local sample counters.
- The hop counter indexing: `sample_count // (burst_samples + guard_samples)`.
- Phase is reset to 0 at each hop boundary to avoid numerical drift.
- Constraint: `|Δf_max| ≤ sample_rate / 2`. With 2 Msps and ±700 kHz hops, there is margin.
- Analog bandwidth = `sample_rate / 2 = 1 MHz` — must be wide enough to pass the full hop range.