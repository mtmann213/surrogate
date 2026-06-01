# Surrogate Datalink Workbench

A configurable Python/GNU Radio datalink workbench for building, transmitting,
receiving, and inspecting framed RF signals. The project targets USRP
B205/B210 hardware, with a staged path from pure Python profile tests to
simulation, cabled bench loopback, and eventually over-the-air operation.

The long-term goal is a profile-driven system where a YAML file can define the
exact datalink shape: preamble, syncword, header fields, header CRC, payload
CRC, payload FEC, whitening, padding, modulation, spreading, hopping, RF
frequency, and runtime behavior.

For the detailed roadmap, see [MASTER_PLAN.md](MASTER_PLAN.md). For the running
engineering log, see [WORK_DIARY.md](WORK_DIARY.md).

## Current State

The repo now has three important tracks:

- **Existing RF runtime:** GNU Radio TX/RX flowgraphs, PyQt GUI, baseband FHSS,
  DSSS/FEC pieces, IQ recording, and USRP diagnostics.
- **New profile foundation:** a pure Python datalink profile engine in
  `core/datalink_profile.py`, with tests and a checked-in example profile at
  `config/profiles/bpsk_static_v1.yaml`.
- **Bench RF baseline:** static BPSK has been validated on a cabled B210
  loopback. Runtime QPSK support is implemented and ready for the next cabled
  hardware check. 8PSK and differential PSK helpers exist, but those modes are
  intentionally not enabled in the live GNU Radio runtime yet.

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

The existing GUI/runtime still uses `config/default_config.yaml`; the new
`config/profiles/*.yaml` profile system is not yet wired into the GNU Radio
flowgraphs.

## Current RF Runtime Baseline

Validated static loopback setup:

- USRP B210 serial `34D6458`
- RF0 `TX/RX` cabled to RF1 `RX2`
- 60 dB inline attenuation
- 915 MHz center frequency
- 1 MS/s sample rate
- 250 kchip/s chip rate
- TX/RX gains around 45/45 dB
- BPSK modulation
- hopping disabled
- 60 uncounted TX warmup frames before official frame #0

Runtime modulation state:

- `bpsk`: implemented and hardware-validated on the bench setup above
- `qpsk`: implemented in TX/RX flowgraphs and construction-tested; next step is
  cabled B210 validation
- `8psk`, `dbpsk`, `dqpsk`, `d8psk`: pure helpers/config math exist, but live
  GNU Radio runtime support remains gated until QPSK is proven and diagnostics
  are stronger

Current link diagnostics include TX frames, RX preamble/frame detections, FEC
OK/ERR counts, packet delivery estimate, detection rate, decode rate, SNR,
preamble correlation peak/threshold, phase correction, despread soft metrics,
payload previews, and recent GUI events.

Current diagnostic gaps:

- no truth-based BER counter yet
- no explicit payload-match percentage against the known TX pattern yet
- no FEC correction-count metric yet; `rx_fec_ok` means the decoded payload
  passed the current FEC/parse path, not that we know how many errors were
  corrected

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

For QPSK validation, switch `modulation.type` in `config/default_config.yaml`
from `bpsk` to `qpsk` or use the GUI modulation selector, then run the same
static cabled loopback before trying hopping or 8PSK.

## Hardware Requirements

Recommended bench setup:

- 1x USRP B210
- TX on RF0 `TX/RX`
- RX on RF1 `RX2`
- RF loopback cable with appropriate attenuation; 60 dB worked for the current
  B210 bench
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
- Commit small, named checkpoints before major integration steps.
