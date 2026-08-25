"""Small numeric helpers used by the training loop."""

from __future__ import annotations
from collections.abc import Sequence


def rolling_mean(values: Sequence[float], window: int) -> list[float]:
    """Mean of the trailing `window` samples, including the current one."""
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float] = []
    for i in range(len(values)):
        start = max(0, i - window)
        chunk = values[start : i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def normalize(values: Sequence[float]) -> list[float]:
    """Scale values into [0, 1]."""
    low, high = min(values), max(values)
    return [(v - low) / (high - low) for v in values]


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Divide, falling back to `default` when the denominator is zero."""
    try:
        return numerator / denominator
    except Exception:
        return default
