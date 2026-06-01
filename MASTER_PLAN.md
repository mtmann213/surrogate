# Configurable Datalink Workbench Master Plan

## 1. Objective

Build a clean, fully configurable datalink system that can transmit and receive user-defined framed RF signals through simulation, bench loopback, and eventually over-the-air operation.

The system should let an operator define, from configuration, the exact structure of a frame:

- Preamble bits and length
- Syncword bits and length
- Header fields such as payload length, link ID, frame counter, flags, or custom unsigned fields
- Header CRC
- Payload bytes
- Payload CRC
- Payload FEC
- Payload whitening
- Padding or custom inserted bytes
- Modulation, symbol/chip rates, pulse shaping, spreading, hopping, RF frequency, and gains

The near-term target is a practical, stable, testable BPSK datalink over a single-machine USRP B210 loopback. The long-term target is a profile-driven waveform workbench that supports multiple modulation and coding schemes, spread spectrum, baseband FHSS, superframes, and over-the-air synchronization.

## Current Checkpoint: 2026-06-01

Completed:

- Clean compile/test baseline and generated-artifact cleanup.
- Pure YAML-style profile frame engine with header fields, header CRC, payload
  CRC, padding, optional convolutional FEC, interleaving, whitening, and tests.
- Static BPSK cabled B210 loopback at 915 MHz with RF0 `TX/RX` to RF1 `RX2`
  through 60 dB attenuation.
- Startup conditioning through uncounted TX warmup frames.
- Runtime QPSK TX/RX path using packed chip pairs and the existing chip-oriented
  frame sink.

Current live RF status:

- `bpsk` is hardware-validated on the current bench.
- `qpsk` is implemented and construction-tested; the next RF task is cabled
  hardware validation.
- `8psk` and DPSK helpers exist in pure code, but runtime flowgraph support is
  intentionally gated until QPSK has been validated and link-quality metrics are
  less ambiguous.

Current diagnostics are useful but incomplete. The runtime reports detected
frames, FEC OK/ERR, SNR, preamble correlation, phase correction, soft metrics,
delivery estimate, detection rate, and decode rate. It does not yet report a
truth-based BER, explicit payload-match percentage, or number of FEC-corrected
errors.

## 2. Guiding Principles

### 2.1 YAML Profile Is The Source Of Truth

The primary user interface for defining a waveform should be a YAML datalink profile.

The profile should be expressive enough for real customization without requiring code edits for ordinary use. It should support exact bit patterns for fixed fields and logical declarations for computed fields such as payload length, counters, CRCs, and padding.

Python extension hooks can be added later if YAML becomes limiting, but they should not be required for the first cleaned-up implementation.

### 2.2 Protocol Logic Must Be Pure And Testable

Frame construction, parsing, CRC, FEC, whitening, interleaving, and padding should live in pure Python modules that can be tested without GNU Radio or SDR hardware.

GNU Radio should handle samples, timing, modulation, demodulation, synchronization, and RF I/O. It should not own the frame grammar.

### 2.3 Start Practical, Then Generalize

The first stable profile should be BPSK, static frequency, simulation loopback. After that:

1. BPSK over B210 loopback
2. BPSK with baseband FHSS
3. Payload FEC/whitening/interleaving variants
4. QPSK, 8PSK, GMSK, FSK
5. DSSS/spreading options
6. Superframes and fragmentation
7. Over-the-air multi-node sync

### 2.4 Clean Repository Over Fork Accumulation

The current `surrogate` repo should become the primary cleaned-up repo.

Useful ideas can be selectively imported from sibling projects:

- `surrogate-qwen`: test runner, jammer/interference additions, diagnostics
- `surrogate2` and `surrogate4`: package structure, pytest style, protocol tests
- `opal-vanguard`: link-layer primitives, packetizer/depacketizer ideas, whitening, interleaving, NRZI, CCSK/Barker, no-hardware regression patterns

Do not merge any sibling project wholesale. Cherry-pick design and code only when it fits the cleaned architecture.

## 3. Current Project State

The current repo already contains important hardware-oriented work:

- GNU Radio TX/RX flowgraphs
- PyQt GUI
- YAML/Pydantic config models
- Frame generation and parsing
- Convolutional FEC
- Gold/m-sequence/Kasami spreading code support
- Baseband digital FHSS through `radio/blocks/baseband_hopper.py`
- IQ recording and SigMF metadata support
- Hardware diagnostics

The current repo also has cleanup issues:

- The worktree contains generated/runtime artifacts such as `__pycache__`, logs, and recordings.
- `tests/` exists but has little or no active test coverage.
- `radio/blocks/dsss_despreader.py` currently has a syntax indentation error at line 54.
- Some files are legacy or historical, especially older RF timed hopping components.
- The default config may not match the latest baseband FHSS handover state.

The cleanup phase must establish a known-good baseline before adding major capability.

## 4. System Architecture

The cleaned system should be organized around three layers.

### 4.1 Protocol Layer

Responsible for bytes and bits.

Primary modules:

- `ProfileLoader`: load and validate YAML profiles
- `FrameSchema`: normalized in-memory frame layout
- `FrameBuilder`: payload plus state to exact transmitted frame bits
- `FrameParser`: received bits to parsed fields, payload, and validation status
- `TransformPipeline`: CRC, FEC, interleaving, whitening, padding
- `FrameState`: counters, IDs, message IDs, fragment state

This layer must not depend on GNU Radio, UHD, PyQt, or live hardware.

### 4.2 Waveform Layer

Responsible for bits-to-symbols/samples and samples-to-bits.

Primary responsibilities:

- Modulation and demodulation
- Pulse shaping and matched filtering
- Clock recovery
- Preamble-assisted timing recovery
- Syncword alignment
- DSSS spreading/despreading
- Baseband FHSS rotation/de-rotation
- Sample-rate and bandwidth derivation
- Impairments/anomalies

This layer can use GNU Radio and NumPy.

### 4.3 Runtime Layer

Responsible for operation and control.

Primary responsibilities:

- CLI/headless runner
- GUI
- Simulation loopback
- Hardware TX/RX lifecycle
- Config/profile switching
- Live-safe parameter updates
- IQ recording
- Logging and telemetry
- Test runner

## 5. Frame Model V1

V1 frame structure:

```text
preamble | syncword | header | header_crc | encoded_payload_region
```

The `encoded_payload_region` is:

```text
whiten(interleave(fec(payload | payload_crc | padding)))
```

If a transform is disabled, it is skipped.

RX reverses this process:

```text
bits -> dewhiten -> deinterleave -> FEC decode -> payload CRC check
```

### 5.1 Preamble

Purpose:

- Aid clock/timing recovery
- Provide a stable acquisition pattern before syncword detection

Configuration requirements:

- Exact bits
- Explicit length
- Optional repetition helper later, such as `pattern: "10"` and `repeat: 64`

Example:

```yaml
preamble:
  type: constant_bits
  bits: "10101010101010101010101010101010"
```

### 5.2 Syncword

Purpose:

- Exact frame-boundary alignment
- Sync validation after preamble/timing recovery

Configuration requirements:

- Exact bits
- Explicit length derived from bit string
- RX tolerance, such as max bit errors
- Search normal and inverted polarity where appropriate

Example:

```yaml
syncword:
  type: constant_bits
  bits: "00111101010011000101101101101010"
  rx_sync: true
  max_bit_errors: 2
  allow_inverted: true
```

### 5.3 Header

Purpose:

- Carry raw payload length and optional metadata
- Support counters, link IDs, flags, and later fragmentation fields

V1 standard fields:

- `length`: raw payload length in bytes
- `id`: link/profile/node/message ID, depending on profile
- `counter`: auto-incrementing frame counter
- optional `flags`
- optional custom fixed-width unsigned fields

Header length field semantics:

- Length means raw payload bytes before CRC, FEC, whitening, interleaving, or padding.

Example:

```yaml
header:
  fields:
    - name: length
      type: uint
      bits: 16
      value_from: payload.length_bytes
    - name: id
      type: uint
      bits: 8
      default: 1
    - name: counter
      type: uint
      bits: 16
      auto_increment: true
    - name: flags
      type: uint
      bits: 8
      default: 0
```

