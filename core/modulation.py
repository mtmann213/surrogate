"""Modulation: bits to complex baseband symbols.

Supports BPSK, QPSK, 8PSK, FSK, and differential PSK variants.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt


def modulate_bpsk(bits: npt.ArrayLike) -> np.ndarray:
    """Modulate bits to BPSK symbols. 0 -> -1, 1 -> +1."""
    bits = np.asarray(bits, dtype=np.uint8)
    return (2 * bits.astype(np.float64) - 1.0).astype(np.complex128)


def modulate_qpsk(bits: npt.ArrayLike) -> np.ndarray:
    """Gray-coded QPSK. Input bits must be even length."""
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) % 2 != 0:
        raise ValueError(f"QPSK requires even number of bits, got {len(bits)}")
    even = bits[0::2]
    odd = bits[1::2]
    scale = 1.0 / np.sqrt(2.0)
    I = scale * (2 * (even ^ odd).astype(np.float64) - 1.0)
    Q = scale * (2 * odd.astype(np.float64) - 1.0)
    return I + 1j * Q


def modulate_8psk(bits: npt.ArrayLike) -> np.ndarray:
    """Gray-coded 8PSK. Input bits must be a multiple of 3."""
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) % 3 != 0:
        raise ValueError(f"8PSK requires multiple of 3 bits, got {len(bits)}")
    n = len(bits) // 3
    triples = bits.reshape(n, 3)
    mapping = np.array(
        [np.exp(2j * np.pi * i / 8) for i in range(8)], dtype=np.complex128
    )
    indices = np.empty(n, dtype=np.int32)
    for i in range(n):
        idx = int(triples[i, 0]) << 2 | int(triples[i, 1]) << 1 | int(triples[i, 2])
        indices[i] = idx ^ (idx >> 1)
    return mapping[indices]


def modulate_fsk(
    bits: npt.ArrayLike,
    samples_per_symbol: int = 8,
    deviation_hz: float = 50e3,
    sample_rate: float = 2e6,
) -> np.ndarray:
    """Modulate bits to continuous-phase FSK.

    Bit 0 frequency = -deviation_hz, Bit 1 frequency = +deviation_hz.

    Args:
        bits: Input bits.
        samples_per_symbol: Samples per FSK symbol.
        deviation_hz: Frequency deviation in Hz.
        sample_rate: Sample rate in Hz.

    Returns:
        Complex FSK signal.
    """
    bits = np.asarray(bits, dtype=np.float64)
    if len(bits) == 0:
        return np.array([], dtype=np.complex128)
    n_samp = len(bits) * samples_per_symbol
    freq_per_sample = 2.0 * np.pi * deviation_hz / sample_rate
    baseband = freq_per_sample * (2.0 * bits - 1.0)
    phase_inc = np.repeat(baseband, samples_per_symbol)
    phase = np.cumsum(phase_inc)
    return np.exp(1j * phase)


def differential_encode(bits: npt.ArrayLike, initial: int = 0) -> np.ndarray:
    """Differentially encode bits (XOR with previous).

    Args:
        bits: Input bits.
        initial: Initial reference bit.

    Returns:
        Differentially encoded bits.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) == 0:
        return bits.copy()
    encoded = np.empty(len(bits), dtype=np.uint8)
    prev = initial
    for i in range(len(bits)):
        encoded[i] = prev ^ bits[i]
        prev = encoded[i]
    return encoded


def _differential_psk_modulate(
    bits: npt.ArrayLike,
    mod_func: Callable[[npt.ArrayLike], np.ndarray],
    initial_phase: float = 0.0,
) -> np.ndarray:
    """Generic differential PSK modulation.

    Encodes bits differentially, then modulates.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) == 0:
        return np.array([], dtype=np.complex128)
    encoded = differential_encode(bits, initial=0)
    symbols = mod_func(encoded)
    symbols[0] *= np.exp(1j * initial_phase)
    return symbols


def modulate_dbpsk(bits: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """DBPSK: differentially encoded BPSK."""
    return _differential_psk_modulate(bits, modulate_bpsk, initial_phase)


def modulate_dqpsk(bits: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """DQPSK: differentially encoded QPSK."""
    return _differential_psk_modulate(bits, modulate_qpsk, initial_phase)


def modulate_d8psk(bits: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """D8PSK: differentially encoded 8PSK."""
    return _differential_psk_modulate(bits, modulate_8psk, initial_phase)


MOD_FUNCS: dict[str, Callable[..., np.ndarray]] = {
    "bpsk": modulate_bpsk,
    "qpsk": modulate_qpsk,
    "8psk": modulate_8psk,
    "8dpsk": modulate_d8psk,
    "fsk": modulate_fsk,
    "dbpsk": modulate_dbpsk,
    "dqpsk": modulate_dqpsk,
    "d8psk": modulate_d8psk,
}


BITS_PER_SYMBOL: dict[str, int] = {
    "bpsk": 1,
    "qpsk": 2,
    "8psk": 3,
    "8dpsk": 3,
    "fsk": 1,
    "dbpsk": 1,
    "dqpsk": 2,
    "d8psk": 3,
}
