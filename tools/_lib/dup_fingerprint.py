"""Duplicate-detection fingerprint computation.

Two-layer fingerprint for an invoice PDF:

  Layer A — `sha256_of(blob)` — raw byte hash. Catches verbatim vendor resends
            (same PDF attached twice). Free, deterministic, no false positives.

  Layer B — `(plan_norm, invoice_number, amount_cents, sender_domain)` —
            semantic key. `(plan, invoice_number, amount)` is extracted from
            PDF text; `sender_domain` is the lowercased domain of the email's
            `From:` address, captured at intake. Catches "vendor regenerated
            the same invoice with different bytes" while keeping two different
            vendors who happen to share an invoice-number/amount from being
            falsely merged — a vendor can't impersonate another vendor's
            domain in this pipeline.

A duplicate is declared when EITHER:
  - Layer A hits (sha256 already in ledger), OR
  - Layer B fully matches (all four semantic fields non-blank AND match)

`sender_domain` is taken from email metadata, not from the PDF — no LLM call,
no PDF extraction, no vendor list to maintain. File-only entry points
(Steps 3/5/6: manual drops, retries) have no email so they pass an empty
domain; Layer B simply does not fire for those rows. They still benefit from
Layer A coverage.

The PDF-text extractors are BEST-EFFORT: they return empty strings / None on
any failure rather than raising. A blank invoice_number, amount_cents, or
sender_domain means "could not determine" — Layer B will not declare a match
against blank fields.
"""

from __future__ import annotations

import hashlib
import re

from tools._lib.pdf_text import extract_full_text


def sha256_of(blob: bytes) -> str:
    """Hex-encoded SHA-256 of the PDF bytes. Deterministic and side-effect free."""
    return hashlib.sha256(blob).hexdigest()


def extract_domain(from_field) -> str:
    """Lowercased email domain from a Microsoft Graph `from` field. "" on failure.

    Accepts:
      - The Graph shape: ``{"emailAddress": {"address": "x@vendor.com", ...}}``
      - A bare email string: ``"x@vendor.com"`` or ``"Name <x@vendor.com>"``
      - ``None``, empty, or malformed input — returns ``""``.

    A blank result causes Layer B to skip the row (the same way blank
    plan_norm / invoice_number / amount_cents do), which is the right
    behaviour for non-email entry points and for malformed senders alike.

    Validation deliberately stops at "has an `@`, has a `.` after it, no
    whitespace in the domain". We're not policing RFC-5322 compliance; we
    just need a stable string that's the same for repeated emails from the
    same vendor and different across vendors.
    """
    if from_field is None:
        return ""
    if isinstance(from_field, dict):
        addr = from_field.get("emailAddress")
        if isinstance(addr, dict):
            email_str = str(addr.get("address") or "")
        else:
            email_str = ""
    else:
        email_str = str(from_field)

    email_str = email_str.strip()
    if not email_str:
        return ""
    # Tolerate "Name <user@host>" by stripping angle brackets.
    if "<" in email_str and ">" in email_str:
        start = email_str.rfind("<")
        end = email_str.rfind(">")
        if start < end:
            email_str = email_str[start + 1:end].strip()

    if "@" not in email_str:
        return ""
    domain = email_str.rsplit("@", 1)[1].strip().lower()
    if not domain or "." not in domain or any(c.isspace() for c in domain):
        return ""
    return domain


# Invoice number patterns. Matches labels seen on real invoices, in priority
# order. The first match wins. All capture groups return the raw value; the
# caller normalises via `normalize_invoice_number()`.
#
# Important: every pattern requires SOMETHING between the label word and the
# captured value (a colon, dash, hash, "No.", "Number", or whitespace + digit
# start). Without that guard, the regex would happily capture the next token
# in the body — e.g. "INVOICE\nINV: 55555" matching as "Invoice + (capture
# INV)" instead of "INV: + (capture 55555)".
_INVOICE_NUMBER_PATTERNS: list[re.Pattern[str]] = [
    # "Invoice No. 12345", "Invoice Number: 12345", "Invoice #: 12345"
    re.compile(
        r"Invoice\s*(?:No\.?|Number|#)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-/_]{1,30})",
        re.IGNORECASE,
    ),
    # "Inv. No. X-100", "Inv No 12345"
    re.compile(
        r"\bInv\.?\s*No\.?\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-/_]{1,30})",
        re.IGNORECASE,
    ),
    # "INV: 55555", "INV-2026-1042", "INV 12345" — bare INV prefix with a
    # required separator (colon / dash / hash / whitespace) and a digit-start
    # value, so we don't capture the next alphabetic token.
    re.compile(
        r"\bINV[\s:#\-]+([0-9][A-Z0-9\-/_]{1,30})",
        re.IGNORECASE,
    ),
    # Bare "Invoice 12345" only when followed by digits — avoids matching
    # "Invoice for services" / "Invoice Date".
    re.compile(
        r"\bInvoice\s+([0-9][A-Z0-9\-/_]{1,30})",
        re.IGNORECASE,
    ),
]


