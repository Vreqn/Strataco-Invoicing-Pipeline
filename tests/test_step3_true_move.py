"""Step 3 true-move regression tests.

Tests verify that _route_one() performs a true move: after routing a PDF to
the manager's To_Approve folder, the source is deleted from _Unmatched/ with
no Processed- marker left behind. Ledger writes are fail-closed.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib import dup_fingerprint, dup_ledger
from tools._lib import paths as _paths
from tools._lib.xls import PlanRow
from steps import step_3_pdf_sort as step3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_simple_pdf() -> bytes:
    """Minimal single-page PDF with no tricky content."""
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)
    c.setFont("Helvetica", 12)
    c.drawString(50, page_h - 60, "Test invoice content")
    c.save()
    return buf.getvalue()


def _make_plan_row() -> PlanRow:
    return PlanRow(
        plan_norm="BCS2707",
        plan_raw="BCS 2707",
        strata_name="Test Strata",
        address="123 Test St",
        manager_name="Test Manager",
        manager_key="TEST_MANAGER",
        manager_email="m@example.com",
        ap_name="Test AP",
        ap_key="TEST_AP",
        ap_email="ap@example.com",
        status_active=True,
    )


class _Run:
    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.processed: int = 0

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


# ---------------------------------------------------------------------------
# Test 1: true move leaves no Processed- marker in _Unmatched/
# ---------------------------------------------------------------------------

def test_step3_move_leaves_no_processed_marker() -> None:
    """After routing, source is gone and NO Processed- marker is left in
    _Unmatched/. The old copy+marker code would leave a Processed-<ts>-<name>
    file behind — this test catches that regression."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run = _Run()

        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run, "MAY 13 2026")

        assert not pdf_path.exists(), "source should be deleted after routing"

        marker_files = [p for p in unmatched.iterdir() if p.name.lower().startswith("processed")]
        assert not marker_files, (
            f"unexpected Processed- marker(s) left in _Unmatched/: "
            f"{[p.name for p in marker_files]}"
        )

        dest_files = list(manager_dir.glob("*.pdf"))
        assert len(dest_files) == 1, (
            f"expected 1 file in manager To_Approve, got {[p.name for p in dest_files]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after route"
        assert row.current_stage == "manager_queue"

        assert run.processed == 1
        assert not run.errors


# ---------------------------------------------------------------------------
# Test 2: retry after interrupted move — no collision copy
# ---------------------------------------------------------------------------

def test_step3_partial_move_retry() -> None:
    """If the source reappears after a completed route (interrupted delete),
    the duplicate detection fires and cleans it up without a collision copy."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        # Pre-seed: ledger already has a manager_queue row (route succeeded)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="manager_queue",
        ))

        # Pre-seed destination on disk
        (manager_dir / "BCS 2707 - Invoice test.pdf").write_bytes(b"already routed")

        # Drop source back (simulates interrupted delete after route)
        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        run = _Run()

        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run, "MAY 13 2026")

        assert not pdf_path.exists(), "leftover source should be unlinked"

        # No new Processed- marker
        marker_files = [p for p in unmatched.iterdir() if p.name.lower().startswith("processed")]
        assert not marker_files, f"unexpected marker: {[p.name for p in marker_files]}"

        # run.processed incremented (duplicate counts as processed)
        assert run.processed == 1
        assert not run.errors


# ---------------------------------------------------------------------------
# Test 3: ledger update failure keeps source in place
# ---------------------------------------------------------------------------

def test_step3_ledger_update_failure_keeps_source() -> None:
    """If the ledger upsert fails after a successful destination write, the
    source must stay in _Unmatched/ for retry (fail-closed)."""
    pdf_bytes = _build_simple_pdf()

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        rows = [_make_plan_row()]
        run = _Run()

        with patch.object(_paths, "manager_to_approve", return_value=manager_dir), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "consume_override_and_insert", side_effect=RuntimeError("ledger fail")):
            step3._route_one(pdf_path, rows, ledger, run, "MAY 13 2026")

        assert pdf_path.exists(), "source must stay when ledger update failed"
        dest_files = list(manager_dir.glob("*.pdf"))
        assert len(dest_files) == 1, "destination should still be written"
        assert run.errors, "an error should be logged"
        assert run.processed == 0, "should not count as processed"


# ---------------------------------------------------------------------------
# Test 4: duplicate leaves no Processed-DUPLICATE- marker
# ---------------------------------------------------------------------------

def test_step3_duplicate_leaves_no_marker() -> None:
    """When a PDF is detected as a duplicate, source is unlinked and NO
    Processed-<ts>-DUPLICATE- marker is written."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        # Pre-seed: ledger has the PDF at manager_queue (it's already been routed)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="manager_queue",
        ))

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        run = _Run()

        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run, "MAY 13 2026")

        assert not pdf_path.exists(), "duplicate source should be deleted"

        # No Processed-DUPLICATE- marker
        marker_files = [
            p for p in unmatched.iterdir()
            if "duplicate" in p.name.lower() or p.name.lower().startswith("processed")
        ]
        assert not marker_files, (
            f"unexpected duplicate marker(s): {[p.name for p in marker_files]}"
        )

        # Dup count incremented
        row = ledger.find_by_hash(sha)
        assert row is not None
        assert row.dup_count == 1

        assert run.processed == 1
        assert not run.errors


