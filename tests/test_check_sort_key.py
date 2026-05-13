"""Unit tests for `_check_sort_key` in steps/step_7_aggregate.py.

Pure-numeric checks should sort together by integer value; alpha-prefixed
sequences ("AB-123", "WIRE-9") should sort in their own bucket so a wire
transfer numbered 9 doesn't interleave between checks #8 and #10.

Standalone: no pytest. Run with `python tests/test_check_sort_key.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steps.step_7_aggregate import _check_sort_key


def _sorted(checks: list[str]) -> list[str]:
    return sorted(checks, key=_check_sort_key)


def test_pure_numeric_sorts_by_int_value() -> list[str]:
    failures: list[str] = []
    got = _sorted(["100", "9", "20", "1000"])
    expected = ["9", "20", "100", "1000"]
    if got != expected:
        failures.append(f"[numeric] expected {expected}, got {got}")
    return failures


def test_leading_zeros_normalised() -> list[str]:
    failures: list[str] = []
    got = _sorted(["00012", "0007", "100"])
    # 7 < 12 < 100 by integer value
    expected = ["0007", "00012", "100"]
    if got != expected:
        failures.append(f"[leading zeros] expected {expected}, got {got}")
    return failures


def test_alpha_prefix_buckets_separately_from_numeric() -> list[str]:
    failures: list[str] = []
    got = _sorted(["100", "AB-9", "20", "AB-100", "1000"])
    # Pure numeric come first (empty prefix sorts before "AB-"):
    # 20, 100, 1000, then AB-9, AB-100
    expected = ["20", "100", "1000", "AB-9", "AB-100"]
    if got != expected:
        failures.append(f"[alpha bucket] expected {expected}, got {got}")
    return failures


def test_two_alpha_prefixes_sort_lexicographically() -> list[str]:
    failures: list[str] = []
    got = _sorted(["WIRE-5", "AB-9", "WIRE-1", "AB-100"])
    expected = ["AB-9", "AB-100", "WIRE-1", "WIRE-5"]
    if got != expected:
        failures.append(f"[two prefixes] expected {expected}, got {got}")
    return failures


def test_no_digits_sort_last() -> list[str]:
    failures: list[str] = []
    got = _sorted(["VOID", "100", "AB-9"])
    expected = ["100", "AB-9", "VOID"]
    if got != expected:
        failures.append(f"[no digits] expected {expected}, got {got}")
    return failures


def test_empty_string_sorts_consistently() -> list[str]:
    failures: list[str] = []
    # Empty check is degenerate but must not crash; it sorts among
    # no-digit values (or last).
    got = _sorted(["100", "", "AB-9"])
    if got[0] != "100" or "" not in got or "AB-9" not in got:
        failures.append(f"[empty string] sort produced {got!r}")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("pure numeric -> int value", test_pure_numeric_sorts_by_int_value),
        ("leading zeros normalised", test_leading_zeros_normalised),
        ("alpha prefix buckets separately", test_alpha_prefix_buckets_separately_from_numeric),
        ("two prefixes sort lex", test_two_alpha_prefixes_sort_lexicographically),
        ("no digits sort last", test_no_digits_sort_last),
        ("empty string degenerate", test_empty_string_sorts_consistently),
    ]:
        fails = fn()
        status = "OK  " if not fails else "FAIL"
        print(f"{status}[{label}] ({len(fails)} failure{'s' if len(fails) != 1 else ''})")
        all_failures.extend(fails)

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
