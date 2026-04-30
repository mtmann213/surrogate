# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install Python dependencies
pip install pydantic pyyaml reedsolo PyQt5 numpy scipy

# Install GNU Radio and UHD (Ubuntu)
sudo apt install gnuradio python3-gnuradio uhd-host

# Run with GUI (default)
python main.py

# Run with a specific config file
python main.py --config config/my_config.yaml

# Run headless (no GUI)
python main.py --no-gui --log-level DEBUG

# Run core module tests (no GNU Radio required)
python -m pytest tests/          # once tests exist
python -c "from core.fec_codec import ConvolutionalCodec; ..."  # quick import check

# Verify core modules independently (no SDR hardware needed)
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
```

## Architecture

### Layer separation

The codebase is split into three layers that can be used/tested independently:

1. **`core/`** — Pure Python/NumPy, no GNU Radio dependency. All signal processing logic (FEC, spreading codes, frame structure, hop scheduling, anomaly state) lives here. This layer is testable on any machine without SDR hardware.

2. **`radio/`** — GNU Radio flowgraphs and custom `gr.sync_block`/`gr.basic_block` wrappers that use the core layer. `TXFlowgraph` and `RXFlowgraph` inherit `gr.top_block`. The `FlowgraphManager` owns their lifetimes and is the single point of contact for start/stop/live updates.

3. **`gui/`** — PyQt5 panels, one per config section. Each panel emits `config_changed(dict)` which routes to `ConfigManager.update()`. Changes that require a flowgraph restart are flagged but not automatic — user restarts via the Status tab or `FlowgraphManager.restart()`.

### Data flow

**TX path** (thread + flowgraph):
- A continuous `analog.noise_source_c` (heartbeat) runs at the full sample rate to drive the flowgraph's "clock" even between bursts.
- `FrameFeeder` builds frames, applies FEC, and spreads with DSSS.
- Gated bursts are mixed with the heartbeat via a `blocks.add_cc` block.
- `BasebandHopper` applies `exp(+j * 2π * Δf_k * t)` per-sample to implement frequency hopping without RF retunes. The UHD sink stays at a single fixed center frequency; the complex rotation shifts the baseband signal to the desired hop frequency on air.
- `HopController` is a pure pass-through, kept only because the IQ-recording tap connects post-hop_ctrl on TX.

**RX path** (flowgraph only):
- UHD source → DC blocker → AGC → `BasebandHopper` (applies `exp(-j * 2π * Δf_k * t)` to cancel the TX rotation) → Demodulator (RRC/Costas for BPSK/QPSK, RRC/M&M for OQPSK, GMSK for MSK) → `FrameSink`.
- `FrameSink` performs preamble correlation, assembles coded bits, and emits a Qt Signal.
- `FrameGenerator.parse_frame()` verifies the 16-bit invariant (0x1234) and automatically corrects for 180° phase inversions.

### Configuration

Config is a Pydantic `SurrogateConfig` model loaded from YAML. The hierarchy mirrors the YAML file: `cfg.rf`, `cfg.hopping`, `cfg.modulation`, `cfg.frame`, `cfg.anomaly`, `cfg.logging`.

`ConfigManager.update(dict)` does a deep merge and notifies registered listeners. The `FlowgraphManager` is a listener and applies live-applicable changes immediately (gain, anomaly params, hop frequencies). Changes that require rebuilding GR filter taps or the modulation chain are flagged with a status-bar warning — call `FlowgraphManager.restart()` to apply them.

### Frequency hopping — current status (2026-04-28)

**Architecture:** baseband digital FHSS via `radio/blocks/baseband_hopper.py:BasebandHopper`.

**How it works:**
- Both TX and RX stay on a single fixed RF center frequency (default 915 MHz).
- Hopping is implemented as a complex rotation per sample:
  - TX: `out = in * exp(+j * 2π * Δf_k * t)` — shifts baseband signal up/down to hop freq
  - RX: `out = in * exp(-j * 2π * Δf_k * t)` — cancels the TX rotation back to baseband
- Hop index = `sample_count // period_samples` where `period_samples = burst + guard`. Both sides share the same hop schedule and timing via `set_start_time(T0)`.
- **No RF retunes** — zero UHD command bus traffic, no PLL settling, no `cmd time errors`.

**Constraint:** Δf_max ≤ sample_rate / 2. With default hop set (±700 kHz) and `sample_rate = 2 Msps`, Nyquist is satisfied with margin. The analog bandwidth is set to `sample_rate / 2 = 1 MHz` to pass the full hop range.

