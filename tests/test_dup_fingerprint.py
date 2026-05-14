"""Unit tests for tools/_lib/dup_fingerprint — Layer A hash + Layer B extractors.

Covers:
  - sha256_of() determinism: same bytes -> same hash; different bytes -> different
  - extract_invoice_number() on a variety of label conventions
  - extract_amount_cents() with "Total Due" / "Amount Due" / "Grand Total" / plain Total
  - Graceful fallback to "" / None on garbage / empty input
  - compute_layer_b() returns (invoice_number, amount_cents) together
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

from tools._lib.dup_fingerprint import (
    compute_layer_b,
    extract_amount_cents,
    extract_domain,
    extract_invoice_number,
    normalize_invoice_number,
    sha256_of,
)


def _synth_invoice(
    *,
    invoice_label: str = "Invoice #",
    invoice_value: str = "INV-2026-1042",
    total_label: str = "Total Due",
    total_value: str = "446.00",
    extra_lines: list[str] | None = None,
) -> bytes:
    """Build a tiny invoice PDF with a labeled invoice# and total."""
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, page_h - 60, "Acme Vendor Co.")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 60, "INVOICE")
    c.drawString(380, page_h - 78, f"{invoice_label} {invoice_value}")
    c.drawString(380, page_h - 92, "Date: MAY 08 2026")

    y = page_h - 220
    c.setFont("Helvetica", 10)
    c.drawString(50, y, "Service A")
    c.drawString(490, y, "300.00")
    y -= 18
    c.drawString(50, y, "Service B")
    c.drawString(490, y, "146.00")
    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, y, "Subtotal: 446.00")
    y -= 18
    c.drawString(380, y, f"{total_label}: {total_value}")

    if extra_lines:
        y -= 30
        c.setFont("Helvetica", 10)
        for line in extra_lines:
            c.drawString(50, y, line)
            y -= 14

    c.save()
    return buf.getvalue()


def test_sha256_determinism() -> None:
    a = b"hello world"
    b = b"hello world"
    c = b"hello world!"
    assert sha256_of(a) == sha256_of(b), "[sha256] same bytes should produce same hash"
    assert sha256_of(a) != sha256_of(c), "[sha256] different bytes should produce different hashes"
    assert len(sha256_of(a)) == 64, f"[sha256] expected 64-char hex, got {len(sha256_of(a))}"
    assert sha256_of(b"") == sha256_of(b""), "[sha256] empty input must be deterministic"


def test_normalize_invoice_number() -> None:
    cases = [
        ("INV-2026-1042", "INV-2026-1042"),
        ("inv-2026-1042", "INV-2026-1042"),
        ("INV 2026 1042", "INV20261042"),
        ("12345", "12345"),
        ("", ""),
        ("---", ""),
        ("INV/2026/1042", "INV20261042"),
    ]
    for raw, expected in cases:
        got = normalize_invoice_number(raw)
        assert got == expected, f"[normalize] {raw!r} -> {got!r}, expected {expected!r}"


def test_extract_invoice_number_common_labels() -> None:
    cases = [
        ("Invoice #", "12345", "12345"),
        ("Invoice No.", "INV-9876", "INV-9876"),
        ("Invoice Number:", "ABC-001", "ABC-001"),
        ("INV:", "55555", "55555"),
        ("Inv. No.", "X-100", "X-100"),
    ]
    for label, value, expected_normalized in cases:
        blob = _synth_invoice(invoice_label=label, invoice_value=value)
        got = extract_invoice_number(blob)
        assert got == expected_normalized, (
            f"[invoice#] label={label!r} value={value!r} -> {got!r}, "
            f"expected {expected_normalized!r}"
        )


