"""
Pure datalink profile frame builder/parser.

This module intentionally has no GNU Radio, UHD, or GUI dependencies. It is the
first pass at the YAML-driven frame grammar described in MASTER_PLAN.md.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from core.fec_codec import FECCodec


def _bits_from_string(bits: str) -> np.ndarray:
    cleaned = "".join(bits.split())
    if any(ch not in "01" for ch in cleaned):
        raise ValueError("bit strings may only contain 0 and 1")
    return np.fromiter((1 if ch == "1" else 0 for ch in cleaned), dtype=np.uint8)


def _int_to_bits(value: int, width: int) -> np.ndarray:
    if width <= 0:
        raise ValueError("field width must be positive")
    if value < 0 or value >= (1 << width):
        raise ValueError(f"value {value} does not fit in {width} bits")
    return np.fromiter(
        ((value >> shift) & 1 for shift in range(width - 1, -1, -1)),
        dtype=np.uint8,
    )


def _bits_to_int(bits: np.ndarray) -> int:
    value = 0
    for bit in bits.astype(np.uint8):
        value = (value << 1) | int(bit)
    return value


def _bytes_to_bits(data: bytes) -> np.ndarray:
    if not data:
        return np.empty(0, dtype=np.uint8)
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(np.uint8)


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) == 0:
        return b""
    pad = (-len(bits)) % 8
    if pad:
        bits = np.pad(bits, (0, pad))
    return np.packbits(bits).tobytes()


def _crc_width(algorithm: str) -> int:
    alg = algorithm.lower()
    if alg == "none":
        return 0
    if alg == "crc8":
        return 8
    if alg == "crc16_ccitt":
        return 16
    if alg == "crc32":
        return 32
    raise ValueError(f"unsupported CRC algorithm: {algorithm}")


def _crc_bytes(data: bytes, algorithm: str) -> bytes:
    alg = algorithm.lower()
    width = _crc_width(alg)
    if width == 0:
        return b""
    if alg == "crc8":
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        return crc.to_bytes(1, "big")
    if alg == "crc16_ccitt":
        return binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big")
    if alg == "crc32":
        return (binascii.crc32(data) & 0xFFFFFFFF).to_bytes(4, "big")
    raise ValueError(f"unsupported CRC algorithm: {algorithm}")


def _lfsr_mask(length: int, seed: int, mask: int) -> np.ndarray:
    state = seed & 0xFF
    if state == 0:
        raise ValueError("whitening seed must be non-zero")
    out = np.empty(length, dtype=np.uint8)
    for idx in range(length):
        out[idx] = state & 1
        feedback = 0
        for bit_pos in range(8):
            if (mask >> bit_pos) & 1:
                feedback ^= (state >> bit_pos) & 1
        state = ((state << 1) & 0xFF) | (feedback & 1)
    return out


def _matrix_interleave(bits: np.ndarray, rows: int) -> np.ndarray:
    if rows <= 1:
        return bits.copy()
    if len(bits) % rows != 0:
        raise ValueError("matrix interleaving requires bit length divisible by rows")
    cols = len(bits) // rows
    return bits.reshape(rows, cols).T.reshape(-1).astype(np.uint8)


def _matrix_deinterleave(bits: np.ndarray, rows: int) -> np.ndarray:
    if rows <= 1:
        return bits.copy()
    if len(bits) % rows != 0:
        raise ValueError("matrix deinterleaving requires bit length divisible by rows")
    cols = len(bits) // rows
    return bits.reshape(cols, rows).T.reshape(-1).astype(np.uint8)


@dataclass
class HeaderField:
    name: str
    bits: int
    default: int = 0
    value_from: Optional[str] = None
    auto_increment: bool = False


@dataclass
class FrameProfile:
    name: str
    preamble_bits: np.ndarray
    syncword_bits: np.ndarray
    sync_max_bit_errors: int
    sync_allow_inverted: bool
    header_fields: List[HeaderField]
    header_crc_algorithm: str
    payload_max_bytes: int
    payload_crc_algorithm: str
    padding_mode: str
    padding_byte: int
    padding_align_bits: int
    fec_cfg: Dict[str, Any] = field(default_factory=dict)
    interleaving_cfg: Dict[str, Any] = field(default_factory=dict)
    whitening_cfg: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FrameProfile":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FrameProfile":
        frame = data.get("frame", {})
        coding = data.get("coding", {})
        profile = data.get("profile", {})

        fields = []
        for item in frame.get("header", {}).get("fields", []):
            if item.get("type", "uint") != "uint":
                raise ValueError("V1 only supports uint header fields")
            fields.append(HeaderField(
                name=item["name"],
                bits=int(item["bits"]),
                default=int(item.get("default", 0)),
                value_from=item.get("value_from"),
                auto_increment=bool(item.get("auto_increment", False)),
            ))
        if not fields:
            raise ValueError("frame.header.fields must define at least one field")

        header_crc = frame.get("header_crc", {})
        payload_crc = frame.get("payload_crc", {})
        padding = frame.get("padding", {})
        return cls(
            name=str(profile.get("name", "unnamed")),
            preamble_bits=_bits_from_string(frame["preamble"]["bits"]),
            syncword_bits=_bits_from_string(frame["syncword"]["bits"]),
            sync_max_bit_errors=int(frame["syncword"].get("max_bit_errors", 0)),
            sync_allow_inverted=bool(frame["syncword"].get("allow_inverted", True)),
            header_fields=fields,
            header_crc_algorithm=header_crc.get("algorithm", "none") if header_crc.get("enabled", False) else "none",
            payload_max_bytes=int(frame.get("payload", {}).get("max_bytes", 0)),
            payload_crc_algorithm=payload_crc.get("algorithm", "none") if payload_crc.get("enabled", False) else "none",
            padding_mode=padding.get("mode", "custom_byte"),
            padding_byte=int(padding.get("byte", 0)),
            padding_align_bits=int(padding.get("align_bits", 8)),
            fec_cfg=coding.get("fec", {"type": "none"}),
            interleaving_cfg=coding.get("interleaving", {"enabled": False}),
            whitening_cfg=coding.get("whitening", {"enabled": False}),
        )

    @property
    def header_bits(self) -> int:
        return sum(item.bits for item in self.header_fields)

    @property
    def header_crc_bits(self) -> int:
        return _crc_width(self.header_crc_algorithm)

    @property
    def payload_crc_bytes(self) -> int:
        return _crc_width(self.payload_crc_algorithm) // 8


@dataclass
class BuiltFrame:
    bits: np.ndarray
    header: Dict[str, int]
    payload: bytes
    counter: int


@dataclass
class ParsedFrame:
    payload: bytes
    header: Dict[str, int]
    header_crc_ok: bool
    payload_crc_ok: bool
    fec_ok: bool
    inverted: bool
    sync_index: int

    @property
    def ok(self) -> bool:
        return self.header_crc_ok and self.payload_crc_ok and self.fec_ok


class FrameCodec:
    """Build and parse frames for a `FrameProfile`."""

    def __init__(self, profile: FrameProfile):
        self.profile = profile
        self._counter = 0
        self._fec = self._build_fec(profile.fec_cfg)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FrameCodec":
        return cls(FrameProfile.from_dict(data))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FrameCodec":
        return cls(FrameProfile.from_yaml(path))

    def payload_region_bytes_for_length(self, raw_payload_bytes: int) -> int:
        """Return bytes in payload|payload_crc|padding before FEC/coding."""
        if raw_payload_bytes < 0:
            raise ValueError("payload length must be non-negative")
        region_len = raw_payload_bytes + self.profile.payload_crc_bytes
        align_bits = self.profile.padding_align_bits
        if align_bits <= 0:
            return region_len
        align_bytes = max(1, (align_bits + 7) // 8)
        return region_len + ((-region_len) % align_bytes)

    def encoded_payload_bits_for_length(self, raw_payload_bytes: int) -> int:
        """Return transmitted bits for the encoded payload region."""
        region_bits = self.payload_region_bytes_for_length(raw_payload_bytes) * 8
        encoded_bits = self._fec.coded_length(region_bits) if self._fec.enabled else region_bits
        cfg = self.profile.interleaving_cfg
        if cfg.get("enabled", False):
            rows = int(cfg.get("rows", 8))
            if rows > 1 and encoded_bits % rows != 0:
                raise ValueError("encoded payload bit length is not divisible by interleaver rows")
        return encoded_bits

    def frame_bits_for_length(self, raw_payload_bytes: int) -> int:
        """Return full transmitted frame length in bits for a raw payload length."""
        p = self.profile
        return (
            len(p.preamble_bits)
            + len(p.syncword_bits)
            + p.header_bits
            + p.header_crc_bits
            + self.encoded_payload_bits_for_length(raw_payload_bytes)
        )

    def build(self, payload: bytes, header_values: Optional[Dict[str, int]] = None) -> BuiltFrame:
        header_values = dict(header_values or {})
        p = self.profile
        if p.payload_max_bytes and len(payload) > p.payload_max_bytes:
            raise ValueError(f"payload is {len(payload)} bytes, max is {p.payload_max_bytes}")

        header = self._resolve_header(payload, header_values)
        header_bits = self._serialize_header(header)
        header_crc_bits = _bytes_to_bits(_crc_bytes(_bits_to_bytes(header_bits), p.header_crc_algorithm))

        payload_region = payload + _crc_bytes(payload, p.payload_crc_algorithm)
        payload_region = self._pad_payload_region(payload_region)
        encoded_bits = self._encode_payload_region(payload_region)

        bits = np.concatenate([
            p.preamble_bits,
            p.syncword_bits,
            header_bits,
            header_crc_bits,
            encoded_bits,
        ]).astype(np.uint8)

        counter = header.get("counter", self._counter)
        self._counter += 1
        return BuiltFrame(bits=bits, header=header, payload=payload, counter=counter)

    def parse(self, frame_bits: np.ndarray) -> ParsedFrame:
        bits = np.asarray(frame_bits, dtype=np.uint8).copy()
        p = self.profile

        sync_index, inverted = self._find_sync(bits)
        if sync_index < 0:
            raise ValueError("syncword not found")
        if inverted:
            bits ^= 1

        header_start = sync_index + len(p.syncword_bits)
        header_end = header_start + p.header_bits
        header_crc_end = header_end + p.header_crc_bits
        if len(bits) < header_crc_end:
            raise ValueError("frame ended before header was complete")

        header_bits = bits[header_start:header_end]
        header_crc_bits = bits[header_end:header_crc_end]
        header = self._parse_header(header_bits)

        expected_header_crc = _crc_bytes(_bits_to_bytes(header_bits), p.header_crc_algorithm)
        header_crc_ok = header_crc_bits.size == 0 or _bits_to_bytes(header_crc_bits) == expected_header_crc

        raw_len = int(header.get("length", 0))
        expected_payload_bits = self.encoded_payload_bits_for_length(raw_len)
        payload_end = header_crc_end + expected_payload_bits
        if len(bits) < payload_end:
            if header_crc_ok:
                raise ValueError("frame ended before encoded payload region was complete")
            payload_end = len(bits)

        encoded_payload_bits = bits[header_crc_end:payload_end]
        fec_ok = True
        try:
            payload_region = self._decode_payload_region(encoded_payload_bits)
        except Exception:
            payload_region = b""
            fec_ok = False

        crc_len = p.payload_crc_bytes
        payload = payload_region[:raw_len]
        rx_payload_crc = payload_region[raw_len:raw_len + crc_len]
        expected_payload_crc = _crc_bytes(payload, p.payload_crc_algorithm)
        payload_crc_ok = crc_len == 0 or rx_payload_crc == expected_payload_crc

        return ParsedFrame(
            payload=payload,
            header=header,
            header_crc_ok=header_crc_ok,
            payload_crc_ok=payload_crc_ok,
            fec_ok=fec_ok,
            inverted=inverted,
            sync_index=sync_index,
        )

    def _resolve_header(self, payload: bytes, overrides: Dict[str, int]) -> Dict[str, int]:
        header = {}
        for item in self.profile.header_fields:
            if item.name in overrides:
                value = int(overrides[item.name])
            elif item.value_from == "payload.length_bytes":
                value = len(payload)
            elif item.auto_increment:
                value = self._counter
            else:
                value = item.default
            header[item.name] = value
        return header

    def _serialize_header(self, header: Dict[str, int]) -> np.ndarray:
        return np.concatenate([
            _int_to_bits(int(header[item.name]), item.bits)
            for item in self.profile.header_fields
        ]).astype(np.uint8)

    def _parse_header(self, bits: np.ndarray) -> Dict[str, int]:
        offset = 0
        header = {}
        for item in self.profile.header_fields:
            field_bits = bits[offset:offset + item.bits]
            header[item.name] = _bits_to_int(field_bits)
            offset += item.bits
        return header

    def _pad_payload_region(self, region: bytes) -> bytes:
        p = self.profile
        if p.padding_align_bits <= 0:
            return region
        align_bytes = max(1, (p.padding_align_bits + 7) // 8)
        pad_len = (-len(region)) % align_bytes
        if pad_len == 0:
            return region
        if p.padding_mode not in ("custom_byte", "zero"):
            raise ValueError(f"unsupported padding mode: {p.padding_mode}")
        byte = 0 if p.padding_mode == "zero" else p.padding_byte & 0xFF
        return region + bytes([byte]) * pad_len

    def _encode_payload_region(self, region: bytes) -> np.ndarray:
        bits = self._fec.encode_bytes(region) if self._fec.enabled else _bytes_to_bits(region)
        bits = self._apply_interleaving(bits)
        bits = self._apply_whitening(bits)
        return bits.astype(np.uint8)

    def _decode_payload_region(self, bits: np.ndarray) -> bytes:
        bits = np.asarray(bits, dtype=np.uint8)
        bits = self._apply_whitening(bits)
        bits = self._undo_interleaving(bits)
        return self._fec.decode_bits(bits) if self._fec.enabled else _bits_to_bytes(bits)

    def _apply_whitening(self, bits: np.ndarray) -> np.ndarray:
        cfg = self.profile.whitening_cfg
        if not cfg.get("enabled", False):
            return bits.copy()
        if cfg.get("type", "lfsr") != "lfsr":
            raise ValueError("V1 only supports lfsr whitening")
        seed = int(cfg.get("seed", 0x7F))
        mask = int(cfg.get("mask", 0x48))
        return (bits ^ _lfsr_mask(len(bits), seed, mask)).astype(np.uint8)

    def _apply_interleaving(self, bits: np.ndarray) -> np.ndarray:
        cfg = self.profile.interleaving_cfg
        if not cfg.get("enabled", False):
            return bits.copy()
        if cfg.get("type", "matrix") != "matrix":
            raise ValueError("V1 only supports matrix interleaving")
        return _matrix_interleave(bits, int(cfg.get("rows", 8)))

    def _undo_interleaving(self, bits: np.ndarray) -> np.ndarray:
        cfg = self.profile.interleaving_cfg
        if not cfg.get("enabled", False):
            return bits.copy()
        return _matrix_deinterleave(bits, int(cfg.get("rows", 8)))

    def _find_sync(self, bits: np.ndarray) -> tuple[int, bool]:
        target = self.profile.syncword_bits
        n = len(target)
        max_errors = self.profile.sync_max_bit_errors
        best_idx = -1
        best_inv = False
        best_dist = n + 1
        for idx in range(0, len(bits) - n + 1):
            window = bits[idx:idx + n]
            dist = int(np.sum(window != target))
            inv_dist = int(np.sum((window ^ 1) != target)) if self.profile.sync_allow_inverted else n + 1
            if dist < best_dist:
                best_idx, best_inv, best_dist = idx, False, dist
            if inv_dist < best_dist:
                best_idx, best_inv, best_dist = idx, True, inv_dist
        if best_dist > max_errors:
            return -1, False
        return best_idx, best_inv

    @staticmethod
    def _build_fec(cfg: Dict[str, Any]) -> FECCodec:
        fec_type = cfg.get("type", "none")
        if fec_type in ("none", None):
            return FECCodec(SimpleNamespace(enabled=False, primary_type="none", outer_type="none"))
        if fec_type != "convolutional_k7_r12":
            raise ValueError(f"unsupported FEC type: {fec_type}")
        cc = SimpleNamespace(
            rate_inv=int(cfg.get("rate_inv", 2)),
            constraint_length=int(cfg.get("constraint_length", 7)),
            polynomials=list(cfg.get("polynomials", [121, 91])),
        )
        return FECCodec(SimpleNamespace(
            enabled=True,
            primary_type="cc",
            outer_type="none",
            cc=cc,
            rs=SimpleNamespace(nsym=0),
        ))