**Pros:** TX/RX synchronized by construction; works on dual_b205 (no shared TCXO needed because hopping is digital); supports arbitrary hop rates up to the burst period.

**What was replaced:** The previous RF-timed approach (`radio/hop_timing.py:HopTimingController`) posted timed retune commands to both UHD sink and source via the gr-uhd `command` message port at each guard interval. This saturated the B210's USB control bus (~25 commands/sec), causing TX/RX to drift onto different frequencies ~60% of the time and crashing after ~10-12 s with `radio_ctrl` packet parse errors.

### Frame structure

```
Transmitted bit stream per burst:
  [PREAMBLE: 32 bits, raw BPSK, not spread]
  [FEC(INVARIANT | PAYLOAD): coded+spread chips]

Default sizes (configurable):
  Total info bits:  300  (preamble=32 + invariant=16 + payload=252)
  After FEC (1/2):  556 coded bits  (272 aligned info × 2 + K-1 tail)
  After spreading:  17268 chips  (32 preamble + 556 × code_length=31)
```

The preamble is **not** FEC-encoded and **not** spread — it is used for burst detection and timing recovery. The invariant section is a fixed 16-bit pattern that serves as a secondary sync marker in `FrameSink`.

### Burst duration and chip rate

The burst duration is determined by `total_chips / chip_rate`. With default 300-bit frames (FEC on, code_length=31):

| `code_length` | `chip_rate` | `sample_rate` | Burst duration | Processing gain |
|---|---|---|---|---|
| 7 | 1 Mchip/s | 2 Msps | 3.9 ms | 8.5 dB |
| 31 | 250 kchip/s | 2 Msps | 69.1 ms | 14.9 dB |
| 31 | 1.15 Mchip/s | 2 Msps | 14.8 ms | 14.9 dB |
| 31 | 5 Mchip/s | 2 Msps | 3.4 ms | 14.9 dB |

The `timing.burst_duration_ms` config field is used by `BurstGate` and `BasebandHopper` for windowing but does not change the chip count — it must be set consistently with the actual chip math or bursts will be clipped/padded.

### Hardware modes

Set via `rf.hw_mode`:
- `single_b210` — one B210, TX on channel 0 (port A), RX on channel 1 (port B). Both share the internal TCXO so timing is inherently synchronized. GND and MSL device strings resolve to the same physical device.
- `dual_b210` — two B210s. For tighter timing, connect REF OUT of the master → REF IN of the slave via SMA cable and set `b210_ref_source: external` on the slave's config entry.
- `dual_b205` — two B205s (1T1R each). Baseband FHSS works on dual_b205 without external reference because hopping is digital — no shared TCXO needed.

Device serials are autodetected when `gnd_device: auto` or `msl_device: auto` using `uhd.find("")`. The RF panel has an "Auto-Detect Devices" button that also fills in the serial fields.

### Live vs restart parameters

**Live (no restart needed):** TX/RX gain, all anomaly parameters, hop frequencies, hop seed, interference power level.

**Requires `FlowgraphManager.restart()`:** sample rate, chip rate, modulation type, spreading code type/length, RRC filter parameters, FEC type/polynomials, interference source type or file path, hopping enable/disable.

### Anomaly injection

`AnomalyInjector` (`core/anomaly_injector.py`) holds a shared `AnomalyState` dataclass. All flowgraph blocks and the TX feeder thread read from this object each processing call. The GUI anomaly panel connects `valueChanged` signals directly to `anomaly_live_update` which calls `FlowgraphManager.set_anomaly_state()` — changes are visible within one burst period.

Interference injection is a permanent branch in the TX flowgraph: a GR source block (file/tone/AWGN/ZMQ) feeds a `multiply_const_cc` gain block whose coefficient is set to 0 when disabled. This means only the interference source type and file path require a restart; power level changes are live.

### IQ recording

`IQRecorder` (`radio/iq_recorder.py`) creates GR `file_sink` blocks. They are attached to `blocks.copy` valve blocks already present in both flowgraphs. Calling `enable_iq_recording(sink)` / `disable_iq_recording()` on the flowgraph toggles the valve — no restart needed.

Formats: `cf32` (raw complex float32, GNU Radio native), `i8` (raw interleaved int8), `sigmf` (cf32 data + `.sigmf-meta` JSON sidecar written on stop).

### Spreading code generation

