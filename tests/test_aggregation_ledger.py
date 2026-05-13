"""Unit tests for tools/_lib/aggregation_ledger — Step 7.

Round-trips through a temporary CSV: empty ledger, append rows of
different statuses, query `is_done` / `completed_for`, reload from disk
and confirm state survives. Also exercises the "ledger file missing"
path and the "corrupted row" rejection.

Standalone: no pytest dependency. Run with `python tests/test_aggregation_ledger.py`.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import datetime as _dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.aggregation_ledger import (
    DONE_STATUSES,
    Ledger,
    LedgerRow,
    load,
    make_row,
)


def _fixed_dt(year=2026, month=6, day=7, hour=14, minute=32, second=1) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, second)


def test_missing_file_returns_empty() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "missing.csv"
        ledger = load(path)
        if ledger.rows:
            failures.append(f"[missing] expected empty, got {len(ledger.rows)} rows")
        if ledger.is_done("BCS1234", 2026, 5):
            failures.append("[missing] is_done should be False")
        if ledger.completed_for(2026, 5):
            failures.append("[missing] completed_for should be []")
    return failures


def test_append_writes_header_and_row() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        row = make_row("BCS1234", 2026, 4, "aggregated",
                       summary_filename="Summary - 04 - BCS1234 April 2026 inv.pdf",
                       sources_merged=17, now=_fixed_dt())
        ledger.append(row)
        if not path.exists():
            failures.append("[append] file should exist after append")
            return failures

        content = path.read_text(encoding="utf-8").splitlines()
        if len(content) != 2:
            failures.append(f"[append] expected 2 lines (header + 1 row), got {len(content)}: {content}")
        if not content[0].startswith("run_date,run_timestamp,plan_norm"):
            failures.append(f"[append] header wrong: {content[0]!r}")
        if "BCS1234" not in content[1] or "aggregated" not in content[1]:
            failures.append(f"[append] data row missing fields: {content[1]!r}")
    return failures


def test_is_done_and_completed_for() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)

        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 4, "skipped_already_done", now=_fixed_dt(day=8)))
        ledger.append(make_row("VR9999",  2026, 4, "skipped_no_files",    now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 5, "aggregated_late",     now=_fixed_dt(day=11)))

        if not ledger.is_done("BCS1234", 2026, 4):
            failures.append("[is_done] BCS1234 April should be done (aggregated)")
        if not ledger.is_done("LMS4193", 2026, 4):
            failures.append("[is_done] LMS4193 April should be done")
        if ledger.is_done("VR9999", 2026, 4):
            failures.append("[is_done] VR9999 April should NOT be done (skipped_no_files)")
        if not ledger.is_done("BCS1234", 2026, 5):
            failures.append("[is_done] BCS1234 May should be done (aggregated_late counts)")
        if ledger.is_done("BCS1234", 2026, 3):
            failures.append("[is_done] BCS1234 March has no rows — should be False")

        # case-insensitive plan_norm match
        if not ledger.is_done("bcs1234", 2026, 4):
            failures.append("[is_done] should be case-insensitive on plan_norm")

        completed_apr = ledger.completed_for(2026, 4)
        completed_plans = sorted(r.plan_norm for r in completed_apr)
        if completed_plans != ["BCS1234", "LMS4193"]:
            failures.append(f"[completed_for] expected [BCS1234, LMS4193], got {completed_plans}")
    return failures


def test_roundtrip_through_disk() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        first = load(path)
        first.append(make_row("BCS1234", 2026, 4, "aggregated", sources_merged=17,
                              notes="first run", now=_fixed_dt()))
        first.append(make_row("BCS1234", 2026, 4, "aggregated_late", sources_merged=1,
                              notes="late check", now=_fixed_dt(day=11)))

        # Fresh load — state must survive the round trip.
        second = load(path)
        if len(second.rows) != 2:
            failures.append(f"[round trip] expected 2 rows, got {len(second.rows)}")
            return failures
        if second.rows[0].plan_norm != "BCS1234" or second.rows[0].sources_merged != 17:
            failures.append(f"[round trip] row 0 wrong: {second.rows[0]}")
        if second.rows[1].status != "aggregated_late" or second.rows[1].notes != "late check":
            failures.append(f"[round trip] row 1 wrong: {second.rows[1]}")
        if not second.is_done("BCS1234", 2026, 4):
            failures.append("[round trip] is_done lost across reload")
    return failures


def test_zero_byte_file_gets_header_on_first_append() -> list[str]:
    """Excel crash mid-save can leave a zero-byte CSV. Append must still
    write the header — otherwise the first data row becomes the de-facto
    header on the next load() and the ledger is silently corrupted."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        # Simulate a zero-byte existing file.
        path.touch()
        if path.stat().st_size != 0:
            failures.append(f"[zero-byte setup] expected size 0, got {path.stat().st_size}")
            return failures

        ledger = load(path)
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", sources_merged=3, now=_fixed_dt()))

        content = path.read_text(encoding="utf-8").splitlines()
        if len(content) != 2:
            failures.append(f"[zero-byte] expected header + 1 row, got {len(content)} lines: {content}")
            return failures
        if not content[0].startswith("run_date,run_timestamp,plan_norm"):
            failures.append(f"[zero-byte] header missing or wrong: {content[0]!r}")

        # Round-trip — the next load must parse this correctly.
        reloaded = load(path)
        if len(reloaded.rows) != 1 or reloaded.rows[0].plan_norm != "BCS1234":
            failures.append(f"[zero-byte] reload wrong: {reloaded.rows}")
    return failures


