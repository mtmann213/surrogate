"""
Frame generator: builds TX frames and parses RX frames.

Frame structure (bits, before FEC+spreading):
  [PREAMBLE: preamble.length_bits]
  [INVARIANT: invariant.length_bits]  (if enabled)
  [PAYLOAD: payload_bits]

The preamble is transmitted as-is (not FEC, not spread).
Invariant + payload are FEC-encoded together, then spread.
"""
from __future__ import annotations

import struct
import time
import numpy as np
from typing import Optional, Tuple

from core.fec_codec import FECCodec
from core.spreading_codes import get_code, to_bipolar


def _parse_preamble_pattern(pattern: str, length_bits: int) -> np.ndarray:
    """Return preamble as binary array (0/1 uint8)."""
    if pattern == "alternating":
        bits = np.zeros(length_bits, dtype=np.uint8)
        bits[0::2] = 1  # 1,0,1,0,...
        return bits
    else:
        val = int(pattern, 16)
        raw = []
        for shift in range(length_bits - 1, -1, -1):
            raw.append((val >> shift) & 1)
        return np.array(raw[:length_bits], dtype=np.uint8)


def _parse_invariant_pattern(pattern: str, length_bits: int) -> np.ndarray:
    val = int(pattern, 16)
    raw = [(val >> (length_bits - 1 - i)) & 1 for i in range(length_bits)]
    return np.array(raw, dtype=np.uint8)


class FrameGenerator:
    """
    Builds complete TX frame bit sequences and parses received frames.
    Thread-safe: instances are stateless after construction.
    """

    def __init__(self, frame_cfg, fec_codec: FECCodec):
        self._cfg = frame_cfg
        self._fec = fec_codec
        self._preamble_bits = _parse_preamble_pattern(
            frame_cfg.preamble.pattern, frame_cfg.preamble.length_bits
        )
        self._invariant_bits: Optional[np.ndarray] = None
        if frame_cfg.invariant.enabled:
            self._invariant_bits = _parse_invariant_pattern(
                frame_cfg.invariant.pattern, frame_cfg.invariant.length_bits
            )

    @property
    def preamble_bits(self) -> np.ndarray:
        return self._preamble_bits.copy()

    @property
    def payload_bits(self) -> int:
        return self._cfg.payload_bits

    def build_frame(self, payload_bytes: bytes, frame_id: int = 0,
                    timestamp: Optional[float] = None) -> np.ndarray:
        """
        Build a complete frame bit sequence ready for spreading.
        Returns: [preamble | FEC(invariant|payload)] as uint8 bit array.
        payload_bytes will be truncated/zero-padded to fit payload_bits.
        """
        if timestamp is None:
            timestamp = time.time()

        # Pack payload into exactly payload_bits bits
        payload_bit_count = self._cfg.payload_bits
        payload_byte_count = (payload_bit_count + 7) // 8
        padded = payload_bytes[:payload_byte_count].ljust(payload_byte_count, b'\x00')

        # Build data section (invariant + payload)
        data_bits = np.array([], dtype=np.uint8)
        if self._invariant_bits is not None:
            data_bits = np.concatenate([data_bits, self._invariant_bits])

        payload_bits_arr = np.unpackbits(np.frombuffer(padded, dtype=np.uint8))
        payload_bits_arr = payload_bits_arr[:payload_bit_count]
        data_bits = np.concatenate([data_bits, payload_bits_arr]).astype(np.uint8)

        # Apply FEC
        if self._fec.enabled:
            # Convert bits → bytes for FEC input
            data_bytes = np.packbits(
                np.pad(data_bits, (0, (8 - len(data_bits) % 8) % 8))
            ).tobytes()
            coded_bits = self._fec.encode_bytes(data_bytes)
        else:
            coded_bits = data_bits

        # Assemble: preamble (raw) + coded data
        frame = np.concatenate([self._preamble_bits, coded_bits]).astype(np.uint8)
        return frame

    def parse_frame(self, frame_bits: np.ndarray) -> Tuple[Optional[bytes], bool]:
        """
        Parse received frame bits after preamble alignment.
        frame_bits: preamble stripped; contains only the data (coded) section.
        Returns (payload_bytes, fec_ok).
        """
        coded_bits = frame_bits.astype(np.uint8)

        if self._fec.enabled:
            try:
                data_bytes = self._fec.decode_bits(coded_bits)
                fec_ok = True
            except Exception:
                # Best-effort: unpack without correction
                n_bytes = (len(coded_bits) // (self._fec._cc.rate_inv if self._fec._cc else 1) + 7) // 8
                data_bytes = np.packbits(coded_bits[:n_bytes * 8]).tobytes()
                fec_ok = False
        else:
            data_bytes = np.packbits(
                np.pad(coded_bits, (0, (8 - len(coded_bits) % 8) % 8))
            ).tobytes()
            fec_ok = True

        data_bits = np.unpackbits(np.frombuffer(data_bytes, dtype=np.uint8))

        # Strip and verify invariant if present
        offset = 0
        if self._invariant_bits is not None:
            offset = len(self._invariant_bits)
            rx_invariant = data_bits[:offset]
            if not np.array_equal(rx_invariant, self._invariant_bits):
                # Check if it's just inverted (common in BPSK/GMSK)
                if np.array_equal(rx_invariant ^ 1, self._invariant_bits):
                    data_bits = data_bits ^ 1
                    fec_ok = True
                else:
                    fec_ok = False

        payload_bits_arr = data_bits[offset:offset + self._cfg.payload_bits]
        payload_bytes = np.packbits(
            np.pad(payload_bits_arr, (0, (8 - len(payload_bits_arr) % 8) % 8))
        ).tobytes()

        return payload_bytes, fec_ok

    def correlate_preamble(self, samples: np.ndarray) -> int:
        """
        Slide preamble over samples (bipolar +1/-1 float) and return peak offset.
        Used by RX to find frame start.
        """
        preamble_bp = to_bipolar(self._preamble_bits).astype(np.float32)
        n = len(preamble_bp)
        if len(samples) < n:
            return 0
        corr = np.correlate(samples.real[:len(samples)], preamble_bp, mode='valid')
        return int(np.argmax(np.abs(corr)))