All generators are in `core/spreading_codes.py` and return `uint8` binary (0/1) arrays:
- `generate_msequence(degree, poly_hex, length, seed)` — LFSR with Fibonacci topology
- `generate_gold(degree, poly1_hex, poly2_hex, code_index)` — XOR of two phase-shifted m-sequences
- `generate_kasami(degree, poly_hex)` — decimated m-sequence XOR (requires even degree)
- `get_code(spreading_config)` — dispatches to the above based on `code_type`

### FEC

`FECCodec` (`core/fec_codec.py`) wraps two codecs:
- `ConvolutionalCodec` — pure NumPy K=7 rate-1/2 encoder with Viterbi hard-decision decoder. Default polynomials: 121 (0o171) and 91 (0o133) — NASA/CCSDS standard.
- `ReedSolomonCodec` — thin wrapper around the `reedsolo` package. Used as outer code when `fec.outer_type: rs`.

FEC is applied at the byte level in the `FrameFeeder` thread before chips enter the GR stream. The decoder runs in the `FrameSink` callback after bit assembly.

## File Reference

### Core Modules
- `core/config_manager.py` — Pydantic config model + YAML loader + live-update dispatcher
- `core/frame_generator.py` — TX frame builder + RX frame parser (preamble/invariant/payload)
- `core/fec_codec.py` — Convolutional (Viterbi) + Reed-Solomon FEC encoder/decoder
- `core/spreading_codes.py` — m-sequence, Gold, Kasami, custom code generators
- `core/hop_scheduler.py` — FHSS sequence generation + frequency lookup
- `core/anomaly_injector.py` — Shared `AnomalyState` dataclass + config sync

### Radio Modules
- `radio/flowgraph_manager.py` — TX/RX flowgraph lifecycle, stats aggregation, live-update bridge
- `radio/tx_flowgraph.py` — GNU Radio TX flowgraph (FrameFeeder → DSSS → modulation → UHD sink)
- `radio/rx_flowgraph.py` — GNU Radio RX flowgraph (UHD source → demodulation → FrameSink)
- `radio/iq_recorder.py` — IQ recording via `file_sink` + SigMF metadata writer

### Custom Blocks (`radio/blocks/`)
- `baseband_hopper.py` — Baseband digital FHSS: per-sample complex rotation (TX: +Δf, RX: -Δf)
- `frame_sink.py` — Preamble detection + chip collection + despreading + FEC callback
- `hop_controller.py` — Pure pass-through (kept as IQ-recording tap point)
- `burst_gate.py` — Gated sample output for timed bursts
- `dsss_spreader.py` — 1:code_length bit-to-chip interpolation
- `dsss_despreader.py` — Integrate-and-dump despreading (chip→soft bit, unused)
- `preamble_inserter.py` — 32-bit preamble prepending to frames (unused)
- `interleave.py` — OQPSK I/Q interleave/deinterleave + hard decision
- `anomaly_block.py` — Real-time RF impairment injection (offset, fade, IQ imbalance)

### GUI Modules (`gui/`)
- `main_window.py` — PyQt5 main window with tabbed interface
- `panels/` — One panel per config section (RF, modulation, hopping, timing, frame, anomaly, logging, status)

## Debugging Tips

- **FEC=ERR with good SNR**: Usually a spreading code mismatch between TX/RX or a phase inversion not corrected. Check that `modulation.spreading.code_seed` is identical on both sides.
- **Underflows/UHD errors**: Reduce `sample_rate` to 2 MHz, lower `chip_rate`, or increase `transition_time_ms` to 5 ms.
- **OQPSK errors**: Verify I/Q alignment — even chips → I, odd chips → Q, Q delayed by T/2.
- **No RX frames with hopping**: Check `sample_rate ≥ 2 * |Δf_max|`. Default ±700 kHz hop set requires ≥ 2 Msps. Also verify `rf.tx_bandwidth`/`rf.rx_bandwidth` ≥ `sample_rate / 2` to pass the full hop range through the analog filter.
- **No RX frames without hopping**: Check that `rf.tx_channel` and `rf.rx_channel` match the physical port mapping.
- **Heartbeat**: The TX heartbeat ensures sample counters stay synced. If frames are dropped, check for CPU spikes or USB bandwidth issues.

## Performance Notes

- Viterbi decoder is vectorized with NumPy to release the GIL during processing.
- FrameSink uses numpy operations (not Python loops) for chip collection and despreading.
- IQ recording uses `blocks.copy` valves — no restart needed to toggle recording.
- Anomaly injection reads from shared `AnomalyState` — changes visible within one burst period.
- Rate tracking uses rolling windows (5 seconds) for frames/sec calculations.
- BasebandHopper short-circuits when only one frequency in the hop sequence (no per-sample rotation overhead).