# LLM Handover — Surrogate Datalink System

Generated: 2026-04-26
Last update: 2026-04-27 (Opus session)

---

## 0. Current State (2026-04-27 update — supersedes Section 2 for the BPSK regression status)

The earlier "BPSK regression" was resolved before the hop-sync investigation began: BPSK without hopping now produces ~80% FEC=OK frames. Active blocker is now FHSS hop synchronization.

**Hop-sync status: STILL UNRESOLVED.**

### Investigation log

1. **Architecture changes (working):**
   - Single shared `HopScheduler` between TX and RX (TX advances; RX reads via `frequency_at()`).
   - `HopController` gained a `mode='tx'|'rx'` parameter and an optional `uhd_src` for the rx_time anchor.
   - RX is preamble-anchored: `FrameSink.preamble_detected` msg → `HopController._on_preamble` → emits a UHD `tune_cmd` PMT.
   - Both UHD blocks initialise at `frequency_at(0)` so burst 0 aligns.

2. **Failed RX retune timing attempts:**
   - IMMEDIATE retune on preamble → 0% FEC=OK (mid-burst PLL).
   - DEFERRED retune via Python `Timer(burst+1ms)` → 0% FEC=OK (host pipeline lag pushes execution past the guard).
   - **Dead-zone analysis:** to land in the 5 ms guard, delay must be in `(17.27 − L, 22.27 − L − PLL_settle)` ms where L = host pipeline lag. Typical B210 L ≈ 5–10 ms — neither 0 nor 18 ms works.

3. **Switched to UHD timed commands (in `_on_preamble`):**
   - Read `rx_time` tags from upstream UHD source in `work()` (try/except wrapped to never break pass-through).
   - Compute `target_hw = anchor_hw + (sample_now − anchor_sample)/sample_rate + burst_s + 1 ms`.
   - Issue `tune_cmd` PMT with a `time` field so FPGA executes the retune at the antenna at exactly that hw_time.
   - **PMT bug found and fixed:** initially built `time` with `pmt.make_tuple(uint64, double)`. gr-uhd's `command` port parses time as a **pair (cons cell)**, not a tuple, and silently ignores tuples. After this fix RX never retuned past freq[0]. Corrected to `pmt.cons(pmt.from_uint64(whole), pmt.from_double(frac))`.

4. **Tested with timed-command RX retune:** Improved but still ~1 FEC=OK frame total. User log showed correct "RX rx_time anchor set" line, several "preamble@idx=N → tune to … MHz @ hw_t=…" lines with reasonable hw_t values, but every received frame's payload was random.

5. **Identified TX bug from that same log:**
   - Lines: `TX Ch 0 Hop-Tag → 915.000 MHz (offset=0)`, `→ 914.000 MHz (offset=44536)`, `→ 916.000 MHz (offset=89072)`.
   - Offsets are exactly multiples of `period_samples = 44536`, i.e. **start of each burst**, not in the guard.
   - UHD retunes at burst-start → PLL settles during data section → every frame corrupted.

6. **Applied TX-side fix in `radio/blocks/hop_controller.py`:**
   - `work()` now places the `tx_command` tag at `(burst_samples − in_period) % period` (start of guard), not at the period boundary.
   - TX-mode constructor pre-advances the scheduler once (so the first guard-start tag carries `freq[1]`, since UHD sink is initialised at `freq[0]` for burst 0).

7. **Tested 2026-04-27 — DID NOT FIX IT.** User's run log shows:
   - **Zero `TX Ch X Hop-Tag → ...` log lines.** With the fix, the first one should fire ~17.27 ms after start.
   - **Zero RX `preamble@idx=` log lines.** RX preamble detection appears to never trigger.
   - "[TX] Frame #N | freq=915.000 MHz" lines come from `FrameFeeder` and just echo the *configured center freq* — they say nothing about hop state.
   - Multiple UHD underflows ("U" chars) and at least one `usrp_sink :error: 1 underflows occurred`.
   - A `Restarting flowgraphs` happens mid-run; after restart, no underflows but also no frames decoded.

### Hypotheses for next session (not yet investigated; user said "don't fix anything just yet")

