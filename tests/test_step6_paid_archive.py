"""Step 6 true-move regression tests.

Tests verify that _archive_one() performs a true move: after a successful
archive, the source PDF is gone from Paid_Invoices/ with no Processed- marker
left behind. Dedup protection comes from the dup-ledger (SHA-256), not
filename markers.

Bug-first: test_move_leaves_no_processed_marker is written to FAIL on the
copy+marker code and PASS after the true-move implementation.
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

import pytest

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib import dup_fingerprint, dup_ledger
from tools._lib import paths as _paths
from tools._lib.stamp import flatten_acroform, render_paid_stamp, render_received_stamp
from tools._lib.xls import PlanRow
from steps import step_6_paid_archive as step6


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _build_busy_invoice_pdf(vendor: str = "Acme Plumbing Supply Co.") -> bytes:
    """Synthesize a single-page invoice with no 'Check Number:' decoy text."""
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, page_h - 60, vendor)
    c.setFont("Helvetica", 10)
    c.drawString(50, page_h - 78, "123 Main St, Anytown, BC V0V 0V0")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(380, page_h - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 78, "Inv #  INV-2026-1042")
    c.drawString(380, page_h - 92, "Issued  2026-04-15")
    items = [
        ("Emergency boiler service call", "275.00"),
        ("Replacement pressure relief valve", "171.00"),
    ]
    y = page_h - 220
    c.setFont("Helvetica", 10)
    for desc, amt in items:
        y -= 18
        c.drawString(50, y, desc)
        c.drawString(490, y, amt)
    c.save()
    return buf.getvalue()


def _fill_acroform_via_pikepdf(pdf_bytes: bytes, values: dict[str, str]) -> bytes:
    """Set /V on AcroForm fields whose /T starts with one of the given prefixes."""
    import pikepdf
    with pikepdf.Pdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/AcroForm" not in pdf.Root:
            return pdf_bytes
        for field in pdf.Root.AcroForm.Fields:
            if "/T" not in field:
                continue
            name = str(field.T)
            for prefix, val in values.items():
                if name.startswith(prefix):
                    field.V = pikepdf.String(val)
                    break
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def _make_paid_invoice_pdf(
    check_number: str = "CHK-12345",
    paid_date: str = "MAY 13 2026",
    vendor: str = "Acme Plumbing Supply Co.",
) -> bytes:
    """Build a full pipeline PDF ready for Step 6 archiving."""
    invoice = _build_busy_invoice_pdf(vendor=vendor)
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat_received = flatten_acroform(received)
    after_step5 = render_paid_stamp(flat_received)
    return _fill_acroform_via_pikepdf(after_step5, {
        "paid_check_number_": check_number,
        "paid_date_": paid_date,
    })


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

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


# ---------------------------------------------------------------------------
# Test 1: bug-first regression — true move leaves no Processed- marker
# ---------------------------------------------------------------------------

def test_move_leaves_no_processed_marker() -> None:
    """After a successful archive, source is deleted and NO Processed- marker
    is written. Current copy+marker code FAILS this assertion."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        plan_row = _make_plan_row()
        plan_to_path = {"BCS2707": plan_row}
        out = step6._Outcomes()
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        # Source must be gone
        assert not pdf_path.exists(), "source PDF should be deleted after archive"

        # No Processed- marker anywhere in Paid_Invoices/
        marker_files = [p for p in ap_paid.iterdir() if p.name.lower().startswith("processed")]
        assert not marker_files, (
            f"unexpected Processed- marker(s) left behind: {[p.name for p in marker_files]}"
        )

        # Archive present
        archive_files = list(archive_dir.glob("*.pdf"))
        assert len(archive_files) == 1, (
            f"expected exactly 1 archive file, got {[p.name for p in archive_files]}"
        )

        # out.processed has one entry, no unmatched
        assert len(out.processed) == 1, f"expected 1 processed entry, got {out.processed}"
        assert not out.unmatched, f"unexpected unmatched: {out.unmatched}"

        # Ledger row is archived
        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row missing after archive"
        assert row.current_stage == "archived", f"expected 'archived', got {row.current_stage!r}"
        assert row.archive_path == str(archive_files[0]), (
            f"archive_path mismatch: {row.archive_path!r} vs {str(archive_files[0])!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: retry after interrupted move — no collision copy
# ---------------------------------------------------------------------------

def test_partial_move_retry_no_collision() -> None:
    """Source dropped back after a completed archive (interrupted move) is
    cleaned up without creating a (1) collision copy."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        # Pre-seed archive on disk and ledger
        archive_name = step6._build_archive_name("CHK-12345", 5, 2026, "BCS2707")
        archive_file = archive_dir / archive_name
        archive_file.write_bytes(b"stub archive content")
        archive_sha = dup_fingerprint.sha256_of(b"stub archive content")

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=str(archive_file),
            archive_sha256=archive_sha,
        ))

        # Drop source back (simulates interrupted delete after archive)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert not pdf_path.exists(), "leftover source should have been unlinked"
        archive_pdfs = list(archive_dir.glob("*.pdf"))
        assert len(archive_pdfs) == 1, (
            f"expected exactly 1 archive (no collision copy), got {[p.name for p in archive_pdfs]}"
        )
        assert not out.processed, "re-archive should not add to out.processed"


# ---------------------------------------------------------------------------
# Test 3: crash gap — archive on disk, ledger empty
# ---------------------------------------------------------------------------

def test_crash_gap_archive_exists_ledger_row_missing() -> None:
    """A prior run wrote the archive but died before the ledger write. On retry
    the ledger should be updated and the source unlinked without re-archiving."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        # Pre-seed archive on disk only (no ledger row).
        # Use the real flattened bytes so the content-SHA crash-gap check matches.
        archive_name = step6._build_archive_name("CHK-12345", 5, 2026, "BCS2707")
        archive_file = archive_dir / archive_name
        archive_file.write_bytes(flatten_acroform(pdf_bytes))

        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert not pdf_path.exists(), "source should be unlinked after crash-gap recovery"
        archive_pdfs = list(archive_dir.glob("*.pdf"))
        assert len(archive_pdfs) == 1, (
            f"expected 1 archive file, got {[p.name for p in archive_pdfs]}"
        )
        row = ledger.find_by_hash(sha)
        assert row is not None, "ledger row should be inserted during crash-gap recovery"
        assert row.current_stage == "archived"
        assert row.archive_path == str(archive_file)


# ---------------------------------------------------------------------------
# Test 4: ledger says archived but archive file is missing — surface error
# ---------------------------------------------------------------------------

def test_ledger_says_archived_but_file_missing() -> None:
    """If the ledger says archived but the archive file is missing, source must
    stay in place and an unmatched entry must be added."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        nonexistent_archive = str(tdir_path / "Strata_Plans" / "BCS 2707" / "gone.pdf")
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=nonexistent_archive,
        ))

        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert pdf_path.exists(), "source must stay when archive is missing"
        assert len(out.unmatched) == 1
        assert "missing" in out.unmatched[0]["reason"].lower(), (
            f"expected 'missing' in reason, got {out.unmatched[0]['reason']!r}"
        )
        assert not out.processed


# ---------------------------------------------------------------------------
# Test 4b: ledger says archived + archive exists but SHA mismatch → keep source
# ---------------------------------------------------------------------------

def test_archive_sha_mismatch_does_not_unlink_source() -> None:
    """If the ledger says archived and the archive file exists but its SHA256
    doesn't match archive_sha256, the source must NOT be deleted and an
    unmatched entry must be added — the archive might be corrupted/replaced."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        # Write a DIFFERENT PDF to the archive path (simulates corrupt/replaced archive)
        archive_file = archive_dir / "BCS 2707 - Invoice test - CHQ 1234 - May 2026.pdf"
        archive_file.write_bytes(b"%PDF-1.4 this is wrong content")
        wrong_sha = "aaaa" * 16  # not the real archive SHA

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=str(archive_file),
            archive_sha256=wrong_sha,
        ))

        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert pdf_path.exists(), "source must NOT be deleted when archive SHA mismatches"
        assert len(out.unmatched) == 1, "unmatched entry expected for SHA mismatch"
        assert "mismatch" in out.unmatched[0]["reason"].lower(), (
            f"expected 'mismatch' in reason, got {out.unmatched[0]['reason']!r}"
        )
        assert not out.processed


