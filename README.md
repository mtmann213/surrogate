# Surrogate Datalink Workbench

A configurable Python/GNU Radio datalink workbench for building, transmitting,
receiving, and inspecting framed RF signals. The project targets USRP
B205/B210 hardware, but the current development path deliberately starts with
pure Python profile tests and no-hardware simulation before RF integration.

The long-term goal is a profile-driven system where a YAML file can define the
exact datalink shape: preamble, syncword, header fields, header CRC, payload
CRC, payload FEC, whitening, padding, modulation, spreading, hopping, RF
frequency, and runtime behavior.

For the detailed roadmap, see [MASTER_PLAN.md](MASTER_PLAN.md). For the running
engineering log, see [WORK_DIARY.md](WORK_DIARY.md).

## Current State

The repo now has two important tracks:

- **Existing RF runtime:** GNU Radio TX/RX flowgraphs, PyQt GUI, baseband FHSS,
  DSSS/FEC pieces, IQ recording, and USRP diagnostics.
- **New profile foundation:** a pure Python datalink profile engine in
  `core/datalink_profile.py`, with tests and a checked-in example profile at
  `config/profiles/bpsk_static_v1.yaml`.

The profile engine is intentionally independent of GNU Radio and hardware. It
can build and parse framed payloads from YAML-style profiles and already
supports:

- exact preamble and syncword bits
- uint header fields
- raw payload length semantics
- auto-increment counters
- header CRC
- payload CRC
- padding
- optional convolutional K=7 rate 1/2 FEC
- optional matrix interleaving
- optional LFSR whitening
- inverted-frame handling
- derived frame and encoded-payload lengths

## Quick Checks

Run the current no-hardware checks:

```bash
python3 -m unittest discover -v
python3 -m compileall -q core radio gui logging_module main.py diagnose_link.py test_rx_power.py tests tools
```

Run the profile smoke command:

```bash
python3 tools/profile_smoke.py config/profiles/bpsk_static_v1.yaml --payload-text hello --id 42
```

Expected output is JSON with `ok: true`, header fields, CRC/FEC status, frame
length, and encoded payload length.

## Running The Existing App

```bash
# GUI mode
python3 main.py

# Headless mode
python3 main.py --no-gui

# Custom config
python3 main.py --config config/default_config.yaml
```

The existing GUI/runtime still uses the older `config/default_config.yaml`
configuration path. The new `config/profiles/*.yaml` profile system is not yet
wired into the GNU Radio flowgraphs.

## Hardware Requirements

Recommended bench setup:

- 1x USRP B210
- TX on channel/port A
- RX on channel/port B
- RF loopback cable with appropriate attenuation
- Ubuntu 22.04/24.04, GNU Radio 3.10+, UHD 4.0+

Never connect TX directly to RX without attenuation.

## Project Structure

```text
core/
  datalink_profile.py     Pure YAML-style frame/profile engine
  fec_codec.py            Convolutional and Reed-Solomon FEC
  frame_generator.py      Existing legacy/current frame builder
  spreading_codes.py      Gold, m-sequence, Kasami, custom codes
  hop_scheduler.py        Hop sequence generation

config/
  default_config.yaml     Existing GUI/runtime config
  profiles/               New datalink profile YAML files

radio/
  flowgraph_manager.py    Existing TX/RX lifecycle manager
  tx_flowgraph.py         Existing GNU Radio TX path
  rx_flowgraph.py         Existing GNU Radio RX path
  blocks/                 Custom GNU Radio blocks

gui/                      PyQt5 interface
logging_module/           File/CSV logging
tests/                    No-hardware tests
tools/                    Developer/operator utility commands
```

## Useful References

- [MASTER_PLAN.md](MASTER_PLAN.md): architecture, frame model, milestones
- [WORK_DIARY.md](WORK_DIARY.md): chronological development diary
- [CLAUDE.md](CLAUDE.md): coding-agent operating notes
- [llm-handover.md](llm-handover.md): older handover notes from prior passes

## Development Notes

- Keep protocol/framing logic pure and testable.
- Do not make GNU Radio own the frame grammar.
- Prefer adding no-hardware tests before touching RF behavior.
- Treat existing unstaged RF/modulation edits as pending review work, not trash.
- Commit small, named checkpoints before major integration steps.