1. **Pre-advance off-by-one.** With the constructor's `next_frequency()` plus the work() guard-start `next_frequency()`, the first guard-start tag may be advancing the scheduler twice before burst 1, skipping freq[1] entirely. Re-check the math.
2. **First tag suppressed by `if freq != self._current_freq` gate.** `_current_freq` starts at 0.0; first scheduler return is freq[1]=914e6 (after pre-advance). 914e6 ≠ 0.0 so the gate should pass. But verify nothing else is short-circuiting.
3. **HopController may not be wired into the TX flowgraph anymore.** The total absence of "Hop-Tag" log lines is the loudest signal in the run. Read `radio/tx_flowgraph.py` and confirm the HopController is connected (between heartbeat-add and UHD sink, per the architecture doc).
4. **RX preamble detection itself is broken with hopping enabled.** RX log shows `RX rx_time anchor set: sample=0 hw_t=2.548645` once at start, then nothing. If TX is in fact still hopping but the RX UHD sink is stuck on freq[0], no burst after burst 0 will be received → no preamble.
5. **UHD underflows under hopping load.** Sustained underflows can corrupt sample timing. Even if the hop logic is correct, the data may be unrecoverable. Investigate root cause of underflows (could be GR scheduler stall, USB bandwidth, or block-level inefficiency triggered by hop logic).

### Files touched in this session

| File | Change |
|---|---|
| `radio/blocks/hop_controller.py` | Added `mode`, `uhd_src` params; rx_time anchor; timed `tune_cmd` via `pmt.cons`; TX guard-start tag placement; TX scheduler pre-advance. |
| `radio/rx_flowgraph.py` | Pass `uhd_src=self._uhd_src` into RX HopController. |
| `RESUME.md` | Updated session status, root-cause hypothesis log, and 2026-04-27 negative test result. |
| `llm-handover.md` | This Section 0 prepended. |

### Items from earlier handover that are still queued (not yet acted on)

- OQPSK `HardDecision` polarity (TX `0 → +1.0`, RX `>=0 → 1`) — likely masked by `parse_frame()` phase-inversion correction but worth flipping for clarity.
- OQPSK TX `_rrc_i`/`_rrc_q` interp factor should be `2 * sps_int` (input is at chip_rate/2). Q delay should be `sps_int` samples at the corrected output rate, not `sps_int // 2`.

Sections 2–11 below were written before the hop-sync investigation and describe the BPSK-regression era; treat them as historical except for the architecture/config/file-inventory references, which are still accurate.

---

## 1. Project Overview

A configurable bidirectional missile datalink surrogate implemented in Python and GNU Radio, targeting USRP B205/B210 SDRs. Emulates FHSS+DSSS burst transmissions with full control over RF parameters, spreading codes, FEC, frame structure, anomaly injection, and IQ recording.

**Repo root:** `/home/dev2/sandbox/surrogate`

### Layered Architecture

| Layer | Path | Description | GR Dependency |
|---|---|---|---|
| Core | `core/` | Pure Python/NumPy: FEC, spreading codes, frame gen/parse, hop scheduler, anomaly state | None |
| Radio | `radio/` | GNU Radio flowgraphs + custom blocks | `gnuradio`, `uhd` |
| GUI | `gui/` | PyQt5 panels, one per config section | `PyQt5` |
| Logging | `logging_module/` | CSV writer, console/file logger | None |

### Entry Point

```
python main.py                          # GUI mode
python main.py --no-gui                 # Headless
python main.py --config config/foo.yaml # Custom config
```

`main.py` → `ConfigManager` → `SurrogateLogger` → `FlowgraphManager` → GUI/headless loop.

---

## 2. Current State: **BPSK REGRESSION BLOCKING ALL TESTING**

### What Was Working
- BPSK producing FEC=OK frames at 6-20 dB SNR, hopping enabled, Gold code 31×, K=7 CC FEC
- All core modules independently tested and working
- IQ recording, anomaly injection, simulation mode all functional

### What Broke
- After OQPSK work (interleave.py blocks added, rx_flowgraph.py modified), BPSK now shows FEC=ERR on all frames despite good SNR
- Preamble detection still works (SNR 6-20 dB reported)
- Payload hex is random (not 0001020304...), indicating FEC decode failure, not SNR issue

