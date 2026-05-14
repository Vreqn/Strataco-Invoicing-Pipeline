"""Unit tests for tools/_lib/plan_match.parse_archive_filename — Step 7.

The parser is the inverse of Step 6's `_build_archive_name` and must agree
with it; any drift causes Step 7 to silently miss invoices or pick up the
wrong ones. The cases here pin down:

  - The exact Step 6 emit format
  - safe_write_unique's collision suffix `... (1).pdf`
  - The MonthName-vs-MM consistency check
  - Rejection of the Step 7 Summary output (so Step 7 never re-aggregates
    its own Summaries)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.plan_match import parse_archive_filename


def test_parses_canonical_step6_name() -> None:
    cases = [
        (
            "12345 - 05 - BCS1234 May 2026 inv.pdf",
            {"check": "12345", "month": 5, "year": 2026, "plan_norm": "BCS1234"},
        ),
        (
            "1 - 01 - VR9 January 2024 inv.pdf",
            {"check": "1", "month": 1, "year": 2024, "plan_norm": "VR9"},
        ),
        (
            "99999 - 12 - LMS4193A December 2030 inv.pdf",
            {"check": "99999", "month": 12, "year": 2030, "plan_norm": "LMS4193A"},
        ),
    ]
    for name, expected in cases:
        got = parse_archive_filename(name)
        assert got == expected, f"[canonical {name!r}] expected {expected}, got {got}"


def test_alpha_check_number() -> None:
    got = parse_archive_filename("AB-123 - 03 - LMS4193A March 2026 inv.pdf")
    assert got is not None, "[alpha check] returned None"
    assert got["check"] == "AB-123" and got["plan_norm"] == "LMS4193A", f"[alpha check] got {got}"


def test_tolerates_collision_suffix() -> None:
    cases = [
        "12345 - 05 - BCS1234 May 2026 inv (1).pdf",
        "12345 - 05 - BCS1234 May 2026 inv (2).pdf",
        "12345 - 05 - BCS1234 May 2026 inv (10).pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        assert got is not None, f"[collision {name!r}] returned None"
        assert (got["check"], got["month"], got["year"], got["plan_norm"]) == ("12345", 5, 2026, "BCS1234"), (
            f"[collision {name!r}] wrong fields: {got}"
        )


def test_rejects_step7_summary() -> None:
    cases = [
        "Summary - 05 - BCS1234 May 2026 inv.pdf",
        "Summary - 05 - BCS1234 May 2026 inv (1).pdf",
        "summary - 05 - BCS1234 May 2026 inv.pdf",
        "SUMMARY - 05 - BCS1234 May 2026 inv.pdf",
        "Summary-05 - BCS1234 May 2026 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        assert got is None, f"[reject summary {name!r}] expected None, got {got}"


def test_rejects_bad_month() -> None:
    cases = [
        "12345 - 13 - BCS1234 May 2026 inv.pdf",
        "12345 - 00 - BCS1234 May 2026 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        assert got is None, f"[bad month {name!r}] should be None, got {got}"


def test_rejects_monthname_mismatch() -> None:
    cases = [
        "12345 - 05 - BCS1234 March 2026 inv.pdf",
        "12345 - 03 - BCS1234 December 2026 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        assert got is None, f"[mismatch {name!r}] should be None, got {got}"


def test_rejects_other_garbage() -> None:
    cases = [
        "",
        "random.pdf",
        "Processed - 12345 - 05 - BCS1234 May 2026 inv.pdf",
        "12345 - 05 - BCS1234 May 2026.pdf",
        "12345 - 5 - BCS1234 May 2026 inv.pdf",
        "12345 - 05 - BCS1234 May inv.pdf",
        "12345 - 05 - BCS1234 May 26 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        assert got is None, f"[garbage {name!r}] should be None, got {got}"


def test_case_insensitive() -> None:
    got = parse_archive_filename("12345 - 05 - bcs1234 may 2026 inv.pdf")
    assert got is not None, "[case insensitive] returned None"
    assert got["plan_norm"] == "BCS1234", f"[case insensitive] plan_norm not uppercased: {got}"
