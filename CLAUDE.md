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

- `7968f1a Establish datalink profile foundation`
- `d4ebd33 Add profile smoke command`

Current no-hardware verification:

```bash
python3 -m unittest discover -v
python3 -m compileall -q core radio gui logging_module main.py diagnose_link.py test_rx_power.py tests tools
```

Profile smoke check:

```bash
python3 tools/profile_smoke.py config/profiles/bpsk_static_v1.yaml --payload-text hello --id 42
```

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

## Current Dirty Worktree Note

After the profile foundation commits, there are still unstaged pre-existing
edits in config/radio/gui/core files, plus untracked `core/modulation.py` and
`core/demodulation.py`.

Recommendation:

- Do not revert them casually.
- Treat them as pending RF/modulation work.
- Review and split into clean commits before wiring the new profile engine into
  GNU Radio.

The edits appear to include useful work: symbol-rate handling, 8PSK/differential
modulation helpers, complex `FrameSink` phase correction, baseband hopper
changes, and status metrics.

## Style And Safety

- Prefer `rg`/`sed` for inspection.
- Keep changes narrowly scoped.
- Add/adjust tests before RF integration.
- Preserve user or prior-agent changes unless explicitly asked to revert.
- Do not track generated files, logs, recordings, or `__pycache__`.
- If hardware scripts are named like tests, guard execution with
  `if __name__ == "__main__":`.

## Near-Term Next Steps

1. Review dirty RF/modulation edits and decide what to bless.
2. Add tests for `core/modulation.py` and `core/demodulation.py` if kept.
3. Build a profile-to-bitstream adapter for TX that does not touch RF yet.
4. Build a no-hardware BPSK symbol loopback using profile bits.
5. Only then wire the profile engine into `TXFlowgraph`/`RXFlowgraph`.
