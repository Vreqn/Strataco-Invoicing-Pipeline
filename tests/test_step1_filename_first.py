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
    """[0.11.3 / Codex finding 3] Direct repro of the safety concern.

    Subject says EPS6008. Filename also says EPS6008. But the PDF body actually
    contains BCS 3396. Pre-0.11.3 (filename-first) would have returned AGREE
    against the subject and routed to EPS 6008 manager — wrong. Post-0.11.3
    (PDF-text-first) extracts the body, finds a confident BCS3396 match, and
    returns CLASH so the strict-first decision matrix flags the email.
    """
    rows = [_row("EPS6008", manager="Alice"), _row("BCS3396", manager="Bob")]

    with patch.object(s1, "extract_full_text", return_value="Strata Plan BCS 3396 Invoice #12345"):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="EPS 6008 invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    assert cls.outcome == PdfOutcome.CLASH, (
        f"[PDF text vs filename conflict] expected CLASH (PDF text wins), "
        f"got {cls.outcome.value} (pdf_plan_norm={cls.pdf_plan_norm!r}, "
        f"note={cls.note!r})"
    )
    assert cls.pdf_plan_norm == "BCS3396", (
        f"[PDF text vs filename conflict] expected pdf_plan_norm=BCS3396, "
        f"got {cls.pdf_plan_norm!r}"
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
