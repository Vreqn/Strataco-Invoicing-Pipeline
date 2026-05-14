"""Regression test for the 0.11.3 bug — single-PDF emails that match nothing
(subject, body, PDF text, filename) stay in the Inbox **without an Outlook
red flag** because `_process_pdf_text_fallback`'s all-or-nothing branch only
calls `run.review(...)` and never `_flag_message_safely(...)`.

Operator wants every Inbox email that carries PDF-shaped content the system
couldn't fully resolve to get the visible red flag — regardless of whether
the failure was "no plan match", "download failed", or "bytes weren't a real
PDF". ZIP-only and discarded-only emails still pass through silently because
the email either leaves the Inbox (ZIP happy path) or has nothing for the
operator to review (signature PNGs).

Tests exercise `_process_pdf_text_fallback` end-to-end with the Graph layer
mocked. Note: Branch B / Branch D of `main()` (text-only emails with no
attachments) never reach this function, so they are deliberately out of
scope here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail at module load.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

import logging

import steps.step_1_intake as s1
from tools._lib import dup_ledger
from tools._lib.log import _Run
from tools._lib.xls import PlanRow


_PDF_BYTES = b"%PDF-1.4\n%fake stub bytes for testing\n%%EOF\n"
_NON_PDF_BYTES = b"\x89PNG\r\n\x1a\nfake png bytes"
_MSG_ID = "msg-id-abc123"
_SUBJECT = "Some random subject — please process"
_PROCESSED_FOLDER = "processed-folder-id"
_DUP_FOLDER = "duplicates-folder-id"


def _row(plan_norm: str, manager: str = "Sue Smith") -> PlanRow:
    return PlanRow(
        plan_norm=plan_norm,
        plan_raw=plan_norm,
        strata_name="",
        address="",
        manager_name=manager,
        manager_key=manager.upper().replace(" ", "_"),
        manager_email="",
        ap_name="Alex AP",
        ap_key="ALEX_AP",
        ap_email="",
        status_active=True,
    )


def _make_run() -> _Run:
    return _Run("test_step1_pdf_fallback_flag", logging.getLogger("strataco.test_pdf_fallback_flag"))


def _empty_ledger(tmp_path: Path) -> dup_ledger.Ledger:
    return dup_ledger.Ledger([], tmp_path / "_state" / "dup_ledger.csv")


def _att(name: str, content_type: str = "application/pdf", att_id: str = "att-1") -> dict:
    return {
        "id": att_id,
        "name": name,
        "contentType": content_type,
        "@odata.type": "#microsoft.graph.fileAttachment",
    }


def _call(monkeypatch, tmp_path: Path, *, rows=None) -> _Run:
    """Drive `_process_pdf_text_fallback` with the common test arguments.

    Returns the `_Run` so callers can inspect `need_review` / `errors`.
    """
    monkeypatch.setenv("STRATACO_ROOT", str(tmp_path))
    run = _make_run()
    s1._process_pdf_text_fallback(
        msg_id=_MSG_ID,
        subject=_SUBJECT,
        received_str="MAY 13 2026",
        sender_domain="vendor.example.com",
        rows=rows if rows is not None else [],
        ledger=_empty_ledger(tmp_path),
        run=run,
        processed_folder_id=_PROCESSED_FOLDER,
        duplicate_folder_id=_DUP_FOLDER,
    )
    return run


def test_unmatched_pdf_sets_red_flag(monkeypatch, tmp_path: Path) -> None:
    """The motivating case: email has one PDF, no plan match anywhere
    (subject/body already failed in `main()`, now PDF text and filename also
    yield nothing). Email stays in Inbox, no disk writes, AND the Outlook
    to-do flag is set so the operator notices it in the daily review.

    BEFORE the fix this test FAILS — `flag_message` is never called.
    """
    attachments = [_att("Random Vendor Invoice.pdf")]

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments) as m_list,
        patch.object(s1.graph, "download_attachment", return_value=_PDF_BYTES) as m_dl,
        patch.object(s1, "extract_full_text", return_value="") as m_text,
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path)

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[unmatched PDF] expected graph.flag_message(msg_id) once; got call list "
        f"{m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    assert m_list.called and m_dl.called and m_text.called
    assert run.need_review, (
        f"[unmatched PDF] expected run.review(...) to log a NEED_REVIEW line; "
        f"need_review={run.need_review}"
    )


def test_download_failure_sets_red_flag(monkeypatch, tmp_path: Path) -> None:
    """Single PDF where the Graph download_attachment call blows up. The email
    still ends up in `_process_pdf_text_fallback`'s all-or-nothing branch
    (no successful PDF -> nothing to route). It MUST get the red flag.
    """
    attachments = [_att("Some Invoice.pdf")]

    def _boom(*_args, **_kwargs):
        raise s1.graph.GraphAPIError("simulated download failure")

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment", side_effect=_boom),
        patch.object(s1, "extract_full_text", return_value=""),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path)

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[download failure] expected flag_message once; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    assert run.errors, (
        f"[download failure] expected run.error(...) with download failure; "
        f"errors={run.errors}"
    )


def test_invalid_pdf_bytes_sets_red_flag(monkeypatch, tmp_path: Path) -> None:
    """Attachment is named `.pdf` but the bytes don't start with `%PDF-`
    (vendor screw-up / mobile scanner glitch). `_is_real_pdf` rejects it;
    no plan match is even attempted. All-or-nothing branch fires — must
    set the red flag so the operator notices.
    """
    attachments = [_att("invoice.pdf")]

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment", return_value=_NON_PDF_BYTES),
        patch.object(s1, "extract_full_text", return_value=""),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path)

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[invalid PDF bytes] expected flag_message once; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()


def test_mixed_matched_and_unmatched_pdfs_sets_red_flag(monkeypatch, tmp_path: Path) -> None:
    """Two PDFs in one email: one matches a plan, one doesn't. Per the
    all-or-nothing rule, NOTHING is routed and the email stays in the Inbox.
    The red flag must be set because there's PDF-shaped content the operator
    needs to review.
    """
    rows = [_row("BCS2707")]
    attachments = [
        _att("matched.pdf", att_id="att-1"),
        _att("unknown.pdf", att_id="att-2"),
    ]

    def _text_for(_msg_id, att_id):
        # extract_full_text is fed (blob), but we drive it by attachment id
        # via a side_effect on download_attachment + a deterministic blob.
        return _PDF_BYTES  # both PDFs have the same stub bytes; text differs below

    text_by_call = iter(["Strata Plan BCS 2707 Invoice #1001", ""])

    def _text_side_effect(_blob):
        return next(text_by_call)

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment", side_effect=_text_for),
        patch.object(s1, "extract_full_text", side_effect=_text_side_effect),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path, rows=rows)

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[mixed PDFs] expected flag_message once; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    # Nothing routed — the matched PDF must NOT have been written to disk.
    manager_dir = tmp_path / "Users" / "Sue Smith" / "Invoices" / "To_Approve"
    assert not manager_dir.exists() or not list(manager_dir.iterdir()), (
        f"[mixed PDFs] all-or-nothing means nothing routed, but found files in "
        f"{manager_dir}: {list(manager_dir.iterdir()) if manager_dir.exists() else []}"
    )


def test_unparseable_zip_only_email_flags_and_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    """Behavior change (2026-05-13 ZIP-orphan fix): ZIPs are no longer saved
    blindly to `_Unmatched/`. Step 1 inspects the bytes in memory via
    `zip_safe.audit_and_extract_pdfs`. Fake/corrupt ZIP bytes raise
    `UnsafeZipError`, which forces the email to stay in the Inbox with the
    Outlook red flag. Nothing should land in `_Unmatched/`.
    """
    attachments = [_att("April invoices.zip", content_type="application/zip", att_id="zip-1")]

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment", return_value=b"PK\x03\x04 fake zip"),
        patch.object(s1, "extract_full_text", return_value=""),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path)

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[unparseable ZIP] expected flag_message once; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    unmatched_dir = tmp_path / "_Unmatched" / "Invoices"
    saved = list(unmatched_dir.glob("*.zip")) if unmatched_dir.exists() else []
    assert not saved, (
        f"[unparseable ZIP] expected NO ZIP saved under _Unmatched/Invoices/; "
        f"found: {[p.name for p in saved]}"
    )


def test_signature_png_only_email_does_not_flag(monkeypatch, tmp_path: Path) -> None:
    """Email whose only attachment is a signature PNG (non-PDF, non-ZIP).
    `_looks_like_pdf_or_zip` rejects it; it's discarded with an INFO log.
    No PDF content was ever seen -> no red flag. Email isn't moved either
    (no actionable content -> `_email_destination` returns None for an empty
    outcomes list, but the all-or-nothing branch returns before reaching the
    move logic anyway).
    """
    attachments = [_att("signature.png", content_type="image/png", att_id="img-1")]

    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment", return_value=_NON_PDF_BYTES) as m_dl,
        patch.object(s1, "extract_full_text", return_value=""),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        run = _call(monkeypatch, tmp_path)

    m_flag.assert_not_called(), (
        f"[signature PNG only] expected flag_message NOT called; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    # The PNG should be discarded at Pass 1 before any download is even attempted.
    m_dl.assert_not_called(), (
        f"[signature PNG only] expected no download (discarded at Pass 1); "
        f"got {m_dl.call_args_list}"
    )
