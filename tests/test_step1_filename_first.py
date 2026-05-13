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

Standalone: no pytest dependency. Run with
`python tests/test_step1_filename_first.py`. Exits 0 on success, 1 on failure.
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


def test_filename_match_returns_agree_when_pdf_text_empty() -> list[str]:
    """The TELUS-style real case: filename has the plan, PDF body has no usable
    text. PDF text matching returns empty (no /Root object on empty bytes), so
    the filename fallback kicks in and produces AGREE.
    """
    failures: list[str] = []
    rows = [_row("EPS6008")]

    cls = _classify_pdf_against_subject(
        blob=b"",  # empty — would be EMPTY under old classifier
        base_name="EPS 6008 - April 2026 Invoice #17942.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    if cls.outcome != PdfOutcome.AGREE:
        failures.append(
            f"[filename match AGREE] expected AGREE, got {cls.outcome.value} "
            f"(note={cls.note!r})"
        )
    if cls.pdf_plan_norm != "EPS6008":
        failures.append(
            f"[filename match AGREE] expected pdf_plan_norm=EPS6008, got {cls.pdf_plan_norm!r}"
        )
    if "filename" not in cls.note.lower():
        failures.append(
            f"[filename match AGREE] expected note to mention filename, got {cls.note!r}"
        )
    return failures


def test_filename_match_returns_clash_when_subject_differs() -> list[str]:
    """Filename and subject point at different plans -> CLASH, not AGREE."""
    failures: list[str] = []
    rows = [_row("EPS6008"), _row("BCS3396")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="EPS 6008 - April 2026 Invoice #17942.pdf",
        subject_plan_norm="BCS3396",  # subject says BCS3396, filename says EPS6008
        rows=rows,
    )
    if cls.outcome != PdfOutcome.CLASH:
        failures.append(
            f"[filename CLASH] expected CLASH, got {cls.outcome.value} "
            f"(note={cls.note!r})"
        )
    if cls.pdf_plan_norm != "EPS6008":
        failures.append(
            f"[filename CLASH] expected pdf_plan_norm=EPS6008, got {cls.pdf_plan_norm!r}"
        )
    return failures


def test_filename_inactive_plan_falls_through_to_pdf_text() -> list[str]:
    """If the filename's plan exists but the row is inactive / has no manager,
    don't trust the filename — fall back to PDF text. (We can't route to a
    manager-less plan anyway, so the filename match is uninformative.)
    """
    failures: list[str] = []
    rows = [_row("EPS6008", manager="")]  # no manager

    cls = _classify_pdf_against_subject(
        blob=b"",  # empty bytes -> text matcher returns EMPTY
        base_name="EPS 6008 - April 2026 Invoice.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    # Empty blob means extract_full_text returns "" -> match_from_pdf_text
    # returns plan_row=None with note "no text extracted" -> EMPTY.
    if cls.outcome != PdfOutcome.EMPTY:
        failures.append(
            f"[manager-less filename -> fallback] expected EMPTY (from text fallback), "
            f"got {cls.outcome.value} (note={cls.note!r})"
        )
    return failures


def test_filename_uninformative_falls_through_to_pdf_text() -> list[str]:
    """Boilerplate filenames like 'Options for Paying Your Invoice NEW.pdf'
    have no plan ID. The classifier must fall back to PDF text (which on
    empty bytes returns EMPTY).
    """
    failures: list[str] = []
    rows = [_row("EPS6008")]

    cls = _classify_pdf_against_subject(
        blob=b"",
        base_name="Options for Paying Your Invoice NEW.pdf",
        subject_plan_norm="EPS6008",
        rows=rows,
    )
    # No plan in filename + empty bytes -> EMPTY (not AMBIGUOUS, not AGREE).
    if cls.outcome != PdfOutcome.EMPTY:
        failures.append(
            f"[boilerplate filename -> fallback EMPTY] expected EMPTY, "
            f"got {cls.outcome.value} (note={cls.note!r})"
        )
    return failures


def test_pdf_text_wins_over_conflicting_filename() -> list[str]:
    """[0.11.3 / Codex finding 3] Direct repro of the safety concern.

    Subject says EPS6008. Filename also says EPS6008. But the PDF body actually
    contains BCS 3396. Pre-0.11.3 (filename-first) would have returned AGREE
    against the subject and routed to EPS 6008 manager — wrong. Post-0.11.3
    (PDF-text-first) extracts the body, finds a confident BCS3396 match, and
    returns CLASH so the strict-first decision matrix flags the email.
    """
    failures: list[str] = []
    rows = [_row("EPS6008", manager="Alice"), _row("BCS3396", manager="Bob")]

    # Monkey-patch the text extractor so we don't need a real PDF fixture
    # carrying live BCS 3396 text. The matcher itself runs normally against
    # the injected text.
    with patch.object(s1, "extract_full_text", return_value="Strata Plan BCS 3396 Invoice #12345"):
        cls = _classify_pdf_against_subject(
            blob=b"%PDF-1.4 stub bytes",
            base_name="EPS 6008 invoice.pdf",
            subject_plan_norm="EPS6008",
            rows=rows,
        )

    if cls.outcome != PdfOutcome.CLASH:
        failures.append(
            f"[PDF text vs filename conflict] expected CLASH (PDF text wins), "
            f"got {cls.outcome.value} (pdf_plan_norm={cls.pdf_plan_norm!r}, "
            f"note={cls.note!r})"
        )
    if cls.pdf_plan_norm != "BCS3396":
        failures.append(
            f"[PDF text vs filename conflict] expected pdf_plan_norm=BCS3396, "
            f"got {cls.pdf_plan_norm!r}"
        )
    return failures


def test_is_real_pdf_magic_byte_check() -> list[str]:
    """[0.11.3 / Codex finding 1] `_is_real_pdf` accepts only bytes that start
    with the PDF magic header `%PDF-`. Rejects PNG, ZIP, empty, short garbage.
    Real scanner output still passes (the wrapper has the header even when
    the inside is image-only).
    """
    failures: list[str] = []

    # Real PDF magic — should pass
    if not _is_real_pdf(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"):
        failures.append("[%PDF-1.7 prefix rejected] expected True")
    if not _is_real_pdf(b"%PDF-1.4"):
        failures.append("[short %PDF-1.4 rejected] expected True")
    if not _is_real_pdf(b"%PDF-2.0 ..."):
        failures.append("[%PDF-2.0 rejected] expected True")

    # Impostors — should fail
    if _is_real_pdf(b"\x89PNG\r\n\x1a\n"):
        failures.append("[PNG accepted as PDF] expected False")
    if _is_real_pdf(b"PK\x03\x04"):
        failures.append("[ZIP accepted as PDF] expected False")
    if _is_real_pdf(b""):
        failures.append("[empty bytes accepted as PDF] expected False")
    if _is_real_pdf(b"abc"):
        failures.append("[short random bytes accepted] expected False")
    if _is_real_pdf(b"<html><body>fake</body></html>"):
        failures.append("[HTML accepted as PDF] expected False")

    return failures


def test_run_review_increments_need_review_without_polluting_errors() -> list[str]:
    """`_Run.review()` is a new method: it appends to `need_review` and logs
    at WARNING level. It must NOT touch `errors` (that's reserved for genuine
    exceptions the operator should be paged on).
    """
    failures: list[str] = []
    logger = logging.getLogger("strataco.test_review_method")
    run = _Run("test_review_method", logger)

    run.review("flagging 'FW: something': ambiguous PDF text")
    if len(run.need_review) != 1:
        failures.append(
            f"[review() appends] expected need_review len 1, got {len(run.need_review)}"
        )
    if run.errors:
        failures.append(
            f"[review() does not pollute errors] expected errors=[], got {run.errors}"
        )

    run.error("genuine download exception")
    if len(run.errors) != 1:
        failures.append(
            f"[error() still works] expected errors len 1, got {len(run.errors)}"
        )
    if len(run.need_review) != 1:
        failures.append(
            f"[error() does not touch need_review] expected need_review len 1, "
            f"got {len(run.need_review)}"
        )
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("filename match -> AGREE when PDF text empty", test_filename_match_returns_agree_when_pdf_text_empty),
        ("filename plan != subject -> CLASH", test_filename_match_returns_clash_when_subject_differs),
        ("filename plan inactive -> fallback to PDF text", test_filename_inactive_plan_falls_through_to_pdf_text),
        ("filename uninformative -> fallback to PDF text", test_filename_uninformative_falls_through_to_pdf_text),
        ("[0.11.3] PDF text wins over conflicting filename", test_pdf_text_wins_over_conflicting_filename),
        ("[0.11.3] _is_real_pdf magic-byte check", test_is_real_pdf_magic_byte_check),
        ("run.review() vs run.error() separation", test_run_review_increments_need_review_without_polluting_errors),
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
