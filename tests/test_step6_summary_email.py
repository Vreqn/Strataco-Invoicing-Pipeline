"""Unit tests for Step 6's combined "Invoices summary" email builder.

Covers the new Action Required section that consolidates four sources:
  - Paid-invoices stuck (Step 6's own archive failures)
  - Manager approvals stuck (Step 5 didn't drain)
  - Unmatched intake files (Steps 1/2/3 couldn't route)
  - Inbox emails (unhandled, from the live Graph query)

Plus the legacy Processed / Duplicates sections (regression).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from steps.step_6_paid_archive import _build_combined_summary_email
from tools._lib.dup_ledger import FingerprintRow


def _fp_row(
    *,
    sha256: str = "a" * 64,
    plan_norm: str = "BCS 1234",
    invoice_number: str = "INV-001",
    amount_cents: int | None = 12345,
    archive_path: str = "Strata_Plans/BCS 1234/000123 - vendor.pdf",
    current_stage: str = "archived",
    dup_count: int = 1,
) -> FingerprintRow:
    return FingerprintRow(
        first_seen_date="2026-04-30",
        sha256=sha256,
        plan_norm=plan_norm,
        invoice_number=invoice_number,
        amount_cents=amount_cents,
        archive_path=archive_path,
        current_stage=current_stage,
        last_seen_date="2026-05-12",
        dup_count=dup_count,
        last_dup_date="2026-05-12",
    )


def _processed_row(
    *,
    ap_name: str = "Attila",
    file_name: str = "000123 - ABC Plumbing.pdf",
    status: str = "archived",
    plan_raw: str = "BCS 1234",
    check_number: str = "000123",
) -> dict:
    return {
        "apName": ap_name,
        "fileName": file_name,
        "status": status,
        "planRaw": plan_raw,
        "checkNumber": check_number,
    }


def _paid_failed_row(
    *,
    file_name: str = "Random.pdf",
    reason: str = "plan not found",
    plan_key: str = "",
    ap_name: str = "",
    local_path: str = "",
    mtime_iso: str = "",
) -> dict:
    row: dict = {"fileName": file_name, "reason": reason}
    if plan_key:
        row["planKey"] = plan_key
    if ap_name:
        row["apName"] = ap_name
    if local_path:
        row["localPath"] = local_path
    if mtime_iso:
        row["mtimeIso"] = mtime_iso
    return row


def _manager_stuck_row(
    *,
    file_name: str = "BCS1234 ABC Plumbing.pdf",
    manager_name: str = "Alice Manager",
    local_path: str = r"D:\Strataco\Users\Alice Manager\Invoices\Approved\BCS1234 ABC Plumbing.pdf",
    mtime_iso: str = "2026-05-11 16:22",
) -> dict:
    return {
        "fileName": file_name,
        "managerName": manager_name,
        "localPath": local_path,
        "mtimeIso": mtime_iso,
    }


def _unmatched_intake_row(
    *,
    file_name: str = "scan_2026-05-12.pdf",
    local_path: str = r"D:\Strataco\_Unmatched\Invoices\scan_2026-05-12.pdf",
    mtime_iso: str = "2026-05-12 06:05",
) -> dict:
    return {
        "fileName": file_name,
        "localPath": local_path,
        "mtimeIso": mtime_iso,
    }


def _inbox_msg(
    *,
    msg_id: str = "AAMkAD-stuck-1",
    subject: str = "Invoice attached",
    sender_name: str = "Vendor Inc",
    sender_address: str = "billing@vendor.example",
    received: str = "2026-05-12T05:40:00Z",
    has_attachments: bool = True,
) -> dict:
    return {
        "id": msg_id,
        "subject": subject,
        "from": {"emailAddress": {"name": sender_name, "address": sender_address}},
        "receivedDateTime": received,
        "hasAttachments": has_attachments,
    }


def _build(
    *,
    today: str = "2026-05-12",
    processed: list[dict] | None = None,
    paid_failed: list[dict] | None = None,
    manager_stuck: list[dict] | None = None,
    unmatched_intake: list[dict] | None = None,
    inbox_messages: list[dict] | None = None,
    inbox_error: str | None = None,
    scan_errors: list[str] | None = None,
    duplicates: list[FingerprintRow] | None = None,
) -> tuple[str, str]:
    return _build_combined_summary_email(
        today,
        processed or [],
        paid_failed or [],
        manager_stuck or [],
        unmatched_intake or [],
        inbox_messages or [],
        inbox_error,
        scan_errors or [],
        duplicates or [],
    )


def test_all_empty() -> None:
    subject, body = _build()

    assert subject == "Invoices summary — 0 processed, 0 action required, 0 duplicate — 2026-05-12", (
        f"subject_format: got: {subject!r}"
    )
    assert "Invoices summary — 2026-05-12" in body, "top_header"
    assert "== Processed (0) ==" in body, "processed_header"
    assert "== Action Required (0) ==" in body, "action_header"
    assert "== Duplicates (0) ==" in body, "duplicates_header"
    assert body.count("None today.") == 3, f"three_none_today_lines:\n{body}"
    assert "-- Paid invoices stuck" not in body, "no_subsection_header_leak"


def test_all_populated() -> None:
    processed = [
        _processed_row(),
        _processed_row(
            ap_name="Sarah",
            file_name="000124 - XYZ Cleaning.pdf",
            check_number="000124",
            plan_raw="BCS 5678",
        ),
    ]
    paid_failed = [
        _paid_failed_row(
            file_name="Bad.pdf",
            reason="no plan",
            ap_name="Attila",
            local_path=r"D:\Strataco\Attila\Paid_Invoices\Bad.pdf",
            mtime_iso="2026-05-12 06:55",
        ),
    ]
    manager_stuck = [_manager_stuck_row()]
    unmatched_intake = [_unmatched_intake_row()]
    inbox_messages = [_inbox_msg()]
    duplicates = [_fp_row()]

    subject, body = _build(
        processed=processed,
        paid_failed=paid_failed,
        manager_stuck=manager_stuck,
        unmatched_intake=unmatched_intake,
        inbox_messages=inbox_messages,
        duplicates=duplicates,
    )

    assert subject == "Invoices summary — 2 processed, 4 action required, 1 duplicate — 2026-05-12", (
        f"subject_counts: got: {subject!r}"
    )
    assert "== Processed (2) ==" in body, "processed_header_count"
    assert "== Action Required (4) ==" in body, "action_header_count"
    assert "== Duplicates (1) ==" in body, "duplicates_header_count"

    assert "-- Paid invoices stuck (Step 6 couldn't archive) (1) --" in body, "subhdr_paid_failed"
    assert "-- Manager approvals stuck (Step 5 didn't pick up) (1) --" in body, "subhdr_manager_stuck"
    assert "-- Unmatched intake files (Steps 1/2/3 couldn't route) (1) --" in body, (
        "subhdr_unmatched_intake"
    )
    assert "-- Inbox emails (unhandled) (1) --" in body, "subhdr_inbox"

    assert "Bad.pdf" in body, "paid_failed_row_filename"
    assert "Manager: Alice Manager" in body, "manager_stuck_manager_line"
    assert "scan_2026-05-12.pdf" in body, "unmatched_intake_filename"
    assert "From:    Vendor Inc <billing@vendor.example>" in body, "inbox_from_line"
    assert "Msg id:  AAMkAD-stuck-1" in body, "inbox_msg_id"

    assert "None today." not in body, "no_none_today: an empty-section sentinel leaked"


def test_only_processed_and_duplicates() -> None:
    """Action Required empty -> single 'None today.' under that section."""
    subject, body = _build(
        processed=[_processed_row()],
        duplicates=[_fp_row()],
    )
    assert subject == "Invoices summary — 1 processed, 0 action required, 1 duplicate — 2026-05-12", (
        f"subject_partial: got: {subject!r}"
    )
    assert "== Processed (1) ==" in body, "processed_header_1"
    assert "== Action Required (0) ==" in body, "action_header_0"
    assert "== Duplicates (1) ==" in body, "duplicates_header_1"
    assert body.count("None today.") == 1, (
        f"single_none_today: expected 1, got {body.count('None today.')}"
    )
    assert "-- Paid invoices stuck" not in body, "no_subsection_headers"


def test_paid_failed_minimal_fields() -> None:
    """When apName/localPath/mtimeIso aren't on a paid_failed row, those lines
    must not render. Guards conditional rendering for degraded rows."""
    _, body = _build(
        paid_failed=[_paid_failed_row(file_name="X.pdf", reason="no plan")],
    )
    paid_block = body.split("-- Paid invoices stuck")[1].split("==")[0]
    assert "AP:" not in paid_block, "no_ap_line"
    assert "Path:" not in paid_block, "no_path_line"
    assert "Stuck since:" not in paid_block, "no_stuck_since_line"
    assert "X.pdf" in body, "filename_present"
    assert "Reason: no plan" in body, "reason_present"


def test_only_manager_stuck() -> None:
    subject, body = _build(manager_stuck=[_manager_stuck_row()])
    assert "1 action required" in subject, "subject_counts"
    assert "== Action Required (1) ==" in body, "action_header"
    assert "-- Manager approvals stuck" in body, "subhdr_only_manager"
    assert "-- Paid invoices stuck" not in body, "no_paid_subhdr"
    assert "-- Unmatched intake" not in body, "no_unmatched_intake_subhdr"
    assert "-- Inbox emails" not in body, "no_inbox_subhdr"
    assert "Manager: Alice Manager" in body, "manager_line"
    assert "Stuck since: 2026-05-11 16:22" in body, "stuck_since_line"


def test_only_unmatched_intake() -> None:
    _, body = _build(unmatched_intake=[_unmatched_intake_row()])
    assert "-- Unmatched intake files (Steps 1/2/3 couldn't route) (1) --" in body, "subhdr"
    intake_block = body.split("-- Unmatched intake")[1].split("==")[0]
    assert "Manager:" not in intake_block, "no_manager_line"
    assert "Reason:" not in intake_block, "no_reason_line"
    assert r"Path: D:\Strataco\_Unmatched\Invoices\scan_2026-05-12.pdf" in body, "path_line"


def test_only_inbox_messages() -> None:
    msgs = [
        _inbox_msg(),
        _inbox_msg(
            msg_id="m2",
            subject="(no plan in subject)",
            sender_name="Other",
            sender_address="other@vendor2.com",
        ),
    ]
    subject, body = _build(inbox_messages=msgs)
    assert "2 action required" in subject, "subject_2"
    assert "-- Inbox emails (unhandled) (2) --" in body, "subhdr_inbox"
    assert "1. From:" in body, "numbered_1"
    assert "2. From:" in body, "numbered_2"


def test_inbox_error_renders_degraded() -> None:
    """When Graph fails, render the 'query failed' notice AND count it as +1 in
    the action total. Subject saying '0 action required' while the body asks
    the operator to open Outlook would be a lie — that was Codex's RISK 1."""
    subject, body = _build(
        inbox_error="HTTPError 503: service unavailable",
    )
    assert "1 action required" in subject, (
        f"subject_one_action: inbox_error must increment action_count; got: {subject!r}"
    )
    assert "-- Inbox emails (unhandled) (query failed) --" in body, "degraded_subhdr"
    assert "Inbox query failed: HTTPError 503: service unavailable" in body, "error_line"
    assert "open Outlook directly" in body, "operator_hint"
    action_section = body.split("== Action Required")[1].split("==")[0]
    assert action_section.count("None today.") == 0, (
        "no_clean_none_today_under_action: the inbox-error sub-section should suppress 'None today.'"
    )


