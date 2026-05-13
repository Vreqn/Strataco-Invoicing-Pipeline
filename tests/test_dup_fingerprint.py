"""Unit tests for tools/_lib/dup_fingerprint — Layer A hash + Layer B extractors.

Covers:
  - sha256_of() determinism: same bytes -> same hash; different bytes -> different
  - extract_invoice_number() on a variety of label conventions
  - extract_amount_cents() with "Total Due" / "Amount Due" / "Grand Total" / plain Total
  - Graceful fallback to "" / None on garbage / empty input
  - compute_layer_b() returns (invoice_number, amount_cents) together

Standalone: no pytest dependency. Run with `python tests/test_dup_fingerprint.py`.
Exits 0 if every case passes, 1 otherwise.
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


def test_sha256_determinism() -> list[str]:
    failures: list[str] = []
    a = b"hello world"
    b = b"hello world"
    c = b"hello world!"
    if sha256_of(a) != sha256_of(b):
        failures.append("[sha256] same bytes should produce same hash")
    if sha256_of(a) == sha256_of(c):
        failures.append("[sha256] different bytes should produce different hashes")
    expected_len = 64  # SHA-256 hex digest is 64 chars
    if len(sha256_of(a)) != expected_len:
        failures.append(f"[sha256] expected 64-char hex, got {len(sha256_of(a))}")
    if sha256_of(b"") != sha256_of(b""):
        failures.append("[sha256] empty input must be deterministic")
    return failures


def test_normalize_invoice_number() -> list[str]:
    failures: list[str] = []
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
        if got != expected:
            failures.append(f"[normalize] {raw!r} -> {got!r}, expected {expected!r}")
    return failures


def test_extract_invoice_number_common_labels() -> list[str]:
    failures: list[str] = []
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
        if got != expected_normalized:
            failures.append(
                f"[invoice#] label={label!r} value={value!r} -> {got!r}, "
                f"expected {expected_normalized!r}"
            )
    return failures


def test_extract_invoice_number_blank_on_garbage() -> list[str]:
    failures: list[str] = []

    # Genuinely empty PDF — must return exactly "" (not None, not garbage).
    got_empty = extract_invoice_number(b"")
    if got_empty != "":
        failures.append(f"[invoice# empty bytes] expected '', got {got_empty!r}")

    # Malformed bytes — must not raise, must return "".
    try:
        got_bad = extract_invoice_number(b"not a pdf at all")
        if got_bad != "":
            failures.append(f"[invoice# bad bytes] expected '', got {got_bad!r}")
    except Exception as exc:
        failures.append(f"[invoice# bad bytes] raised {type(exc).__name__}: {exc}")

    # Invoice-shaped PDF but no labelled invoice-number field. The function
    # MAY surface something near the "INVOICE" header word (e.g. "INV"-style
    # patterns) but it must not hallucinate the customer-id value as an
    # invoice number.
    blob = _synth_invoice(invoice_label="Customer ID:", invoice_value="C-99021")
    got = extract_invoice_number(blob)
    # Specifically: the customer-id value (normalized "C-99021") must NOT
    # surface as an invoice number.
    if got == "C-99021":
        failures.append(
            "[invoice# garbage] regex hallucinated customer ID 'C-99021' as invoice number"
        )
    # And the result must be a string (never None, never an int).
    if not isinstance(got, str):
        failures.append(f"[invoice# garbage] expected str, got {type(got).__name__}")
    return failures


def test_extract_amount_cents_common_labels() -> list[str]:
    failures: list[str] = []
    cases = [
        ("Total Due", "446.00", 44600),
        ("Amount Due", "1,234.56", 123456),
        ("Balance Due", "75.50", 7550),
        ("Grand Total", "999.99", 99999),
    ]
    for label, value, expected_cents in cases:
        blob = _synth_invoice(total_label=label, total_value=value)
        got = extract_amount_cents(blob)
        if got != expected_cents:
            failures.append(
                f"[amount] label={label!r} value={value!r} -> {got!r}, "
                f"expected {expected_cents!r}"
            )
    return failures


def test_extract_amount_cents_blank_on_garbage() -> list[str]:
    failures: list[str] = []
    if extract_amount_cents(b"") is not None:
        failures.append("[amount empty] should return None")
    try:
        _ = extract_amount_cents(b"not a pdf")
    except Exception as exc:
        failures.append(f"[amount bad bytes] raised {type(exc).__name__}: {exc}")
    return failures


def test_compute_layer_b_pair() -> list[str]:
    failures: list[str] = []
    blob = _synth_invoice(
        invoice_label="Invoice #",
        invoice_value="INV-777",
        total_label="Total Due",
        total_value="2,500.00",
    )
    inv, cents = compute_layer_b(blob, plan_norm="BCS2707")
    if inv != "INV-777":
        failures.append(f"[layer_b] invoice {inv!r} != 'INV-777'")
    if cents != 250000:
        failures.append(f"[layer_b] cents {cents!r} != 250000")
    return failures


def test_extract_domain() -> list[str]:
    """extract_domain handles every shape we expect from Microsoft Graph + edge cases."""
    failures: list[str] = []
    cases: list[tuple] = [
        # Graph's shape: {"emailAddress": {"address": "..."}}
        ({"emailAddress": {"address": "billing@vendor.com", "name": "Vendor"}}, "vendor.com"),
        # Subdomain preserved
        ({"emailAddress": {"address": "ar@bills.vendor.co.uk"}}, "bills.vendor.co.uk"),
        # Uppercase normalised to lowercase
        ({"emailAddress": {"address": "BILLING@Vendor.COM"}}, "vendor.com"),
        # Bare string
        ("billing@vendor.com", "vendor.com"),
        # "Name <addr>" form
        ("Vendor Co <ar@vendor.com>", "vendor.com"),
        # Whitespace around the address
        ("   ar@vendor.com   ", "vendor.com"),
        # None -> ""
        (None, ""),
        # Empty dict -> ""
        ({}, ""),
        # Empty string -> ""
        ("", ""),
        # Graph shape with null emailAddress
        ({"emailAddress": None}, ""),
        # Graph shape with empty address
        ({"emailAddress": {"address": ""}}, ""),
        # No @ at all
        ("not an email", ""),
        # @ but no domain part
        ("user@", ""),
        # Domain without a dot (rejected — not a real vendor domain)
        ("user@localhost", ""),
        # Whitespace in the domain (rejected)
        ("user@bad domain.com", ""),
    ]
    for inp, expected in cases:
        got = extract_domain(inp)
        if got != expected:
            failures.append(f"[extract_domain] {inp!r} -> {got!r}, expected {expected!r}")
    return failures


def test_layer_a_catches_regenerated_pdf_misses() -> list[str]:
    """Same invoice number + amount, different PDF bytes -> different sha256."""
    failures: list[str] = []
    blob1 = _synth_invoice(invoice_value="SAME-123", total_value="100.00",
                           extra_lines=["Reference: A"])
    blob2 = _synth_invoice(invoice_value="SAME-123", total_value="100.00",
                           extra_lines=["Reference: B"])
    if sha256_of(blob1) == sha256_of(blob2):
        failures.append("[regen] different bytes must produce different hashes")
    # Layer B should still catch it
    inv1, cents1 = compute_layer_b(blob1, "BCS2707")
    inv2, cents2 = compute_layer_b(blob2, "BCS2707")
    if inv1 != inv2 or cents1 != cents2:
        failures.append(
            f"[regen] Layer B should produce identical semantic key: "
            f"({inv1!r}, {cents1}) vs ({inv2!r}, {cents2})"
        )
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("sha256 determinism", test_sha256_determinism),
        ("normalize_invoice_number", test_normalize_invoice_number),
        ("extract_invoice_number — common labels", test_extract_invoice_number_common_labels),
        ("extract_invoice_number — graceful on garbage", test_extract_invoice_number_blank_on_garbage),
        ("extract_amount_cents — common labels", test_extract_amount_cents_common_labels),
        ("extract_amount_cents — graceful on garbage", test_extract_amount_cents_blank_on_garbage),
        ("compute_layer_b pair", test_compute_layer_b_pair),
        ("extract_domain — Graph shape, strings, edge cases", test_extract_domain),
        ("regenerated PDF: Layer A misses, Layer B catches", test_layer_a_catches_regenerated_pdf_misses),
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