### Why It's Mysterious
- BPSK path is in a separate `else` branch in both TX and RX `_connect()` — OQPSK wiring changes shouldn't affect it
- `forecast()` API fixes (return `list` instead of modifying array) were applied correctly to all blocks
- No spreading code or FEC changes between working and broken states

### Suspect Areas for Investigation
1. **Import side effects** from `from radio.blocks.interleave import Interleave` in `rx_flowgraph.py` — could this trigger GR module initialization that changes behavior?
2. **Shared block instance reuse** — `_rrc_mf` and `_timing_recovery` and `_costas` are always created in `__init__`. For OQPSK, a second M&M (`_oqpsk_mm`) is created in `_connect()`. Does creating blocks inside `_connect()` cause any GR scheduler issues?
3. **Type mismatch** — verify `complex_to_real` output type matches FrameSink's `np.float32` input expectation
4. **GR version mismatch** — check if `gnuradio` package was updated between sessions

---

## 3. OQPSK Implementation: CODED BUT UNVERIFIED

### TX Path (tx_flowgraph.py)

```
FrameChipSource → char_to_float → scale(-2) → offset(+1) → [±1 float32]
  → Deinterleave → I channel → interp_fir_filter_fff(sps_int, rrc_taps)
                      → Q channel → interp_fir_filter_fff(sps_int, rrc_taps) → delay(sps_int//2)
  → float_to_complex → BurstGate → AnomalyBlock → HopController → UHD sink
```

**BUG — Rate Mismatch:**
- Deinterleave outputs I/Q at `chip_rate/2` each
- `interp_fir_filter_fff(sps_int, rrc_taps)` with sps_int=2: input 500kchips/s → output 1Msps
- **Expected:** 2Msps (sample_rate). **Actual:** 1Msps (sample_rate/2)
- **Fix:** Use `2 * sps_int` as interpolation factor. Also redesign taps: `firdes.root_raised_cosine(1.0, sample_rate, chip_rate/2, rolloff, n_taps)` since input symbol rate is `chip_rate/2`.
- Q delay: `sps_int // 2` at sample_rate/2 = `1` sample = full chip period T, not T/2. **Fix:** delay by `sps_int` samples at the corrected output rate.

### RX Path (rx_flowgraph.py) — Implemented by Claude

```
UHD → DC blocker → AGC → HopController → RRC MF (fir_filter_ccf)
  → clock_recovery_mm_cc (single, on complex signal)
  → complex_to_real → HardDecision → char_to_float → scale(-2) → offset(+1) → Interleave → FrameSink
  → complex_to_imag → HardDecision → char_to_float → scale(-2) → offset(+1) ──────────────┘
```

**Potential Issues:**
- M&M on complex OQPSK signal: I chips and Q chips are offset by T/2. M&M sees transitions on both I and Q simultaneously, which may cause timing ambiguity (locking to 2 samples/chip instead of 1).
- HardDecision threshold at 0: `>= 0 → 1, < 0 → 0`. This matches the TX mapping (`0 → +1, 1 → -1`), so `+1 → 1, -1 → 0`. Wait — TX maps bit 0 to +1.0 and bit 1 to -1.0. HardDecision maps `>= 0 → 1`, which means +1.0 → bit 1. **This is inverted!** The HardDecision should be `>= 0 → 0, < 0 → 1` to match the TX mapping. Or the scaling should be different.

**HardDecision Inversion Bug:**
```python
# TX: 0 → +1.0, 1 → -1.0  (via scale(-2) + offset(+1): 0→1, 1→-1)
# RX HardDecision: >= 0 → 1, < 0 → 0
# So +1.0 (TX bit 0) → HardDecision outputs 1 (WRONG, should be 0)
```

This inversion would cause every bit to be flipped. For BPSK, the invariant check detects and corrects this. For OQPSK, the same correction applies. **This may not be a showstopper** since `parse_frame()` handles phase inversion.

---

## 4. File-by-File Reference

### Core Modules

