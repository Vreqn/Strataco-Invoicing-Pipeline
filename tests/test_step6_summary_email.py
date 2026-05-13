"""Unit tests for Step 6's combined "Invoices summary" email builder.

Covers the new Action Required section that consolidates four sources:
  - Paid-invoices stuck (Step 6's own archive failures)
  - Manager approvals stuck (Step 5 didn't drain)
  - Unmatched intake files (Steps 1/2/3 couldn't route)
  - Inbox emails (unhandled, from the live Graph query)

Plus the legacy Processed / Duplicates sections (regression).

Standalone: no pytest dependency. Run with `python tests/test_step6_summary_email.py`.
Exits 0 if every case passes, 1 otherwise.
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


FAILED: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")
        FAILED.append(name)


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
    print("test_all_empty")
    subject, body = _build()

    _check(
        "subject_format",
        subject == "Invoices summary — 0 processed, 0 action required, 0 duplicate — 2026-05-12",
        f"got: {subject!r}",
    )
    _check("top_header", "Invoices summary — 2026-05-12" in body)
    _check("processed_header", "== Processed (0) ==" in body)
    _check("action_header", "== Action Required (0) ==" in body)
    _check("duplicates_header", "== Duplicates (0) ==" in body)
    _check(
        "three_none_today_lines",
        body.count("None today.") == 3,
        f"body:\n{body}",
    )
    _check("no_subsection_header_leak", "-- Paid invoices stuck" not in body)


def test_all_populated() -> None:
    print("test_all_populated")
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

    _check(
        "subject_counts",
        subject == "Invoices summary — 2 processed, 4 action required, 1 duplicate — 2026-05-12",
        f"got: {subject!r}",
    )
    _check("processed_header_count", "== Processed (2) ==" in body)
    _check("action_header_count", "== Action Required (4) ==" in body)
    _check("duplicates_header_count", "== Duplicates (1) ==" in body)

    _check("subhdr_paid_failed", "-- Paid invoices stuck (Step 6 couldn't archive) (1) --" in body)
    _check(
        "subhdr_manager_stuck",
        "-- Manager approvals stuck (Step 5 didn't pick up) (1) --" in body,
    )
    _check(
        "subhdr_unmatched_intake",
        "-- Unmatched intake files (Steps 1/2/3 couldn't route) (1) --" in body,
    )
    _check("subhdr_inbox", "-- Inbox emails (unhandled) (1) --" in body)

    _check("paid_failed_row_filename", "Bad.pdf" in body)
    _check("manager_stuck_manager_line", "Manager: Alice Manager" in body)
    _check("unmatched_intake_filename", "scan_2026-05-12.pdf" in body)
    _check("inbox_from_line", "From:    Vendor Inc <billing@vendor.example>" in body)
    _check("inbox_msg_id", "Msg id:  AAMkAD-stuck-1" in body)

    _check("no_none_today", "None today." not in body, "an empty-section sentinel leaked")


def test_only_processed_and_duplicates() -> None:
    """Action Required empty -> single 'None today.' under that section."""
    print("test_only_processed_and_duplicates")
    subject, body = _build(
        processed=[_processed_row()],
        duplicates=[_fp_row()],
    )
    _check(
        "subject_partial",
        subject == "Invoices summary — 1 processed, 0 action required, 1 duplicate — 2026-05-12",
        f"got: {subject!r}",
    )
    _check("processed_header_1", "== Processed (1) ==" in body)
    _check("action_header_0", "== Action Required (0) ==" in body)
    _check("duplicates_header_1", "== Duplicates (1) ==" in body)
    _check(
        "single_none_today",
        body.count("None today.") == 1,
        f"expected 1, got {body.count('None today.')}",
    )
    _check("no_subsection_headers", "-- Paid invoices stuck" not in body)


def test_paid_failed_minimal_fields() -> None:
    """When apName/localPath/mtimeIso aren't on a paid_failed row, those lines
    must not render. Guards conditional rendering for degraded rows."""
    print("test_paid_failed_minimal_fields")
    _, body = _build(
        paid_failed=[_paid_failed_row(file_name="X.pdf", reason="no plan")],
    )
    paid_block = body.split("-- Paid invoices stuck")[1].split("==")[0]
    _check("no_ap_line", "AP:" not in paid_block)
    _check("no_path_line", "Path:" not in paid_block)
    _check("no_stuck_since_line", "Stuck since:" not in paid_block)
    _check("filename_present", "X.pdf" in body)
    _check("reason_present", "Reason: no plan" in body)


def test_only_manager_stuck() -> None:
    print("test_only_manager_stuck")
    subject, body = _build(manager_stuck=[_manager_stuck_row()])
    _check("subject_counts", "1 action required" in subject)
    _check("action_header", "== Action Required (1) ==" in body)
    _check("subhdr_only_manager", "-- Manager approvals stuck" in body)
    _check("no_paid_subhdr", "-- Paid invoices stuck" not in body)
    _check("no_unmatched_intake_subhdr", "-- Unmatched intake" not in body)
    _check("no_inbox_subhdr", "-- Inbox emails" not in body)
    _check("manager_line", "Manager: Alice Manager" in body)
    _check("stuck_since_line", "Stuck since: 2026-05-11 16:22" in body)


def test_only_unmatched_intake() -> None:
    print("test_only_unmatched_intake")
    _, body = _build(unmatched_intake=[_unmatched_intake_row()])
    _check("subhdr", "-- Unmatched intake files (Steps 1/2/3 couldn't route) (1) --" in body)
    intake_block = body.split("-- Unmatched intake")[1].split("==")[0]
    _check("no_manager_line", "Manager:" not in intake_block)
    _check("no_reason_line", "Reason:" not in intake_block)
    _check("path_line", r"Path: D:\Strataco\_Unmatched\Invoices\scan_2026-05-12.pdf" in body)


def test_only_inbox_messages() -> None:
    print("test_only_inbox_messages")
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
    _check("subject_2", "2 action required" in subject)
    _check("subhdr_inbox", "-- Inbox emails (unhandled) (2) --" in body)
    _check("numbered_1", "1. From:" in body)
    _check("numbered_2", "2. From:" in body)


def test_inbox_error_renders_degraded() -> None:
    """When Graph fails, render the 'query failed' notice AND count it as +1 in
    the action total. Subject saying '0 action required' while the body asks
    the operator to open Outlook would be a lie — that was Codex's RISK 1."""
    print("test_inbox_error_renders_degraded")
    subject, body = _build(
        inbox_error="HTTPError 503: service unavailable",
    )
    _check(
        "subject_one_action",
        "1 action required" in subject,
        f"inbox_error must increment action_count; got: {subject!r}",
    )
    _check("degraded_subhdr", "-- Inbox emails (unhandled) (query failed) --" in body)
    _check("error_line", "Inbox query failed: HTTPError 503: service unavailable" in body)
    _check("operator_hint", "open Outlook directly" in body)
    _check(
        "no_clean_none_today_under_action",
        body.split("== Action Required")[1].split("==")[0].count("None today.") == 0,
        "the inbox-error sub-section should suppress 'None today.' under Action Required",
    )


