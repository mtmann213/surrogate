"""
FEC encoder and decoder wrappers.
Supports convolutional codes (K=7 Viterbi) and Reed-Solomon outer code.
All operate on numpy uint8 bit arrays or byte arrays.
"""
from __future__ import annotations

import numpy as np
from typing import List, Tuple, Optional


# ---------------------------------------------------------------------------
# Convolutional Codec (rate 1/2, configurable K and polynomials)
# ---------------------------------------------------------------------------

class ConvolutionalCodec:
    """
    Rate 1/rate_inv convolutional encoder/decoder.
    Default: K=7, rate 1/2, NASA polynomials (octal 171=121, 133=91).
    """

    def __init__(self, constraint_length: int = 7,
                 polynomials: List[int] = None, rate_inv: int = 2):
        self.K = constraint_length
        self.rate_inv = rate_inv
        self.polynomials = polynomials if polynomials else [121, 91]
        self._n_states = 1 << (self.K - 1)
        self._precompute_trellis()

    # --- Encoder ---

    def encode(self, bits: np.ndarray) -> np.ndarray:
        """
        Encode binary input (0/1 uint8) to coded bits.
        Output length = len(bits) * rate_inv.
        Includes tail bits to flush the register.
        """
        bits = np.asarray(bits, dtype=np.uint8)
        # Append K-1 tail bits to flush encoder
        tail = np.zeros(self.K - 1, dtype=np.uint8)
        padded = np.concatenate([bits, tail])
        output = np.empty(len(padded) * self.rate_inv, dtype=np.uint8)
        register = 0
        for i, bit in enumerate(padded):
            register = ((register >> 1) | (int(bit) << (self.K - 1))) & (self._n_states * 2 - 1)
            for j, poly in enumerate(self.polynomials):
                output[i * self.rate_inv + j] = bin(register & poly).count("1") % 2
        return output

    def _precompute_trellis(self) -> None:
        """
        Precompute output bits and predecessor lookup for the vectorized ACS.

        State convention: the full K-bit shift register (same as the encoder).
        n_total = 2 * n_states = 2^K states.

        For new_state ns = (prev_state >> 1) | (input_bit << (K-1)):
          - input_bit  = (ns >> (K-1)) & 1   (MSB of ns)
          - predecessor = (ns & (n_states-1)) << 1  (even) or +1 (odd)

        Both predecessors share the same output bits (determined by ns alone).
        """
        n_total = self._n_states * 2
        K = self.K

        # output_table[ns, j] = j-th coded output bit for new_state ns
        out = np.zeros((n_total, self.rate_inv), dtype=np.uint8)
        for ns in range(n_total):
            for j, poly in enumerate(self.polynomials):
                out[ns, j] = bin(ns & poly).count("1") % 2
        self._output_table = out                         # (n_total, rate_inv)

        # Predecessor state indices (vectorised ACS uses these as fancy indices)
        ns_idx = np.arange(n_total, dtype=np.int32)
        lo6   = (ns_idx & (self._n_states - 1))         # lower (K-1) bits of ns
        self._pred0 = (lo6 << 1).astype(np.int32)       # even predecessor
        self._pred1 = (lo6 << 1 | 1).astype(np.int32)  # odd  predecessor

    # --- Viterbi Decoder (vectorised — GIL-friendly) ---

    def decode(self, received: np.ndarray) -> np.ndarray:
        """
        Hard-decision Viterbi decoder — fully vectorised with NumPy.

        NumPy array operations release the Python GIL, so the GNU Radio
        scheduler threads can run freely while the decoder works.

        received: coded bits (0/1 uint8), length must be multiple of rate_inv.
        Returns decoded bits, length = len(received) // rate_inv - (K-1).
        """
        received = np.asarray(received, dtype=np.uint8)
        n_bits   = len(received) // self.rate_inv
        n_total  = self._n_states * 2

        INF = np.int32(2**30)
        path_metrics = np.full(n_total, INF, dtype=np.int32)
        path_metrics[0] = 0
        survivors = np.empty((n_bits, n_total), dtype=np.int32)

        rx = received[:n_bits * self.rate_inv].reshape(n_bits, self.rate_inv)

        for step in range(n_bits):
            # Branch metric: Hamming distance between coded output and received
            branch = np.sum(self._output_table ^ rx[step], axis=1, dtype=np.int32)

            # ACS: add, compare, select over both predecessors
            m0 = path_metrics[self._pred0] + branch
            m1 = path_metrics[self._pred1] + branch
            sel = m0 > m1
            path_metrics = np.where(sel, m1, m0)
            survivors[step] = np.where(sel, self._pred1, self._pred0)

        # Traceback from the best final state. The encoder appends K-1 zero
        # tail bits, which flushes memory but does not force this full-register
        # state convention to numeric state 0 for every input length.
        decoded = np.empty(n_bits, dtype=np.uint8)
        state = int(np.argmin(path_metrics))
        for step in range(n_bits - 1, -1, -1):
            prev           = int(survivors[step, state])
            decoded[step]  = (state >> (self.K - 1)) & 1
            state          = prev

        return decoded[:n_bits - (self.K - 1)]


