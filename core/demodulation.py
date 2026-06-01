"""Demodulation: complex symbols to bits (hard decision).

Supports BPSK, QPSK, 8PSK, FSK, and differential PSK variants.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt


def demodulate_bpsk(symbols: npt.ArrayLike) -> np.ndarray:
    """Hard-decision BPSK. I >= 0 -> 1, else 0."""
    symbols = np.asarray(symbols, dtype=np.complex128)
    symbols = np.nan_to_num(symbols, nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.real(symbols) >= 0).astype(np.uint8)


def demodulate_qpsk(symbols: npt.ArrayLike) -> np.ndarray:
    """Hard-decision Gray-coded QPSK."""
    symbols = np.asarray(symbols, dtype=np.complex128)
    I, Q = np.real(symbols), np.imag(symbols)
    odd = (Q >= 0).astype(np.uint8)
    even = ((I >= 0).astype(np.uint8)) ^ odd
    bits = np.empty(len(symbols) * 2, dtype=np.uint8)
    bits[0::2] = even
    bits[1::2] = odd
    return bits


def demodulate_8psk(symbols: npt.ArrayLike) -> np.ndarray:
    """Hard-decision Gray-coded 8PSK."""
    symbols = np.asarray(symbols, dtype=np.complex128)
    n = len(symbols)
    mapping = np.array(
        [np.exp(2j * np.pi * i / 8) for i in range(8)], dtype=np.complex128
    )
    gray_decode = {
        0: 0b000,
        1: 0b001,
        2: 0b011,
        3: 0b010,
        4: 0b111,
        5: 0b110,
        6: 0b100,
        7: 0b101,
    }
    bits = np.empty(n * 3, dtype=np.uint8)
    for i, s in enumerate(symbols):
        idx = int(np.argmin(np.abs(s - mapping)))
        b = gray_decode[idx]
        bits[i * 3 : i * 3 + 3] = [(b >> 2) & 1, (b >> 1) & 1, b & 1]
    return bits


def demodulate_fsk(
    samples: npt.ArrayLike,
    samples_per_symbol: int = 8,
    deviation_hz: float = 50e3,
    sample_rate: float = 2e6,
) -> np.ndarray:
    """Non-coherent FSK demodulation using quadrature discriminator.

    Args:
        samples: Complex FSK signal.
        samples_per_symbol: Samples per symbol.
        deviation_hz: Frequency deviation in Hz.
        sample_rate: Sample rate in Hz.

    Returns:
        Demodulated bits.
    """
    samples = np.asarray(samples, dtype=np.complex128)
    if len(samples) < 2:
        return np.array([], dtype=np.uint8)
    inst_phase = np.unwrap(np.angle(samples))
    inst_freq = np.diff(inst_phase, prepend=inst_phase[0]) * sample_rate / (2.0 * np.pi)
    n_sym = len(samples) // samples_per_symbol
    bits = np.empty(n_sym, dtype=np.uint8)
    for i in range(n_sym):
        seg = inst_freq[i * samples_per_symbol:(i + 1) * samples_per_symbol]
        bits[i] = 1 if np.mean(seg) > 0 else 0
    return bits


def differential_decode(encoded: npt.ArrayLike, initial: int = 0) -> np.ndarray:
    """Differentially decode bits.

    Args:
        encoded: Differentially encoded bits.
        initial: Initial reference bit used in encoding.

    Returns:
        Decoded bits.
    """
    encoded = np.asarray(encoded, dtype=np.uint8)
    if len(encoded) == 0:
        return encoded.copy()
    prev = np.concatenate([[initial], encoded[:-1]])
    return (encoded ^ prev).astype(np.uint8)


def _differential_psk_demodulate(
    symbols: npt.ArrayLike,
    demod_func: Callable[[npt.ArrayLike], np.ndarray],
    initial_phase: float = 0.0,
) -> np.ndarray:
    """Generic differential PSK demodulation.

    Demodulates symbols, then differentially decodes.
    """
    symbols = np.asarray(symbols, dtype=np.complex128)
    if len(symbols) == 0:
        return np.array([], dtype=np.uint8)
    encoded = demod_func(symbols)
    return differential_decode(encoded, initial=0)


def demodulate_dbpsk(symbols: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """DBPSK demodulation."""
    return _differential_psk_demodulate(symbols, demodulate_bpsk, initial_phase)


def demodulate_dqpsk(symbols: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """DQPSK demodulation."""
    return _differential_psk_demodulate(symbols, demodulate_qpsk, initial_phase)


def demodulate_d8psk(symbols: npt.ArrayLike, initial_phase: float = 0.0) -> np.ndarray:
    """D8PSK demodulation."""
    return _differential_psk_demodulate(symbols, demodulate_8psk, initial_phase)


DEMOD_FUNCS: dict[str, Callable[..., np.ndarray]] = {
    "bpsk": demodulate_bpsk,
    "qpsk": demodulate_qpsk,
    "8psk": demodulate_8psk,
    "8dpsk": demodulate_d8psk,
    "fsk": demodulate_fsk,
    "dbpsk": demodulate_dbpsk,
    "dqpsk": demodulate_dqpsk,
    "d8psk": demodulate_d8psk,
}
