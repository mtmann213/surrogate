"""
Spreading code generators: Gold, m-sequence, Kasami, custom.
All generators return bipolar codes (+1/-1 as float32) and binary codes (0/1 as uint8).
"""
from __future__ import annotations

import numpy as np
from typing import List, Optional


def _lfsr(polynomial: int, degree: int, length: int, seed: int = 1) -> np.ndarray:
    """
    Generate an m-sequence using a Fibonacci LFSR.
    polynomial: integer mask (e.g. 0x25 for x^5+x^2+1, bit positions are exponents)
    degree:     shift register length in bits
    length:     number of output chips
    Returns binary array (0/1, uint8).
    """
    if seed == 0:
        seed = 1  # all-zero state is forbidden
    state = seed & ((1 << degree) - 1)
    mask = polynomial & ((1 << degree) - 1)
    out = np.empty(length, dtype=np.uint8)
    for i in range(length):
        feedback = bin(state & mask).count("1") % 2
        out[i] = state & 1
        state = ((state >> 1) | (feedback << (degree - 1))) & ((1 << degree) - 1)
    return out


def generate_msequence(degree: int, poly_hex: str, length: Optional[int] = None,
                       seed: int = 1) -> np.ndarray:
    """
    Generate a maximal-length sequence.
    degree:   LFSR degree; natural period = 2^degree - 1
    poly_hex: primitive polynomial as hex string e.g. '0x25'
    length:   output length; defaults to one full period
    Returns binary array (0/1, uint8).
    """
    poly = int(poly_hex, 16)
    period = (1 << degree) - 1
    n = length if length is not None else period
    base = _lfsr(poly, degree, period, seed)
    # Tile to requested length
    reps = (n // period) + 1
    return np.tile(base, reps)[:n]


def generate_gold(degree: int, poly1_hex: str, poly2_hex: str,
                  code_index: int = 0) -> np.ndarray:
    """
    Generate a Gold code of length 2^degree - 1.
    code_index shifts the second m-sequence (0 = standard XOR, >0 = phase shift).
    Returns binary array (0/1, uint8).
    """
    period = (1 << degree) - 1
    seq1 = generate_msequence(degree, poly1_hex, period)
    seq2 = generate_msequence(degree, poly2_hex, period)
    # Apply code_index as cyclic shift of seq2
    seq2 = np.roll(seq2, code_index)
    return (seq1 ^ seq2).astype(np.uint8)


def generate_kasami(degree: int, poly_hex: str) -> np.ndarray:
    """
    Generate a small Kasami code set member (index 0).
    Based on decimating the m-sequence by (2^(degree/2) + 1).
    degree must be even.
    Returns binary array (0/1, uint8).
    """
    if degree % 2 != 0:
        raise ValueError("Kasami codes require even degree")
    period = (1 << degree) - 1
    decimation = (1 << (degree // 2)) + 1
    seq = generate_msequence(degree, poly_hex, period * 2)  # extra for decimation
    decimated = seq[::decimation][:period]
    return (seq[:period] ^ decimated).astype(np.uint8)


def get_code(config) -> np.ndarray:
    """
    Dispatch to the appropriate generator based on SpreadingConfig.
    Returns binary code (0/1, uint8) of length config.code_length.
    """
    ct = config.code_type
    length = config.code_length
    seed = config.code_seed
    degree = config.degree

    if ct == "msequence":
        return generate_msequence(degree, config.poly1, length, seed)
    elif ct == "gold":
        code = generate_gold(degree, config.poly1, config.poly2, seed % ((1 << degree) - 1))
        # Tile/truncate to requested length
        period = (1 << degree) - 1
        reps = (length // period) + 1
        return np.tile(code, reps)[:length].astype(np.uint8)
    elif ct == "kasami":
        code = generate_kasami(degree, config.poly1)
        period = len(code)
        reps = (length // period) + 1
        return np.tile(code, reps)[:length].astype(np.uint8)
    elif ct == "custom":
        if not config.custom_code:
            raise ValueError("custom_code list is empty")
        code = np.array(config.custom_code, dtype=np.uint8)
        reps = (length // len(code)) + 1
        return np.tile(code, reps)[:length]
    else:
        raise ValueError(f"Unknown code_type: {ct}")


def to_bipolar(code: np.ndarray) -> np.ndarray:
    """Convert binary (0/1) code to bipolar (+1/-1) float32."""
    return (1 - 2 * code.astype(np.float32))


def from_bipolar(code: np.ndarray) -> np.ndarray:
    """Convert bipolar (+1/-1) code to binary (0/1) uint8."""
    return ((1 - code) / 2).astype(np.uint8)