def test_corrupted_row_raises() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        # Hand-write a malformed CSV — target_year is not an integer.
        path.write_text(
            "run_date,run_timestamp,plan_norm,target_year,target_month,status,summary_filename,sources_merged,notes\n"
            "2026-06-07,2026-06-07T14:32:01,BCS1234,not_a_year,4,aggregated,foo.pdf,1,\n",
            encoding="utf-8",
        )
        try:
            load(path)
        except ValueError:
            return failures
        except Exception as exc:
            failures.append(f"[corrupted] expected ValueError, got {type(exc).__name__}: {exc}")
            return failures
        failures.append("[corrupted] expected ValueError, got no exception")
    return failures


def test_completed_plans_for_dedups() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        # Same plan, two 'done' rows (initial + late) — should count as ONE plan.
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated_late", now=_fixed_dt(day=11)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        # dry_run and skipped_no_files must NOT count
        ledger.append(make_row("VR9999", 2026, 4, "dry_run", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS9999", 2026, 4, "skipped_no_files", now=_fixed_dt(day=7)))

        plans = ledger.completed_plans_for(2026, 4)
        if plans != {"BCS1234", "LMS4193"}:
            failures.append(f"[completed_plans_for] expected {{BCS1234, LMS4193}}, got {plans}")

        # dry_run alone (no aggregated row) — plan is NOT done
        if ledger.is_done("VR9999", 2026, 4):
            failures.append("[is_done] dry_run should not count as done")
    return failures


def test_latest_completed_timestamp() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7, hour=14)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7, hour=14, minute=32, second=2)))
        ledger.append(make_row("VR9999",  2026, 4, "skipped_no_files", now=_fixed_dt(day=20)))  # day 20 but skipped — shouldn't count
        latest = ledger.latest_completed_timestamp(2026, 4)
        if latest is None or not latest.startswith("2026-06-07T14:32:02"):
            failures.append(f"[latest] expected 2026-06-07T14:32:02..., got {latest!r}")
        if ledger.latest_completed_timestamp(2026, 5) is not None:
            failures.append("[latest] month with no completed rows should return None")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("missing file -> empty ledger", test_missing_file_returns_empty),
        ("append writes header and row", test_append_writes_header_and_row),
        ("is_done / completed_for", test_is_done_and_completed_for),
        ("round trip through disk", test_roundtrip_through_disk),
        ("zero-byte file gets header", test_zero_byte_file_gets_header_on_first_append),
        ("corrupted row raises", test_corrupted_row_raises),
        ("completed_plans_for dedups", test_completed_plans_for_dedups),
        ("latest_completed_timestamp", test_latest_completed_timestamp),
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
