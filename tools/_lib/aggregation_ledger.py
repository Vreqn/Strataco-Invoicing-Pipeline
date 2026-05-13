"""Append-only audit ledger for Step 7 (Monthly Invoice Aggregator).

Every (plan, target-month) attempt — whether it produced a new Summary,
hit the idempotency guard, ran into an inconsistency, or just had nothing
to do — appends one row here. Two reasons:

1. Operator answer to "did April actually finish for every plan?" lives
   in one greppable, Excel-friendly file.
2. The script's own idempotency check (rule 1 in the workflow) consults
   this ledger before doing work, so an accidental second run on day 8
   doesn't re-merge a month that already completed on day 7.

Schema (single header row, one append per (plan, month) per run):
    run_date,run_timestamp,plan_norm,target_year,target_month,
    status,summary_filename,sources_merged,notes

Status values are documented in `workflows/step_7_aggregate.md`. The
"done" statuses — those that count as "this month has been aggregated"
— are listed in `DONE_STATUSES` here.

Writes use the same `portalocker.Lock` + CSV append pattern that
`tools/_lib/log.py` uses for `logs/daily_summary.csv`, so the operator
can keep the ledger open in Excel between runs without breaking writes.
"""

from __future__ import annotations

import csv
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import portalocker

from tools._lib import paths

HEADER: list[str] = [
    "run_date",
    "run_timestamp",
    "plan_norm",
    "target_year",
    "target_month",
    "status",
    "summary_filename",
    "sources_merged",
    "notes",
]

# Status values that count as "the month has been aggregated" — both first-time
# aggregation and additive late-check runs flip the (plan, month) into "done"
# from the ledger's perspective.
DONE_STATUSES: frozenset[str] = frozenset({"aggregated", "aggregated_late"})


@dataclass
class LedgerRow:
    run_date: str
    run_timestamp: str
    plan_norm: str
    target_year: int
    target_month: int
    status: str
    summary_filename: str = ""
    sources_merged: int = 0
    notes: str = ""

    def to_csv_row(self) -> list:
        return [
            self.run_date,
            self.run_timestamp,
            self.plan_norm,
            self.target_year,
            self.target_month,
            self.status,
            self.summary_filename,
            self.sources_merged,
            self.notes,
        ]


def make_row(
    plan_norm: str,
    year: int,
    month: int,
    status: str,
    *,
    summary_filename: str = "",
    sources_merged: int = 0,
    notes: str = "",
    now: _dt.datetime | None = None,
) -> LedgerRow:
    """Build a `LedgerRow` with the current America/Vancouver timestamp.

    `now` override is for tests; production callers omit it.
    """
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
        except Exception:
            now = _dt.datetime.now()
    return LedgerRow(
        run_date=now.date().isoformat(),
        run_timestamp=now.isoformat(timespec="seconds"),
        plan_norm=plan_norm,
        target_year=int(year),
        target_month=int(month),
        status=status,
        summary_filename=summary_filename,
        sources_merged=int(sources_merged),
        notes=notes,
    )


class Ledger:
    """In-memory view of the ledger, with file-backed append.

    Callers `load()` once at the start of a run, query via `is_done` and
    `completed_for`, then call `append()` for each (plan, month) outcome.
    `append()` updates the in-memory list AND the on-disk file, so a
    later query inside the same run reflects rows written earlier in the
    same run.
    """

    def __init__(self, rows: list[LedgerRow], path: Path):
        self.rows = rows
        self.path = path

    def is_done(self, plan_norm: str, year: int, month: int) -> bool:
        plan_up = plan_norm.upper()
        for r in self.rows:
            if (
                r.plan_norm.upper() == plan_up
                and r.target_year == year
                and r.target_month == month
                and r.status in DONE_STATUSES
            ):
                return True
        return False

    def completed_for(self, year: int, month: int) -> list[LedgerRow]:
        """Every row for (year, month) whose status counts as 'done'.

        Includes duplicates if a plan was aggregated multiple times (e.g. an
        original `aggregated` row plus a later `aggregated_late` row). Use
        `completed_plans_for` if you want a dedup'd plan set.
        """
        return [
            r for r in self.rows
            if r.target_year == year
            and r.target_month == month
            and r.status in DONE_STATUSES
        ]

    def completed_plans_for(self, year: int, month: int) -> set[str]:
        """Set of `plan_norm` values that have any 'done' row for (year, month)."""
        return {r.plan_norm.upper() for r in self.completed_for(year, month)}

    def latest_completed_timestamp(self, year: int, month: int) -> str | None:
        completed = self.completed_for(year, month)
        if not completed:
            return None
        return max(r.run_timestamp for r in completed)

    def append(self, row: LedgerRow) -> None:
        """Append a row under a cross-process file lock; update memory.

        Writes the header when the file is missing OR exists but is zero-bytes
        (e.g. Excel crashed mid-save, or the operator hand-created an empty
        file). Without the size check, a zero-byte existing file would skip
        the header and the first data row would be misread as the header on
        the next `load()`.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_header = (
            not self.path.exists()
            or self.path.stat().st_size == 0
        )
        with portalocker.Lock(str(self.path) + ".lock", timeout=30):
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(HEADER)
                w.writerow(row.to_csv_row())
        self.rows.append(row)


def load(path: Path | None = None) -> Ledger:
    """Read the ledger from disk. Returns an empty ledger if the file is missing.

    Raises `ValueError` if any row has a non-integer year/month/sources_merged
    so a corrupted ledger is surfaced rather than silently treated as empty.
    """
    if path is None:
        path = paths.monthly_aggregations_csv()
    rows: list[LedgerRow] = []
    if not path.exists():
        return Ledger(rows, path)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            try:
                rows.append(LedgerRow(
                    run_date=raw.get("run_date", "") or "",
                    run_timestamp=raw.get("run_timestamp", "") or "",
                    plan_norm=raw.get("plan_norm", "") or "",
                    target_year=int(raw.get("target_year") or 0),
                    target_month=int(raw.get("target_month") or 0),
                    status=raw.get("status", "") or "",
                    summary_filename=raw.get("summary_filename", "") or "",
                    sources_merged=int(raw.get("sources_merged") or 0),
                    notes=raw.get("notes", "") or "",
                ))
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Ledger CSV {path} has a malformed row {raw!r}: {exc}"
                ) from exc
    return Ledger(rows, path)