def test_archive_sha_unverifiable_does_not_unlink_source() -> None:
    """When the ledger row has no archive_sha256 (pre-0.12.1 row) but the archive
    file exists, the source must NOT be deleted — SHA can't be verified."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        archive_file = archive_dir / "BCS 2707 - Invoice test - CHQ 1234 - May 2026.pdf"
        archive_file.write_bytes(b"%PDF-1.4 some content")

        # archive_sha256 left as "" (pre-0.12.1 default)
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        ledger.upsert(dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=str(archive_file),
            archive_sha256="",
        ))

        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert pdf_path.exists(), "source must NOT be deleted when archive SHA is unverifiable"
        assert len(out.unmatched) == 1, "unmatched entry expected when SHA unverifiable"
        assert "unverifiable" in out.unmatched[0]["reason"].lower(), (
            f"expected 'unverifiable' in reason, got {out.unmatched[0]['reason']!r}"
        )
        assert not out.processed


# ---------------------------------------------------------------------------
# Test 5: ledger update failure keeps source — fail-closed
# ---------------------------------------------------------------------------

def test_ledger_update_failure_keeps_source() -> None:
    """If the ledger update raises after a successful archive write, the source
    must stay in place (fail-closed). The archive IS written to disk."""
    pdf_bytes = _make_paid_invoice_pdf()

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir), \
             patch.object(ledger, "update_stage", side_effect=RuntimeError("ledger fail")), \
             patch.object(ledger, "upsert", side_effect=RuntimeError("ledger fail")):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert pdf_path.exists(), "source must stay when ledger update failed"
        archive_pdfs = list(archive_dir.glob("*.pdf"))
        assert len(archive_pdfs) == 1, "archive should be written despite ledger failure"
        assert len(out.unmatched) == 1
        assert "ledger" in out.unmatched[0]["reason"].lower()
        assert not out.processed


# ---------------------------------------------------------------------------
# Test 6: ledger row pre-exists at ap_queue — update_stage preserves first_seen_date
# ---------------------------------------------------------------------------

def test_happy_path_ledger_row_preexists() -> None:
    """When a ledger row already exists at ap_queue, update_stage is used
    (not upsert), preserving first_seen_date."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        original_first_seen = "2026-05-01"
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        from dataclasses import replace as _replace
        seed_row = dup_ledger.make_row(
            sha256=sha,
            plan_norm="BCS2707",
            current_stage="ap_queue",
        )
        seed_row = _replace(seed_row, first_seen_date=original_first_seen)
        ledger.upsert(seed_row)

        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert not pdf_path.exists(), "source should be deleted"
        row = ledger.find_by_hash(sha)
        assert row is not None
        assert row.current_stage == "archived"
        assert row.first_seen_date == original_first_seen, (
            f"first_seen_date should be preserved: expected {original_first_seen!r}, "
            f"got {row.first_seen_date!r}"
        )
        assert len(out.processed) == 1


