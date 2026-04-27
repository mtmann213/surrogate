"""
Configuration manager: load, validate, save YAML config and autodetect UHD devices.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PreambleConfig(BaseModel):
    length_bits: int = 32
    pattern: str = "alternating"  # "alternating" | hex string


class InvariantConfig(BaseModel):
    enabled: bool = True
    length_bits: int = 16
    pattern: str = "0x1234"


class CCConfig(BaseModel):
    rate_inv: int = 2
    constraint_length: int = 7
    polynomials: List[int] = [121, 91]


class RSConfig(BaseModel):
    nsym: int = 16


class FECConfig(BaseModel):
    enabled: bool = True
    primary_type: str = "cc"   # cc | none
    outer_type: str = "none"   # rs | none
    cc: CCConfig = Field(default_factory=CCConfig)
    rs: RSConfig = Field(default_factory=RSConfig)


class FrameConfig(BaseModel):
    total_bits: int = 300
    payload_hex: str = "" # Hex string, if empty uses repeating 00-FF
    preamble: PreambleConfig = Field(default_factory=PreambleConfig)
    invariant: InvariantConfig = Field(default_factory=InvariantConfig)
    fec: FECConfig = Field(default_factory=FECConfig)

    @property
    def payload_bits(self) -> int:
        return self.total_bits - self.preamble.length_bits - (
            self.invariant.length_bits if self.invariant.enabled else 0
        )


class SpreadingConfig(BaseModel):
    enabled: bool = True
    code_type: str = "gold"    # gold | msequence | kasami | custom
    code_length: int = 31
    code_seed: int = 42
    degree: int = 5
    poly1: str = "0x25"
    poly2: str = "0x37"
    custom_code: List[int] = []


class PulseShapingConfig(BaseModel):
    filter_type: str = "rrc"
    rolloff: float = 0.35
    span_symbols: int = 11


class ModulationConfig(BaseModel):
    type: str = "bpsk"          # bpsk | qpsk | oqpsk | msk | gmsk
    bit_rate_bps: float = 50000.0
    chip_rate_sps: float = 1.0e6
    spreading: SpreadingConfig = Field(default_factory=SpreadingConfig)
    pulse_shaping: PulseShapingConfig = Field(default_factory=PulseShapingConfig)


class TimingConfig(BaseModel):
    burst_duration_ms: float = 17.02  # auto-computed by SurrogateConfig validator
    preamble_duration_ms: float = 1.0
    transition_time_ms: float = 0.5
    timing_jitter_us: float = 0.0


class HoppingConfig(BaseModel):
    enabled: bool = False
    hop_type: str = "random"   # random | sequential | custom
    hop_seed: int = 0xDEADBEEF
    hop_rate_hz: float = 0.0
    hop_frequencies: List[float] = []
    custom_pattern: List[float] = []


class RFConfig(BaseModel):
    hw_mode: str = "single_b210"  # single_b210 | dual_b210 | dual_b205
    simulation: bool = False
    gnd_device: str = "auto"
    msl_device: str = "auto"
    b210_ref_source: str = "internal"
    center_frequency: float = 915.0e6
    sample_rate: float = 8.0e6
    tx_gain: float = 10.0
    rx_gain: float = 30.0
    tx_channel: int = 0
    rx_channel: int = 1
    tx_antenna: str = "TX/RX"
    rx_antenna: str = "RX2"
    tx_bandwidth: float = 1.35e6  # auto-computed by SurrogateConfig validator
    rx_bandwidth: float = 1.35e6  # auto-computed by SurrogateConfig validator


class BurstDropoutConfig(BaseModel):
    enabled: bool = False
    dropout_probability: float = 0.0


class PowerFadeConfig(BaseModel):
    enabled: bool = False
    fade_depth_db: float = 20.0
    fade_rate_hz: float = 5.0


class IQImbalanceConfig(BaseModel):
    enabled: bool = False
    amplitude_db: float = 0.0
    phase_deg: float = 0.0


class InterferenceConfig(BaseModel):
    enabled: bool = False
    source_type: str = "awgn"  # file | zmq_stream | tone | awgn
    file_path: str = ""
    zmq_address: str = "tcp://127.0.0.1:5555"
    tone_freq_hz: float = 100000.0
    awgn_bandwidth_hz: float = 1.0e6
    relative_power_db: float = -20.0


class AnomalyConfig(BaseModel):
    enabled: bool = False
    ber_injection: float = 0.0
    carrier_freq_offset_hz: float = 0.0
    extra_timing_jitter_us: float = 0.0
    burst_dropout: BurstDropoutConfig = Field(default_factory=BurstDropoutConfig)
    power_fade: PowerFadeConfig = Field(default_factory=PowerFadeConfig)
    iq_imbalance: IQImbalanceConfig = Field(default_factory=IQImbalanceConfig)
    interference: InterferenceConfig = Field(default_factory=InterferenceConfig)


class CSVLoggingConfig(BaseModel):
    enabled: bool = True
    file: str = "logs/surrogate_frames.csv"


class IQRecordingConfig(BaseModel):
    enabled: bool = False
    format: str = "sigmf"    # cf32 | i8 | sigmf
    output_path: str = "recordings/"
    max_size_mb: int = 2000
    record_tx: bool = True
    record_rx: bool = True


class LoggingConfig(BaseModel):
    enabled: bool = True
    log_file: str = "logs/surrogate.log"
    log_level: str = "INFO"
    log_transmission: bool = True
    log_timing: bool = True
    csv: CSVLoggingConfig = Field(default_factory=CSVLoggingConfig)
    iq_recording: IQRecordingConfig = Field(default_factory=IQRecordingConfig)


def _derive_frame_params(data: dict) -> dict:
    """
    Compute burst_duration_ms and RF bandwidth from frame/modulation config.
    Called both from the model validator (on dict) and as a standalone helper.

    burst_duration_ms must exactly match the chip count or BurstGate will clip
    frames (the primary cause of low packet delivery if misconfigured).
    """
    mod = data.get("modulation", {})
    frame_d = data.get("frame", {})
    rf = data.get("rf", {})

    sample_rate = float(rf.get("sample_rate", 8.0e6))
    chip_rate   = float(mod.get("chip_rate_sps", 1.0e6))

    # Snap chip_rate so sps = sample_rate / chip_rate is a positive integer.
    # Non-integer sps makes int(sps) truncate in the RRC interpolation filter,
    # causing the actual output sample rate to differ from sample_rate.
    sps_raw = sample_rate / chip_rate
    sps_int = max(1, round(sps_raw))
    if abs(sps_raw - sps_int) > 0.02:
        chip_rate = sample_rate / sps_int
        data.setdefault("modulation", {})["chip_rate_sps"] = chip_rate

    preamble  = frame_d.get("preamble", {})
    invariant = frame_d.get("invariant", {})
    fec_d     = frame_d.get("fec", {})
    spread    = mod.get("spreading", {})
    ps        = mod.get("pulse_shaping", {})

    pre_bits    = int(preamble.get("length_bits", 32))
    total_bits  = int(frame_d.get("total_bits", 300))
    inv_enabled = bool(invariant.get("enabled", True))
    inv_bits    = int(invariant.get("length_bits", 16))
    payload_bits = total_bits - pre_bits - (inv_bits if inv_enabled else 0)
    info_bits    = payload_bits + (inv_bits if inv_enabled else 0)

    fec_enabled = bool(fec_d.get("enabled", True))
    fec_type    = fec_d.get("primary_type", "cc")
    outer_type  = fec_d.get("outer_type", "none")

    if fec_enabled and fec_type == "cc":
        cc_d = fec_d.get("cc", {})
        k          = int(cc_d.get("constraint_length", 7))
        rate_inv   = int(cc_d.get("rate_inv", 2))
        # frame_generator.build_frame packs data to byte boundary before FEC;
        # align info_bits to bytes to match that padding.
        n = ((info_bits + 7) // 8) * 8
        if outer_type == "rs":
            rs_d  = fec_d.get("rs", {})
            nsym  = int(rs_d.get("nsym", 16))
            n     = ((n + 7) // 8 + nsym) * 8
        coded_bits = (n + k - 1) * rate_inv
    else:
        coded_bits = info_bits

    code_length = int(spread.get("code_length", 31))
    total_chips = pre_bits + coded_bits * code_length
    burst_ms    = total_chips / chip_rate * 1000.0

    # Hardware analog filter bandwidth: use sample_rate/2 so it never clips the
    # signal. The software RRC filter already band-limits to chip_rate*(1+rolloff).
    # Setting hardware BW = chip_rate*(1+rolloff) is too tight: the AD9361 filter
    # has ~10-15% accuracy and introduces ISI at narrow settings.
    bw = sample_rate / 2.0

    data.setdefault("timing", {})["burst_duration_ms"] = burst_ms
    data.setdefault("rf", {})["tx_bandwidth"] = bw
    data["rf"]["rx_bandwidth"] = bw

    return data


def compute_frame_stats(cfg: "SurrogateConfig") -> Dict:
    """
    Return a dict of human-readable derived link parameters for GUI display.
    All values are computed from the current config after auto-derivation.
    """
    mod   = cfg.modulation
    frame = cfg.frame
    rf    = cfg.rf

    chip_rate    = mod.chip_rate_sps
    code_length  = mod.spreading.code_length
    pre_bits     = frame.preamble.length_bits
    info_bits    = frame.payload_bits + (
        frame.invariant.length_bits if frame.invariant.enabled else 0
    )

    if frame.fec.enabled and frame.fec.primary_type == "cc":
        cc = frame.fec.cc
        n = ((info_bits + 7) // 8) * 8   # byte-align, matching build_frame padding
        if frame.fec.outer_type == "rs":
            n = ((n + 7) // 8 + frame.fec.rs.nsym) * 8
        coded_bits = (n + cc.constraint_length - 1) * cc.rate_inv
    else:
        coded_bits = info_bits

    total_chips     = pre_bits + coded_bits * code_length
    burst_ms        = cfg.timing.burst_duration_ms
    period_ms       = burst_ms + cfg.timing.transition_time_ms
    throughput      = frame.payload_bits / (period_ms / 1000.0)
    sps             = rf.sample_rate / chip_rate
    signal_bw       = chip_rate * (1.0 + mod.pulse_shaping.rolloff)
    hw_bw           = rf.sample_rate / 2.0
    proc_gain_db    = 10.0 * __import__("math").log10(code_length)

    return {
        "total_chips":     total_chips,
        "coded_bits":      coded_bits,
        "burst_ms":        burst_ms,
        "period_ms":       period_ms,
        "throughput_bps":  throughput,
        "sps":             sps,
        "signal_bw_hz":    signal_bw,   # RRC filter null-to-null bandwidth
        "hw_bw_hz":        hw_bw,       # hardware analog filter (sample_rate/2)
        "bandwidth_hz":    signal_bw,   # kept for GUI compat
        "proc_gain_db":    proc_gain_db,
    }


class SurrogateConfig(BaseModel):
    rf: RFConfig = Field(default_factory=RFConfig)
    hopping: HoppingConfig = Field(default_factory=HoppingConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    modulation: ModulationConfig = Field(default_factory=ModulationConfig)
    frame: FrameConfig = Field(default_factory=FrameConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="before")
    @classmethod
    def _auto_derive(cls, data: Any) -> Any:
        if isinstance(data, dict):
            _derive_frame_params(data)
        return data


# ---------------------------------------------------------------------------
# Device autodetection
# ---------------------------------------------------------------------------

def _uhd_addr_to_string(d) -> str:
    """
    Convert a uhd.device_addr_t to a UHD args string suitable for usrp_source/sink.
    Prefers 'serial=XXXX' over the raw str() which returns a human-readable
    multi-line format that UHD constructors cannot parse.
    """
    try:
        serial = d.get("serial")
        if serial:
            return f"serial={serial}"
    except Exception:
        pass
    # to_string() returns "key=val,key=val" on most UHD builds
    try:
        s = d.to_string()
        if "=" in s and "\n" not in s:
            return s
    except Exception:
        pass
    # Last resort: parse the pretty-print output ourselves
    raw = str(d)
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("serial:"):
            serial = line.split(":", 1)[1].strip()
            if serial:
                return f"serial={serial}"
    return ""


def _find_uhd_devices() -> List[str]:
    """Return list of UHD device address strings ('serial=XXXX') found on this host."""
    try:
        import uhd
        devs = uhd.find("")
        addresses = [_uhd_addr_to_string(d) for d in devs]
        addresses = [a for a in addresses if a]
        if addresses:
            return addresses
    except Exception:
        pass
    # Fallback: parse uhd_find_devices output
    try:
        result = subprocess.run(
            ["uhd_find_devices"],
            capture_output=True, text=True, timeout=10
        )
        addresses = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.lower().startswith("serial:"):
                serial = line.split(":", 1)[1].strip()
                if serial:
                    addresses.append(f"serial={serial}")
        if addresses:
            return addresses
    except Exception as exc:
        log.warning("UHD device discovery failed: %s", exc)
    return []


def autodetect_devices(hw_mode: str) -> tuple[str, str]:
    """
    Return (gnd_device_addr, msl_device_addr) strings suitable for UHD.
    For single_b210 mode both point to the same device.
    """
    devices = _find_uhd_devices()
    if not devices:
        log.warning("No UHD devices found; using empty device strings")
        return ("", "")

    if hw_mode == "single_b210":
        # Use first device for both TX and RX channels
        return (devices[0], devices[0])
    else:
        # dual mode: assign first two found devices
        if len(devices) >= 2:
            return (devices[0], devices[1])
        else:
            log.warning("dual mode requested but only one device found; using same device for both")
            return (devices[0], devices[0])


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """Load, validate, watch, and live-update the system configuration."""

    def __init__(self, config_path: str = "config/default_config.yaml"):
        self._path = Path(config_path)
        self._lock = threading.RLock()
        self._listeners: List[Callable[[SurrogateConfig], None]] = []
        self.config: SurrogateConfig = self._load()

    def _load(self) -> SurrogateConfig:
        if not self._path.exists():
            log.warning("Config file not found: %s — using defaults", self._path)
            cfg = SurrogateConfig()
        else:
            with open(self._path) as f:
                raw = yaml.safe_load(f) or {}
            cfg = SurrogateConfig.model_validate(raw)

        # Resolve "auto" device addresses
        if cfg.rf.gnd_device == "auto" or cfg.rf.msl_device == "auto":
            gnd, msl = autodetect_devices(cfg.rf.hw_mode)
            if cfg.rf.gnd_device == "auto":
                cfg.rf.gnd_device = gnd
                log.info("Autodetected GND device: %s", gnd)
            if cfg.rf.msl_device == "auto":
                cfg.rf.msl_device = msl
                log.info("Autodetected MSL device: %s", msl)

        return cfg

    def reload(self) -> None:
        with self._lock:
            self.config = self._load()
        self._notify()

    def save(self, path: Optional[str] = None) -> None:
        out = Path(path) if path else self._path
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = self.config.model_dump()
        with open(out, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        log.info("Configuration saved to %s", out)

    def update(self, updates: dict) -> None:
        """Apply a nested dict of updates and notify listeners."""
        with self._lock:
            current = self.config.model_dump()
            _deep_update(current, updates)
            self.config = SurrogateConfig.model_validate(current)
        self._notify()

    def register_listener(self, fn: Callable[[SurrogateConfig], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            try:
                fn(self.config)
            except Exception as exc:
                log.error("Config listener error: %s", exc)


def _deep_update(base: dict, updates: dict) -> None:
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