| File | Role | Key Details |
|---|---|---|
| `core/config_manager.py` | Pydantic models + YAML loader + live-update dispatcher | Auto-derives `burst_duration_ms` and `rx_bandwidth` from frame/mod params. Snaps `chip_rate` to integer SPS. |
| `core/frame_generator.py` | TX frame builder + RX frame parser | Structure: `[PREAMBLE: 32 bits raw] [FEC(INVARIANT: 16 bits + PAYLOAD: 252 bits)]`. Parses with invariant verification + phase inversion correction. |
| `core/fec_codec.py` | Convolutional (Viterbi) + Reed-Solomon | K=7 rate-1/2, NASA polys 121/91. Viterbi fully vectorized with NumPy (releases GIL). `coded_length()` includes K-1 tail bits × rate_inv. |
| `core/spreading_codes.py` | Gold/m-seq/Kasami/custom code generators | Returns uint8 binary (0/1). `get_code()` dispatches based on `code_type`. `to_bipolar()` converts to ±1 float32. |
| `core/hop_scheduler.py` | FHSS sequence generation | Thread-safe. TX calls `next_frequency()` to advance; RX calls `frequency_at(index)` read-only. Supports random/sequential/custom patterns. |
| `core/anomaly_injector.py` | Shared `AnomalyState` dataclass + config sync | BER injection, CFO, IQ imbalance, power fade, burst dropout, interference. Lock-free reads for hot path. |

### Radio Modules

| File | Role | Key Details |
|---|---|---|
| `radio/flowgraph_manager.py` | TX/RX lifecycle, stats, live-update bridge | Owns flowgraph start/stop/restart. `LinkStats` with rolling 5s rate tracking. Hardware monitor thread queries USRP freq every 2s. |
| `radio/tx_flowgraph.py` | TX flowgraph | `FrameChipSource` (sync_block) pulls pre-built chip arrays from queue. `FrameFeeder` thread builds frames, applies FEC, spreads with DSSS. Heartbeat noise source keeps hop counter synced. |
| `radio/rx_flowgraph.py` | RX flowgraph | RRC MF + M&M timing recovery + Costas loop (BPSK/QPSK). OQPSK: separate M&M + I/Q extraction + interleave. MSK/GMSK: `gmsk_demod`. |
| `radio/iq_recorder.py` | IQ recording via file_sink | cf32, i8, SigMF formats. Dynamic valve connection — no restart needed. |

### Custom Blocks (`radio/blocks/`)

| File | Role | Input → Output | Notes |
|---|---|---|---|
| `frame_sink.py` | Preamble detection + chip collection + despreading | float32 → (none, callback) | `sync_block`. Sliding bipolar correlation for preamble. Integrate-and-dump despreading via numpy matrix multiply. Background decode thread. |
| `hop_controller.py` | TX: stream tag insertion. RX: preamble-anchored retune | complex64 → complex64 | TX: sample-driven boundaries, emits `tx_command` tags. RX: message-driven, deferred retune by `burst_duration_s + 1ms`. |
| `burst_gate.py` | Gated sample output | complex64 → complex64 | Vectorized phase mask with numpy. |
| `dsss_spreader.py` | Bit-to-chip interpolation | uint8 → uint8 | `basic_block`, forecast returns `list`. XOR bit with spreading code. |
| `dsss_despreader.py` | Integrate-and-dump despreader | float32 → float32 | Two-phase: acquisition (sliding correlation) then tracking. **NOT USED in current RX path** — FrameSink does despreading internally. |
| `preamble_inserter.py` | Preamble prepending | uint8 → uint8 | **NOT USED in current TX path** — feeder builds chips including preamble. |
| `interleave.py` | OQPSK I/Q processing | See below | Three blocks: Deinterleave (1→2), Interleave (2→1), HardDecision (1→1). |
| `anomaly_block.py` | RF impairment injection | complex64 → complex64 | Reads shared `AnomalyState` each work() call. CFO with phase continuity. |

### Interleave Block Details

