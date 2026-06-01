# CLAUDE.md

Guidance for coding agents working in this repository.

## North Star

This project is becoming a configurable datalink workbench. The target is a
YAML-profile-driven system where a user can define exact frame structure and
waveform behavior, then run it through pure tests, simulation, bench loopback,
and eventually over-the-air operation.

Read these first:

1. `MASTER_PLAN.md` - detailed architecture and milestones
2. `WORK_DIARY.md` - chronological work log and decisions
3. `README.md` - current user-facing overview

## Current Baseline

Recent checkpoint commits:

- `51cc541 Add runtime QPSK path`
- `584698c Record warmup loopback result`
- `e003c28 Add uncounted TX warmup frames`
- `c49b44f Record successful BPSK loopback gains`
- `7968f1a Establish datalink profile foundation`

Current no-hardware verification:

```bash
python3 -m unittest discover -v
python3 -m compileall -q core radio gui logging_module main.py diagnose_link.py test_rx_power.py tests tools
```

Profile smoke check:

```bash
python3 tools/profile_smoke.py config/profiles/bpsk_static_v1.yaml --payload-text hello --id 42
```

Current RF baseline:

- Static BPSK cabled B210 loopback is working.
- Known-good bench setup: B210 serial `34D6458`, RF0 `TX/RX` to RF1 `RX2`,
  60 dB attenuation, 915 MHz, 1 MS/s, 250 kchip/s, TX/RX gain around 45/45 dB,
  hopping disabled, 60 TX warmup frames.
- Runtime QPSK is implemented in the GNU Radio TX/RX path and construction
  tested, but still needs cabled RF validation.
- Runtime 8PSK and DPSK modes are deliberately gated in
  `radio/runtime_modulation.py` until QPSK is proven and better quality metrics
  are present.

Current diagnostics:

- `radio/flowgraph_manager.py` tracks TX frames, RX detected frames,
  `rx_fec_ok`, `rx_fec_err`, TX/RX rates, SNR history, packet-loss estimate,
  detection rate, and FEC OK rate.
- `radio/blocks/frame_sink.py` logs preamble correlation, threshold, phase
  correction, SNR, and despread soft-value diagnostics.
- The GUI status panel displays delivery/PDR, detect, decode, FEC OK/ERR, SNR,
  and rates.
- Missing: truth-based BER, explicit payload-match percentage, sequence-aware
  loss accounting, and FEC correction-count reporting.

## Architecture Direction

Keep three layers distinct:

1. **Protocol/profile layer**
   - Pure Python.
   - No GNU Radio, UHD, or PyQt dependency.
   - Owns frame grammar, header fields, CRC, FEC, whitening, interleaving,
     padding, counters, and length derivation.
   - Current entry point: `core/datalink_profile.py`.

2. **Waveform/RF layer**
   - GNU Radio and NumPy.
   - Owns modulation, demodulation, timing recovery, sync detection,
     spreading, baseband FHSS, impairment injection, and USRP I/O.
   - Existing code lives under `radio/`.

3. **Runtime/UI layer**
   - CLI, GUI, logging, test runner, IQ recording, and orchestration.

Do not push profile grammar into GNU Radio blocks. The radio layer should
consume bits/metadata from the profile layer.

## Commands

```bash
# GUI mode
python3 main.py

# Headless mode
python3 main.py --no-gui

# No-hardware test suite
python3 -m unittest discover -v

# Syntax/import compile sweep
python3 -m compileall -q core radio gui logging_module main.py diagnose_link.py test_rx_power.py tests tools

# Profile smoke command
python3 tools/profile_smoke.py config/profiles/bpsk_static_v1.yaml --payload-text hello --id 42

# Hardware diagnostic, guarded by main()
python3 test_rx_power.py
```

## Important Files

### Planning And Log

- `MASTER_PLAN.md` - detailed master plan
- `WORK_DIARY.md` - chronological diary
- `README.md` - public overview
- `llm-handover.md` - older handover/history

### New Profile Foundation

- `core/datalink_profile.py` - pure profile/frame codec
- `config/profiles/bpsk_static_v1.yaml` - first profile
- `tools/profile_smoke.py` - build/parse smoke command
- `tests/test_datalink_profile.py` - profile tests
- `tests/test_fec_codec.py` - FEC regression tests
- `tests/test_profile_smoke.py` - CLI smoke test

### Existing RF Runtime

- `main.py` - app entry point
- `core/config_manager.py` - existing runtime config model
- `core/frame_generator.py` - existing legacy/current frame builder
- `core/fec_codec.py` - FEC implementation
- `radio/flowgraph_manager.py` - flowgraph lifecycle
- `radio/tx_flowgraph.py` - existing TX flowgraph
- `radio/rx_flowgraph.py` - existing RX flowgraph
- `radio/blocks/baseband_hopper.py` - baseband digital FHSS
- `radio/blocks/frame_sink.py` - existing preamble/despread/FEC sink

## Style And Safety

- Prefer `rg`/`sed` for inspection.
- Keep changes narrowly scoped.
- Add/adjust tests before RF integration.
- Preserve user or prior-agent changes unless explicitly asked to revert.
- Do not track generated files, logs, recordings, or `__pycache__`.
- If hardware scripts are named like tests, guard execution with
  `if __name__ == "__main__":`.

## Near-Term Next Steps

1. Run cabled B210 static QPSK on the same 60 dB loopback and document the
   result in `WORK_DIARY.md`.
2. Add truth-based payload diagnostics: expected payload comparison, payload
   bit-error count, BER, payload match rate, and sequence-aware loss accounting.
3. If QPSK is stable, add 8PSK runtime support using the same packed-chip
   architecture, with frame-alignment validation for groups of 3 chips.
4. Re-enable static baseband FHSS after static PSK modes have measurable link
   quality.
5. Continue wiring the pure profile engine toward the GNU Radio runtime without
   moving frame grammar into GNU Radio blocks.