# ---------------------------------------------------------------------------
# Reed-Solomon Codec (outer code over GF(2^8))
# ---------------------------------------------------------------------------

class ReedSolomonCodec:
    """Thin wrapper around the reedsolo library."""

    def __init__(self, nsym: int = 16):
        self.nsym = nsym
        try:
            import reedsolo
            self._rs = reedsolo.RSCodec(nsym)
        except ImportError as exc:
            raise ImportError("Install 'reedsolo' for Reed-Solomon FEC support") from exc

    def encode(self, data: bytes) -> bytes:
        return bytes(self._rs.encode(data))

    def decode(self, data: bytes) -> bytes:
        try:
            decoded, _, _ = self._rs.decode(data)
            return bytes(decoded)
        except Exception as exc:
            raise ValueError("Reed-Solomon uncorrectable error") from exc


# ---------------------------------------------------------------------------
# Unified FECCodec
# ---------------------------------------------------------------------------

class FECCodec:
    """
    Unified FEC interface supporting CC + optional RS outer code.
    Encodes bytes → coded bits array; decodes coded bits array → bytes.
    """

    def __init__(self, config):
        self._cfg = config
        self._cc: Optional[ConvolutionalCodec] = None
        self._rs: Optional[ReedSolomonCodec] = None

        if config.enabled:
            if config.primary_type == "cc":
                cc = config.cc
                self._cc = ConvolutionalCodec(
                    constraint_length=cc.constraint_length,
                    polynomials=cc.polynomials,
                    rate_inv=cc.rate_inv,
                )
            if config.outer_type == "rs":
                self._rs = ReedSolomonCodec(nsym=config.rs.nsym)

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def encode_bytes(self, payload: bytes) -> np.ndarray:
        """bytes → coded bit array (uint8 0/1)."""
        data = payload
        if self._rs:
            data = self._rs.encode(data)
        bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
        if self._cc:
            bits = self._cc.encode(bits)
        return bits.astype(np.uint8)

    def decode_bits(self, coded_bits: np.ndarray) -> bytes:
        """coded bit array (uint8 0/1) → bytes."""
        bits = coded_bits.astype(np.uint8)
        if self._cc:
            bits = self._cc.decode(bits)
        data = np.packbits(bits).tobytes()
        if self._rs:
            data = self._rs.decode(data)
        return data

    def coded_length(self, info_bits: int) -> int:
        """Return the number of coded bits for a given number of info bits."""
        n = info_bits
        if self._rs:
            # RS operates on bytes; add nsym ECC bytes
            info_bytes = (n + 7) // 8
            n = (info_bytes + self._rs.nsym) * 8
        if self._cc:
            n = (n + self._cc.K - 1) * self._cc.rate_inv
        return n
