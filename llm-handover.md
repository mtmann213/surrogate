# LLM Handover — Surrogate Datalink System

Generated: 2026-04-26
Last update: 2026-04-28 (baseband digital FHSS implemented, RF-timed architecture eliminated)

---

## 0zzzzz. 2026-04-28 (fifth pass) — BASEBAND DIGITAL FHSS IS HERE

The RF-timed FHSS architecture has been fully replaced with baseband digital FHSS. The `HopTimingController` (`radio/hop_timing.py`) is no longer imported, instantiated, or referenced in any runtime code path. It is kept as a reference only.

### New module: `radio/blocks/baseband_hopper.py`

A `gr.sync_block` that applies frequency hopping as a complex rotation per sample:

- TX: `out = in * exp(+j * 2π * Δf_k * t)` — shifts the baseband signal so the UHD sink (at fixed center freq 915 MHz) emits the right frequency on air
- RX: `out = in * exp(-j * 2π * Δf_k * t)` — cancels the TX rotation, bringing the signal back to baseband for demodulation
- Short-circuits (pure pass-through) when `len(hop_sequence) == 1`, avoiding per-sample rotation overhead in static mode
- Phase accumulator maintains continuity across hop boundaries

### Wiring

- **TX** (`radio/tx_flowgraph.py`): `BasebandHopper` inserted between `_hop_ctrl` and `_uhd_sink`. Applied to the continuous stream (heartbeat + bursts) to maintain phase continuity.
- **RX** (`radio/rx_flowgraph.py`): `BasebandHopper` inserted after AGC, before demod chain. Applied to entire captured stream to de-hop before RRC matched filter.
- **FlowgraphManager** (`radio/flowgraph_manager.py`): Creates separate TX/RX `BasebandHopper` instances sharing the same `HopScheduler`. No more `HopTimingController`.
- `_hop_timing` field set to `None` everywhere. All stats/display methods now use `hop_scheduler.current_index()` directly.

### Config changes

- `hopping.enabled: true` (re-enabled after static test)
- `sample_rate: 2.0e6` (was 1.0e6 — needed for Nyquist with ±700 kHz hop range)
- Analog bandwidth auto-derived to `sample_rate / 2 = 1.0 MHz` (covers full hop range)
- `chip_rate: 250 kHz`, `SPS = 8` (up from 4 — better timing recovery)

### Verification

- Core module smoke test: frame build/parse OK, FEC encode/decode OK, burst duration 69.072 ms, 17268 chips/frame
- BasebandHopper TX+RX cancellation test: error < 1e-6
- SPS = 8.0 clean integer (sample_rate / chip_rate = 2e6 / 250e3 = 8)

### Files changed this turn

| File | Change |
|---|---|
| **NEW** `radio/blocks/baseband_hopper.py` | Baseband digital FHSS gr.sync_block (~120 lines) |
| `radio/tx_flowgraph.py` | Import + accept `baseband_hopper` kwarg; insert between _hop_ctrl and _uhd_sink |
| `radio/rx_flowgraph.py` | Import + accept `baseband_hopper` kwarg; insert after AGC |
| `radio/flowgraph_manager.py` | Creates TX/RX BasebandHopper instances; removes HopTimingController setup/teardown |
| `config/default_config.yaml` | `sample_rate: 2.0e6`, `hopping.enabled: true` |
| `CLAUDE.md` | Updated architecture, FHSS section, file reference, debugging tips |
| `RESUME.md` | Session status update |
| `llm-handover.md` | This section prepended |

### Open concerns for hardware test

1. **Sample-counter alignment**: The TX and RX `BasebandHopper` instances maintain independent local `_sample_count` counters that start at 0 when their respective flowgraphs start processing. With `set_start_time(T0)` on both UHD sink and source, both sides should see the same absolute sample timeline. However, pipeline delays (hopper → UHD sink → antenna vs. antenna → UHD source → hopper) create a constant offset. For a 79 ms period and estimated ~1 ms pipeline skew, the offset is ~1.3% of a period — both sides will be on the same hop index. If hardware testing shows hop misalignment, the fix is to add a shared time anchor (read FPGA time in work() and compute hop index from that).
2. **Baseband FHSS is not revertible in the GUI**: The `hopping.enabled` toggle currently requires a `FlowgraphManager.restart()` because the BasebandHopper blocks are created at flowgraph build time. Changing `hopping.enabled` via the GUI triggers the restart.
3. **`hop_rate_hz` is unused**: The config parameter `hop_rate_hz` is not consumed anywhere in the baseband FHSS path. The hop rate is always `1 / (burst + guard)` ≈ 12.7 hops/sec.

