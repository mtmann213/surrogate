# Surrogate Datalink System

A configurable bidirectional missile datalink surrogate implemented in Python and GNU Radio, targeting USRP B205/B210 software-defined radios. Emulates FHSS+DSSS burst transmissions with full control over RF parameters, spreading codes, FEC, frame structure, anomaly injection, and IQ recording.

## Features

- **Bidirectional FHSS** — Baseband digital frequency hopping via complex rotation. No RF retunes, no command bus traffic, no PLL settling. TX/RX synchronized by construction.
- **DSSS spreading** — Gold codes, m-sequences, Kasami codes, or custom codes; chip rates up to ~10 Mchip/s.
- **Burst mode** — Timed gate with configurable duration, preamble, and guard intervals.
- **FEC** — Convolutional K=7 rate-1/2 (NASA polynomials) with optional Reed-Solomon outer code.
- **Modulation** — BPSK, QPSK, OQPSK, GMSK/MSK.
- **Simulation Mode** — Integrated ZMQ-based loopback for testing logic and protocols without SDR hardware.
- **Custom Payloads** — Support for hex-encoded custom payloads per burst.
- **IQ recording** — Real-time start/stop for cf32, i8, and SigMF formats.
- **PyQt5 GUI** — Thread-safe live parameter updates and real-time payload monitoring.

## Hardware Requirements

- **Recommended**: 1× USRP B210 (single-radio mode uses both 2T2R ports).
- **Setup**: Port A (RF0) for TX, Port B (RF1) for RX. Cable RF0 TX/RX → RF1 RX2 (with 60 dB attenuation).
- Ubuntu 22.04 or 24.04, USB 3.0.

## Running

```bash
# GUI mode (default)
python main.py

# Headless/CLI mode
python main.py --no-gui
```

### Simulation Mode
To run without hardware:
1. Go to the **RF** tab.
2. Check **Simulation Mode**.
3. Click **Apply**.
The system will use ZMQ to loop back the signal internally.

## Configuration

### Hardware Ports (`rf`)
- `tx_channel`: 0 (Port A), 1 (Port B)
- `rx_channel`: 0 (Port A), 1 (Port B)
- `tx_antenna`: "TX/RX"
- `rx_antenna`: "RX2" or "TX/RX"

### Modulation (`modulation`)
- `type`: `bpsk`, `qpsk`, `oqpsk`, `msk`, `gmsk`.
- **Note**: OQPSK uses coherent RRC matched filter + M&M timing recovery; GMSK/MSK use non-coherent demodulation.

### Hopping (`hopping`)
- **Baseband digital FHSS**: Both TX and RX stay on a single fixed RF center frequency. Hopping is a complex rotation per sample in baseband. No timed UHD commands.
- `sample_rate`: Must be ≥ 2 × |Δf_max|. Default hop set (±700 kHz at 915 MHz) requires **2.0 MHz**.
- `transition_time_ms`: Recommended **10.0 ms** guard interval.

## Project Structure

- `core/`: Pure Python signal processing logic (FEC, spreading, scheduling).
- `radio/`: GNU Radio flowgraphs and hardware interface.
- `radio/blocks/`: Custom C++/Python blocks (BasebandHopper, FrameSink, BurstGate).
- `gui/`: PyQt5 user interface.

## Recent Architectural Changes
- **Baseband Digital FHSS**: Replaced RF-timed retune approach (saturated USB control bus, TX/RX freq mismatch, crashes) with per-sample complex rotation. Both TX and RX stay on a single fixed center frequency; `BasebandHopper` applies `exp(±j * 2π * Δf_k * t)` to implement hopping without any UHD command traffic.
- **Heartbeat TX**: The transmitter runs a continuous low-level noise stream ("heartbeat") to keep the GR scheduler fed even between bursts.
- **Qt Signals**: All frame dispatching uses Qt Signals to ensure thread-safe GUI updates.
- **Invariant Sync**: Every frame is verified against a 16-bit invariant (0x1234) and handles 180° phase inversions automatically.