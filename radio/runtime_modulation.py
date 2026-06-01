"""Runtime modulation guardrails for GNU Radio flowgraphs."""

from core.modulation import BITS_PER_SYMBOL

SUPPORTED_RUNTIME_MODULATIONS = {"bpsk", "qpsk"}


def validate_runtime_modulation(modulation_type: str) -> None:
    """Raise if the live GNU Radio path does not implement this modulation yet."""
    if modulation_type not in SUPPORTED_RUNTIME_MODULATIONS:
        raise ValueError(
            f"Runtime flowgraph currently supports {sorted(SUPPORTED_RUNTIME_MODULATIONS)}, "
            f"got modulation.type={modulation_type!r}"
        )


def runtime_bits_per_symbol(modulation_type: str) -> int:
    validate_runtime_modulation(modulation_type)
    return BITS_PER_SYMBOL[modulation_type]


def validate_runtime_frame_alignment(modulation_type: str, total_chips: int) -> None:
    """Raise if packed-chip modulation would split a frame across symbols."""
    bits_per_symbol = runtime_bits_per_symbol(modulation_type)
    if total_chips % bits_per_symbol != 0:
        raise ValueError(
            f"modulation.type={modulation_type!r} requires total frame chips "
            f"({total_chips}) to be divisible by {bits_per_symbol}"
        )