```python
class Deinterleave(gr.basic_block):
    """1 float32 input → 2 outputs (I even, Q odd)"""
    # forecast: return [2 * noutput_items]

class Interleave(gr.basic_block):
    """2 float32 inputs → 1 output (I[0], Q[0], I[1], Q[1]...)"""
    # forecast: return [noutput_items//2 + 1, noutput_items//2 + 1]

class HardDecision(gr.basic_block):
    """float32 ±1 → char 0/1. Threshold at 0."""
    # >= 0 → 1, < 0 → 0  [NOTE: inverted vs TX mapping]
```

### GUI Panels

| Panel | Config Section | Key Controls |
|---|---|---|
| `rf_panel.py` | `rf` | HW mode, simulation toggle, device strings, channels, antennas, gains, bandwidth |
| `modulation_panel.py` | `modulation` | Type (bpsk/qpsk/oqpsk/msk/gmsk), chip rate, spreading code config, pulse shaping |
| `frame_panel.py` | `frame` | Total bits, payload hex, preamble/invariant config, FEC type/polys |
| `hop_panel.py` | `hopping` | Enable toggle, hop type, seed, frequencies list |
| `timing_panel.py` | `timing` | Burst duration, preamble duration, transition time, jitter |
| `anomaly_panel.py` | `anomaly` | BER, CFO, dropout, fade, IQ imbalance, interference |
| `logging_panel.py` | `logging` | Log level, CSV toggle, IQ recording controls |
| `status_panel.py` | Runtime stats | Frame counts, rates, SNR history, hop frequency, recent events, restart button |

---

## 5. Signal Chain Detail

### TX Path (BPSK)

```
FrameFeeder thread:
  build_frame(payload) → [preamble | FEC(invariant|payload)]
  spread data section: data_bits XOR spread_code → chips
  concatenate: [preamble_chips | data_chips | guard_padding]
  push to _chip_queue

GR Flowgraph:
  [noise_source_c: heartbeat] ──────────────────────────┐
  [FrameChipSource] → char_to_float → scale(-2) → offset(+1)
    → float_to_complex(I=data, Q=null_source) → interp_fir_filter_ccf (RRC, sps×)
    → AnomalyBlock → interference_adder → BurstGate ──→ [add_cc] → HopController → UHD sink
```

### TX Path (OQPSK) — WITH BUGS

```
[FrameChipSource] → char_to_float → scale(-2) → offset(+1)
  → Deinterleave → I: interp_fir_filter_fff(sps×) → float_to_complex(I)
                  → Q: interp_fir_filter_fff(sps×) → delay(sps//2) → float_to_complex(Q)
    → [BUG: output rate is sample_rate/2, not sample_rate]
    → [BUG: Q delay is T, not T/2]
    → AnomalyBlock → ... → BurstGate → [add_cc] → HopController → UHD sink
```

### RX Path (BPSK)

```
[UHD source] → DC blocker → AGC → HopController (pass-through)
  → fir_filter_ccf (RRC matched filter, decimation=1)
  → clock_recovery_mm_cc (M&M timing recovery, sps→1)
  → costas_loop_cc (2nd order, bw=2π/500)
  → complex_to_real → FrameSink (preamble detect → collect → despread → callback)
```

### RX Path (OQPSK) — IMPLEMENTED, UNVERIFIED

```
[UHD source] → DC blocker → AGC → HopController (pass-through)
  → fir_filter_ccf (RRC matched filter)
  → clock_recovery_mm_cc (single M&M on complex signal)
  → complex_to_real → HardDecision → char_to_float → scale(-2) → offset(+1) → Interleave(I) → FrameSink
  → complex_to_imag → HardDecision → char_to_float → scale(-2) → offset(+1) → Interleave(Q) ──┘
```

---

## 6. Configuration

### Active Config (`config/default_config.yaml`)

