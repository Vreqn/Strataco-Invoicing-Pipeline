"""All path builders for the Strataco file tree.

Every path the project reads or writes is constructed here so that the
single STRATACO_ROOT setting in .env controls everything. The N8n flow
uses Linux paths under /home/node/.n8n-files/Strataco/...; we use the
same relative layout under STRATACO_ROOT.
"""

from __future__ import annotations

import calendar
from pathlib import Path

from tools._lib import config, safe_io


def root() -> Path:
    return config.strataco_root()


def _safe_component(name: str) -> str:
    """Sanitize a path component from external data (manager/AP/plan name).

    Raises ValueError on anything that could escape STRATACO_ROOT — kept here
    so every path builder funnels through the same check.
    """
    return safe_io.sanitize_path_component(name)


def _under_root(p: Path) -> Path:
    """Return `p` after asserting it resolves inside STRATACO_ROOT."""
    return safe_io.assert_under_root(p, root())


def strataplan_xlsx() -> Path:
    """The master Strataplan_List.xlsx (plan -> manager -> AP map).

    Only Step 1's snapshot refresh should read this directly. All other
    callers go through `strataplan_snapshot_xlsx()` so they never crash
    when the operator has the master open in Excel.
    """
    return root() / "Strataplan_List.xlsx"


def strataplan_snapshot_xlsx() -> Path:
    """Working-copy snapshot of Strataplan_List.xlsx. Refreshed by Step 1."""
    return root() / "_state" / "strataplan_list_snapshot.xlsx"


def strataplan_snapshot_marker() -> Path:
    """Sidecar containing the YYYY-MM-DD the snapshot was successfully refreshed."""
    return root() / "_state" / "strataplan_list_snapshot.ok"


def unmatched_invoices() -> Path:
    """Holding pen for files we couldn't route by email subject (Step 1, 2, 3)."""
    return root() / "_Unmatched" / "Invoices"


def manager_to_approve(manager_name: str) -> Path:
    name = _safe_component(manager_name)
    return _under_root(root() / "Users" / name / "Invoices" / "To_Approve")


def manager_approved(manager_name: str) -> Path:
    name = _safe_component(manager_name)
    return _under_root(root() / "Users" / name / "Invoices" / "Approved")


def ap_approved_invoices(ap_name: str) -> Path:
    name = _safe_component(ap_name)
    return _under_root(root() / "Users" / name / "Approved_Invoices")


def ap_paid_invoices(ap_name: str) -> Path:
    name = _safe_component(ap_name)
    return _under_root(root() / "Users" / name / "Paid_Invoices")


def strata_plan_folder(plan_raw: str) -> Path:
    """Final archive folder for a paid invoice."""
    name = _safe_component(plan_raw)
    return _under_root(root() / "Strata_Plans" / name)


def strata_plan_processed_month(plan_raw: str, year: int, month: int) -> Path:
    """Per-month archive subfolder used by Step 7 to park source PDFs and the summary."""
    name = _safe_component(plan_raw)
    month_folder = f"{month:02d} - {calendar.month_name[month]}"
    return _under_root(
        root() / "Strata_Plans" / name / "Processed" / f"{year:04d}" / month_folder
    )


def monthly_aggregations_csv() -> Path:
    """Step 7's append-only audit ledger. Auto-created on first run."""
    return root() / "_state" / "monthly_aggregations.csv"


def invoice_fingerprints_csv() -> Path:
    """Duplicate-detection ledger. One row per unique invoice fingerprint.

    Upserted in place (not append-only) — see tools/_lib/dup_ledger.py.
    Auto-created on first detection.
    """
    return root() / "_state" / "invoice_fingerprints.csv"


# State / history paths (used by Step 4 and Step 5 for old/new diffs)

def toapprove_history_dir() -> Path:
    return root() / "_state" / "toapprove_history"


def toapprove_history_file(date_str: str, manager_key: str) -> Path:
    """Legacy combined history file. New code uses the split variants below."""
    return toapprove_history_dir() / f"{date_str}__{manager_key}.xls"


def toapprove_scanned_file(date_str: str, manager_key: str) -> Path:
    """What Step 4 actually saw today. Written unconditionally for diagnostics."""
    return toapprove_history_dir() / f"{date_str}__{manager_key}__scanned.xls"


def toapprove_notified_file(date_str: str, manager_key: str) -> Path:
    """What Step 4 successfully emailed about. Drives the next run's old/new diff."""
    return toapprove_history_dir() / f"{date_str}__{manager_key}__notified.xls"


def ap_approved_history_dir() -> Path:
    return root() / "_state" / "ap_approved_history"


def ap_approved_baseline_file(ap_key: str) -> Path:
    """Legacy rolling baseline file. New code uses the split variants below."""
    return ap_approved_history_dir() / f"_latest__{ap_key}.xls"


def ap_approved_scanned_baseline_file(ap_key: str) -> Path:
    """Rolling 'what Step 5 saw' baseline for the AP's Approved_Invoices folder."""
    return ap_approved_history_dir() / f"_latest__{ap_key}__scanned.xls"


def ap_approved_notified_baseline_file(ap_key: str) -> Path:
    """Rolling 'what Step 5 successfully notified about' baseline. Drives the diff."""
    return ap_approved_history_dir() / f"_latest__{ap_key}__notified.xls"


def ap_approved_history_file(date_str: str, ap_key: str) -> Path:
    return ap_approved_history_dir() / f"{date_str}__{ap_key}.xls"
