"""Existing tests — these all pass, which is the problem."""

from demo.src.metrics import normalize, rolling_mean, safe_divide


def test_rolling_mean_returns_one_value_per_input() -> None:
    assert len(rolling_mean([1, 2, 3, 4], window=2)) == 4


def test_normalize_bounds() -> None:
    assert normalize([0, 5, 10]) == [0.0, 0.5, 1.0]


def test_safe_divide_handles_zero() -> None:
    assert safe_divide(1, 0) == 0.0
