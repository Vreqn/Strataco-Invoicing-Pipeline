"""Unit tests for tools/_lib/aggregation_ledger — Step 7.

Round-trips through a temporary CSV: empty ledger, append rows of
different statuses, query `is_done` / `completed_for`, reload from disk
and confirm state survives. Also exercises the "ledger file missing"
path and the "corrupted row" rejection.
"""

from __future__ import annotations

import datetime as _dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools._lib.aggregation_ledger import (
    DONE_STATUSES,
    Ledger,
    LedgerRow,
    load,
    make_row,
)


def _fixed_dt(year=2026, month=6, day=7, hour=14, minute=32, second=1) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, second)


def test_missing_file_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "missing.csv"
        ledger = load(path)
        assert not ledger.rows, f"[missing] expected empty, got {len(ledger.rows)} rows"
        assert not ledger.is_done("BCS1234", 2026, 5), "[missing] is_done should be False"
        assert not ledger.completed_for(2026, 5), "[missing] completed_for should be []"


def test_append_writes_header_and_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        row = make_row("BCS1234", 2026, 4, "aggregated",
                       summary_filename="Summary - 04 - BCS1234 April 2026 inv.pdf",
                       sources_merged=17, now=_fixed_dt())
        ledger.append(row)
        assert path.exists(), "[append] file should exist after append"

        content = path.read_text(encoding="utf-8").splitlines()
        assert len(content) == 2, (
            f"[append] expected 2 lines (header + 1 row), got {len(content)}: {content}"
        )
        assert content[0].startswith("run_date,run_timestamp,plan_norm"), (
            f"[append] header wrong: {content[0]!r}"
        )
        assert "BCS1234" in content[1] and "aggregated" in content[1], (
            f"[append] data row missing fields: {content[1]!r}"
        )


def test_is_done_and_completed_for() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)

        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 4, "skipped_already_done", now=_fixed_dt(day=8)))
        ledger.append(make_row("VR9999",  2026, 4, "skipped_no_files",    now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 5, "aggregated_late",     now=_fixed_dt(day=11)))

        assert ledger.is_done("BCS1234", 2026, 4), "[is_done] BCS1234 April should be done (aggregated)"
        assert ledger.is_done("LMS4193", 2026, 4), "[is_done] LMS4193 April should be done"
        assert not ledger.is_done("VR9999", 2026, 4), (
            "[is_done] VR9999 April should NOT be done (skipped_no_files)"
        )
        assert ledger.is_done("BCS1234", 2026, 5), (
            "[is_done] BCS1234 May should be done (aggregated_late counts)"
        )
        assert not ledger.is_done("BCS1234", 2026, 3), (
            "[is_done] BCS1234 March has no rows — should be False"
        )

        assert ledger.is_done("bcs1234", 2026, 4), "[is_done] should be case-insensitive on plan_norm"

        completed_apr = ledger.completed_for(2026, 4)
        completed_plans = sorted(r.plan_norm for r in completed_apr)
        assert completed_plans == ["BCS1234", "LMS4193"], (
            f"[completed_for] expected [BCS1234, LMS4193], got {completed_plans}"
        )


def test_roundtrip_through_disk() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        first = load(path)
        first.append(make_row("BCS1234", 2026, 4, "aggregated", sources_merged=17,
                              notes="first run", now=_fixed_dt()))
        first.append(make_row("BCS1234", 2026, 4, "aggregated_late", sources_merged=1,
                              notes="late check", now=_fixed_dt(day=11)))

        second = load(path)
        assert len(second.rows) == 2, f"[round trip] expected 2 rows, got {len(second.rows)}"
        assert second.rows[0].plan_norm == "BCS1234" and second.rows[0].sources_merged == 17, (
            f"[round trip] row 0 wrong: {second.rows[0]}"
        )
        assert second.rows[1].status == "aggregated_late" and second.rows[1].notes == "late check", (
            f"[round trip] row 1 wrong: {second.rows[1]}"
        )
        assert second.is_done("BCS1234", 2026, 4), "[round trip] is_done lost across reload"


def test_zero_byte_file_gets_header_on_first_append() -> None:
    """Excel crash mid-save can leave a zero-byte CSV. Append must still
    write the header — otherwise the first data row becomes the de-facto
    header on the next load() and the ledger is silently corrupted."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        path.touch()
        assert path.stat().st_size == 0, (
            f"[zero-byte setup] expected size 0, got {path.stat().st_size}"
        )

        ledger = load(path)
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", sources_merged=3, now=_fixed_dt()))

        content = path.read_text(encoding="utf-8").splitlines()
        assert len(content) == 2, (
            f"[zero-byte] expected header + 1 row, got {len(content)} lines: {content}"
        )
        assert content[0].startswith("run_date,run_timestamp,plan_norm"), (
            f"[zero-byte] header missing or wrong: {content[0]!r}"
        )

        reloaded = load(path)
        assert len(reloaded.rows) == 1 and reloaded.rows[0].plan_norm == "BCS1234", (
            f"[zero-byte] reload wrong: {reloaded.rows}"
        )


def test_corrupted_row_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        path.write_text(
            "run_date,run_timestamp,plan_norm,target_year,target_month,status,summary_filename,sources_merged,notes\n"
            "2026-06-07,2026-06-07T14:32:01,BCS1234,not_a_year,4,aggregated,foo.pdf,1,\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load(path)


def test_completed_plans_for_dedups() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated_late", now=_fixed_dt(day=11)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7)))
        ledger.append(make_row("VR9999", 2026, 4, "dry_run", now=_fixed_dt(day=7)))
        ledger.append(make_row("BCS9999", 2026, 4, "skipped_no_files", now=_fixed_dt(day=7)))

        plans = ledger.completed_plans_for(2026, 4)
        assert plans == {"BCS1234", "LMS4193"}, (
            f"[completed_plans_for] expected {{BCS1234, LMS4193}}, got {plans}"
        )

        assert not ledger.is_done("VR9999", 2026, 4), (
            "[is_done] dry_run should not count as done"
        )


def test_latest_completed_timestamp() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.append(make_row("BCS1234", 2026, 4, "aggregated", now=_fixed_dt(day=7, hour=14)))
        ledger.append(make_row("LMS4193", 2026, 4, "aggregated", now=_fixed_dt(day=7, hour=14, minute=32, second=2)))
        ledger.append(make_row("VR9999",  2026, 4, "skipped_no_files", now=_fixed_dt(day=20)))
        latest = ledger.latest_completed_timestamp(2026, 4)
        assert latest is not None and latest.startswith("2026-06-07T14:32:02"), (
            f"[latest] expected 2026-06-07T14:32:02..., got {latest!r}"
        )
        assert ledger.latest_completed_timestamp(2026, 5) is None, (
            "[latest] month with no completed rows should return None"
        )