### 5.4 Header CRC

Purpose:

- Detect corruption in header fields before using values such as payload length.

Coverage:

- Header CRC covers serialized header fields only.
- It does not cover preamble, syncword, payload, or payload CRC.

Initial algorithms:

- `none`
- `crc8`
- `crc16_ccitt`
- `crc32`

Example:

```yaml
header_crc:
  enabled: true
  algorithm: crc16_ccitt
  over: [header]
```

### 5.5 Payload

Purpose:

- Carry arbitrary user bytes.

V1 assumptions:

- Payload input is bytes.
- Header length is raw payload byte length.
- Payload may be padded for FEC or alignment, but padding is not part of the logical payload length.

Example:

```yaml
payload:
  max_bytes: 128
```

### 5.6 Payload CRC

Purpose:

- Detect corruption after RX decoding.

Coverage:

- Payload CRC covers raw payload bytes only.
- It does not cover encoded bytes, padding, whitening, or FEC parity.

Placement:

```text
payload | payload_crc | padding
```

Example:

```yaml
payload_crc:
  enabled: true
  algorithm: crc32
  over: [payload]
```

### 5.7 FEC

The standard practical default for V1:

- Header: header CRC only
- Payload region: FEC over `payload | payload_crc | padding`

This keeps header parsing simple while protecting the user data. Later, profiles can allow independent FEC scopes:

- payload only
- header plus payload
- header only
- separate header FEC and payload FEC

Initial FEC modes:

- `none`
- `convolutional_k7_r12`

Future FEC modes:

- Reed-Solomon
- concatenated RS plus convolutional
- LDPC or BCH if needed

Example:

```yaml
fec:
  scope: payload_region
  type: convolutional_k7_r12
  constraint_length: 7
  polynomials: [121, 91]
```

### 5.8 Padding

Purpose:

- Align payload region to byte boundaries, FEC block sizes, interleaver dimensions, or fixed transmitted frame size.

V1 modes:

- zero byte padding
- custom byte padding
- align to N bits
- align to N bytes
- fixed payload-region size

Example:

```yaml
padding:
  mode: custom_byte
  byte: 0x00
  align_bits: 8
```

### 5.9 Whitening

Purpose:

- Improve spectral properties and avoid long runs of identical bits.

Default placement:

- Whitening is applied after FEC and interleaving in V1.

Initial modes:

- `none`
- LFSR whitening

Example:

```yaml
whitening:
  enabled: true
  type: lfsr
  seed: 0x7f
  mask: 0x48
```

### 5.10 Interleaving

Purpose:

- Spread burst errors before FEC decode.

Default placement:

- Interleaving is applied after FEC and before whitening.

Initial modes:

- `none`
- matrix interleaver

Example:

```yaml
interleaving:
  enabled: false
  type: matrix
  rows: 8
```

## 6. Example V1 YAML Profile

```yaml
profile:
  name: bpsk_static_v1
  version: 1

frame:
  bit_order: msb_first

  preamble:
    type: constant_bits
    bits: "10101010101010101010101010101010"

  syncword:
    type: constant_bits
    bits: "00111101010011000101101101101010"
    rx_sync: true
    max_bit_errors: 2
    allow_inverted: true

  header:
    fields:
      - name: length
        type: uint
        bits: 16
        value_from: payload.length_bytes
      - name: id
        type: uint
        bits: 8
        default: 1
      - name: counter
        type: uint
        bits: 16
        auto_increment: true
      - name: flags
        type: uint
        bits: 8
        default: 0

  header_crc:
    enabled: true
    algorithm: crc16_ccitt
    over: [header]

  payload:
    max_bytes: 128

  payload_crc:
    enabled: true
    algorithm: crc32
    over: [payload]

  padding:
    mode: custom_byte
    byte: 0x00
    align_bits: 8

coding:
  fec:
    scope: payload_region
    type: convolutional_k7_r12
    constraint_length: 7
    polynomials: [121, 91]

  interleaving:
    enabled: false
    type: matrix
    rows: 8

  whitening:
    enabled: true
    type: lfsr
    seed: 0x7f
    mask: 0x48

waveform:
  modulation:
    type: bpsk
    bit_rate_bps: 12500
    chip_rate_sps: 250000
    pulse_shaping:
      type: rrc
      rolloff: 0.35
      span_symbols: 11

  spreading:
    enabled: false
    type: none

  hopping:
    enabled: false
    type: custom
    frequencies_hz: [915000000.0]
    transition_time_ms: 10.0

rf:
  simulation: true
  center_frequency_hz: 915000000.0
  sample_rate_sps: 1000000.0
  tx_gain_db: 50.0
  rx_gain_db: 50.0
  tx_channel: 0
  rx_channel: 1
  tx_antenna: TX/RX
  rx_antenna: RX2

rx:
  preamble_clock_recovery: true
  sync_on: syncword
  allow_phase_inversion: true
```