# Amount patterns. Look for "Total Due" / "Amount Due" / "Balance Due" /
# "Grand Total" first — these are the canonical "what to pay" fields. Plain
# "Total" comes last because invoices often have subtotal/tax/total triples
# where we want the last one.
_AMOUNT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:Total\s*Due|Amount\s*Due|Balance\s*Due|Amount\s*Payable|Pay\s*This\s*Amount)\s*[:$]?\s*\$?\s*([0-9][0-9,]*\.\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"Grand\s*Total\s*[:$]?\s*\$?\s*([0-9][0-9,]*\.\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bTotal\s*[:$]?\s*\$?\s*([0-9][0-9,]*\.\d{2})",
        re.IGNORECASE,
    ),
]


def normalize_invoice_number(raw: str) -> str:
    """Upper-case, strip non-alphanumeric except dashes. Returns empty on garbage."""
    if not raw:
        return ""
    cleaned = re.sub(r"[^A-Z0-9\-]", "", raw.upper())
    # An invoice number that's all dashes or empty after cleanup is useless.
    if not cleaned or cleaned.strip("-") == "":
        return ""
    return cleaned


def extract_invoice_number(blob: bytes) -> str:
    """Best-effort extraction. Returns "" if nothing reliable found.

    Errors are swallowed so a malformed PDF doesn't crash the pipeline — the
    ledger just records a blank, Layer B falls back to Layer A.
    """
    try:
        text = extract_full_text(blob)
    except Exception:
        return ""
    if not text:
        return ""

    # Strip the Received/Paid stamp text we add ourselves — otherwise our own
    # stamp's metadata could pollute extraction. The stamps don't contain
    # "Invoice <number>"-style labels today, but stripping is cheap insurance.
    # (No-op currently; placeholder for future stamp text additions.)

    for pat in _INVOICE_NUMBER_PATTERNS:
        m = pat.search(text)
        if m:
            normalized = normalize_invoice_number(m.group(1))
            if normalized:
                return normalized
    return ""


def extract_amount_cents(blob: bytes) -> int | None:
    """Best-effort extraction of the invoice's "what to pay" amount, in cents.

    Returns None when no labelled total can be located. We deliberately do
    NOT pick the largest dollar value on the page — that often grabs a YTD
    figure or a contract value rather than the invoice's actual amount due.
    """
    try:
        text = extract_full_text(blob)
    except Exception:
        return None
    if not text:
        return None

    for pat in _AMOUNT_PATTERNS:
        # Use findall so "Total: 100.00 ... Total: 250.00" picks the LAST one
        # via the second pattern's iteration. But for "Total Due" / "Amount
        # Due" the first hit is usually correct.
        matches = pat.findall(text)
        if not matches:
            continue
        raw = matches[-1] if pat is _AMOUNT_PATTERNS[-1] else matches[0]
        cents = _parse_amount_to_cents(raw)
        if cents is not None:
            return cents
    return None


def _parse_amount_to_cents(raw: str) -> int | None:
    """Convert "1,234.56" -> 123456. Returns None on parse failure."""
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        return int(round(float(cleaned) * 100))
    except (ValueError, TypeError):
        return None


def compute_layer_b(blob: bytes, plan_norm: str) -> tuple[str, int | None]:
    """Convenience: return `(invoice_number, amount_cents)` for the ledger row.

    `plan_norm` is included in the function signature for symmetry with the
    overall fingerprint key, but it's not used during extraction — the caller
    already knows the plan from the filename/subject/PDF match.
    """
    _ = plan_norm  # documented but unused in this function
    inv_num = extract_invoice_number(blob)
    amount = extract_amount_cents(blob)
    return inv_num, amount
