"""Unit tests for `_check_sort_key` in steps/step_7_aggregate.py.

Pure-numeric checks should sort together by integer value; alpha-prefixed
sequences ("AB-123", "WIRE-9") should sort in their own bucket so a wire
transfer numbered 9 doesn't interleave between checks #8 and #10.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steps.step_7_aggregate import _check_sort_key


def _sorted(checks: list[str]) -> list[str]:
    return sorted(checks, key=_check_sort_key)


def test_pure_numeric_sorts_by_int_value() -> None:
    got = _sorted(["100", "9", "20", "1000"])
    expected = ["9", "20", "100", "1000"]
    assert got == expected, f"[numeric] expected {expected}, got {got}"


def test_leading_zeros_normalised() -> None:
    got = _sorted(["00012", "0007", "100"])
    expected = ["0007", "00012", "100"]
    assert got == expected, f"[leading zeros] expected {expected}, got {got}"


def test_alpha_prefix_buckets_separately_from_numeric() -> None:
    got = _sorted(["100", "AB-9", "20", "AB-100", "1000"])
    expected = ["20", "100", "1000", "AB-9", "AB-100"]
    assert got == expected, f"[alpha bucket] expected {expected}, got {got}"


def test_two_alpha_prefixes_sort_lexicographically() -> None:
    got = _sorted(["WIRE-5", "AB-9", "WIRE-1", "AB-100"])
    expected = ["AB-9", "AB-100", "WIRE-1", "WIRE-5"]
    assert got == expected, f"[two prefixes] expected {expected}, got {got}"


def test_no_digits_sort_last() -> None:
    got = _sorted(["VOID", "100", "AB-9"])
    expected = ["100", "AB-9", "VOID"]
    assert got == expected, f"[no digits] expected {expected}, got {got}"


def test_empty_string_sorts_consistently() -> None:
    got = _sorted(["100", "", "AB-9"])
    assert got[0] == "100", f"[empty string] sort produced {got!r}"
    assert "" in got and "AB-9" in got, f"[empty string] sort produced {got!r}"
