"""Unit tests for tools/_lib/plan_match.parse_archive_filename — Step 7.

The parser is the inverse of Step 6's `_build_archive_name` and must agree
with it; any drift causes Step 7 to silently miss invoices or pick up the
wrong ones. The cases here pin down:

  - The exact Step 6 emit format
  - safe_write_unique's collision suffix `... (1).pdf`
  - The MonthName-vs-MM consistency check
  - Rejection of the Step 7 Summary output (so Step 7 never re-aggregates
    its own Summaries)

Standalone: no pytest dependency. Run with `python tests/test_aggregate_filename.py`.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.plan_match import parse_archive_filename


def test_parses_canonical_step6_name() -> list[str]:
    failures: list[str] = []

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
        if got != expected:
            failures.append(f"[canonical {name!r}] expected {expected}, got {got}")

    return failures


def test_alpha_check_number() -> list[str]:
    failures: list[str] = []
    got = parse_archive_filename("AB-123 - 03 - LMS4193A March 2026 inv.pdf")
    if got is None:
        failures.append("[alpha check] returned None")
    elif got["check"] != "AB-123" or got["plan_norm"] != "LMS4193A":
        failures.append(f"[alpha check] got {got}")
    return failures


def test_tolerates_collision_suffix() -> list[str]:
    failures: list[str] = []

    cases = [
        "12345 - 05 - BCS1234 May 2026 inv (1).pdf",
        "12345 - 05 - BCS1234 May 2026 inv (2).pdf",
        "12345 - 05 - BCS1234 May 2026 inv (10).pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        if got is None:
            failures.append(f"[collision {name!r}] returned None")
            continue
        if (got["check"], got["month"], got["year"], got["plan_norm"]) != ("12345", 5, 2026, "BCS1234"):
            failures.append(f"[collision {name!r}] wrong fields: {got}")
    return failures


def test_rejects_step7_summary() -> list[str]:
    failures: list[str] = []
    # Step 7's own output ("Summary - ...") must NOT parse as a Step 6 archive
    # name; otherwise Step 7 would re-aggregate its own previous output on the
    # next run. The parser rejects the "summary -" prefix defensively even
    # though the call site also filters — belt and suspenders.
    cases = [
        "Summary - 05 - BCS1234 May 2026 inv.pdf",
        "Summary - 05 - BCS1234 May 2026 inv (1).pdf",
        "summary - 05 - BCS1234 May 2026 inv.pdf",   # lowercase
        "SUMMARY - 05 - BCS1234 May 2026 inv.pdf",   # uppercase
        "Summary-05 - BCS1234 May 2026 inv.pdf",     # no-space variant
    ]
    for name in cases:
        got = parse_archive_filename(name)
        if got is not None:
            failures.append(f"[reject summary {name!r}] expected None, got {got}")
    return failures


def test_rejects_bad_month() -> list[str]:
    failures: list[str] = []
    cases = [
        # month=13 — regex won't accept it
        "12345 - 13 - BCS1234 May 2026 inv.pdf",
        # month=00 — regex requires 01-12
        "12345 - 00 - BCS1234 May 2026 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        if got is not None:
            failures.append(f"[bad month {name!r}] should be None, got {got}")
    return failures


def test_rejects_monthname_mismatch() -> list[str]:
    failures: list[str] = []
    # month=05 but MonthName is "March" — Step 6 should never emit this; if it
    # does, refuse to consume it rather than create a misnamed Summary.
    cases = [
        "12345 - 05 - BCS1234 March 2026 inv.pdf",
        "12345 - 03 - BCS1234 December 2026 inv.pdf",
    ]
    for name in cases:
        got = parse_archive_filename(name)
        if got is not None:
            failures.append(f"[mismatch {name!r}] should be None, got {got}")
    return failures


def test_rejects_other_garbage() -> list[str]:
    failures: list[str] = []
    cases = [
        "",
        "random.pdf",
        "Processed - 12345 - 05 - BCS1234 May 2026 inv.pdf",   # leading Processed- isn't Step 6's archive emit
        "12345 - 05 - BCS1234 May 2026.pdf",                   # missing 'inv'
        "12345 - 5 - BCS1234 May 2026 inv.pdf",                # single-digit month — Step 6 zero-pads
        "12345 - 05 - BCS1234 May inv.pdf",                    # no year
        "12345 - 05 - BCS1234 May 26 inv.pdf",                 # 2-digit year
    ]
    for name in cases:
        got = parse_archive_filename(name)
        if got is not None:
            failures.append(f"[garbage {name!r}] should be None, got {got}")
    return failures


def test_case_insensitive() -> list[str]:
    failures: list[str] = []
    # The regex has re.IGNORECASE; mixed-case still parses (and norm_plan upper-cases).
    got = parse_archive_filename("12345 - 05 - bcs1234 may 2026 inv.pdf")
    if got is None:
        failures.append("[case insensitive] returned None")
    elif got["plan_norm"] != "BCS1234":
        failures.append(f"[case insensitive] plan_norm not uppercased: {got}")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("canonical Step 6 names", test_parses_canonical_step6_name),
        ("alpha check numbers", test_alpha_check_number),
        ("collision suffix (1)/(2)", test_tolerates_collision_suffix),
        ("Summary parse contract", test_rejects_step7_summary),
        ("bad month (13/00)", test_rejects_bad_month),
        ("month/MonthName mismatch", test_rejects_monthname_mismatch),
        ("garbage names", test_rejects_other_garbage),
        ("case-insensitive plan", test_case_insensitive),
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
