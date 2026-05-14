"""End-to-end tests for the 2026-05-13 ZIP-orphan fix.

Covers the seven scenarios from the plan: Step 1 now inspects ZIPs in
memory at intake time, so email-originated ZIPs never land in
`_Unmatched/Invoices/`. ZIP-contained PDFs are full participants in
the per-PDF decision matrix.

Both Step 1 entry points are exercised:

  - `_process_self_attachments` (subject-matched path)
  - `_process_pdf_text_fallback` (no-subject-match path)

Graph calls are mocked; `extract_full_text` is mocked so each PDF
returns deterministic, controllable text. ZIPs are built in memory.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail at module load.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

import steps.step_1_intake as s1
from tools._lib import dup_ledger
from tools._lib.log import _Run
from tools._lib.xls import PlanRow


_MSG_ID = "msg-id-zip-test"
_SUBJECT_2707 = "BCS 2707 — April invoices"
_SUBJECT_NOMATCH = "April invoice pack — please process"
_PROCESSED_FOLDER = "processed-folder-id"
_DUP_FOLDER = "duplicates-folder-id"


# ---- fixture helpers --------------------------------------------------------


def _row(plan_norm: str, manager: str) -> PlanRow:
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


def _rows() -> list[PlanRow]:
    return [
        _row("BCS2707", "Sue Smith"),
        _row("BCS2800", "Bob Jones"),
    ]


def _make_run() -> _Run:
    return _Run("test_step1_zip_intake", logging.getLogger("strataco.test_zip_intake"))


def _empty_ledger(tmp_path: Path) -> dup_ledger.Ledger:
    return dup_ledger.Ledger([], tmp_path / "_state" / "dup_ledger.csv")


def _pdf_att(name: str, att_id: str) -> dict:
    return {
        "id": att_id,
        "name": name,
        "contentType": "application/pdf",
        "@odata.type": "#microsoft.graph.fileAttachment",
    }


def _zip_att(name: str, att_id: str) -> dict:
    return {
        "id": att_id,
        "name": name,
        "contentType": "application/zip",
        "@odata.type": "#microsoft.graph.fileAttachment",
    }


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP archive in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _pdf_with_marker(marker: str) -> bytes:
    """Stub PDF bytes that pass `_is_real_pdf` (start with `%PDF-`) and
    embed a deterministic marker so we can wire `extract_full_text` to
    return matching text by-marker without re-deriving from bytes."""
    return f"%PDF-1.4\n%MARKER={marker}\n%%EOF\n".encode("ascii")


def _make_download_side_effect(blob_by_id: dict[str, bytes]):
    def _dl(_msg_id: str, att_id: str) -> bytes:
        if att_id not in blob_by_id:
            raise KeyError(f"download_attachment called with unexpected att_id={att_id!r}")
        return blob_by_id[att_id]
    return _dl


def _make_extract_text_side_effect(text_by_blob: dict[bytes, str]):
    def _et(blob: bytes) -> str:
        return text_by_blob.get(blob, "")
    return _et


def _manager_folder(tmp_path: Path, manager: str) -> Path:
    return tmp_path / "Users" / manager / "Invoices" / "To_Approve"


def _unmatched_dir(tmp_path: Path) -> Path:
    return tmp_path / "_Unmatched" / "Invoices"


# ---- subject-matched path (`_process_self_attachments`) ---------------------


def _run_self(monkeypatch, tmp_path: Path, *, attachments, blob_by_id, text_by_blob, plan_norm: str):
    """Drive `_process_self_attachments` with the subject plan already chosen."""
    monkeypatch.setenv("STRATACO_ROOT", str(tmp_path))
    rows = _rows()
    plan_row = next(r for r in rows if r.plan_norm == plan_norm)
    run = _make_run()
    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment",
                     side_effect=_make_download_side_effect(blob_by_id)),
        patch.object(s1, "extract_full_text",
                     side_effect=_make_extract_text_side_effect(text_by_blob)),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        s1._process_self_attachments(
            msg_id=_MSG_ID,
            subject=_SUBJECT_2707,
            plan_row=plan_row,
            match_source="subject",
            received_str="MAY 13 2026",
            sender_domain="vendor.example.com",
            rows=rows,
            ledger=_empty_ledger(tmp_path),
            run=run,
            processed_folder_id=_PROCESSED_FOLDER,
            duplicate_folder_id=_DUP_FOLDER,
        )
    return run, m_flag, m_move


def test_subject_matched_zip_contents_agree_routes_all_pdfs(monkeypatch, tmp_path: Path) -> None:
    """Scenario 1: subject says BCS 2707, ZIP contents independently identify
    BCS 2707 in their text. All contained PDFs should route to Sue Smith's
    To_Approve folder; the email should move to processed_emails; nothing
    should land in `_Unmatched/Invoices/`."""
    pdf_a = _pdf_with_marker("agree-A")
    pdf_b = _pdf_with_marker("agree-B")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("inv_002.pdf", pdf_b),
    ])
    attachments = [_zip_att("April invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {
        pdf_a: "Property: BCS 2707 — Building A — Invoice 1001",
        pdf_b: "Property: BCS 2707 — Building A — Invoice 1002",
    }

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_not_called(), (
        f"[ZIP-contents-AGREE] expected NO flag; got {m_flag.call_args_list}"
    )
    m_move.assert_called_once_with(_MSG_ID, _PROCESSED_FOLDER), (
        f"[ZIP-contents-AGREE] expected move to processed; got {m_move.call_args_list}"
    )
    mgr = _manager_folder(tmp_path, "Sue Smith")
    routed = sorted(p.name for p in mgr.iterdir()) if mgr.exists() else []
    assert len(routed) == 2, (
        f"[ZIP-contents-AGREE] expected 2 routed PDFs under {mgr}; got {routed}"
    )
    # ZIP base name should appear as a prefix in routed filenames (audit trail).
    assert all("April invoices__inv_" in name for name in routed), (
        f"[ZIP-contents-AGREE] expected zipbase__inner prefix in routed names; got {routed}"
    )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir()), (
        f"[ZIP-contents-AGREE] expected _Unmatched empty; got {list(unmatched.iterdir())}"
    )


def test_subject_matched_zip_contents_silent_routes_via_strict_first(monkeypatch, tmp_path: Path) -> None:
    """Scenario 2 (mode-2 failure mode): subject says BCS 2707, ZIP contents
    have NO extractable plan text (scanned PDFs / empty text layer / silent).
    Strict-first matrix says EMPTY classifications trust the subject, so all
    contained PDFs route to BCS 2707. Nothing in _Unmatched.

    This is the case that used to silently orphan PDFs in `_Unmatched/`
    under the old Step 1+2+3 pipeline."""
    pdf_a = _pdf_with_marker("silent-A")
    pdf_b = _pdf_with_marker("silent-B")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("inv_002.pdf", pdf_b),
    ])
    attachments = [_zip_att("April invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_a: "", pdf_b: ""}  # silent — extract returns ""

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_not_called(), (
        f"[ZIP-contents-EMPTY] expected NO flag; got {m_flag.call_args_list}"
    )
    m_move.assert_called_once_with(_MSG_ID, _PROCESSED_FOLDER), (
        f"[ZIP-contents-EMPTY] expected move to processed; got {m_move.call_args_list}"
    )
    mgr = _manager_folder(tmp_path, "Sue Smith")
    routed = sorted(p.name for p in mgr.iterdir()) if mgr.exists() else []
    assert len(routed) == 2, (
        f"[ZIP-contents-EMPTY] expected 2 routed PDFs (strict-first EMPTY trusts subject); "
        f"got {routed}"
    )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir()), (
        f"[ZIP-contents-EMPTY] expected _Unmatched empty; got {list(unmatched.iterdir())}"
    )


def test_subject_matched_zip_contents_clash_flags_email(monkeypatch, tmp_path: Path) -> None:
    """Scenario 3: subject says BCS 2707, contained PDF says BCS 2800 in its
    text. Strict-first → flag the email, write nothing."""
    pdf_clash = _pdf_with_marker("clash-2800")
    zip_bytes = _build_zip([("inv_001.pdf", pdf_clash)])
    attachments = [_zip_att("April invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_clash: "Property: BCS 2800 — different building"}

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[ZIP-content-CLASH] expected flag; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    mgr_a = _manager_folder(tmp_path, "Sue Smith")
    mgr_b = _manager_folder(tmp_path, "Bob Jones")
    for mgr in (mgr_a, mgr_b):
        assert not mgr.exists() or not list(mgr.iterdir()), (
            f"[ZIP-content-CLASH] expected NO routed PDFs; found in {mgr}: "
            f"{list(mgr.iterdir()) if mgr.exists() else []}"
        )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir())


def test_subject_matched_zip_with_docx_flags_email(monkeypatch, tmp_path: Path) -> None:
    """Scenario 7: ZIP contains a `.docx` alongside PDFs. Step 1 has no Word-
    doc inspector, so the strictest interpretation of SSOT applies: force the
    email to stay in the Inbox flagged. Nothing on disk, including the PDFs
    that would otherwise have routed."""
    pdf_a = _pdf_with_marker("zip-with-docx-pdf")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("cover_letter.docx", b"PK fake docx bytes"),
    ])
    attachments = [_zip_att("Mixed.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_a: "BCS 2707 invoice content"}

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[ZIP-with-docx] expected flag; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    mgr = _manager_folder(tmp_path, "Sue Smith")
    assert not mgr.exists() or not list(mgr.iterdir()), (
        f"[ZIP-with-docx] expected NO routed PDFs; got "
        f"{list(mgr.iterdir()) if mgr.exists() else []}"
    )


def test_subject_matched_zip_with_txt_companion_routes_pdfs(monkeypatch, tmp_path: Path) -> None:
    """A `.txt` companion file inside the ZIP must NOT poison it. Real-world
    case: TELUS Bill Analyzer emails carry a ZIP with the invoice PDF plus a
    `manifest.txt`. The `.txt` is skipped, the PDF routes to the manager's
    To_Approve folder, the email moves to processed_emails, nothing is
    flagged. Regression for the 7-error 0.14.1 run on 2026-05-13."""
    pdf_a = _pdf_with_marker("zip-with-txt-pdf")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("manifest.txt", b"plan summary text, not an invoice"),
    ])
    attachments = [_zip_att("TELUS Bill Analyzer.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_a: "BCS 2707 invoice content"}

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_not_called(), (
        f"[ZIP-with-txt] expected NO flag; got {m_flag.call_args_list}"
    )
    m_move.assert_called_once_with(_MSG_ID, _PROCESSED_FOLDER), (
        f"[ZIP-with-txt] expected move to processed; got {m_move.call_args_list}"
    )
    mgr = _manager_folder(tmp_path, "Sue Smith")
    routed = sorted(p.name for p in mgr.iterdir()) if mgr.exists() else []
    assert len(routed) == 1, (
        f"[ZIP-with-txt] expected 1 routed PDF under {mgr}; got {routed}"
    )
    assert "TELUS Bill Analyzer__inv_001" in routed[0], (
        f"[ZIP-with-txt] expected zipbase__inner prefix in routed name; got {routed}"
    )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir()), (
        f"[ZIP-with-txt] expected _Unmatched empty; got {list(unmatched.iterdir())}"
    )


def test_subject_matched_bomb_zip_flags_email(monkeypatch, tmp_path: Path) -> None:
    """Scenario 6: bomb/oversized ZIP — UnsafeZipError surfaces, email flags,
    nothing on disk. Lower ZIP_MAX_ENTRIES via env so we don't need a real
    bomb on disk to trigger the check."""
    monkeypatch.setenv("ZIP_MAX_ENTRIES", "2")
    # Build a 5-entry ZIP, exceeds the cap.
    entries = [(f"inv_{i:03d}.pdf", _pdf_with_marker(f"bomb-{i}")) for i in range(5)]
    zip_bytes = _build_zip(entries)
    attachments = [_zip_att("Huge.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {}

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[bomb-ZIP] expected flag; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir())


# ---- no-subject-match path (`_process_pdf_text_fallback`) -------------------


def _run_fallback(monkeypatch, tmp_path: Path, *, attachments, blob_by_id, text_by_blob, rows=None):
    """Drive `_process_pdf_text_fallback` (no subject match)."""
    monkeypatch.setenv("STRATACO_ROOT", str(tmp_path))
    run = _make_run()
    with (
        patch.object(s1.graph, "list_attachments", return_value=attachments),
        patch.object(s1.graph, "download_attachment",
                     side_effect=_make_download_side_effect(blob_by_id)),
        patch.object(s1, "extract_full_text",
                     side_effect=_make_extract_text_side_effect(text_by_blob)),
        patch.object(s1.graph, "flag_message") as m_flag,
        patch.object(s1.graph, "move_message_to_folder") as m_move,
    ):
        s1._process_pdf_text_fallback(
            msg_id=_MSG_ID,
            subject=_SUBJECT_NOMATCH,
            received_str="MAY 13 2026",
            sender_domain="vendor.example.com",
            rows=rows if rows is not None else _rows(),
            ledger=_empty_ledger(tmp_path),
            run=run,
            processed_folder_id=_PROCESSED_FOLDER,
            duplicate_folder_id=_DUP_FOLDER,
        )
    return run, m_flag, m_move


def test_no_subject_match_zip_contents_identify_plan_routes(monkeypatch, tmp_path: Path) -> None:
    """Scenario 4 (mode-1 resolved cleanly): no subject signal, but the ZIP
    contains PDFs whose text clearly says BCS 2707. Step 1's fallback path
    inspects the ZIP, finds the plan in the contained PDFs, and routes them.
    Email moves to processed_emails. Nothing in _Unmatched."""
    pdf_a = _pdf_with_marker("fallback-A")
    pdf_b = _pdf_with_marker("fallback-B")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("inv_002.pdf", pdf_b),
    ])
    attachments = [_zip_att("April invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {
        pdf_a: "Strata Plan BCS 2707 — invoice details",
        pdf_b: "Strata Plan BCS 2707 — second invoice",
    }

    run, m_flag, m_move = _run_fallback(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id, text_by_blob=text_by_blob,
    )

    m_flag.assert_not_called(), (
        f"[no-subject ZIP routable] expected NO flag; got {m_flag.call_args_list}"
    )
    m_move.assert_called_once_with(_MSG_ID, _PROCESSED_FOLDER), (
        f"[no-subject ZIP routable] expected move to processed; got {m_move.call_args_list}"
    )
    mgr = _manager_folder(tmp_path, "Sue Smith")
    routed = sorted(p.name for p in mgr.iterdir()) if mgr.exists() else []
    assert len(routed) == 2, (
        f"[no-subject ZIP routable] expected 2 routed PDFs; got {routed}"
    )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir()), (
        f"[no-subject ZIP routable] expected _Unmatched empty; got "
        f"{list(unmatched.iterdir())}"
    )


def test_subject_matched_zip_stem_plan_does_NOT_leak_into_inner_classifier(
    monkeypatch, tmp_path: Path,
) -> None:
    """Codex review regression (2026-05-13): the ZIP filename's plan token
    must NOT influence the classification of a contained PDF.

    Concrete case: ZIP named `BCS2707 invoices.zip`, contains a scanned
    PDF named `BCS2800 invoice.pdf` (empty text). Subject says BCS 2707.

    Correct behaviour: inner filename says BCS 2800 -> CLASH against
    subject -> flag, write nothing. Under the original 0.14.0 code, the
    synthetic `BCS2707 invoices__BCS2800 invoice.pdf` name fed into the
    filename-fallback matcher would hit `BCS2707` first and silently
    route the PDF to Sue Smith (wrong manager).
    """
    pdf_inner = _pdf_with_marker("zip-stem-leak-A")
    zip_bytes = _build_zip([("BCS2800 invoice.pdf", pdf_inner)])
    attachments = [_zip_att("BCS2707 invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_inner: ""}  # scanned — no extractable text

    run, m_flag, m_move = _run_self(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id,
        text_by_blob=text_by_blob, plan_norm="BCS2707",
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[ZIP-stem-leak] expected flag (inner filename CLASHes with subject); "
        f"got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    mgr_2707 = _manager_folder(tmp_path, "Sue Smith")
    mgr_2800 = _manager_folder(tmp_path, "Bob Jones")
    for mgr in (mgr_2707, mgr_2800):
        assert not mgr.exists() or not list(mgr.iterdir()), (
            f"[ZIP-stem-leak] expected NO routed PDFs (CLASH should flag); "
            f"found in {mgr}: {list(mgr.iterdir()) if mgr.exists() else []}"
        )


def test_no_subject_match_zip_stem_plan_does_NOT_route_silent_inner(
    monkeypatch, tmp_path: Path,
) -> None:
    """Codex review regression (no-subject-match path): if the ZIP filename
    contains a plan token but the contained PDF is fully silent (no plan
    in inner text, no plan in inner filename), the email must be flagged.
    The ZIP filename's plan token must NEVER be the routing signal.

    Under the 0.14.0 bug: the synthetic `BCS2707 April__random.pdf` name
    flows into `match_from_filename_with_base_fallback`, which sees
    `BCS2707` first and routes the silent PDF to Sue Smith — silently,
    using the ZIP filename's plan as if it were authoritative.
    """
    pdf_inner = _pdf_with_marker("zip-stem-leak-B")
    zip_bytes = _build_zip([("random.pdf", pdf_inner)])
    attachments = [_zip_att("BCS2707 April.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_inner: ""}  # silent

    run, m_flag, m_move = _run_fallback(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id, text_by_blob=text_by_blob,
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[ZIP-stem-leak fallback] expected flag (silent inner PDF, no signal "
        f"anywhere except the ZIP stem which must NOT be trusted); "
        f"got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    mgr_2707 = _manager_folder(tmp_path, "Sue Smith")
    assert not mgr_2707.exists() or not list(mgr_2707.iterdir()), (
        f"[ZIP-stem-leak fallback] expected NO routed PDFs; got "
        f"{list(mgr_2707.iterdir()) if mgr_2707.exists() else []}"
    )
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir())


def test_no_subject_match_silent_zip_flags_email(monkeypatch, tmp_path: Path) -> None:
    """Scenario 5 (mode-1 worst case, prevention path): no subject signal,
    ZIP contents are silent. All-or-nothing fails → email stays in Inbox with
    Outlook red flag. NOTHING is written to disk — no orphan in _Unmatched.

    This is the failure mode the 2026-05-13 fix was specifically built to
    eliminate. Under the OLD pipeline this would silently park the ZIP in
    `_Unmatched/`, the email would move to `processed_emails`, and Step 2+3
    would leave silent PDFs orphaned forever."""
    pdf_a = _pdf_with_marker("silent-fallback-A")
    pdf_b = _pdf_with_marker("silent-fallback-B")
    zip_bytes = _build_zip([
        ("inv_001.pdf", pdf_a),
        ("inv_002.pdf", pdf_b),
    ])
    attachments = [_zip_att("April invoices.zip", att_id="zip-1")]
    blob_by_id = {"zip-1": zip_bytes}
    text_by_blob = {pdf_a: "", pdf_b: ""}  # silent

    run, m_flag, m_move = _run_fallback(
        monkeypatch, tmp_path,
        attachments=attachments, blob_by_id=blob_by_id, text_by_blob=text_by_blob,
    )

    m_flag.assert_called_once_with(_MSG_ID), (
        f"[no-subject silent ZIP] expected flag; got {m_flag.call_args_list}"
    )
    m_move.assert_not_called()
    unmatched = _unmatched_dir(tmp_path)
    assert not unmatched.exists() or not list(unmatched.iterdir()), (
        f"[no-subject silent ZIP] expected _Unmatched empty (the headline bug); "
        f"got {list(unmatched.iterdir())}"
    )
    mgr = _manager_folder(tmp_path, "Sue Smith")
    assert not mgr.exists() or not list(mgr.iterdir()), (
        f"[no-subject silent ZIP] expected NO routed PDFs"
    )