def test_extract_invoice_number_blank_on_garbage() -> None:
    got_empty = extract_invoice_number(b"")
    assert got_empty == "", f"[invoice# empty bytes] expected '', got {got_empty!r}"

    # Malformed bytes — must not raise, must return "".
    got_bad = extract_invoice_number(b"not a pdf at all")
    assert got_bad == "", f"[invoice# bad bytes] expected '', got {got_bad!r}"

    blob = _synth_invoice(invoice_label="Customer ID:", invoice_value="C-99021")
    got = extract_invoice_number(blob)
    assert got != "C-99021", (
        "[invoice# garbage] regex hallucinated customer ID 'C-99021' as invoice number"
    )
    assert isinstance(got, str), f"[invoice# garbage] expected str, got {type(got).__name__}"


def test_extract_amount_cents_common_labels() -> None:
    cases = [
        ("Total Due", "446.00", 44600),
        ("Amount Due", "1,234.56", 123456),
        ("Balance Due", "75.50", 7550),
        ("Grand Total", "999.99", 99999),
    ]
    for label, value, expected_cents in cases:
        blob = _synth_invoice(total_label=label, total_value=value)
        got = extract_amount_cents(blob)
        assert got == expected_cents, (
            f"[amount] label={label!r} value={value!r} -> {got!r}, "
            f"expected {expected_cents!r}"
        )


def test_extract_amount_cents_blank_on_garbage() -> None:
    assert extract_amount_cents(b"") is None, "[amount empty] should return None"
    # Must not raise on bad bytes.
    extract_amount_cents(b"not a pdf")


def test_compute_layer_b_pair() -> None:
    blob = _synth_invoice(
        invoice_label="Invoice #",
        invoice_value="INV-777",
        total_label="Total Due",
        total_value="2,500.00",
    )
    inv, cents = compute_layer_b(blob, plan_norm="BCS2707")
    assert inv == "INV-777", f"[layer_b] invoice {inv!r} != 'INV-777'"
    assert cents == 250000, f"[layer_b] cents {cents!r} != 250000"


def test_extract_domain() -> None:
    """extract_domain handles every shape we expect from Microsoft Graph + edge cases."""
    cases: list[tuple] = [
        ({"emailAddress": {"address": "billing@vendor.com", "name": "Vendor"}}, "vendor.com"),
        ({"emailAddress": {"address": "ar@bills.vendor.co.uk"}}, "bills.vendor.co.uk"),
        ({"emailAddress": {"address": "BILLING@Vendor.COM"}}, "vendor.com"),
        ("billing@vendor.com", "vendor.com"),
        ("Vendor Co <ar@vendor.com>", "vendor.com"),
        ("   ar@vendor.com   ", "vendor.com"),
        (None, ""),
        ({}, ""),
        ("", ""),
        ({"emailAddress": None}, ""),
        ({"emailAddress": {"address": ""}}, ""),
        ("not an email", ""),
        ("user@", ""),
        ("user@localhost", ""),
        ("user@bad domain.com", ""),
    ]
    for inp, expected in cases:
        got = extract_domain(inp)
        assert got == expected, f"[extract_domain] {inp!r} -> {got!r}, expected {expected!r}"


def test_layer_a_catches_regenerated_pdf_misses() -> None:
    """Same invoice number + amount, different PDF bytes -> different sha256."""
    blob1 = _synth_invoice(invoice_value="SAME-123", total_value="100.00",
                           extra_lines=["Reference: A"])
    blob2 = _synth_invoice(invoice_value="SAME-123", total_value="100.00",
                           extra_lines=["Reference: B"])
    assert sha256_of(blob1) != sha256_of(blob2), (
        "[regen] different bytes must produce different hashes"
    )
    inv1, cents1 = compute_layer_b(blob1, "BCS2707")
    inv2, cents2 = compute_layer_b(blob2, "BCS2707")
    assert inv1 == inv2 and cents1 == cents2, (
        f"[regen] Layer B should produce identical semantic key: "
        f"({inv1!r}, {cents1}) vs ({inv2!r}, {cents2})"
    )