# ---------------------------------------------------------------------------
# Test 5: retry after ledger failure — no collision copy
# ---------------------------------------------------------------------------

def test_step3_ledger_failure_retry_no_collision_copy() -> None:
    """If the ledger upsert fails on run 1 (destination written, source stays),
    run 2 must complete without creating a collision copy in manager To_Approve."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run1 = _Run()

        # Run 1: ledger upsert fails → destination is written, source stays
        with patch.object(_paths, "manager_to_approve", return_value=manager_dir), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "consume_override_and_insert", side_effect=RuntimeError("ledger fail")):
            step3._route_one(pdf_path, rows, ledger, run1, "MAY 13 2026")

        assert pdf_path.exists(), "source must stay after ledger failure"
        dest_files_after_run1 = list(manager_dir.glob("*.pdf"))
        assert len(dest_files_after_run1) == 1, "destination should be written on run 1"

        # Run 2: real ledger — should complete cleanly with no collision copy
        run2 = _Run()
        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run2, "MAY 13 2026")

        assert not pdf_path.exists(), "source must be deleted after successful run 2"
        assert not run2.errors, f"unexpected errors on run 2: {run2.errors}"

        dest_files_after_run2 = list(manager_dir.glob("*.pdf"))
        assert len(dest_files_after_run2) == 1, (
            f"expected exactly 1 file in manager To_Approve after retry, "
            f"got {[p.name for p in dest_files_after_run2]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after run 2"
        assert row.current_stage == "manager_queue"
        assert run2.processed == 1


# ---------------------------------------------------------------------------
# Test 6: different invoice, same dest filename — collision copy, no data loss
# ---------------------------------------------------------------------------

def test_step3_different_invoice_same_dest_gets_collision_copy() -> None:
    """If the manager To_Approve path already holds a DIFFERENT invoice (different
    bytes), safe_write_unique must create a (1) collision copy for the new invoice.
    The new invoice's source must NOT be silently dropped."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        # Pre-seed dest with a DIFFERENT invoice (different bytes, same filename)
        dest_name = "BCS 2707 - Invoice test.pdf"
        (manager_dir / dest_name).write_bytes(b"completely different invoice content")

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run = _Run()

        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run, "MAY 14 2026")

        assert not pdf_path.exists(), "source should be deleted after routing"
        assert not run.errors, f"unexpected errors: {run.errors}"

        dest_files = sorted(manager_dir.glob("*.pdf"))
        assert len(dest_files) == 2, (
            f"expected 2 files in manager To_Approve (original + collision copy), "
            f"got {[p.name for p in dest_files]}"
        )
        names = {p.name for p in dest_files}
        assert dest_name in names, "original file must still be present"
        collision = next(p for p in dest_files if p.name != dest_name)
        assert "(1)" in collision.name, f"expected '(1)' in collision filename: {collision.name}"

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after routing"
        assert row.current_stage == "manager_queue"
        assert run.processed == 1


# ---------------------------------------------------------------------------
# Test 7: cross-day retry — different received_str must not produce collision
# ---------------------------------------------------------------------------

def test_step3_cross_day_retry_no_collision_copy() -> None:
    """If the ledger upsert fails on day 1 (destination written, source stays),
    and the retry runs on day 2 with a different received_str, re-stamping with
    the new date would change the bytes and cause safe_write_unique to create a
    (1) collision copy. The P2 fix reads dest bytes instead of re-stamping when
    dest already exists, so the retry should be collision-free."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        unmatched = tdir_path / "_Unmatched" / "Invoices"
        unmatched.mkdir(parents=True)
        manager_dir = tdir_path / "Manager" / "To_Approve"
        manager_dir.mkdir(parents=True)

        pdf_path = unmatched / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run1 = _Run()

        # Day 1: ledger upsert fails → destination written with MAY 13 stamp, source stays
        with patch.object(_paths, "manager_to_approve", return_value=manager_dir), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "consume_override_and_insert", side_effect=RuntimeError("ledger fail")):
            step3._route_one(pdf_path, rows, ledger, run1, "MAY 13 2026")

        assert pdf_path.exists(), "source must stay after ledger failure"
        dest_files_after_day1 = list(manager_dir.glob("*.pdf"))
        assert len(dest_files_after_day1) == 1, "destination should be written on day 1"

        # Day 2: retry with a different received_str — must NOT create a collision copy
        run2 = _Run()
        with patch.object(_paths, "manager_to_approve", return_value=manager_dir):
            step3._route_one(pdf_path, rows, ledger, run2, "MAY 14 2026")  # next day

        assert not pdf_path.exists(), "source must be deleted after successful day-2 retry"
        assert not run2.errors, f"unexpected errors on day-2 retry: {run2.errors}"

        dest_files_after_day2 = list(manager_dir.glob("*.pdf"))
        assert len(dest_files_after_day2) == 1, (
            f"expected exactly 1 file in manager To_Approve after cross-day retry "
            f"(no collision copy), got {[p.name for p in dest_files_after_day2]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after day-2 retry"
        assert row.current_stage == "manager_queue"
        assert run2.processed == 1