def test_inbox_error_with_other_action_items() -> None:
    """Action count includes other sources PLUS the inbox_error when Graph fails."""
    print("test_inbox_error_with_other_action_items")
    subject, body = _build(
        manager_stuck=[_manager_stuck_row()],
        inbox_error="auth failed",
    )
    _check(
        "subject_two",
        "2 action required" in subject,
        f"1 stuck manager + 1 inbox_error = 2; got: {subject!r}",
    )
    _check("manager_subhdr", "-- Manager approvals stuck" in body)
    _check("inbox_degraded", "-- Inbox emails (unhandled) (query failed) --" in body)


def test_scan_errors_subsection_renders() -> None:
    """scan_errors populate a dedicated sub-section AND count toward action_count."""
    print("test_scan_errors_subsection_renders")
    errs = [
        "_Unmatched/Invoices scan failed: PermissionError(13)",
        "manager Alice: PermissionError(13)",
    ]
    subject, body = _build(scan_errors=errs)
    _check(
        "subject_two_action",
        "2 action required" in subject,
        f"two scan errors should count as 2; got: {subject!r}",
    )
    _check(
        "scan_errors_subhdr",
        "-- Pipeline scan errors (investigate folder permissions) (2) --" in body,
    )
    _check("first_error_rendered", "1. _Unmatched/Invoices scan failed" in body)
    _check("second_error_rendered", "2. manager Alice" in body)
    _check(
        "no_none_today",
        "None today." not in body.split("== Action Required")[1].split("==")[0],
    )


def test_scan_errors_with_other_action_items() -> None:
    """scan_errors integrate with other Action Required sources cleanly."""
    print("test_scan_errors_with_other_action_items")
    subject, body = _build(
        manager_stuck=[_manager_stuck_row()],
        scan_errors=["_Unmatched/Invoices scan failed: PermissionError"],
    )
    _check(
        "subject_two_action",
        "2 action required" in subject,
        f"1 manager + 1 scan error = 2; got: {subject!r}",
    )
    _check("manager_subhdr", "-- Manager approvals stuck" in body)
    _check("scan_errors_subhdr", "-- Pipeline scan errors" in body)


def test_clean_day_renders_single_none_today() -> None:
    """Explicit assertion that with empty scan_errors and clean Inbox, exactly
    one 'None today.' appears under Action Required (and the others stay where
    they were before — under Processed and Duplicates)."""
    print("test_clean_day_renders_single_none_today")
    subject, body = _build()  # all empty, no errors
    _check("zero_action", "0 action required" in subject)
    action_block = body.split("== Action Required")[1].split("== Duplicates")[0]
    _check(
        "exactly_one_none_today_in_action_block",
        action_block.count("None today.") == 1,
        f"got: {action_block.count('None today.')}",
    )
    _check("three_none_today_total", body.count("None today.") == 3)


def main() -> int:
    test_all_empty()
    test_all_populated()
    test_only_processed_and_duplicates()
    test_paid_failed_minimal_fields()
    test_only_manager_stuck()
    test_only_unmatched_intake()
    test_only_inbox_messages()
    test_inbox_error_renders_degraded()
    test_inbox_error_with_other_action_items()
    test_scan_errors_subsection_renders()
    test_scan_errors_with_other_action_items()
    test_clean_day_renders_single_none_today()
    if FAILED:
        print(f"\nFAILED ({len(FAILED)}):")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