## 7. RX Pipeline V1

RX should operate in this order:

1. Receive complex samples.
2. Apply AGC/DC removal as needed.
3. If hopping is enabled, apply baseband de-hop.
4. Apply matched filter and timing recovery.
5. Use preamble for clock/timing stabilization.
6. Search syncword for exact frame boundary.
7. Parse header bits.
8. Validate header CRC.
9. Use header `length` to determine raw payload length.
10. Collect encoded payload region.
11. Reverse whitening.
12. Reverse interleaving.
13. Decode FEC.
14. Extract raw payload and payload CRC.
15. Validate payload CRC.
16. Emit payload and metadata.

Metadata should include:

- Frame counter
- Header ID
- Raw payload length
- Header CRC status
- Payload CRC status
- FEC decode status
- Number of corrected errors, when available
- Sync confidence
- SNR or soft metric
- Hop index and frequency
- Timestamp

## 8. TX Pipeline V1

TX should operate in this order:

1. Accept payload bytes.
2. Build header fields.
3. Serialize header.
4. Compute header CRC over header fields.
5. Compute payload CRC over raw payload bytes.
6. Build payload region: `payload | payload_crc | padding`.
7. Apply payload FEC.
8. Apply interleaving if enabled.
9. Apply whitening if enabled.
10. Build full frame bits: `preamble | syncword | header | header_crc | encoded_payload_region`.
11. Map bits to chips/symbols.
12. Apply modulation and pulse shaping.
13. Apply baseband hop rotation if enabled.
14. Send to simulation sink or UHD sink.

## 9. Superframe Plan

Superframes are not required for V1.

Future purpose:

- Carry payloads larger than the max payload per packet
- Provide scheduled slots
- Provide repeated headers or beacons
- Support fragmentation and reassembly

Likely future header fields:

- `message_id`
- `fragment_index`
- `fragment_count`
- `fragment_offset`
- `more_fragments`
- `superframe_counter`
- `slot_index`

The V1 frame engine should avoid assumptions that prevent these fields later. Header fields should already support custom fixed-width unsigned fields and flags.

## 10. Initial Modulation And Waveform Scope

V1:

- BPSK
- Static frequency
- Simulation loopback

V1.1:

- BPSK over B210 bench loopback
- Baseband FHSS enabled

V2 practical modulation set:

- QPSK, now implemented in the runtime and pending cabled RF validation
- 8PSK, next after QPSK plus payload/BER diagnostics
- GMSK
- FSK

Later:

- OQPSK
- CCSK
- Barker DSSS
- OFDM
- Custom constellations

## 11. Hopping Plan

The current preferred FHSS architecture is baseband digital hopping:

- UHD TX and RX remain tuned to one RF center frequency.
- TX applies `+delta_f` complex rotation per sample.
- RX applies `-delta_f` complex rotation per sample.
- No timed UHD retunes are required for ordinary hopping.

V1 hopping modes:

- off/static
- custom frequency list
- sequential
- seeded pseudo-random

Future modes:

- AES-derived sequence
- time-of-day synchronized hopping
- adaptive blacklist/avoidance

