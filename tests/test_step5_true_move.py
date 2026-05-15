"""Step 5 true-move regression tests.

Tests verify that _transfer_one() performs a true move: after transferring a PDF
to the AP's Approved_Invoices folder, the source is deleted from Manager/Approved/
with no Processed- marker left behind. Ledger writes are fail-closed.
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
from tools._lib.xls import PlanRow, base_plan_index, plan_to_ap
from steps import step_5_to_ap as step5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_simple_pdf() -> bytes:
    """Minimal single-page PDF with no tricky content."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=LETTER)
    _, page_h = LETTER
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
# Test 1: true move leaves no Processed- marker in Approved/
# ---------------------------------------------------------------------------

def test_step5_move_leaves_no_processed_marker() -> None:
    """After transfer, source is gone and NO Processed- marker is left in
    Manager/Approved/. The old copy+marker code would write 'Processed - <name>'
    back — this test catches that regression."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run = _Run()

        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run)

        assert not pdf_path.exists(), "source should be deleted after transfer"

        marker_files = [p for p in approved_dir.iterdir() if p.name.lower().startswith("processed")]
        assert not marker_files, (
            f"unexpected Processed- marker(s) left in Approved/: "
            f"{[p.name for p in marker_files]}"
        )

        dest_files = list(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files) == 1, (
            f"expected 1 file in AP Approved_Invoices, got {[p.name for p in dest_files]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after transfer"
        assert row.current_stage == "ap_queue"

        assert run.processed == 1
        assert not run.errors


# ---------------------------------------------------------------------------
# Test 2: retry after interrupted move — no collision copy
# ---------------------------------------------------------------------------

def test_step5_partial_move_retry_no_collision() -> None:
    """If the source reappears after a completed transfer (interrupted delete),
    the duplicate detection fires and cleans it up without a collision copy."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        # Pre-seed: ledger already has an ap_queue row (transfer succeeded)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="ap_queue",
        ))

        # Pre-seed destination on disk
        (ap_invoices_dir / "BCS 2707 - Invoice test.pdf").write_bytes(b"already transferred")

        # Drop source back (simulates interrupted delete after transfer)
        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        run = _Run()

        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run)

        assert not pdf_path.exists(), "leftover source should be unlinked"

        marker_files = [p for p in approved_dir.iterdir() if p.name.lower().startswith("processed")]
        assert not marker_files, f"unexpected marker: {[p.name for p in marker_files]}"

        # Only the pre-seeded file — no collision copy
        dest_files = list(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files) == 1, (
            f"expected 1 file in AP folder, got {[p.name for p in dest_files]}"
        )

        assert not run.errors


# ---------------------------------------------------------------------------
# Test 3: ledger update failure keeps source in place
# ---------------------------------------------------------------------------

def test_step5_ledger_update_failure_keeps_source() -> None:
    """If the ledger update fails after a successful destination write, the
    source must stay in Manager/Approved/ for retry (fail-closed)."""
    pdf_bytes = _build_simple_pdf()

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        run = _Run()

        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "update_stage", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "consume_override_and_insert", side_effect=RuntimeError("ledger fail")):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run)

        assert pdf_path.exists(), "source must stay when ledger update failed"
        dest_files = list(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files) == 1, "destination should still be written"
        assert run.errors, "an error should be logged"
        assert run.processed == 0, "should not count as processed"


# ---------------------------------------------------------------------------
# Test 4: duplicate leaves no Processed- marker
# ---------------------------------------------------------------------------

def test_step5_duplicate_leaves_no_marker() -> None:
    """When a PDF is detected as a duplicate, source is unlinked and NO
    'Processed - <name>' or similar marker is written."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        # Pre-seed: ledger has the PDF at ap_queue (already transferred once)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="ap_queue",
        ))

        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        run = _Run()

        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run)

        assert not pdf_path.exists(), "duplicate source should be deleted"

        marker_files = [
            p for p in approved_dir.iterdir()
            if "duplicate" in p.name.lower() or p.name.lower().startswith("processed")
        ]
        assert not marker_files, (
            f"unexpected marker(s): {[p.name for p in marker_files]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None
        assert row.dup_count == 1

        assert not run.errors


# ---------------------------------------------------------------------------
# Test 5: retry after ledger failure — no collision copy
# ---------------------------------------------------------------------------

def test_step5_ledger_failure_retry_no_collision_copy() -> None:
    """If the ledger update fails on run 1 (destination written, source stays),
    run 2 must complete without creating a collision copy in AP Approved_Invoices."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run1 = _Run()

        # Run 1: ledger update fails → destination is written, source stays
        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "update_stage", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "consume_override_and_insert", side_effect=RuntimeError("ledger fail")):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run1)

        assert pdf_path.exists(), "source must stay after ledger failure"
        dest_files_after_run1 = list(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files_after_run1) == 1, "destination should be written on run 1"

        # Run 2: real ledger — should complete cleanly with no collision copy
        run2 = _Run()
        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run2)

        assert not pdf_path.exists(), "source must be deleted after successful run 2"
        assert not run2.errors, f"unexpected errors on run 2: {run2.errors}"

        dest_files_after_run2 = list(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files_after_run2) == 1, (
            f"expected exactly 1 file in AP Approved_Invoices after retry, "
            f"got {[p.name for p in dest_files_after_run2]}"
        )

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after run 2"
        assert row.current_stage == "ap_queue"
        assert run2.processed == 1


# ---------------------------------------------------------------------------
# Test 6: different invoice, same dest filename — collision copy, no data loss
# ---------------------------------------------------------------------------

def test_step5_different_invoice_same_dest_gets_collision_copy() -> None:
    """If AP Approved_Invoices already holds a DIFFERENT invoice with the same
    filename, safe_write_unique must create a (1) collision copy. The new
    invoice's source must NOT be silently dropped."""
    pdf_bytes = _build_simple_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        approved_dir = tdir_path / "Manager" / "Approved"
        approved_dir.mkdir(parents=True)
        ap_invoices_dir = tdir_path / "AP" / "Approved_Invoices"
        ap_invoices_dir.mkdir(parents=True)

        # Pre-seed AP folder with a DIFFERENT invoice (different bytes, same filename)
        dest_name = "BCS 2707 - Invoice test.pdf"
        (ap_invoices_dir / dest_name).write_bytes(b"completely different AP invoice")

        pdf_path = approved_dir / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        rows = [_make_plan_row()]
        plan_map = plan_to_ap(rows)
        base_idx = base_plan_index(rows)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run = _Run()

        with patch.object(_paths, "ap_approved_invoices", return_value=ap_invoices_dir):
            step5._transfer_one(pdf_path, plan_map, base_idx, ledger, run)

        assert not pdf_path.exists(), "source should be deleted after transfer"
        assert not run.errors, f"unexpected errors: {run.errors}"

        dest_files = sorted(ap_invoices_dir.glob("*.pdf"))
        assert len(dest_files) == 2, (
            f"expected 2 files in AP folder (original + collision copy), "
            f"got {[p.name for p in dest_files]}"
        )
        names = {p.name for p in dest_files}
        assert dest_name in names, "original file must still be present"
        collision = next(p for p in dest_files if p.name != dest_name)
        assert "(1)" in collision.name, f"expected '(1)' in collision filename: {collision.name}"

        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after transfer"
        assert row.current_stage == "ap_queue"
        assert run.processed == 1