def test_inbox_error_with_other_action_items() -> None:
    """Action count includes other sources PLUS the inbox_error when Graph fails."""
    subject, body = _build(
        manager_stuck=[_manager_stuck_row()],
        inbox_error="auth failed",
    )
    assert "2 action required" in subject, (
        f"subject_two: 1 stuck manager + 1 inbox_error = 2; got: {subject!r}"
    )
    assert "-- Manager approvals stuck" in body, "manager_subhdr"
    assert "-- Inbox emails (unhandled) (query failed) --" in body, "inbox_degraded"


def test_scan_errors_subsection_renders() -> None:
    """scan_errors populate a dedicated sub-section AND count toward action_count."""
    errs = [
        "_Unmatched/Invoices scan failed: PermissionError(13)",
        "manager Alice: PermissionError(13)",
    ]
    subject, body = _build(scan_errors=errs)
    assert "2 action required" in subject, (
        f"subject_two_action: two scan errors should count as 2; got: {subject!r}"
    )
    assert "-- Pipeline scan errors (investigate folder permissions) (2) --" in body, (
        "scan_errors_subhdr"
    )
    assert "1. _Unmatched/Invoices scan failed" in body, "first_error_rendered"
    assert "2. manager Alice" in body, "second_error_rendered"
    action_block = body.split("== Action Required")[1].split("==")[0]
    assert "None today." not in action_block, "no_none_today"


def test_scan_errors_with_other_action_items() -> None:
    """scan_errors integrate with other Action Required sources cleanly."""
    subject, body = _build(
        manager_stuck=[_manager_stuck_row()],
        scan_errors=["_Unmatched/Invoices scan failed: PermissionError"],
    )
    assert "2 action required" in subject, (
        f"subject_two_action: 1 manager + 1 scan error = 2; got: {subject!r}"
    )
    assert "-- Manager approvals stuck" in body, "manager_subhdr"
    assert "-- Pipeline scan errors" in body, "scan_errors_subhdr"


def test_clean_day_renders_single_none_today() -> None:
    """Explicit assertion that with empty scan_errors and clean Inbox, exactly
    one 'None today.' appears under Action Required (and the others stay where
    they were before — under Processed and Duplicates)."""
    subject, body = _build()
    assert "0 action required" in subject, "zero_action"
    action_block = body.split("== Action Required")[1].split("== Duplicates")[0]
    assert action_block.count("None today.") == 1, (
        f"exactly_one_none_today_in_action_block: got: {action_block.count('None today.')}"
    )
    assert body.count("None today.") == 3, "three_none_today_total"