```yaml
rf:
  hw_mode: single_b210
  simulation: false
  gnd_device: serial=34D628A
  sample_rate: 2000000.0        # 2 MHz
  tx_gain: 50.0
  rx_gain: 50.0
  tx_channel: 0                 # Port A
  rx_channel: 1                 # Port B
  tx_antenna: TX/RX
  rx_antenna: RX2

hopping:
  enabled: true
  hop_type: custom
  hop_frequencies: [915e6, 914e6, 916e6]

timing:
  burst_duration_ms: 17.268     # auto-derived
  transition_time_ms: 5.0

modulation:
  type: bpsk                    # Currently BPSK
  chip_rate_sps: 1000000.0      # 1 MHz → sps = 2
  spreading:
    code_type: gold
    code_length: 31
    code_seed: 42
    degree: 5
  pulse_shaping:
    rolloff: 0.35
    span_symbols: 11

frame:
  total_bits: 300
  preamble: length_bits: 32
  invariant: enabled: true, length_bits: 16, pattern: 0x1234
  fec:
    enabled: true
    primary_type: cc
    cc: rate_inv: 2, K: 7, polys: [121, 91]
```

### Derived Parameters

With above config:
- **SPS:** 2 (sample_rate / chip_rate)
- **Info bits:** 268 (16 invariant + 252 payload), byte-aligned → 272
- **Coded bits:** (272 + 6) × 2 = 556
- **Data chips:** 556 × 31 = 17,236
- **Total chips:** 32 + 17,236 = 17,268
- **Burst duration:** 17,268 / 1e6 × 1000 = 17.268 ms
- **Period:** 17.268 + 5.0 = 22.268 ms
- **Processing gain:** 10×log10(31) = 14.9 dB

### Hardware Setup

- **Device:** USRP B210 (serial 34D628A)
- **Cabling:** RF0 TX/RX (Port A) → 60 dB attenuator → RF1 RX2 (Port B)
- **Shared TCXO:** TX and RX share internal oscillator → timing inherently synced

---

## 7. Known Bugs and Issues

### Critical

| # | Issue | Impact | Status |
|---|---|---|---|
| 1 | **BPSK Regression** | All frames FEC=ERR, blocks all testing | UNFIXED — root cause unknown |
| 2 | **OQPSK TX rate mismatch** | Output at sample_rate/2 instead of sample_rate | UNFIXED — needs `2*sps_int` interpolation |
| 3 | **OQPSK TX Q delay** | Delay is T instead of T/2 | UNFIXED — needs `sps_int` samples at corrected rate |
| 4 | **HardDecision polarity** | `>= 0 → 1` but TX maps `0 → +1` | POTENTIAL BUG — may be handled by phase inversion correction |

### Minor / Technical Debt

| # | Issue | Impact |
|---|---|---|
| 5 | `dsss_despreader.py` and `preamble_inserter.py` not used in current paths | Dead code |
| 6 | OQPSK M&M timing recovery may not converge on complex OQPSK | Unverified |
| 7 | No automatic burst alignment for RX joining mid-sequence | Sync requires fresh start |
| 8 | `RESUME.md` outdated (says OQPSK RX not implemented, but it is) | Documentation only |

---

## 8. Recommended Fix Order

1. **Fix BPSK regression first** — restore baseline before touching OQPSK
   - Try reverting `rx_flowgraph.py` to pre-OQPSK state and test BPSK
   - If BPSK works after revert, bisect which change broke it
   - Check if `from radio.blocks.interleave import Interleave` import causes side effects
   - Verify GR block types and signal compatibility in BPSK path

2. **Fix OQPSK TX rate** — once BPSK baseline restored
   - Change `interp_fir_filter_fff(sps_int, rrc_taps)` to `interp_fir_filter_fff(2 * sps_int, rrc_taps)`
   - Redesign taps: `firdes.root_raised_cosine(1.0, sample_rate, chip_rate/2, rolloff, n_taps)`
   - Change Q delay to `sps_int` samples at sample_rate

3. **Fix HardDecision polarity** — verify TX/RX bit mapping
   - TX: `0 → +1.0`, `1 → -1.0`
   - RX HardDecision: `>= 0 → 1`, `< 0 → 0`
   - Either flip HardDecision logic or flip the scale/offset

4. **Test OQPSK end-to-end** — with BPSK baseline working and TX rate fixed

5. **Evaluate M&M timing recovery** for OQPSK — if SNR remains low, consider alternative approaches (separate M&M per channel, or non-coherent detection)

---

## 9. Testing Commands