# ---------------------------------------------------------------------------
# Test 7: orphan (no prior ledger row) — upsert inserts new archived row
# ---------------------------------------------------------------------------

def test_orphan_no_ledger_row_inserts() -> None:
    """When no ledger row exists for the PDF, an archived row is inserted via
    upsert after a successful archive."""
    pdf_bytes = _make_paid_invoice_pdf()
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert not pdf_path.exists()
        row = ledger.find_by_hash(sha)
        assert row is not None, "orphan should get a new archived ledger row"
        assert row.current_stage == "archived"
        assert len(out.processed) == 1


# ---------------------------------------------------------------------------
# Test 8: unreadable check number — source stays, unmatched entry added
# ---------------------------------------------------------------------------

def test_unreadable_check_number_leaves_source() -> None:
    """A PDF with no paid_check_number_ value must surface as unmatched and
    leave the source in place. Confirms the MOVE didn't weaken fail-closed paths."""
    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat_received = flatten_acroform(received)
    after_step5 = render_paid_stamp(flat_received)
    # Deliberately omit paid_check_number_ fill
    pdf_bytes = _fill_acroform_via_pikepdf(after_step5, {
        "paid_date_": "MAY 13 2026",
    })

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice test.pdf"
        pdf_path.write_bytes(pdf_bytes)

        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        plan_to_path = {"BCS2707": _make_plan_row()}
        out = step6._Outcomes()
        run = _Run()

        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        assert pdf_path.exists(), "source must stay when check number unreadable"
        assert len(out.unmatched) == 1
        assert "check number" in out.unmatched[0]["reason"].lower(), (
            f"expected 'check number' in reason, got {out.unmatched[0]['reason']!r}"
        )
        assert not out.processed
        assert not list(archive_dir.glob("*.pdf")), "no archive should be written"


