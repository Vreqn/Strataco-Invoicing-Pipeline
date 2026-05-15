"""Unit tests for the Step 1 0.11.2 + 0.11.3 changes:

  (a) `_classify_pdf_against_subject` falls back to the FILENAME when PDF text
      doesn't yield a confident match. (Order: PDF text first, filename second
      — swapped in 0.11.3 per Codex review.)
  (b) Filename plan and subject disagree -> CLASH.
  (c) Filename plan is manager-less / inactive -> falls through to PDF text.
  (d) Boilerplate filename (no plan ID) -> falls through to PDF text.
  (e) `_Run.review()` increments `need_review` without polluting `errors`.
  (f) [0.11.3 / Codex finding 3] PDF text wins over a conflicting filename.
  (g) [0.11.3 / Codex finding 1] `_is_real_pdf` magic-byte check.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

import steps.step_1_intake as s1
from steps.step_1_intake import (
    PdfOutcome,
    _classify_pdf_against_subject,
    _is_real_pdf,
)
from tools._lib.log import _Run
from tools._lib.xls import PlanRow


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


def test_filename_match_returns_agree_when_pdf_text_empty() -> None:
    """The TELUS-style real case: filename has the plan, PDF body has no usable
    text. PDF text matching returns empty (no /Root object on empty bytes), so
    the filename fallback kicks in and produces AGREE.
    """
    rows = [_row("EPS6008")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="EPS 6008 - April 2026 Invoice #17942.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    assert cls.outcome == PdfOutcome.AGREE, (
        f"[filename match AGREE] expected AGREE, got {cls.outcome.value} "
        f"(note={cls.note!r})"
    )
    assert cls.pdf_plan_norm == "EPS6008", (
        f"[filename match AGREE] expected pdf_plan_norm=EPS6008, got {cls.pdf_plan_norm!r}"
    )
    assert "filename" in cls.note.lower(), (
        f"[filename match AGREE] expected note to mention filename, got {cls.note!r}"
    )


def test_filename_match_returns_clash_when_subject_differs() -> None:
    """Filename and subject point at different plans -> CLASH, not AGREE."""
    rows = [_row("EPS6008"), _row("BCS3396")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="EPS 6008 - April 2026 Invoice #17942.pdf",
        subject_plan_norm="BCS3396",
        rows=rows,
    )
    assert cls.outcome == PdfOutcome.CLASH, (
        f"[filename CLASH] expected CLASH, got {cls.outcome.value} "
        f"(note={cls.note!r})"
    )
    assert cls.pdf_plan_norm == "EPS6008", (
        f"[filename CLASH] expected pdf_plan_norm=EPS6008, got {cls.pdf_plan_norm!r}"
    )


def test_filename_inactive_plan_falls_through_to_pdf_text() -> None:
    """If the filename's plan exists but the row is inactive / has no manager,
    don't trust the filename — fall back to PDF text. (We can't route to a
    manager-less plan anyway, so the filename match is uninformative.)
    """
    rows = [_row("EPS6008", manager="")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="EPS 6008 - April 2026 Invoice.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    assert cls.outcome == PdfOutcome.EMPTY, (
        f"[manager-less filename -> fallback] expected EMPTY (from text fallback), "
        f"got {cls.outcome.value} (note={cls.note!r})"
    )


def test_filename_uninformative_falls_through_to_pdf_text() -> None:
    """Boilerplate filenames like 'Options for Paying Your Invoice NEW.pdf'
    have no plan ID. The classifier must fall back to PDF text (which on
    empty bytes returns EMPTY).
    """
    rows = [_row("EPS6008")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="Options for Paying Your Invoice NEW.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    assert cls.outcome == PdfOutcome.EMPTY, (
        f"[boilerplate filename -> fallback EMPTY] expected EMPTY, "
        f"got {cls.outcome.value} (note={cls.note!r})"
    )


def test_pdf_text_wins_over_conflicting_filename() -> None:
    """[0.11.3 / Codex finding 3 / Decision 01] PDF text always wins over the filename.

    Subject says EPS6008. Filename also says EPS6008. But the PDF body actually
    contains BCS 3396. Pre-0.11.3 (filename-first) would have returned AGREE
    against the subject and routed to EPS 6008 manager — wrong.

    Post-0.11.3: PDF-text-first; the body's confident BCS3396 match is used.
    Post-Decision-01 (v0.16.0): when the PDF text confidently identifies a managed
    plan that differs from the subject, return PDF_OVERRIDE (not CLASH) so the
    email routes to BCS3396's manager rather than being flagged.
    """
    rows = [_row("EPS6008", manager="Alice"), _row("BCS3396", manager="Bob")]

    with patch.object(s1, "extract_full_text", return_value="Strata Plan BCS 3396 Invoice #12345"):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="EPS 6008 invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    assert cls.outcome == PdfOutcome.PDF_OVERRIDE, (
        f"[PDF text vs filename conflict] expected PDF_OVERRIDE (Decision 01: trust PDF), "
        f"got {cls.outcome.value} (pdf_plan_norm={cls.pdf_plan_norm!r}, "
        f"note={cls.note!r})"
    )
    assert cls.pdf_plan_norm == "BCS3396", (
        f"[PDF text vs filename conflict] expected pdf_plan_norm=BCS3396, "
        f"got {cls.pdf_plan_norm!r}"
    )


def test_pdf_text_with_no_plan_token_is_no_plan() -> None:
    """PDF has extractable text but it carries no strata plan number at all.
    The matcher detects zero plan-shaped tokens -> NO_PLAN, which the decision
    matrix treats like EMPTY (route on the subject). Flagging this would loop
    the front desk's reply-to-self recovery forever.
    """
    rows = [_row("EPS6008")]

    with patch.object(
        s1, "extract_full_text",
        return_value="Invoice for services rendered. Total $500.00. Thank you.",
    ):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    assert cls.outcome == PdfOutcome.NO_PLAN, (
        f"[no plan token -> NO_PLAN] expected NO_PLAN, got {cls.outcome.value} "
        f"(note={cls.note!r})"
    )


def test_pdf_text_with_unmanaged_plan_token_stays_ambiguous() -> None:
    """PDF text contains a plan-shaped token (EPS 9999) whose prefix IS managed
    but whose number is not. `match_from_pdf_text` detects it (EPS is an active
    prefix), so `result.detected` is non-empty -> AMBIGUOUS, and the email stays
    flagged for human review (Case 3 — see To-Speak-About.txt).
    """
    rows = [_row("EPS6008")]

    with patch.object(
        s1, "extract_full_text",
        return_value="Strata Plan EPS 9999 invoice #12345",
    ):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    assert cls.outcome == PdfOutcome.AMBIGUOUS, (
        f"[unmanaged plan token -> AMBIGUOUS] expected AMBIGUOUS, got "
        f"{cls.outcome.value} (note={cls.note!r}, detected={cls.detected!r})"
    )


def test_pdf_text_names_unmanaged_plan_stays_ambiguous() -> None:
    """[Codex finding] PDF text explicitly names a plan whose PREFIX is not in
    the managed list at all ("Strata Plan KAS 9999" with only EPS/BCS rows).

    `match_from_pdf_text` builds its detection regex only from managed prefixes,
    so KAS is invisible to it and `result.detected` comes back empty. The pre-
    fix code keyed NO_PLAN purely off `not result.detected` and would have
    auto-routed this PDF to the subject's manager. `find_explicit_plan_tokens`
    catches the "Strata Plan ..." wording independently of the managed list, so
    the classifier now returns AMBIGUOUS and the email is flagged for review.
    """
    rows = [_row("EPS6008"), _row("BCS3396")]

    with patch.object(
        s1, "extract_full_text",
        return_value="Please remit for Strata Plan KAS 9999 invoice #12345",
    ):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    assert cls.outcome == PdfOutcome.AMBIGUOUS, (
        f"[unmanaged-prefix plan -> AMBIGUOUS] expected AMBIGUOUS (must not be "
        f"NO_PLAN), got {cls.outcome.value} (note={cls.note!r})"
    )
    assert "KAS9999" in cls.note, (
        f"[unmanaged-prefix plan] note should name the unmanaged plan, got {cls.note!r}"
    )


def test_is_real_pdf_magic_byte_check() -> None:
    """[0.11.3 / Codex finding 1] `_is_real_pdf` accepts only bytes that start
    with the PDF magic header `%PDF-`. Rejects PNG, ZIP, empty, short garbage.
    Real scanner output still passes (the wrapper has the header even when
    the inside is image-only).
    """
    assert _is_real_pdf(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"), "[%PDF-1.7 prefix rejected] expected True"
    assert _is_real_pdf(b"%PDF-1.4"), "[short %PDF-1.4 rejected] expected True"
    assert _is_real_pdf(b"%PDF-2.0 ..."), "[%PDF-2.0 rejected] expected True"

    assert not _is_real_pdf(b"\x89PNG\r\n\x1a\n"), "[PNG accepted as PDF] expected False"
    assert not _is_real_pdf(b"PK\x03\x04"), "[ZIP accepted as PDF] expected False"
    assert not _is_real_pdf(b""), "[empty bytes accepted as PDF] expected False"
    assert not _is_real_pdf(b"abc"), "[short random bytes accepted] expected False"
    assert not _is_real_pdf(b"<html><body>fake</body></html>"), "[HTML accepted as PDF] expected False"


def test_run_review_increments_need_review_without_polluting_errors() -> None:
    """`_Run.review()` is a new method: it appends to `need_review` and logs
    at WARNING level. It must NOT touch `errors` (that's reserved for genuine
    exceptions the operator should be paged on).
    """
    logger = logging.getLogger("strataco.test_review_method")
    run = _Run("test_review_method", logger)

    run.review("flagging 'FW: something': ambiguous PDF text")
    assert len(run.need_review) == 1, (
        f"[review() appends] expected need_review len 1, got {len(run.need_review)}"
    )
    assert not run.errors, (
        f"[review() does not pollute errors] expected errors=[], got {run.errors}"
    )

    run.error("genuine download exception")
    assert len(run.errors) == 1, (
        f"[error() still works] expected errors len 1, got {len(run.errors)}"
    )
    assert len(run.need_review) == 1, (
        f"[error() does not touch need_review] expected need_review len 1, "
        f"got {len(run.need_review)}"
    )
