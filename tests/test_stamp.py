"""Regression tests for the stamp-placement semantic exclusion + lower-left
fallback shipped in 0.13.

The pre-0.13 algorithm in `tools/_lib/stamp.find_largest_whitespace_box`
was purely pixel-based: when neither the strict (>=99.5% white) nor the
loose (>=98.5% white) pass found a candidate, it dropped a hard-coded
lower-right fallback at `(360, 60) pt` — exactly where vendors put the
totals block. The 2026-05-12 LMS 3297 live run made the bug obvious:
the Received stamp landed across "Subtotal / GST / PST / Total /
Amount Paid / Amount Due / Account Balance".

0.13 changes:
  - Page-1 text gets scanned for totals-block + invoice-number labels;
    each matching row becomes a forbidden horizontal band.
  - Three-tier search (strict / loose / last-resort), all tiers refuse
    to place the stamp on a forbidden band.
  - `FALLBACK_X_PT` flips from 360 (lower-right) to `PAGE_MARGIN_PT = 36`
    (lower-left), because invoices universally put totals on the right.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib.stamp import (
    FALLBACK_X_PT,
    FALLBACK_Y_PT,
    PAGE_MARGIN_PT,
    STAMP_HEIGHT_PAID_PT,
    STAMP_HEIGHT_RECEIVED_PT,
    STAMP_WIDTH_PT,
    StampPlacement,
    _extract_forbidden_bands,
    _INVOICE_NUMBER_LABEL_TOKENS,
    _TOTAL_LABEL_TOKENS,
    _line_matches_any_label,
    _normalize_token,
    find_largest_whitespace_box,
    flatten_acroform,
    render_received_stamp,
)


LETTER_W_PT, LETTER_H_PT = LETTER


# ----------------------------------------------------------------- helpers

def _placement_overlaps_band(
    placement: StampPlacement,
    band_top_pt_td: float,
    band_bot_pt_td: float,
    page_h_pt: float,
) -> bool:
    """Overlap check between a stamp placement (PDF bottom-up coords) and
    a forbidden band expressed in top-down coords (pdfplumber convention).
    """
    band_top_bu = page_h_pt - band_bot_pt_td
    band_bot_bu = page_h_pt - band_top_pt_td
    stamp_bot = placement.y_pt
    stamp_top = placement.y_pt + placement.height_pt
    return stamp_bot < band_bot_bu and stamp_top > band_top_bu


def _assert_placement_avoids_all_bands(
    placement: StampPlacement,
    bands_pt_td: list[tuple[float, float]],
    page_h_pt: float = LETTER_H_PT,
    label: str = "",
) -> None:
    for band_top, band_bot in bands_pt_td:
        assert not _placement_overlaps_band(
            placement, band_top, band_bot, page_h_pt
        ), (
            f"{label}stamp overlaps forbidden band y_td=[{band_top:.1f}, {band_bot:.1f}]: "
            f"placement.y_pt={placement.y_pt:.1f}, "
            f"placement.height_pt={placement.height_pt:.1f}"
        )


def _build_invoice_with_totals_lower_right() -> bytes:
    """Mid-density invoice with the totals block in the lower-right. The
    strict tier should still find a clean spot above the items, but the
    totals labels MUST be detected as forbidden bands and avoided.
    """
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, page_h - 60, "Acme Locksmith Co.")
    c.setFont("Helvetica", 10)
    c.drawString(50, page_h - 78, "123 Maple St, Vancouver BC")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(380, page_h - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 78, "Invoice # 88001")
    c.drawString(380, page_h - 92, "Invoice Date May 5 2026")

    # Line items in the middle of the page (deliberate clean band between
    # header and items so the strict tier has a place to land).
    y = page_h - 320
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Item")
    c.drawString(400, y, "Amount")
    c.line(50, y - 4, page_w - 50, y - 4)
    c.setFont("Helvetica", 10)
    for i in range(6):
        y -= 18
        c.drawString(50, y, f"Service line item {i + 1}")
        c.drawString(400, y, f"${100 + i * 7}.00")

    # Totals block — lower-right, where the old (360, 60) fallback would have landed.
    top_y = 200
    c.setFont("Helvetica", 10)
    c.drawString(380, top_y,        "Subtotal       $700.00")
    c.drawString(380, top_y - 14,   "GST             $35.00")
    c.drawString(380, top_y - 28,   "PST             $49.00")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, top_y - 42,   "Total          $784.00")
    c.setFont("Helvetica", 10)
    c.drawString(380, top_y - 56,   "Amount Paid     $0.00")
    c.drawString(380, top_y - 70,   "Amount Due     $784.00")
    c.drawString(380, top_y - 84,   "Account Balance  $0.00")

    c.save()
    return buf.getvalue()


def _build_dense_invoice_with_totals() -> bytes:
    """High-density invoice — strict + loose passes will almost certainly
    fail, forcing the last-resort tier. Totals labels are still in the
    lower-right and must remain protected.
    """
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)

    c.setFont("Helvetica", 8)
    for y_offset in range(60, 700, 11):
        c.drawString(50, page_h - y_offset, " ".join(["noise"] * 22))

    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 60, "Invoice # DENSE-001")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, 140, "Total       $9,999.99")
    c.setFont("Helvetica", 10)
    c.drawString(380, 126, "Amount Due  $9,999.99")
    c.drawString(380, 112, "Balance Due $9,999.99")

    c.save()
    return buf.getvalue()


def _build_clean_invoice() -> bytes:
    """Sparse invoice with abundant whitespace — strict tier should succeed."""
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, page_h - 60, "Clean Vendor Co.")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 60, "Invoice # 99999")
    c.drawString(50, page_h - 200, "Service rendered")
    c.drawString(400, page_h - 200, "$80.00")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, page_h - 240, "Total $80.00")

    c.save()
    return buf.getvalue()


def _build_tiny_page() -> bytes:
    """Page smaller than the stamp + margins — trips the early-return
    fallback branch in `find_largest_whitespace_box`.
    """
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(200, 250))
    c.drawString(10, 10, "tiny")
    c.save()
    return buf.getvalue()


# ----------------------------------------------------------------- label matching units

def test_normalize_strips_trailing_punctuation():
    assert _normalize_token("Total:") == "total"
    assert _normalize_token("No.") == "no"
    assert _normalize_token("Subtotal,") == "subtotal"
    assert _normalize_token("#") == "#"
    assert _normalize_token("Invoice") == "invoice"


def test_subtotal_matches_total_label_set_but_is_distinct_from_total():
    # Subtotal is its own label — matches the totals set.
    assert _line_matches_any_label(("subtotal", "$1,000.00"), _TOTAL_LABEL_TOKENS)
    # Token equality means "subtotal" does NOT spuriously match the
    # standalone "total" label by substring.
    assert _line_matches_any_label(("total", "$1,000.00"), _TOTAL_LABEL_TOKENS)


def test_invoice_date_is_not_an_invoice_number():
    tokens = ("invoice", "date", "may", "5", "2026")
    assert not _line_matches_any_label(tokens, _INVOICE_NUMBER_LABEL_TOKENS)


def test_invoice_hash_matches_invoice_number():
    assert _line_matches_any_label(
        ("invoice", "#", "88001"), _INVOICE_NUMBER_LABEL_TOKENS
    )


def test_amount_due_matches_total_set():
    assert _line_matches_any_label(("amount", "due", "$100"), _TOTAL_LABEL_TOKENS)


def test_account_number_is_not_a_totals_label():
    # "Account # 5078" appears on real invoices and is NOT in the totals
    # block — must not be detected as a band.
    assert not _line_matches_any_label(
        ("account", "#", "5078"), _TOTAL_LABEL_TOKENS
    )
    assert not _line_matches_any_label(
        ("account", "#", "5078"), _INVOICE_NUMBER_LABEL_TOKENS
    )


# ----------------------------------------------------------------- placement behaviour

def test_stamp_avoids_totals_block():
    invoice = _build_invoice_with_totals_lower_right()
    placement = find_largest_whitespace_box(
        invoice, STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    bands = _extract_forbidden_bands(invoice)
    assert bands, "expected at least one forbidden band from totals labels"
    _assert_placement_avoids_all_bands(placement, bands, label="[totals] ")


def test_stamp_avoids_invoice_number_row():
    invoice = _build_invoice_with_totals_lower_right()
    placement = find_largest_whitespace_box(
        invoice, STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    bands = _extract_forbidden_bands(invoice)
    # An "Invoice # 88001" line sits at top-right (y_td ~78). Its band
    # should be in the top third of the page.
    upper_bands = [(t, b) for t, b in bands if b < LETTER_H_PT / 3]
    assert upper_bands, f"expected an invoice-number band in upper page, got bands={bands}"
    _assert_placement_avoids_all_bands(placement, bands, label="[invoice#] ")


def test_old_fallback_case_now_avoids_totals():
    """Regression of record: a dense invoice with totals in the lower-right
    used to drop the stamp onto the totals via the (360, 60) fallback.
    Now the last-resort tier picks an exclusion-free spot.
    """
    invoice = _build_dense_invoice_with_totals()
    placement = find_largest_whitespace_box(
        invoice, STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    bands = _extract_forbidden_bands(invoice)
    assert bands, "expected totals labels to produce bands"
    _assert_placement_avoids_all_bands(placement, bands, label="[dense] ")
    # Explicit guard against the old (360, 60) coords landing.
    assert not (abs(placement.x_pt - 360) < 1.0 and abs(placement.y_pt - 60) < 1.0), (
        f"stamp landed at old lower-right fallback (360, 60): {placement}"
    )


def test_fallback_is_lower_left():
    """When the early-return fallback fires (page smaller than stamp +
    margins), it must be in the lower-LEFT — not the old lower-right.
    """
    placement = find_largest_whitespace_box(
        _build_tiny_page(), STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    assert placement.fallback_used is True
    assert placement.x_pt == FALLBACK_X_PT
    assert placement.x_pt == PAGE_MARGIN_PT
    assert placement.x_pt != 360, "old lower-right x leaked back in"
    assert placement.y_pt == FALLBACK_Y_PT


def test_clean_invoice_unchanged():
    """Easy invoice — placement should be a real (non-fallback) spot
    inside the page margins that avoids the small totals block.
    """
    invoice = _build_clean_invoice()
    placement = find_largest_whitespace_box(
        invoice, STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    assert placement.fallback_used is False
    assert placement.x_pt >= PAGE_MARGIN_PT
    assert placement.y_pt >= PAGE_MARGIN_PT
    assert placement.x_pt + placement.width_pt <= LETTER_W_PT - PAGE_MARGIN_PT
    assert placement.y_pt + placement.height_pt <= LETTER_H_PT - PAGE_MARGIN_PT
    bands = _extract_forbidden_bands(invoice)
    _assert_placement_avoids_all_bands(placement, bands, label="[clean] ")


def test_paid_stamp_after_received_avoids_totals():
    """End-to-end: apply Received, flatten, then compute Paid placement.
    Neither stamp's rectangle may overlap the totals / invoice# rows.
    """
    invoice = _build_invoice_with_totals_lower_right()

    received_placement = find_largest_whitespace_box(
        invoice, STAMP_WIDTH_PT, STAMP_HEIGHT_RECEIVED_PT
    )
    bands_before = _extract_forbidden_bands(invoice)
    _assert_placement_avoids_all_bands(
        received_placement, bands_before, label="[received] "
    )

    received_pdf = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat_pdf = flatten_acroform(received_pdf)

    paid_placement = find_largest_whitespace_box(
        flat_pdf, STAMP_WIDTH_PT, STAMP_HEIGHT_PAID_PT
    )
    bands_after = _extract_forbidden_bands(flat_pdf)
    # The flattened PDF now contains "Received: MAY 12 2026" and "Strata
    # Plan #: BCS 2707" text from the Received stamp — none of which
    # should match the totals or invoice-number label sets.
    _assert_placement_avoids_all_bands(
        paid_placement, bands_after, label="[paid] "
    )