# ---------------------------------------------------------------------------
# Test 9: filename collision — two different invoices, same archive name
# ---------------------------------------------------------------------------

def test_step6_filename_collision_two_invoices_both_archived() -> None:
    """Two genuinely different invoices that produce the same archive filename
    (same check number, plan, month/year but different content) must both be
    archived without data loss. The second gets a (1) collision copy; crash-gap
    must NOT fire for it since the existing archive belongs to a different source."""
    # Invoice A and Invoice B — same stamp values, different vendor content baked in
    pdf_bytes_a = _make_paid_invoice_pdf(
        check_number="CHK-99999", paid_date="MAY 13 2026",
        vendor="Acme Plumbing Supply Co.",
    )
    pdf_bytes_b = _make_paid_invoice_pdf(
        check_number="CHK-99999", paid_date="MAY 13 2026",
        vendor="Westside Electrical Services Ltd.",
    )
    assert pdf_bytes_a != pdf_bytes_b, "test setup: two invoices must have different content"

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        plan_to_path = {"BCS2707": _make_plan_row()}

        # --- Archive invoice A ---
        pdf_path_a = ap_paid / "BCS 2707 - InvoiceA.pdf"
        pdf_path_a.write_bytes(pdf_bytes_a)
        sha_a = dup_fingerprint.sha256_of(pdf_bytes_a)
        ledger_a = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")
        out_a = step6._Outcomes()
        run_a = _Run()
        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path_a, plan_to_path, out_a, ledger_a, run_a, "Test AP")

        assert not pdf_path_a.exists(), "invoice A source should be gone"
        assert len(out_a.processed) == 1, f"invoice A should be processed: {out_a.unmatched}"
        archives_after_a = list(archive_dir.glob("*.pdf"))
        assert len(archives_after_a) == 1, "exactly 1 archive after invoice A"

        # --- Archive invoice B (same check/plan/month, different content) ---
        pdf_path_b = ap_paid / "BCS 2707 - InvoiceB.pdf"
        pdf_path_b.write_bytes(pdf_bytes_b)
        sha_b = dup_fingerprint.sha256_of(pdf_bytes_b)
        out_b = step6._Outcomes()
        run_b = _Run()
        with patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path_b, plan_to_path, out_b, ledger_a, run_b, "Test AP")

        assert not pdf_path_b.exists(), "invoice B source should be gone"
        assert not run_b.errors, f"unexpected errors archiving invoice B: {run_b.errors}"

        archives_after_b = list(archive_dir.glob("*.pdf"))
        assert len(archives_after_b) == 2, (
            f"expected 2 archives (original + collision copy for B), "
            f"got {[p.name for p in archives_after_b]}"
        )
        assert len(out_b.processed) == 1, "invoice B should be in processed"
        row_b = ledger_a.find_by_hash(sha_b)
        assert row_b is not None, "ledger row for invoice B must exist"
        assert row_b.current_stage == "archived"