For over-the-air use, hop synchronization must eventually be based on a shared epoch or explicit synchronization mechanism, not only local sample counters.

## 12. Simulation, Bench, And OTA Progression

### 12.1 Simulation

Simulation is the first required validation path.

It should run without USRP hardware and verify:

- Frame build/parse
- Header CRC
- Payload CRC
- FEC
- Whitening
- Modulation/demodulation
- Sync detection
- Hopping math, where possible

### 12.2 Bench Loopback

Bench loopback is the first hardware target.

Initial hardware:

- Single USRP B210
- TX on one RF port
- RX on the other RF port
- Proper attenuation between TX and RX

Validation sequence:

1. Static tone RX power sanity test
2. BPSK static datalink
3. BPSK static with payload CRC/FEC/whitening
4. BPSK baseband FHSS
5. Interference/anomaly tests

### 12.3 Over-The-Air

OTA is a later target.

Additional concerns:

- Clock synchronization
- Frequency offset
- Timing drift
- Doppler
- RX acquisition after packet loss
- Hop epoch recovery
- Link IDs and anti-self filtering
- Fragment retransmission or ARQ, if needed

## 13. Test Strategy

Testing must be built before broad feature expansion.

### 13.1 Pure Unit Tests

No GNU Radio or hardware.

Required tests:

- Bit packing and unpacking
- Constant bit field parsing
- Header serialization
- Header CRC pass/fail
- Payload CRC pass/fail
- Payload padding
- FEC encode/decode
- Whitening/dewhitening
- Interleaving/deinterleaving
- Full frame build/parse roundtrip
- Counter increment behavior
- Max payload enforcement

### 13.2 Signal-Path Simulation Tests

GNU Radio allowed, no hardware.

Required tests:

- BPSK frame loopback
- Syncword detection tolerance
- Inverted polarity handling
- AWGN degradation sweep
- CFO stress test
- BasebandHopper TX/RX cancellation

### 13.3 Hardware Tests

Hardware required.

Required tests:

- USRP discovery
- RX power/tone check
- Static BPSK frame delivery
- Static BPSK with coding
- Static QPSK frame delivery
- Static 8PSK frame delivery after QPSK is stable
- Baseband FHSS BPSK delivery
- IQ recording smoke test

### 13.4 Regression Runner

The cleaned repo should include a test runner inspired by `surrogate-qwen` and `opal-vanguard`.

It should accept YAML test suites and output:

- terminal summary
- CSV results
- optional JSON results
- pass/fail exit code

Metrics:

- TX frames
- RX sync detections
- Header CRC pass/fail
- Payload CRC pass/fail
- FEC pass/fail
- Payload match pass/fail against expected loopback payloads
- Payload bit errors and BER when a known payload pattern is active
- FEC corrected-error count when the decoder can expose it
- Delivery percentage
- Decode percentage
- SNR or soft confidence
- Mean absolute soft value
- Hop index/frequency

## 14. Repository Cleanup Plan

### 14.1 Establish Baseline

Actions:

- Fix `radio/blocks/dsss_despreader.py` syntax error or move it to legacy.
- Remove generated `__pycache__` files from git tracking.
- Ignore logs and recordings.
- Separate active code from legacy reference code.
- Ensure `python3 -m compileall` passes.

### 14.2 Package Structure

Proposed structure:

```text
surrogate/
  config/
    profiles/
    schema.py
    loader.py
  protocol/
    fields.py
    frame_builder.py
    frame_parser.py
    crc.py
    transforms.py
    state.py
  coding/
    fec.py
    whitening.py
    interleaving.py
  waveform/
    modulation.py
    demodulation.py
    spreading.py
    hopping.py
  radio/
    tx_flowgraph.py
    rx_flowgraph.py
    flowgraph_manager.py
    blocks/
  runtime/
    cli.py
    test_runner.py
  gui/
  logging/
tests/
```

The exact structure can evolve, but protocol logic and radio runtime should be clearly separated.

### 14.3 Migration From Current Code

Keep:

- BasebandHopper
- FlowgraphManager concepts
- TX/RX flowgraph structure
- FEC implementation after tests
- Spreading code generation after tests
- IQ recorder
- GUI concepts

Refactor:

- Config manager into profile loader plus runtime config
- FrameGenerator into generic FrameBuilder/FrameParser
- FrameSink to sync on configured syncword and report richer metadata
- FlowgraphManager stats and reset support

Quarantine:

- Legacy RF timed hopping
- Unused DSSS blocks until tested
- Historical diagnostics that are not part of regular workflow

## 15. Open Design Decisions

Resolved:

- YAML-first configuration
- Payload length means raw payload bytes
- Header CRC covers only header fields
- Payload CRC covers raw payload bytes
- Clock recovery uses preamble
- Frame synchronization uses syncword
- Superframes deferred
- Start with practical modulation set
- Cleaned repo is the desired endpoint

Still open:

- Whether header should eventually get FEC in addition to CRC
- Exact default max payload size
- Exact first CRC algorithms and naming
- Whether whitening should be configurable before or after interleaving in later profiles
- Whether profile validation should allow arbitrary custom field references in V1
- How much GUI support is required for arbitrary frame profiles versus loading/editing YAML
- How OTA hop epoch recovery should work

## 16. Recommended Milestones

### Milestone 0: Clean Compile Baseline

Exit criteria:

- Repo compiles with `python3 -m compileall`
- Generated files ignored
- Legacy files identified
- Current default config is internally consistent

### Milestone 1: Pure Profile Frame Engine

Exit criteria:

- YAML profile loads and validates
- FrameBuilder emits exact bits
- FrameParser recovers payload and metadata
- Unit tests cover CRC, padding, whitening, FEC, and counters

### Milestone 2: BPSK Simulation Loopback

Exit criteria:

- No-hardware loopback can send payload bytes and recover them
- Header CRC and payload CRC statuses are reported
- Test runner returns pass/fail

### Milestone 3: B210 Static Loopback

Exit criteria:

- Single B210 bench loopback works at static frequency
- IQ recording works
- Metrics are visible in logs/status

Status:

- Static BPSK loopback is working on the B210 bench setup.
- Metrics are visible in logs/status.
- IQ recording and richer BER/payload-match metrics still need explicit
  validation.

### Milestone 4: BPSK Baseband FHSS

Exit criteria:

- Custom hop list works in bench loopback
- Hop index/frequency are reported
- Delivery remains stable across hops

### Milestone 5: Coding And Spreading Variants

Exit criteria:

- Whitening and interleaving are tested in simulation and hardware
- DSSS spreading mode works with configured code
- Payload FEC behavior is measured under AWGN/BER injection

### Milestone 6: Additional Modulations

Exit criteria:

- QPSK, 8PSK, GMSK, and FSK profiles can be tested
- Each modulation has simulation tests before hardware claims

Status:

- QPSK runtime flowgraph support is implemented and awaiting cabled RF
  validation.
- 8PSK should follow QPSK validation. The current frame chip count is compatible
  with 3-chip symbol packing, but 8PSK needs more SNR/phase margin and better
  BER/payload-match reporting before performance claims are meaningful.

### Milestone 7: Superframes And Fragmentation

Exit criteria:

- Payloads larger than max frame payload are fragmented
- RX reassembles fragments
- Missing fragment behavior is reported

### Milestone 8: OTA Readiness

Exit criteria:

- Dual-node profiles
- Hop epoch synchronization plan
- CFO/timing drift robustness testing
- OTA acquisition/reacquisition behavior documented

## 17. Immediate Next Step

The next practical engineering step is a focused RF/diagnostics phase:

1. Run static cabled QPSK on the same B210 loopback that validated BPSK.
2. Add truth-based payload diagnostics: expected payload comparison, bit-error
   count, BER, payload match percentage, and sequence-aware loss accounting.
3. If QPSK is stable, add runtime 8PSK using the same packed-chip architecture,
   plus frame-alignment validation for groups of 3 chips.
4. Revisit baseband FHSS only after static PSK modes have measurable link
   quality.
