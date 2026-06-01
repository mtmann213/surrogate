"""Runtime modulation guardrails for GNU Radio flowgraphs."""

SUPPORTED_RUNTIME_MODULATIONS = {"bpsk"}


def validate_runtime_modulation(modulation_type: str) -> None:
    """Raise if the live GNU Radio path does not implement this modulation yet."""
    if modulation_type not in SUPPORTED_RUNTIME_MODULATIONS:
        raise ValueError(
            f"Runtime flowgraph currently supports {sorted(SUPPORTED_RUNTIME_MODULATIONS)}, "
            f"got modulation.type={modulation_type!r}"
        )