---

## 0zzzz. 2026-04-28 (fourth pass) — RF-timed FHSS architecture is structurally limited; proposing baseband digital FHSS

[Previous content preserved below — sections 0zzzz through 11 describe the failed RF-timed approach and are kept for historical reference]

---

## 0zzzz. 2026-04-28 (third pass) — Switched to gr-uhd `command` message port (NOT ENOUGH)

---

## 0zzz. 2026-04-28 (later) — `set_center_freq` was a block-vs-device channel bug (SUPERSEDED)

---

## 0z. 2026-04-28 — FHSS redesigned around a shared FPGA epoch

---

## 0a. 2026-04-27 (Opus, second pass) — found and fixed two compounding RX bugs

---

## 0. Current State (2026-04-27 update — supersedes Section 2 for the BPSK regression status)

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

## 2. Current State: **BPSK REGRESSION BLOCKING ALL TESTING** (HISTORICAL — BPSK regression resolved before FHSS rewrite)

---

## 3. OQPSK Implementation: CODED BUT UNVERIFIED

### TX Path (tx_flowgraph.py)

```
FrameChipSource → char_to_float → scale(-2) → offset(+1) → [±1 float32]
  → Deinterleave → I channel → interp_fir_filter_fff(sps_int, rrc_taps)
                      → Q channel → interp_fir_filter_fff(sps_int, rrc_taps) → delay(sps_int//2)
  → float_to_complex → BurstGate → AnomalyBlock → HopController → BasebandHopper → UHD sink
```

**BUG — Rate Mismatch:**
- Deinterleave outputs I/Q at `chip_rate/2` each
- `interp_fir_filter_fff(sps_int, rrc_taps)` with sps_int=2: input 500kchips/s → output 1Msps
- **Expected:** 2Msps (sample_rate). **Actual:** 1Msps (sample_rate/2)
- **Fix:** Use `2 * sps_int` as interpolation factor. Also redesign taps: `firdes.root_raised_cosine(1.0, sample_rate, chip_rate/2, rolloff, n_taps)` since input symbol rate is `chip_rate/2`.
- Q delay: `sps_int // 2` at sample_rate/2 = `1` sample = full chip period T, not T/2. **Fix:** delay by `sps_int` samples at the corrected output rate.

### RX Path (rx_flowgraph.py) — Implemented by Claude

```
UHD → DC blocker → AGC → BasebandHopper → RRC MF (fir_filter_ccf)
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
| `baseband_hopper.py` | Baseband digital FHSS | complex64 → complex64 | `gr.sync_block`. Per-sample `exp(±j*2π*Δf*t)` rotation. TX: +Δf, RX: -Δf. Phase accumulator for continuity. Short-circuits on single-freq sequences. |
| `frame_sink.py` | Preamble detection + chip collection + despreading | float32 → (none, callback) | `sync_block`. Sliding bipolar correlation for preamble. Integrate-and-dump despreading via numpy matrix multiply. Background decode thread. |
| `hop_controller.py` | Pure pass-through (legacy) | complex64 → complex64 | Kept only as IQ-recording tap point. No frequency-hopping logic remains. |
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
    → AnomalyBlock → interference_adder → BurstGate ──→ [add_cc]
    → HopController → [BasebandHopper: exp(+j*2π*Δf*t)] → UHD sink
```

### TX Path (OQPSK) — WITH BUGS

```
[FrameChipSource] → char_to_float → scale(-2) → offset(+1)
  → Deinterleave → I: interp_fir_filter_fff(sps×) → float_to_complex(I)
                  → Q: interp_fir_filter_fff(sps×) → delay(sps//2) → float_to_complex(Q)
    → [BUG: output rate is sample_rate/2, not sample_rate]
    → [BUG: Q delay is T, not T/2]
    → AnomalyBlock → ... → BurstGate → [add_cc] → HopController → [BasebandHopper] → UHD sink
```

### RX Path (BPSK)

```
[UHD source] → DC blocker → AGC → [BasebandHopper: exp(-j*2π*Δf*t)]
  → fir_filter_ccf (RRC matched filter, decimation=1)
  → clock_recovery_mm_cc (M&M timing recovery, sps→1)
  → costas_loop_cc (2nd order, bw=2π/500)
  → complex_to_real → FrameSink (preamble detect → collect → despread → callback)
```

### RX Path (OQPSK) — IMPLEMENTED, UNVERIFIED

