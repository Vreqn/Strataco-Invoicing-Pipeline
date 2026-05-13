"""Smoke test: extract_paid_stamp_values reads back the check number from a flattened Paid stamp.

For each mock invoice in reference/stamp_samples/, applies the Paid stamp
via render_paid_stamp (producing the AcroForm-bearing PDF Step 5 hands to
the accountant), then writes a plain-text check-number value at the field
coordinates — mimicking what Print-to-PDF flattening produces — and
verifies that extract_paid_stamp_values reads it back correctly.

Falls back to a synthesized in-memory invoice if no mocks are present, so
the test stays runnable even after a clean checkout.

Standalone: no pytest dependency. Run with `python tests/test_stamp_read.py`.
Exits 0 if every case passes, 1 otherwise.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib.stamp import (
    LABEL_WIDTH_PT,
    ROW_HEIGHT_PT,
    STAMP_HEIGHT_PAID_PT,
    STAMP_WIDTH_PT,
    StampPlacement,
    find_largest_whitespace_box,
    flatten_acroform,
    render_paid_stamp,
    render_received_stamp,
)
from tools._lib.stamp_read import extract_paid_stamp_values, parse_paid_date

MOCKS_DIR = PROJECT_ROOT / "reference" / "stamp_samples"
MOCK_NAMES = (
    "mock_invoice_1_cascade_plumbing.pdf",
    "mock_invoice_2_westshore_elevator.pdf",
    "mock_invoice_3_northshore_landscape.pdf",
)


def _build_invoice_with_decoy_check_label_pdf(
    decoy_value: str,
    decoy_date: str = "JAN 01 1999",
) -> bytes:
    """Synthesize an invoice whose body text already contains literal
    'Check Number:' AND 'Date:' fields — the kind of vendor-side reference
    that the pre-0.3.0 stamp_read would happily grab instead of the Paid
    stamp values.

    Both decoys are placed near the page header so they sit OUTSIDE the
    rendered Paid stamp's bounding box (the whitespace search picks the
    lower-clean area). The post-fix extractor must skip them and return
    the values sitting inside the Paid stamp region.
    """
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, page_h - 60, "Acme Plumbing Supply Co.")
    c.setFont("Helvetica", 10)
    c.drawString(50, page_h - 78, "123 Main St, Anytown, BC V0V 0V0")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(380, page_h - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 78, "Inv #  INV-2026-1042")
    # The decoys: a "Check Number:" and "Date:" label in the invoice header
    # that reference the vendor's own internal billing fields.
    c.drawString(380, page_h - 92, f"Date: {decoy_date}")
    c.drawString(380, page_h - 106, f"Check Number: {decoy_value}")

    y = page_h - 220
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description")
    c.drawString(490, y, "Amount")
    c.line(50, y - 4, page_w - 50, y - 4)
    c.setFont("Helvetica", 10)
    for desc, amt in [
        ("Emergency boiler service call", "275.00"),
        ("Replacement pressure relief valve", "171.00"),
    ]:
        y -= 18
        c.drawString(50, y, desc)
        c.drawString(490, y, amt)

    c.save()
    return buf.getvalue()


def _build_busy_invoice_pdf() -> bytes:
    """Synthesize a busy invoice in memory. Used only when no real mocks
    are present on disk. Avoids the literal phrase 'Check Number:' so the
    only such label on the page comes from the Paid stamp.
    """
    buf = io.BytesIO()
    page_w, page_h = LETTER
    c = rl_canvas.Canvas(buf, pagesize=LETTER)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, page_h - 60, "Acme Plumbing Supply Co.")
    c.setFont("Helvetica", 10)
    c.drawString(50, page_h - 78, "123 Main St, Anytown, BC V0V 0V0")
    c.drawString(50, page_h - 92, "GST# 123456789  |  Phone: 555-0123")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(380, page_h - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(380, page_h - 78, "Inv #  INV-2026-1042")
    c.drawString(380, page_h - 92, "Issued  2026-04-15")
    c.drawString(380, page_h - 106, "Terms  Net 30")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, page_h - 140, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(50, page_h - 154, "Strataco Management Ltd.")
    c.drawString(50, page_h - 168, "On behalf of Strata Plan BCS 2707")

    y = page_h - 220
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description")
    c.drawString(360, y, "Qty")
    c.drawString(420, y, "Rate")
    c.drawString(490, y, "Amount")
    c.line(50, y - 4, page_w - 50, y - 4)

    items = [
        ("Emergency boiler service call", "1", "275.00", "275.00"),
        ("Replacement pressure relief valve", "2", "85.50", "171.00"),
        ("Pipe insulation, 10 ft", "5", "12.40", "62.00"),
        ("Travel & disposal fee", "1", "45.00", "45.00"),
    ]
    c.setFont("Helvetica", 10)
    for desc, qty, rate, amt in items:
        y -= 18
        c.drawString(50, y, desc)
        c.drawString(360, y, qty)
        c.drawString(420, y, rate)
        c.drawString(490, y, amt)

    y -= 30
    c.line(420, y + 12, page_w - 50, y + 12)
    c.setFont("Helvetica", 10)
    c.drawString(420, y, "Subtotal")
    c.drawString(490, y, "553.00")
    y -= 14
    c.drawString(420, y, "GST (5%)")
    c.drawString(490, y, "27.65")
    y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawString(420, y, "Total")
    c.drawString(490, y, "580.65")

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(50, 80, "Thank you for your business. Please remit payment to the address above.")

    c.save()
    return buf.getvalue()


def _page_size(pdf_bytes: bytes) -> tuple[float, float]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    box = reader.pages[0].mediabox
    return float(box.width), float(box.height)


def _flatten_paid_stamp_values(
    stamped_bytes: bytes,
    placement: StampPlacement,
    page_w_pt: float,
    page_h_pt: float,
    check_value: str,
    date_value: str,
) -> bytes:
    """Overlay both the date and check-number values as plain text at the
    Paid stamp's body-row coordinates. Mimics what Print-to-PDF produces
    when an accountant flattens the AcroForm-bearing PDF the pipeline
    hands them.

    Geometry mirrors stamp._draw_stamp_overlay for the Paid stamp's
    1-title-row + 2-body-row layout (Date is row 0, Check Number is row 1).
    """
    body_count = 2
    body_top_y = placement.y_pt + body_count * ROW_HEIGHT_PT
    value_x = placement.x_pt + LABEL_WIDTH_PT + 2
    date_text_y = (body_top_y - 0 * ROW_HEIGHT_PT - ROW_HEIGHT_PT) + 7
    check_text_y = (body_top_y - 1 * ROW_HEIGHT_PT - ROW_HEIGHT_PT) + 7

    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pagesize=(page_w_pt, page_h_pt))
    c.setFont("Helvetica", 10)
    c.drawString(value_x, date_text_y, date_value)
    c.drawString(value_x, check_text_y, check_value)
    c.save()
    overlay_bytes = overlay_buf.getvalue()

    base = PdfReader(io.BytesIO(stamped_bytes))
    overlay = PdfReader(io.BytesIO(overlay_bytes))
    writer = PdfWriter()
    writer.append_pages_from_reader(base)
    writer.pages[0].merge_page(overlay.pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _check_one(
    label: str,
    invoice_bytes: bytes,
    check_value: str,
    date_value: str,
    expected_month_year: tuple[int, int],
) -> list[str]:
    """Run the full apply-stamp / flatten / extract pipeline against one
    invoice and return a list of failure strings (empty == passed).
    Verifies both the check number AND the date round-trip from the
    flattened stamp.
    """
    placement = find_largest_whitespace_box(
        invoice_bytes, STAMP_WIDTH_PT, STAMP_HEIGHT_PAID_PT
    )
    stamped = render_paid_stamp(invoice_bytes)
    page_w_pt, page_h_pt = _page_size(invoice_bytes)
    flattened = _flatten_paid_stamp_values(
        stamped, placement, page_w_pt, page_h_pt,
        check_value=check_value, date_value=date_value,
    )
    values = extract_paid_stamp_values(flattened)

    failures: list[str] = []
    if values.check_number != check_value:
        failures.append(
            f"[{label}] check_number: expected {check_value!r}, got {values.check_number!r}"
        )
    if values.paid_date != date_value:
        failures.append(
            f"[{label}] paid_date: expected {date_value!r}, got {values.paid_date!r}"
        )
    parsed = parse_paid_date(values.paid_date)
    if parsed != expected_month_year:
        failures.append(
            f"[{label}] parse_paid_date({values.paid_date!r}): "
            f"expected {expected_month_year!r}, got {parsed!r}"
        )

    note = f"  note: {values.note}" if values.note else ""
    status = "OK  " if not failures else "FAIL"
    print(
        f"{status}[{label}] check_number={values.check_number!r} "
        f"paid_date={values.paid_date!r} parsed={parsed!r}{note}"
    )
    return failures


_PARSE_PAID_DATE_CASES: tuple[tuple[str, tuple[int, int] | None], ...] = (
    ("MAY 08 2026", (5, 2026)),         # default `render_paid_stamp` format
    ("May 08 2026", (5, 2026)),
    ("May 8, 2026", (5, 2026)),
    ("March 31 2026", (3, 2026)),
    ("2026-05-08", (5, 2026)),          # ISO
    ("05/08/2026", (5, 2026)),          # MM/DD default
    ("13/08/2026", (8, 2026)),          # day-first (13 > 12)
    ("08/13/2026", (8, 2026)),          # month-first (13 > 12 on right)
    ("", None),                         # empty
    ("not a date", None),
    ("05/08/26", None),                 # 2-digit year rejected
)


def _check_parse_paid_date_units() -> list[str]:
    failures: list[str] = []
    for raw, expected in _PARSE_PAID_DATE_CASES:
        got = parse_paid_date(raw)
        if got != expected:
            failures.append(
                f"parse_paid_date({raw!r}): expected {expected!r}, got {got!r}"
            )
            print(f"FAIL[parse_paid_date] {raw!r} -> {got!r} (expected {expected!r})")
        else:
            print(f"OK  [parse_paid_date] {raw!r} -> {got!r}")
    return failures


def _fill_acroform_via_pikepdf(pdf_bytes: bytes, values: dict[str, str]) -> bytes:
    """Set /V on AcroForm fields whose /T starts with one of `values`' prefixes.

    Mimics what Acrobat does when an operator types into the form and saves:
    the matching widget gets its `/V` populated; we don't regenerate
    appearance streams here (the downstream flatten / render code will do
    that). Keys in `values` are matched as prefixes against the widget's
    `/T`, so e.g. `{"gl_code_": "12345"}` covers `gl_code_<sid>` regardless
    of which timestamp the renderer chose.
    """
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


def _check_acroform_round_trip() -> list[str]:
    """Manager fills Received fields → Step 5 flattens + stamps Paid →
    AP fills Paid fields → Step 6 flatten on archive.

    The whole point of 0.12.0+: no Print-to-PDF, no Kofax, just Ctrl+S in
    Acrobat and the system handles flattening at the right two points.
    0.12.1 moved the flatten call out of `_merge_overlay_onto_page_one`
    (vendor /AcroForm at intake must survive) and into Step 5 directly,
    which is why this test calls `flatten_acroform` explicitly before
    `render_paid_stamp` — matching what `steps/step_5_to_ap.py` now does.
    """
    import pdfplumber

    failures: list[str] = []
    label = "acroform_round_trip"

    # Manager-filled Received stamp.
    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    # Test values picked to NOT be substrings of anything in
    # _build_busy_invoice_pdf — e.g. the busy invoice has "GST# 123456789"
    # so the gl_code can't be "12345" (would match as a substring).
    manager_filled = _fill_acroform_via_pikepdf(
        received,
        {
            "gl_code_": "RCV-77001",
            "amount_": "999.42",
            "approved_": "Manager-Test-Name",
        },
    )

    # Step 5: flatten the manager-filled Received fields, then add the
    # editable Paid stamp.
    flat_received = flatten_acroform(manager_filled)
    after_step5 = render_paid_stamp(flat_received)

    reader = PdfReader(io.BytesIO(after_step5))
    fields = reader.get_fields() or {}
    paid_field_names = sorted(n for n in fields if n.startswith("paid_"))
    other_field_names = sorted(n for n in fields if not n.startswith("paid_"))
    if len(paid_field_names) != 2:
        failures.append(
            f"[{label}] expected 2 paid_* fields after Step 5, got "
            f"{paid_field_names!r}"
        )
    if other_field_names:
        failures.append(
            f"[{label}] expected no non-paid_ fields after Step 5 flatten, "
            f"got {other_field_names!r}"
        )

    with pdfplumber.open(io.BytesIO(after_step5)) as pdf:
        page_text = pdf.pages[0].extract_text() or ""
    for marker in ("RCV-77001", "999.42", "Manager-Test-Name"):
        if marker not in page_text:
            failures.append(
                f"[{label}] expected Received value {marker!r} as static "
                f"text after Step 5 flatten — not found"
            )
        elif page_text.count(marker) > 1:
            failures.append(
                f"[{label}] Received value {marker!r} appears "
                f"{page_text.count(marker)}× after Step 5 flatten — doubling bug"
            )

    # AP fills Paid stamp and saves (Acrobat Ctrl+S).
    ap_filled = _fill_acroform_via_pikepdf(
        after_step5,
        {
            "paid_check_number_": "AP-CHK-44021",
            "paid_date_": "MAY 13 2026",
        },
    )

    # Step 6 reads via the AcroForm tier.
    values = extract_paid_stamp_values(ap_filled)
    if values.check_number != "AP-CHK-44021":
        failures.append(
            f"[{label}] AcroForm read of check_number: "
            f"expected 'AP-CHK-44021', got {values.check_number!r}"
        )
    if values.paid_date != "MAY 13 2026":
        failures.append(
            f"[{label}] AcroForm read of paid_date: "
            f"expected 'MAY 13 2026', got {values.paid_date!r}"
        )
    if values.image_only:
        failures.append(f"[{label}] image_only should be False on a real form PDF")

    # Step 6 flattens before archive write.
    archived = flatten_acroform(ap_filled)
    archived_reader = PdfReader(io.BytesIO(archived))
    archived_fields = archived_reader.get_fields() or {}
    if archived_fields:
        failures.append(
            f"[{label}] archive should have zero editable fields, got "
            f"{sorted(archived_fields)!r}"
        )
    with pdfplumber.open(io.BytesIO(archived)) as pdf:
        archive_text = pdf.pages[0].extract_text() or ""
    for marker in (
        "RCV-77001", "999.42", "Manager-Test-Name",
        "AP-CHK-44021", "MAY 13 2026",
    ):
        if marker not in archive_text:
            failures.append(
                f"[{label}] expected {marker!r} as static text in archive — "
                f"not found"
            )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] step5_fields={len(fields)} archive_fields={len(archived_fields)}")
    return failures


def _check_image_only_detection() -> list[str]:
    """A PDF with no text and no AcroForm should be flagged image_only.

    Mirrors what Microsoft 'Print to PDF' produces from a form-bearing
    invoice: a page composed of raster image XObjects, zero text, no
    /AcroForm. Step 6 turns image_only into a more useful operator message.
    """
    import pikepdf

    label = "image_only_detection"
    failures: list[str] = []

    # Build a 1x1 JPEG and embed it as the only page content via pikepdf.
    from PIL import Image as _PIL
    jpeg_buf = io.BytesIO()
    _PIL.new("RGB", (10, 10), color="white").save(jpeg_buf, format="JPEG")
    jpeg_bytes = jpeg_buf.getvalue()

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]
    img = pikepdf.Stream(pdf, jpeg_bytes)
    img.Type = pikepdf.Name("/XObject")
    img.Subtype = pikepdf.Name("/Image")
    img.Width = 10
    img.Height = 10
    img.ColorSpace = pikepdf.Name("/DeviceRGB")
    img.BitsPerComponent = 8
    img.Filter = pikepdf.Name("/DCTDecode")
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Image1=img))
    page.Contents = pikepdf.Stream(pdf, b"q 612 0 0 792 0 0 cm /Image1 Do Q")
    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    image_only_bytes = out.getvalue()

    values = extract_paid_stamp_values(image_only_bytes)
    if not values.image_only:
        failures.append(
            f"[{label}] expected image_only=True, got "
            f"{values.image_only!r} (note={values.note!r})"
        )
    if values.has_check_number:
        failures.append(
            f"[{label}] expected empty check_number, got "
            f"{values.check_number!r}"
        )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] image_only={values.image_only}")
    return failures


def _check_vendor_acroform_survives_intake() -> list[str]:
    """Codex #1: vendor /AcroForm must survive Step 1/3 unchanged.

    A vendor sometimes sends a fillable PDF that the manager or AP needs
    to complete. If `render_received_stamp` flattens that on intake, the
    operator can't fill it any more. The 0.12.1 fix moves the flatten out
    of `_merge_overlay_onto_page_one` so intake is non-destructive.
    """
    import pikepdf
    label = "vendor_acroform_survives_intake"
    failures: list[str] = []

    # Build a PDF with a synthetic "vendor" AcroForm widget on page 1.
    invoice = _build_busy_invoice_pdf()
    pdf = pikepdf.Pdf.open(io.BytesIO(invoice))
    page = pdf.pages[0]
    annot = pikepdf.Dictionary(
        Type=pikepdf.Name("/Annot"),
        Subtype=pikepdf.Name("/Widget"),
        FT=pikepdf.Name("/Tx"),
        T=pikepdf.String("vendor_email"),
        Rect=pikepdf.Array([400, 600, 560, 620]),
        F=4,
        DA=pikepdf.String("/Helv 10 Tf 0 g"),
    )
    annot_ref = pdf.make_indirect(annot)
    page.Annots = pikepdf.Array([annot_ref])
    pdf.Root.AcroForm = pikepdf.Dictionary(
        Fields=pikepdf.Array([annot_ref]),
        NeedAppearances=True,
    )
    buf = io.BytesIO(); pdf.save(buf); pdf.close()
    with_vendor_form = buf.getvalue()

    # Apply the Received stamp (the intake stamp). Vendor widget must
    # survive AND appear in /AcroForm/Fields so it stays editable.
    received = render_received_stamp(with_vendor_form, "MAY 12 2026", "BCS 2707")
    reader = PdfReader(io.BytesIO(received))
    fields = reader.get_fields() or {}

    strataco_names = sorted(
        n for n in fields
        if n.startswith(("gl_code_", "chargeback_", "amount_", "approved_"))
    )
    if not strataco_names:
        failures.append(
            f"[{label}] no Strataco stamp fields after render_received_stamp"
        )
    if "vendor_email" not in fields:
        failures.append(
            f"[{label}] vendor_email widget orphaned — got fields {sorted(fields)!r}"
        )
    # Vendor /V must still be empty (no flatten happened).
    vendor_v = fields.get("vendor_email", {}).get("/V") if "vendor_email" in fields else None
    if vendor_v is not None and str(vendor_v).strip() != "":
        failures.append(
            f"[{label}] vendor_email /V should be empty (untouched), got {vendor_v!r}"
        )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] vendor_email in fields={'vendor_email' in fields}")
    return failures


def _check_step5_flatten_locks_vendor_and_received() -> list[str]:
    """Codex #1 + Step 5 contract: Step 5 flattens whatever the manager
    filled — Strataco Received-stamp values AND vendor fields — before
    adding the editable Paid stamp.
    """
    import pikepdf
    import pdfplumber

    label = "step5_flatten_locks_vendor_and_received"
    failures: list[str] = []

    # Same setup as vendor_acroform_survives_intake.
    invoice = _build_busy_invoice_pdf()
    pdf = pikepdf.Pdf.open(io.BytesIO(invoice))
    page = pdf.pages[0]
    annot = pikepdf.Dictionary(
        Type=pikepdf.Name("/Annot"),
        Subtype=pikepdf.Name("/Widget"),
        FT=pikepdf.Name("/Tx"),
        T=pikepdf.String("vendor_po_number"),
        Rect=pikepdf.Array([400, 580, 560, 600]),
        F=4,
        DA=pikepdf.String("/Helv 10 Tf 0 g"),
    )
    annot_ref = pdf.make_indirect(annot)
    page.Annots = pikepdf.Array([annot_ref])
    pdf.Root.AcroForm = pikepdf.Dictionary(
        Fields=pikepdf.Array([annot_ref]),
        NeedAppearances=True,
    )
    buf = io.BytesIO(); pdf.save(buf); pdf.close()
    with_vendor_form = buf.getvalue()

    received = render_received_stamp(with_vendor_form, "MAY 12 2026", "BCS 2707")

    # Manager fills Strataco fields + the vendor PO field.
    manager_filled = _fill_acroform_via_pikepdf(received, {
        "gl_code_": "GL-V77",
        "approved_": "Manager-Vendor-Test",
        "vendor_po_number": "PO-XYZ-998",
    })

    # Step 5 path: explicit flatten → render_paid_stamp.
    flat_received = flatten_acroform(manager_filled)
    after_step5 = render_paid_stamp(flat_received)

    reader = PdfReader(io.BytesIO(after_step5))
    fields = reader.get_fields() or {}
    paid_names = sorted(n for n in fields if n.startswith("paid_"))
    other_names = sorted(n for n in fields if not n.startswith("paid_"))
    if len(paid_names) != 2:
        failures.append(
            f"[{label}] expected 2 paid_* fields after Step 5, got {paid_names!r}"
        )
    if other_names:
        failures.append(
            f"[{label}] non-paid fields still editable after Step 5 flatten: {other_names!r}"
        )

    with pdfplumber.open(io.BytesIO(after_step5)) as pdf2:
        text = pdf2.pages[0].extract_text() or ""
    for marker in ("GL-V77", "Manager-Vendor-Test", "PO-XYZ-998"):
        if marker not in text:
            failures.append(
                f"[{label}] expected baked text {marker!r} after Step 5 — not found"
            )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] paid_only_editable={len(other_names) == 0}")
    return failures


def _check_step6_fails_closed_on_flatten_error() -> list[str]:
    """Codex #3: Step 6 must NOT silently archive unflattened on error.

    Run `_archive_one` against a real Paid-stamped + AP-filled PDF, with
    `flatten_acroform` monkeypatched to raise. Expected behaviour: a
    paid_failed entry is added, the source PDF stays in place, and no
    archive file is written.
    """
    import tempfile
    from unittest.mock import patch

    from tools._lib import dup_ledger
    from tools._lib.xls import PlanRow
    from steps import step_6_paid_archive as step6

    label = "step6_fails_closed_on_flatten_error"
    failures: list[str] = []

    # Build a real Paid-stamped + AP-filled PDF so `_archive_one` reaches
    # the flatten block (it short-circuits earlier if the Paid stamp
    # can't be read).
    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat_received = flatten_acroform(received)
    after_step5 = render_paid_stamp(flat_received)
    ap_filled = _fill_acroform_via_pikepdf(after_step5, {
        "paid_check_number_": "FAIL-CHK-1",
        "paid_date_": "MAY 13 2026",
    })

    with tempfile.TemporaryDirectory() as tdir:
        tdir_path = Path(tdir)
        ap_paid = tdir_path / "Users" / "AP" / "Paid_Invoices"
        ap_paid.mkdir(parents=True)
        pdf_path = ap_paid / "BCS 2707 - Invoice fail.pdf"
        pdf_path.write_bytes(ap_filled)

        plan_row = PlanRow(
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
        plan_to_path = {"BCS2707": plan_row}

        out = step6._Outcomes()
        ledger = dup_ledger.Ledger(rows=[], path=tdir_path / "ledger.csv")

        class _Run:
            def __init__(self):
                self.errors: list[str] = []
            def error(self, msg): self.errors.append(msg)
            def info(self, msg): pass

        run = _Run()

        # Patch the imports inside step_6 so flatten and the archive
        # folder lookup are both under test control.
        from tools._lib import paths as _paths
        archive_dir = tdir_path / "Strata_Plans" / "BCS 2707"
        archive_dir.mkdir(parents=True)

        with patch.object(
            step6, "flatten_acroform",
            side_effect=RuntimeError("simulated pikepdf failure"),
        ), patch.object(_paths, "strata_plan_folder", return_value=archive_dir):
            step6._archive_one(pdf_path, plan_to_path, out, ledger, run, "Test AP")

        # Assertions
        flatten_failures = [
            r for r in out.unmatched
            if "Archive flatten failed" in r.get("reason", "")
        ]
        if not flatten_failures:
            failures.append(
                f"[{label}] expected an `out.unmatched` entry with 'Archive "
                f"flatten failed', got {[r.get('reason') for r in out.unmatched]!r}"
            )
        if not pdf_path.exists():
            failures.append(
                f"[{label}] AP source was deleted despite flatten failure"
            )
        archive_files = list(archive_dir.glob("*.pdf"))
        if archive_files:
            failures.append(
                f"[{label}] archive wrote despite flatten failure: "
                f"{[p.name for p in archive_files]!r}"
            )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] errors={len(failures)}")
    return failures


def _check_archive_sha256_round_trip() -> list[str]:
    """Codex #4: Step 6's ledger row carries both chain SHA and archive SHA.

    The two hash different bytes (pre-flatten vs post-flatten), so they
    must be distinct, AND `dup_reconcile` must recognise an archive file
    via the archive SHA index.
    """
    from tools._lib import dup_fingerprint

    label = "archive_sha256_round_trip"
    failures: list[str] = []

    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    manager_filled = _fill_acroform_via_pikepdf(received, {
        "gl_code_": "GL-AR1", "approved_": "AR-Test",
    })
    flat_received = flatten_acroform(manager_filled)
    after_step5 = render_paid_stamp(flat_received)
    ap_filled = _fill_acroform_via_pikepdf(after_step5, {
        "paid_check_number_": "AR-CHK-1",
        "paid_date_": "MAY 13 2026",
    })

    pre_flatten_sha = dup_fingerprint.sha256_of(ap_filled)
    archive_bytes = flatten_acroform(ap_filled)
    post_flatten_sha = dup_fingerprint.sha256_of(archive_bytes)

    if pre_flatten_sha == post_flatten_sha:
        failures.append(
            f"[{label}] chain SHA and archive SHA should differ; both = "
            f"{pre_flatten_sha[:12]}..."
        )

    # Round-trip through the ledger to make sure the schema persists it.
    import tempfile
    from tools._lib import dup_ledger
    with tempfile.TemporaryDirectory() as tdir:
        path = Path(tdir) / "ledger.csv"
        led = dup_ledger.Ledger(rows=[], path=path)
        row = dup_ledger.make_row(
            sha256=pre_flatten_sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=str(Path(tdir) / "archive.pdf"),
            archive_sha256=post_flatten_sha,
        )
        led.upsert(row)
        # Re-load from disk.
        led2 = dup_ledger.load(path)
        got = led2.find_by_hash(pre_flatten_sha)
        if got is None:
            failures.append(f"[{label}] row missing after reload")
        elif got.archive_sha256 != post_flatten_sha:
            failures.append(
                f"[{label}] archive_sha256 not persisted: "
                f"got {got.archive_sha256!r}, expected {post_flatten_sha!r}"
            )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] chain!=archive sha")
    return failures


def _check_dup_reconcile_recognises_archive_via_either_sha() -> list[str]:
    """Codex #4: `tools/dup_reconcile.py` must find archived files via
    EITHER the chain `sha256` or the new `archive_sha256` index.
    """
    import tempfile
    from tools._lib import dup_fingerprint, dup_ledger
    from tools import dup_reconcile

    label = "dup_reconcile_via_archive_sha"
    failures: list[str] = []

    archive_content = b"this represents flattened archive bytes"
    chain_sha = "f" * 64  # made-up chain SHA — file on disk hashes differently
    archive_sha = dup_fingerprint.sha256_of(archive_content)

    with tempfile.TemporaryDirectory() as tdir:
        archive_path = Path(tdir) / "archived.pdf"
        archive_path.write_bytes(archive_content)

        led_path = Path(tdir) / "ledger.csv"
        led = dup_ledger.Ledger(rows=[], path=led_path)
        led.upsert(dup_ledger.make_row(
            sha256=chain_sha,
            plan_norm="BCS2707",
            current_stage="archived",
            archive_path=str(archive_path),
            archive_sha256=archive_sha,
        ))

        by_hash = {r.sha256: r for r in led.all_rows()}
        by_archive_hash = {
            r.archive_sha256: r for r in led.all_rows() if r.archive_sha256
        }

        # Replicate dup_reconcile's orphan check.
        on_disk_sha = dup_fingerprint.sha256_of(archive_path.read_bytes())
        is_orphan = (
            on_disk_sha not in by_hash and on_disk_sha not in by_archive_hash
        )
        if is_orphan:
            failures.append(
                f"[{label}] archive was treated as orphan even with archive_sha256"
            )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] orphan_check_uses_both_indexes")
    return failures


def _check_multipage_image_only() -> list[str]:
    """Codex #5: image_only must look at page 1 only.

    A 2-page PDF with rasterised page 1 + text-bearing page 2 must
    still flag as image_only — page 2 having text doesn't make page 1
    readable.
    """
    import pikepdf
    from PIL import Image as _PIL

    label = "multipage_image_only"
    failures: list[str] = []

    # Page 1: a single JPEG image, no text.
    jpeg_buf = io.BytesIO()
    _PIL.new("RGB", (10, 10), color="white").save(jpeg_buf, format="JPEG")
    jpeg_bytes = jpeg_buf.getvalue()

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page1 = pdf.pages[0]
    img = pikepdf.Stream(pdf, jpeg_bytes)
    img.Type = pikepdf.Name("/XObject")
    img.Subtype = pikepdf.Name("/Image")
    img.Width = 10; img.Height = 10
    img.ColorSpace = pikepdf.Name("/DeviceRGB")
    img.BitsPerComponent = 8
    img.Filter = pikepdf.Name("/DCTDecode")
    page1.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Image1=img))
    page1.Contents = pikepdf.Stream(pdf, b"q 612 0 0 792 0 0 cm /Image1 Do Q")

    # Page 2: a reportlab-rendered text page (real text on this page).
    page2_buf = io.BytesIO()
    c2 = rl_canvas.Canvas(page2_buf, pagesize=LETTER)
    c2.setFont("Helvetica", 10)
    c2.drawString(50, 700, "Page 2: cover letter text the operator must read.")
    c2.save()
    page2_pdf = pikepdf.Pdf.open(io.BytesIO(page2_buf.getvalue()))
    pdf.pages.append(page2_pdf.pages[0])

    out = io.BytesIO(); pdf.save(out); pdf.close()
    multipage_bytes = out.getvalue()

    values = extract_paid_stamp_values(multipage_bytes)
    if not values.image_only:
        failures.append(
            f"[{label}] expected image_only=True, got False "
            f"(values: check={values.check_number!r} date={values.paid_date!r})"
        )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] image_only={values.image_only}")
    return failures


def _check_partial_acroform_fail_closed() -> list[str]:
    """Codex #6: if AcroForm has /V on one paid_* field but not the
    other, the function must NOT fall through to positioned text to
    fill in the missing one. Partial = partial.
    """
    label = "partial_acroform_fail_closed"
    failures: list[str] = []

    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat = flatten_acroform(received)
    after_step5 = render_paid_stamp(flat)

    # AP only fills the check number — paid_date stays empty.
    partial = _fill_acroform_via_pikepdf(after_step5, {
        "paid_check_number_": "PARTIAL-CHK-1",
        # paid_date_ deliberately left unset
    })

    values = extract_paid_stamp_values(partial)
    if values.check_number != "PARTIAL-CHK-1":
        failures.append(
            f"[{label}] AcroForm check_number lost: got {values.check_number!r}"
        )
    if values.paid_date.strip():
        failures.append(
            f"[{label}] paid_date should be empty (no positional fallback), "
            f"got {values.paid_date!r}"
        )
    if values.note != "check_number/paid_date from AcroForm":
        failures.append(
            f"[{label}] note should indicate AcroForm tier, got {values.note!r}"
        )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] check={values.check_number!r} date={values.paid_date!r}")
    return failures


def _check_multipage_regex_no_false_positive() -> list[str]:
    """Codex #7: a vendor remittance stub on page 2 with "PAID — Check
    Number: 999" must not be picked up when page 1's PAID stamp has no
    values. Regex fallback must scope to page 1.
    """
    import pikepdf

    label = "multipage_regex_no_false_positive"
    failures: list[str] = []

    # Page 1: invoice with empty Paid stamp widgets (no /V values).
    invoice = _build_busy_invoice_pdf()
    received = render_received_stamp(invoice, "MAY 12 2026", "BCS 2707")
    flat = flatten_acroform(received)
    page1_pdf_bytes = render_paid_stamp(flat)

    # Page 2: reportlab text with the false-positive bait.
    page2_buf = io.BytesIO()
    c2 = rl_canvas.Canvas(page2_buf, pagesize=LETTER)
    c2.setFont("Helvetica", 10)
    c2.drawString(50, 700, "Remittance — PAID")
    c2.drawString(50, 680, "Check Number: 99999999 (vendor reference, not ours)")
    c2.drawString(50, 660, "Date: JAN 01 1999")
    c2.save()

    pdf = pikepdf.Pdf.open(io.BytesIO(page1_pdf_bytes))
    page2_pdf = pikepdf.Pdf.open(io.BytesIO(page2_buf.getvalue()))
    pdf.pages.append(page2_pdf.pages[0])
    out = io.BytesIO(); pdf.save(out); pdf.close()
    multi = out.getvalue()

    # Now empty the page-1 Paid widgets so the AcroForm tier returns
    # nothing — that's the case where the regex fallback runs.
    multi_clean = _fill_acroform_via_pikepdf(multi, {
        "paid_check_number_": "",
        "paid_date_": "",
    })

    values = extract_paid_stamp_values(multi_clean)
    if values.check_number == "99999999":
        failures.append(
            f"[{label}] regex fallback grabbed page-2 vendor check number"
        )
    if values.paid_date and "1999" in values.paid_date:
        failures.append(
            f"[{label}] regex fallback grabbed page-2 vendor date: "
            f"{values.paid_date!r}"
        )

    status = "OK  " if not failures else "FAIL"
    print(f"{status}[{label}] check={values.check_number!r} date={values.paid_date!r}")
    return failures


def main() -> int:
    all_failures: list[str] = _check_parse_paid_date_units()
    all_failures.extend(_check_acroform_round_trip())
    all_failures.extend(_check_image_only_detection())
    all_failures.extend(_check_vendor_acroform_survives_intake())
    all_failures.extend(_check_step5_flatten_locks_vendor_and_received())
    all_failures.extend(_check_step6_fails_closed_on_flatten_error())
    all_failures.extend(_check_archive_sha256_round_trip())
    all_failures.extend(_check_dup_reconcile_recognises_archive_via_either_sha())
    all_failures.extend(_check_multipage_image_only())
    all_failures.extend(_check_partial_acroform_fail_closed())
    all_failures.extend(_check_multipage_regex_no_false_positive())

    cases: list[tuple[str, bytes, str, str, tuple[int, int]]] = []

    available_mocks = [p for p in (MOCKS_DIR / n for n in MOCK_NAMES) if p.is_file()]
    if available_mocks:
        for i, path in enumerate(available_mocks):
            cases.append((path.name, path.read_bytes(), f"1234{i}", "MAR 03 2026", (3, 2026)))
    else:
        print("(no real mocks found in reference/stamp_samples/ — using synthesized invoice)")
        cases.append(("synthesized", _build_busy_invoice_pdf(), "12345", "MAR 03 2026", (3, 2026)))

    # 0.3.0 regression: invoice contains its own "Check Number:" AND "Date:"
    # labels outside the Paid stamp region. The extractor must pick the
    # stamp's values, not the decoys. Pre-fix code (whole-page search)
    # would grab the decoys first.
    cases.append((
        "decoy_check_and_date",
        _build_invoice_with_decoy_check_label_pdf(
            decoy_value="99999", decoy_date="JAN 01 1999",
        ),
        "55555",
        "MAY 08 2026",
        (5, 2026),
    ))

    for label, pdf_bytes, check_value, date_value, expected_my in cases:
        all_failures.extend(
            _check_one(label, pdf_bytes, check_value, date_value, expected_my)
        )

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