```bash
# Core module smoke test (no hardware needed)
python3 -c "
from core.config_manager import SurrogateConfig
from core.spreading_codes import generate_gold
from core.fec_codec import FECCodec
from core.frame_generator import FrameGenerator
cfg = SurrogateConfig()
fec = FECCodec(cfg.frame.fec)
fg = FrameGenerator(cfg.frame, fec)
frame = fg.build_frame(b'test')
print('OK', len(frame), 'bits')
"

# Simulation mode test (no hardware)
# Edit config: rf.simulation: true, then:
python3 main.py --no-gui

# Full hardware test
python3 main.py --log-level DEBUG
```

---

## 10. Key Design Decisions

- **Heartbeat TX:** Continuous noise source keeps hop counter synced even during CPU lag
- **Tag-based hopping:** `tx_command` stream tags for sub-microsecond retune accuracy
- **Preamble-anchored RX hopping:** RX HopController advances on FrameSink preamble messages
- **FrameSink does despreading internally:** The `dsss_despreader.py` block exists but isn't used
- **Background FEC decode:** FrameSink dispatches Viterbi decode to a separate thread
- **Shared AnomalyState:** Lock-free reads from hot path, RLock for writes
- **Dynamic IQ recording:** Valve connect/disconnect pattern avoids restart
- **Single M&M for OQPSK:** Chosen over separate M&M per channel to ensure I/Q alignment

---

## 11. Complete File Inventory

```
surrogate/
├── main.py                          # Entry point
├── config/
│   └── default_config.yaml          # Active config (BPSK, 3-hop, 2 MHz)
├── core/
│   ├── __init__.py                  # Empty
│   ├── config_manager.py            # Pydantic models, YAML loader, live-update
│   ├── frame_generator.py           # TX frame build, RX frame parse
│   ├── fec_codec.py                 # Convolutional (Viterbi) + RS FEC
│   ├── spreading_codes.py           # Gold, m-seq, Kasami generators
│   ├── hop_scheduler.py             # FHSS sequence generation
│   └── anomaly_injector.py          # Shared anomaly state machine
├── radio/
│   ├── __init__.py                  # (not present)
│   ├── flowgraph_manager.py         # TX/RX lifecycle, stats, live-update
│   ├── tx_flowgraph.py              # TX GNU Radio flowgraph
│   ├── rx_flowgraph.py              # RX GNU Radio flowgraph
│   ├── iq_recorder.py               # IQ recording (cf32, i8, SigMF)
│   └── blocks/
│       ├── __init__.py              # Empty
│       ├── frame_sink.py            # Preamble detect + despreading
│       ├── hop_controller.py        # TX tags / RX preamble-anchored
│       ├── burst_gate.py            # Timed burst gating
│       ├── dsss_spreader.py         # Bit-to-chip interpolation
│       ├── dsss_despreader.py       # (unused) integrate-and-dump despreader
│       ├── preamble_inserter.py     # (unused) preamble prepending
│       ├── interleave.py            # OQPSK: Deinterleave, Interleave, HardDecision
│       └── anomaly_block.py         # RF impairment injection
├── gui/
│   ├── __init__.py                  # Empty
│   ├── main_window.py               # PyQt5 main window with tabs
│   └── panels/
│       ├── __init__.py              # Empty
│       ├── rf_panel.py              # Hardware, device, gain config
│       ├── modulation_panel.py      # Mod type, spreading, pulse shaping
│       ├── frame_panel.py           # Frame structure, FEC config
│       ├── hop_panel.py             # Hopping enable, frequencies
│       ├── timing_panel.py          # Burst/guard duration, jitter
│       ├── anomaly_panel.py         # BER, CFO, fade, IQ imbalance, interference
│       ├── logging_panel.py         # Log level, CSV, IQ recording
│       └── status_panel.py          # Frame counts, rates, SNR, events
├── logging_module/
│   ├── __init__.py                  # Empty
│   ├── surrogate_logger.py          # Console + file logging, CSV dispatch
│   └── csv_writer.py                # CSV frame log writer
├── README.md                        # Project overview, features, hardware setup
├── CLAUDE.md                        # Architecture guide for AI assistants
├── RESUME.md                        # Session status (outdated — see section 7.8)
└── llm-handover.md                  # This file
```