```
[UHD source] → DC blocker → AGC → [BasebandHopper: exp(-j*2π*Δf*t)]
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
  sample_rate: 2000000.0        # 2 MHz (bumped for baseband FHSS Nyquist)
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
  burst_duration_ms: 69.072     # auto-derived
  transition_time_ms: 10.0

modulation:
  type: bpsk                    # Currently BPSK
  chip_rate_sps: 250000.0       # 250 kHz → sps = 8
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
- **SPS:** 8 (sample_rate / chip_rate)
- **Info bits:** 268 (16 invariant + 252 payload), byte-aligned → 272
- **Coded bits:** (272 + 7 - 1) × 2 = 556
- **Data chips:** 556 × 31 = 17,236
- **Total chips:** 32 + 17,236 = 17,268
- **Burst duration:** 17,268 / 250e3 × 1000 = 69.072 ms
- **Period:** 69.072 + 10.0 = 79.072 ms
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
| 1 | **BPSK Regression** (historical) | All frames FEC=ERR, blocks all testing | RESOLVED — OQPSK changes were isolated to separate code paths |
| 2 | **OQPSK TX rate mismatch** | Output at sample_rate/2 instead of sample_rate | UNFIXED — needs `2*sps_int` interpolation |
| 3 | **OQPSK TX Q delay** | Delay is T instead of T/2 | UNFIXED — needs `sps_int` samples at corrected rate |
| 4 | **HardDecision polarity** | `>= 0 → 1` but TX maps `0 → +1` | POTENTIAL BUG — may be handled by phase inversion correction |
| 5 | **Baseband FHSS sample-counter alignment** | TX/RX use independent local sample counters | POTENTIAL BUG — small constant offset due to pipeline delays |

### Minor / Technical Debt

| # | Issue | Impact |
|---|---|---|
| 6 | `dsss_despreader.py` and `preamble_inserter.py` not used in current paths | Dead code |
| 7 | OQPSK M&M timing recovery may not converge on complex OQPSK | Unverified |
| 8 | No automatic burst alignment for RX joining mid-sequence | Sync requires fresh start |
| 9 | `hop_rate_hz` config field not consumed | Unused parameter in baseband FHSS path |
| 10 | `radio/hop_timing.py` (HopTimingController) no longer used | Kept as reference; can delete after baseband FHSS verified |

---

## 8. Recommended Fix Order

1. **Test baseband FHSS on hardware** — verify with hopping enabled at 2 Msps
2. **If baseband FHSS works**, delete/rename `radio/hop_timing.py` to `_legacy_rf_hopping.py`
3. **Fix OQPSK TX rate** — if user needs OQPSK
4. **Fix HardDecision polarity** — verify TX/RX bit mapping
5. **Test OQPSK end-to-end** — with baseband FHSS working and TX rate fixed
6. **Evaluate M&M timing recovery** for OQPSK — if SNR remains low, consider alternative approaches

---

## 9. Testing Commands

```bash
# Core module smoke test (no hardware needed)
python3 -c "
from core.config_manager import ConfigManager
from core.spreading_codes import generate_gold
from core.fec_codec import FECCodec
from core.frame_generator import FrameGenerator
cm = ConfigManager('config/default_config.yaml')
cfg = cm.config
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

- **Baseband digital FHSS**: Both TX and RX stay on a single RF center frequency; hopping is a complex rotation in the GR chain. No RF retunes, no command bus traffic, no PLL settling.
- **Heartbeat TX**: Continuous noise source keeps the GR scheduler fed even between bursts.
- **FrameSink does despreading internally**: The `dsss_despreader.py` block exists but isn't used.
- **Background FEC decode**: FrameSink dispatches Viterbi decode to a separate thread.
- **Shared AnomalyState**: Lock-free reads from hot path, RLock for writes.
- **Dynamic IQ recording**: Valve connect/disconnect pattern avoids restart.
- **Single M&M for OQPSK**: Chosen over separate M&M per channel to ensure I/Q alignment.

---

## 11. Complete File Inventory

```
surrogate/
├── main.py                          # Entry point
├── config/
│   └── default_config.yaml          # Active config (BPSK, 3-hop baseband FHSS, 2 MHz)
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
│   ├── hop_timing.py                # LEGACY — no longer imported; kept as reference
│   ├── iq_recorder.py               # IQ recording (cf32, i8, SigMF)
│   └── blocks/
│       ├── __init__.py              # Empty
│       ├── baseband_hopper.py       # NEW — baseband digital FHSS
│       ├── frame_sink.py            # Preamble detect + despreading
│       ├── hop_controller.py        # Pure pass-through (IQ tap point)
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
├── RESUME.md                        # Session status
└── llm-handover.md                  # This file
```