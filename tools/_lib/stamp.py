"""Render the Received and Paid stamps onto invoice PDFs.

Replaces the external 192.168.123.183:5055/stamp-pdf service. Uses:
- pypdfium2  — rasterize page 1 to a PIL image (no poppler needed on Windows)
- numpy      — integral-image whitespace search
- reportlab  — draw the stamp graphics + AcroForm text fields on a transparent overlay
- pypdf      — merge the overlay onto page 1, propagate widget annotations,
               build /AcroForm in the document catalog

Visual reference: q:\\AI Automation\\Strataco Invoicing\\Recieved Stamp Ex #1.pdf
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib.pdf_text import extract_page_words

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- constants

RED = Color(0.75, 0.05, 0.05)
BLUE = Color(0.10, 0.20, 0.65)

# Stamp dimensions (PDF points, 1 pt = 1/72 inch)
STAMP_WIDTH_PT = 210
STAMP_HEIGHT_RECEIVED_PT = 180   # 7 body rows
STAMP_HEIGHT_PAID_PT = 100       # title band (~52pt) + 2 body rows
ROW_HEIGHT_PT = 24
LABEL_WIDTH_PT = 90
PADDING_PT = 6
BORDER_WIDTH_PT = 1.0
FIELD_HEIGHT_PT = 16
TITLE_FONT_SIZE = 22             # Paid stamp header — sized as a title
HINT_FONT_SIZE = 7               # date-format caption in the Paid title band
HINT_COLOR = Color(0.5, 0.5, 0.5)

# Whitespace search
RASTER_DPI = 100
WHITE_THRESHOLD = 245
WHITE_PCT_MIN = 0.995          # strict pass — combined with PER_ROW_WHITE_PCT_MIN
WHITE_PCT_FALLBACK = 0.985     # loose pass when the strict pass finds nothing
PER_ROW_WHITE_PCT_MIN = 0.95   # strict-pass guard: every row inside the candidate
                               # window must be at least this white. Catches text
                               # lines that span the full stamp width even when
                               # the overall area is "mostly white".
PAGE_MARGIN_PT = 36            # No-go zone around the page edges (0.5 inch). The
                               # stamp bounding box is never placed inside this
                               # margin — keeps stamps away from the physical edge
                               # in case of print/save quirks, and prevents the
                               # algorithm from clustering stamps right next to
                               # bottom-of-page footers.

# Fallback placement — used only when ALL search tiers fail (essentially
# impossible on a real invoice). Lower-LEFT, not lower-right, because
# invoices almost universally put the grand total in the lower-right.
# Defense-in-depth: even if forbidden-band detection broke and every
# search tier exhausted, the worst-case landing is the side of the page
# that's overwhelmingly empty. Pre-0.13 this lived in the lower-right
# (x=360) and the algorithm dropped Received stamps directly onto the
# Amount Due / Balance Due rows on dense invoices.
FALLBACK_X_PT = PAGE_MARGIN_PT  # left edge of the inner page area
FALLBACK_Y_PT = 60              # PDF y is bottom-up; 60 pt from the bottom

# Semantic exclusion zones — the stamp must never cover the totals block
# or the invoice-number row. These outrank pixel-whitespace: even a clean
# patch of pixels gets vetoed if it would land across one of these rows.
# See `_extract_forbidden_bands`.
BAND_PAD_PT = 4   # vertical breathing room so the stamp can't visually brush the row
_LINE_GROUP_Y_TOLERANCE_PT = 2.0  # words within this many points share a "line"
_TRAILING_PUNCT_FOR_NORMALIZATION = ":,;."

# Labels that mark the totals block. Token-boundary matched after
# normalization (lowercase, trailing :,;. stripped), so `Subtotal` and
# `Total` are distinct matches (no substring confusion). Krisztian's
# directive (2026-05-13): exclude the whole summary block, not just the
# bottom-line "grand total" — vendors disagree on which row is "the"
# total, and covering Subtotal/GST also looks wrong.
_TOTAL_LABELS_RAW: tuple[str, ...] = (
    "Subtotal",
    "Total",
    "Invoice Total",
    "Grand Total",
    "Total Due",
    "Amount Due",
    "Amount Owing",
    "Balance Due",
    "Balance Owing",
    "Amount Paid",
    "Account Balance",
    "GST",
    "PST",
    "HST",
)

# Labels that mark the invoice-number row.
_INVOICE_NUMBER_LABELS_RAW: tuple[str, ...] = (
    "Invoice #",
    "Invoice No",
    "Invoice Number",
    "Inv #",
    "Inv No",
)


def _normalize_token(text: str) -> str:
    return text.lower().rstrip(_TRAILING_PUNCT_FOR_NORMALIZATION)


_TOTAL_LABEL_TOKENS: tuple[tuple[str, ...], ...] = tuple(
    tuple(_normalize_token(t) for t in label.split())
    for label in _TOTAL_LABELS_RAW
)
_INVOICE_NUMBER_LABEL_TOKENS: tuple[tuple[str, ...], ...] = tuple(
    tuple(_normalize_token(t) for t in label.split())
    for label in _INVOICE_NUMBER_LABELS_RAW
)
_ALL_FORBIDDEN_LABEL_TOKENS: tuple[tuple[str, ...], ...] = (
    _TOTAL_LABEL_TOKENS + _INVOICE_NUMBER_LABEL_TOKENS
)


# ---------------------------------------------------------------- whitespace


@dataclass
class StampPlacement:
    x_pt: float
    y_pt: float
    width_pt: float
    height_pt: float
    fallback_used: bool


def _group_words_into_lines(
    words: list[dict],
    y_tolerance_pt: float = _LINE_GROUP_Y_TOLERANCE_PT,
) -> list[list[dict]]:
    """Cluster pdfplumber word records into reading lines by `top` coordinate."""
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = []
    current_top: float | None = None
    for w in sorted_words:
        if current_top is None or abs(w["top"] - current_top) <= y_tolerance_pt:
            current.append(w)
            if current_top is None:
                current_top = w["top"]
        else:
            lines.append(current)
            current = [w]
            current_top = w["top"]
    if current:
        lines.append(current)
    return lines


def _line_matches_any_label(
    line_tokens: tuple[str, ...],
    label_token_sets: tuple[tuple[str, ...], ...],
) -> bool:
    """True if any label appears as a contiguous run in the line's tokens."""
    n = len(line_tokens)
    for label in label_token_sets:
        m = len(label)
        if m == 0 or m > n:
            continue
        for i in range(n - m + 1):
            if line_tokens[i : i + m] == label:
                return True
    return False


def _extract_forbidden_bands(pdf_bytes: bytes) -> list[tuple[float, float]]:
    """Return forbidden y-bands `(top_pt, bottom_pt)` where the stamp must
    not land. Coordinates are top-down PDF points (origin = top of page),
    matching pdfplumber's `top`/`bottom` convention.

    Each band corresponds to a line of text whose normalized tokens match
    one of the totals-block or invoice-number labels. Bands are padded by
    `BAND_PAD_PT` on both sides so the stamp can't visually brush the row.

    Returns `[]` on any extraction failure — better to lose semantic
    protection than to crash the stamp pipeline. The hard-coded fallback
    is also in the lower-LEFT now, so even with empty bands the
    last-resort landing is the safe side of the page.
    """
    try:
        words = extract_page_words(pdf_bytes, page_index=0)
    except Exception as exc:
        logger.warning(
            "forbidden-band text extraction failed (%s); proceeding without semantic exclusions",
            exc,
        )
        return []

    if not words:
        return []

    bands: list[tuple[float, float]] = []
    for line in _group_words_into_lines(words):
        tokens = tuple(_normalize_token(w["text"]) for w in line)
        if _line_matches_any_label(tokens, _ALL_FORBIDDEN_LABEL_TOKENS):
            top = min(float(w["top"]) for w in line) - BAND_PAD_PT
            bottom = max(float(w["bottom"]) for w in line) + BAND_PAD_PT
            bands.append((top, bottom))
    return bands


def _intersects_any_band(
    y_px: int,
    h_px: int,
    bands_px: list[tuple[int, int]],
) -> bool:
    """True if pixel rectangle `[y_px, y_px+h_px)` overlaps any band."""
    candidate_bottom = y_px + h_px
    for band_top, band_bottom in bands_px:
        if y_px < band_bottom and candidate_bottom > band_top:
            return True
    return False


def _rasterize_page_one(pdf_bytes: bytes, dpi: int = RASTER_DPI) -> tuple[Image.Image, float, float]:
    """Return (PIL image of page 1, page_width_pt, page_height_pt)."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    if len(pdf) == 0:
        raise ValueError("PDF has no pages")
    page = pdf[0]
    w_pt, h_pt = page.get_size()
    scale = dpi / 72.0
    image = page.render(scale=scale, grayscale=True).to_pil()
    page.close()
    pdf.close()
    return image, float(w_pt), float(h_pt)


def find_largest_whitespace_box(
    pdf_bytes: bytes,
    target_width_pt: float,
    target_height_pt: float,
) -> StampPlacement:
    """Find a rectangle of mostly-white pixels that fits the stamp.

    Hard constraints (in priority order):
      1. Forbidden bands — the stamp must never cover a totals-block row
         (Subtotal / Total / Amount Due / etc.) or the invoice-number
         row. These are computed from page-1 text via pdfplumber and
         outrank pixel whitespace: a clean patch is vetoed if it
         intersects any forbidden band. See `_extract_forbidden_bands`.
      2. Page-edge margin — bounding box must sit entirely inside
         `PAGE_MARGIN_PT` on every side. Keeps stamps off the physical
         edge and away from edge-adjacent footers.

    Three-tier search (each tier respects both constraints above):
      1. Strict — overall whitespace ≥ WHITE_PCT_MIN AND every horizontal
         pixel row in the window ≥ PER_ROW_WHITE_PCT_MIN. Rejects
         "mostly white but text running through" areas.
      2. Loose — overall whitespace ≥ WHITE_PCT_FALLBACK only. Catches
         dense invoices where strict misses.
      3. Last-resort — `min_total = 0`, no whitespace floor at all.
         Picks the cleanest exclusion-free rectangle even if it overlaps
         line-item text. Krisztian's directive: covering line items is
         acceptable; covering the grand total or invoice number is not.

    Scoring within each tier: pure "cleanest area wins" by white-pixel
    count, with last-seen tiebreaker (scan order is top-to-bottom,
    left-to-right, so ties resolve to bottom-right of the equivalent set).

    Returns a `StampPlacement` in PDF point coordinates (origin = bottom-left).
    Falls back to `(FALLBACK_X_PT, FALLBACK_Y_PT)` — now lower-LEFT — only
    when ALL three tiers find nothing (essentially impossible on a real
    invoice; would require the target rectangle to be larger than the
    entire unforbidden region of the page).
    """
    image, page_w_pt, page_h_pt = _rasterize_page_one(pdf_bytes)
    arr = np.asarray(image, dtype=np.uint8)
    if arr.ndim == 3:
        arr = arr[..., 0]
    H, W = arr.shape
    px_per_pt = W / page_w_pt

    target_w_px = max(1, int(round(target_width_pt * px_per_pt)))
    target_h_px = max(1, int(round(target_height_pt * px_per_pt)))
    margin_px = max(0, int(round(PAGE_MARGIN_PT * px_per_pt)))

    if target_w_px + 2 * margin_px > W or target_h_px + 2 * margin_px > H:
        logger.warning("stamp + margin larger than page; falling back to default position")
        return StampPlacement(FALLBACK_X_PT, FALLBACK_Y_PT, target_width_pt, target_height_pt, True)

    # Forbidden bands in PDF points, then converted to image pixel rows.
    # pdfplumber's top-down y matches PIL's top-down y, so no flip needed.
    bands_pt = _extract_forbidden_bands(pdf_bytes)
    bands_px: list[tuple[int, int]] = [
        (
            max(0, int(round(top_pt * px_per_pt))),
            min(H, int(round(bottom_pt * px_per_pt))),
        )
        for top_pt, bottom_pt in bands_pt
    ]
    if bands_px:
        logger.info("stamp forbidden bands (pixels, top-down): %s", bands_px)

    binary = (arr >= WHITE_THRESHOLD).astype(np.int32)
    # Pad integral image with a zero row/column so we don't need branches in the
    # rectangle-sum formula. II_pad[y+1, x+1] = sum of binary[:y+1, :x+1].
    II_pad = np.zeros((H + 1, W + 1), dtype=np.int64)
    II_pad[1:, 1:] = binary.cumsum(0).cumsum(1)

    def _rect_sum(y: int, x: int, h: int, w: int) -> int:
        return int(
            II_pad[y + h, x + w]
            - II_pad[y, x + w]
            - II_pad[y + h, x]
            + II_pad[y, x]
        )

    def _all_rows_clean(y: int, x: int, min_per_row: int) -> bool:
        # Each row sum = II_pad[y+1, x+w] - II_pad[y+1, x] - II_pad[y, x+w] + II_pad[y, x].
        # Vectorised across all rows in the window.
        right = II_pad[y + 1 : y + target_h_px + 1, x + target_w_px]
        left = II_pad[y + 1 : y + target_h_px + 1, x]
        right_above = II_pad[y : y + target_h_px, x + target_w_px]
        left_above = II_pad[y : y + target_h_px, x]
        row_sums = right - left - right_above + left_above
        return bool(np.all(row_sums >= min_per_row))

    stride_x = max(target_w_px // 12, 5)
    stride_y = max(target_h_px // 12, 5)
    needed_strict = int(target_w_px * target_h_px * WHITE_PCT_MIN)
    needed_loose = int(target_w_px * target_h_px * WHITE_PCT_FALLBACK)
    needed_per_row = int(target_w_px * PER_ROW_WHITE_PCT_MIN)

    # Search range honours the page-edge margin: the stamp bounding box is
    # never placed inside [0, margin) or (W - margin, W] horizontally, nor
    # in the equivalent vertical bands.
    y_lo = margin_px
    y_hi = H - target_h_px - margin_px
    x_lo = margin_px
    x_hi = W - target_w_px - margin_px

    def _search(min_total: int, require_clean_rows: bool) -> tuple[int, int] | None:
        best_score = -1
        best_xy: tuple[int, int] | None = None
        for y in range(y_lo, y_hi + 1, stride_y):
            if _intersects_any_band(y, target_h_px, bands_px):
                continue
            for x in range(x_lo, x_hi + 1, stride_x):
                total = _rect_sum(y, x, target_h_px, target_w_px)
                if total < min_total:
                    continue
                if require_clean_rows and not _all_rows_clean(y, x, needed_per_row):
                    continue
                # Pure "cleanest area wins" by white-pixel count. Use >= so that
                # among truly tied candidates (e.g. several pure-white spots),
                # the LAST one seen wins — and since the search scans top-to-
                # bottom and left-to-right, that's the bottom-right tied spot.
                # Matches the conventional "stamps go below the content" look
                # without overriding genuinely cleaner candidates elsewhere.
                if total >= best_score:
                    best_score = total
                    best_xy = (x, y)
        return best_xy

    best_xy = _search(needed_strict, require_clean_rows=True)
    tier = "strict"
    if best_xy is None:
        best_xy = _search(needed_loose, require_clean_rows=False)
        tier = "loose"
    if best_xy is None:
        # Last-resort: any exclusion-free rectangle. May cover line-item text;
        # totals and invoice number stay visible.
        best_xy = _search(min_total=0, require_clean_rows=False)
        tier = "last_resort"

    if best_xy is None:
        logger.error(
            "stamp last-resort exhausted (target too large for unforbidden region); "
            "using lower-left fallback at (%d, %d) pt",
            FALLBACK_X_PT, FALLBACK_Y_PT,
        )
        return StampPlacement(FALLBACK_X_PT, FALLBACK_Y_PT, target_width_pt, target_height_pt, True)

    x_px, y_px = best_xy
    logger.info("stamp placement: tier=%s pixel_xy=(%d, %d)", tier, x_px, y_px)
    # Image origin is top-left; PDF origin is bottom-left. Flip y.
    x_pt = x_px / px_per_pt
    y_pt_top = y_px / px_per_pt
    y_pt = page_h_pt - y_pt_top - target_height_pt
    return StampPlacement(x_pt, y_pt, target_width_pt, target_height_pt, False)


# ---------------------------------------------------------------- overlay drawing


@dataclass
class Row:
    label: str
    fixed_value: str | None  # None means render as editable field
    field_name: str | None   # None for fixed-value or pure-header rows
    hint: str | None = None  # format hint drawn right of an editable field


def _draw_stamp_overlay(
    page_w_pt: float,
    page_h_pt: float,
    placement: StampPlacement,
    rows: list[Row],
    color: Color,
) -> bytes:
    """Build a one-page transparent PDF the size of the original page,
    with the stamp graphics + AcroForm text fields drawn at `placement`.

    Rows split into:
      * title rows  — `fixed_value is None and field_name is None`. Rendered
        as a centered, larger title above the body box (no border around them).
      * body rows   — everything else. Rendered inside a bordered rectangle,
        with thin separator lines between rows.
    """
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w_pt, page_h_pt))

    title_rows = [r for r in rows if r.fixed_value is None and r.field_name is None]
    body_rows = [r for r in rows if not (r.fixed_value is None and r.field_name is None)]

    title_count = len(title_rows)
    body_count = len(body_rows)

    # Body box height = body_count * ROW_HEIGHT_PT (with body_count > 0).
    # Title block sits above it inside the placement rectangle.
    body_height = body_count * ROW_HEIGHT_PT
    body_top_y = placement.y_pt + body_height
    title_block_top_y = placement.y_pt + placement.height_pt
    title_block_height = title_block_top_y - body_top_y

    if title_count > 0 and body_count > 0:
        # Outer box enclosing the entire stamp (title + body).
        c.setStrokeColor(color)
        c.setLineWidth(BORDER_WIDTH_PT)
        c.rect(placement.x_pt, placement.y_pt, placement.width_pt, placement.height_pt, stroke=1, fill=0)
        # Separator between title band and body rows.
        c.setStrokeColor(color)
        c.setLineWidth(BORDER_WIDTH_PT)
        c.line(placement.x_pt, body_top_y, placement.x_pt + placement.width_pt, body_top_y)
    elif body_count > 0:
        # Received stamp: border only around the body rows (no title rows).
        c.setStrokeColor(color)
        c.setLineWidth(BORDER_WIDTH_PT)
        c.rect(placement.x_pt, placement.y_pt, placement.width_pt, body_height, stroke=1, fill=0)

    # --- Title rows (above the body border) ---
    if title_count > 0 and title_block_height > 0:
        per_title_height = title_block_height / title_count
        center_x = placement.x_pt + placement.width_pt / 2
        for i, row in enumerate(title_rows):
            band_top = title_block_top_y - (i * per_title_height)
            band_bot = band_top - per_title_height
            text_y = band_bot + (per_title_height - TITLE_FONT_SIZE) / 2 + 1
            c.setFillColor(color)
            c.setFont("Helvetica-Bold", TITLE_FONT_SIZE)
            c.drawCentredString(center_x, text_y, row.label)

        # Format-hint caption: drawn in the title band, just above the body
        # box, centered. It deliberately lives OFF every field's read
        # baseline — a hint on the "Date:" row would be swept up by the
        # positioned-text reader and corrupt the extracted date. The text
        # also carries no "date"/"check" tokens or digits, so no reader
        # tier (label-match or regex) can mistake it for a value.
        hint = next((r.hint for r in body_rows if r.hint), None)
        if hint:
            c.setFillColor(HINT_COLOR)
            c.setFont("Helvetica", HINT_FONT_SIZE)
            c.drawCentredString(center_x, body_top_y + 3, hint)

    # --- Body rows (inside the border) ---
    label_x = placement.x_pt + PADDING_PT
    value_x = placement.x_pt + LABEL_WIDTH_PT
    for i, row in enumerate(body_rows):
        row_top = body_top_y - (i * ROW_HEIGHT_PT)
        row_bot = row_top - ROW_HEIGHT_PT
        text_y = row_bot + 7

        # Separator below each body row except the last (the last is the box's own border)
        if i < body_count - 1:
            c.setStrokeColor(color)
            c.setLineWidth(0.4)
            c.line(placement.x_pt, row_bot, placement.x_pt + placement.width_pt, row_bot)

        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(label_x, text_y, row.label)

        if row.fixed_value is not None:
            c.setFont("Helvetica", 10)
            c.drawString(value_x, text_y, row.fixed_value)
            text_w = c.stringWidth(row.fixed_value, "Helvetica", 10)
            c.setLineWidth(0.6)
            c.line(value_x, text_y - 2, value_x + max(text_w, 60), text_y - 2)
        elif row.field_name is not None:
            field_x = value_x
            field_y = text_y - 4
            field_w = placement.x_pt + placement.width_pt - field_x - PADDING_PT
            c.acroForm.textfield(
                name=row.field_name,
                x=field_x, y=field_y,
                width=field_w, height=FIELD_HEIGHT_PT,
                borderColor=color, fillColor=Color(1, 1, 1, alpha=0),
                textColor=color, forceBorder=True,
                borderWidth=0.6, fontSize=10,
            )

    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- form flattening


def flatten_acroform(pdf_bytes: bytes) -> bytes:
    """Bake every AcroForm field value into static page text and strip widgets.

    Used at two points in the pipeline:
      * Step 5 — before adding the Paid stamp, the manager-filled Received
        stamp gets flattened so the AP can't edit its values.
      * Step 6 — before archiving to Strata_Plans/, the AP-filled Paid stamp
        gets flattened so the archived copy has no editable fields anywhere.

    pikepdf does both steps: `generate_appearance_streams` builds /AP from
    each widget's /V using the AcroForm /DA + /DR resources, and
    `flatten_annotations(mode='all')` merges each appearance into the page
    content stream and drops the widget annotations + AcroForm /Fields.
    Returns the original bytes unchanged when the PDF has no AcroForm.

    The bytes go through pypdf elsewhere in the pipeline (Step 6 archive
    write, Step 1/3 stamp merges); pikepdf-produced PDFs are standard and
    pypdf handles them without issue.
    """
    with pikepdf.Pdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/AcroForm" not in pdf.Root:
            return pdf_bytes
        pdf.generate_appearance_streams()
        pdf.flatten_annotations(mode="all")
        out = io.BytesIO()
        pdf.save(out, deterministic_id=True)
        return out.getvalue()


# ---------------------------------------------------------------- merging + form-field plumbing


def _ensure_acroform(writer: PdfWriter) -> DictionaryObject:
    """Create or fetch the document's /AcroForm dictionary."""
    root = writer._root_object  # type: ignore[attr-defined]
    if NameObject("/AcroForm") in root:
        return root[NameObject("/AcroForm")]  # type: ignore[return-value]
    acroform = DictionaryObject(
        {
            NameObject("/Fields"): ArrayObject(),
            NameObject("/NeedAppearances"): BooleanObject(True),
        }
    )
    root[NameObject("/AcroForm")] = writer._add_object(acroform)  # type: ignore[attr-defined]
    return acroform


def _migrate_widget_annotations(writer: PdfWriter, page_idx: int, overlay_page) -> None:
    """Copy widget annotations from `overlay_page` to writer page `page_idx`,
    and register their fields in the document /AcroForm.
    """
    if NameObject("/Annots") not in overlay_page:
        return
    overlay_annots = overlay_page[NameObject("/Annots")]
    if not overlay_annots:
        return

    target_page = writer.pages[page_idx]
    if NameObject("/Annots") not in target_page:
        target_page[NameObject("/Annots")] = ArrayObject()
    target_annots = target_page[NameObject("/Annots")]

    acroform = _ensure_acroform(writer)
    fields_array = acroform[NameObject("/Fields")]

    for raw in overlay_annots:
        annot = raw.get_object() if isinstance(raw, IndirectObject) else raw
        subtype = annot.get(NameObject("/Subtype"))
        if subtype != NameObject("/Widget"):
            continue
        # Re-add the annotation as a writer-owned object so it survives.
        new_annot_ref = writer._add_object(annot)  # type: ignore[attr-defined]
        target_annots.append(new_annot_ref)
        fields_array.append(new_annot_ref)


def _register_existing_widgets_in_acroform(writer: PdfWriter, page_idx: int) -> None:
    """Promote pre-existing /Widget annotations on the page into /AcroForm/Fields.

    `append_pages_from_reader` copies a page's /Annots but not the document's
    /AcroForm. Any widget the base PDF brought along (vendor fillable invoice,
    prior-stage stamp) ends up visible on the page but unregistered in
    /AcroForm/Fields. Some viewers will refuse to treat such widgets as form
    fields. This walks the page's /Annots and appends each /Widget reference
    into the freshly-created /AcroForm/Fields, preserving editability.

    Idempotent: skips widget references already present in /Fields.
    """
    target_page = writer.pages[page_idx]
    if NameObject("/Annots") not in target_page:
        return
    annots = target_page[NameObject("/Annots")]
    if not annots:
        return

    acroform = _ensure_acroform(writer)
    fields_array = acroform[NameObject("/Fields")]
    existing_ids = {
        f.idnum for f in fields_array if isinstance(f, IndirectObject)
    }

    for raw in annots:
        if not isinstance(raw, IndirectObject):
            continue
        annot = raw.get_object()
        if annot.get(NameObject("/Subtype")) != NameObject("/Widget"):
            continue
        if raw.idnum in existing_ids:
            continue
        fields_array.append(raw)
        existing_ids.add(raw.idnum)


def _merge_overlay_onto_page_one(pdf_bytes: bytes, overlay_bytes: bytes) -> bytes:
    """Merge overlay page 1 onto page 1 of the original PDF.

    Preserves any pre-existing /AcroForm widgets (e.g. a vendor fillable
    invoice arriving at Step 1) by promoting them into the writer's
    /AcroForm/Fields after `append_pages_from_reader` — pypdf copies pages
    but not the document /AcroForm, so without this promotion the base's
    widgets would orphan and become unfillable in some viewers. Flattening
    of prior-stage stamps is the responsibility of the caller (Step 5
    flattens before adding the Paid stamp; Step 6 flattens on archive).
    """
    base = PdfReader(io.BytesIO(pdf_bytes))
    overlay = PdfReader(io.BytesIO(overlay_bytes))

    writer = PdfWriter()
    writer.append_pages_from_reader(base)
    _register_existing_widgets_in_acroform(writer, 0)

    overlay_page = overlay.pages[0]
    writer.pages[0].merge_page(overlay_page)
    _migrate_widget_annotations(writer, 0, overlay_page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------- public API


def _stamp_id(seed: str = "") -> str:
    return seed[:16] if seed else str(int(time.time() * 1000))


def received_stamp_sha_matches(pdf_bytes: bytes, sha: str) -> bool:
    """Return True if pdf_bytes contains a Received stamp created from source PDF with `sha`.

    Checks for the deterministic AcroForm field name that render_received_stamp
    writes when sha is provided. Used by step_3 to detect cross-day retries so
    it can reuse the already-written dest bytes instead of re-stamping with a new date.
    """
    if not sha:
        return False
    expected_field = f"gl_code_{sha[:16]}"
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        fields = reader.get_fields()
        return fields is not None and expected_field in fields
    except Exception:
        return False


def render_received_stamp(
    pdf_bytes: bytes,
    received_date: str,
    plan_pretty: str,
    sha: str = "",
) -> bytes:
    """Add the red Received stamp to page 1.

    Rows 1-2 are hardcoded (received_date, plan_pretty). Rows 3-7 are
    editable AcroForm text fields the manager fills in.

    Pass `sha` (the source PDF's SHA-256 hex string) to generate deterministic
    AcroForm field names, enabling content-idempotency on retry.
    """
    sid = _stamp_id(sha)
    rows = [
        Row("Received:", received_date, None),
        Row("Strata Plan #:", plan_pretty, None),
        Row("GL Code:", None, f"gl_code_{sid}"),
        Row("Chargeback:", None, f"chargeback_{sid}"),
        Row("To:", None, f"chargeback_to_{sid}"),
        Row("Amount:", None, f"amount_{sid}"),
        Row("Approved:", None, f"approved_{sid}"),
    ]
    return _render_stamp(pdf_bytes, rows, color=RED, height_pt=STAMP_HEIGHT_RECEIVED_PT)


def render_paid_stamp(pdf_bytes: bytes, sha: str = "") -> bytes:
    """Add the blue Paid stamp to page 1.

    Header row says 'Paid' (no value field). Date and Check Number rows are
    blank editable AcroForm text fields the accountant fills in before saving.

    Pass `sha` (the source PDF's SHA-256 hex string) for deterministic field
    names, enabling content-idempotency on retry.
    """
    sid = _stamp_id(sha)
    rows = [
        Row("PAID", None, None),  # header-only row
        Row("Date:", None, f"paid_date_{sid}", hint="format:  MMM DD YYYY"),
        Row("Check Number:", None, f"paid_check_number_{sid}"),
    ]
    return _render_stamp(pdf_bytes, rows, color=BLUE, height_pt=STAMP_HEIGHT_PAID_PT)


def _render_stamp(pdf_bytes: bytes, rows: list[Row], color: Color, height_pt: int) -> bytes:
    placement = find_largest_whitespace_box(pdf_bytes, STAMP_WIDTH_PT, height_pt)
    # Need page dimensions for the overlay canvas
    _, page_w_pt, page_h_pt = _rasterize_page_one(pdf_bytes, dpi=72)
    overlay_bytes = _draw_stamp_overlay(page_w_pt, page_h_pt, placement, rows, color)
    return _merge_overlay_onto_page_one(pdf_bytes, overlay_bytes)


# ---------------------------------------------------------------- CLI smoke test

if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Smoke-test the stamp module.")
    ap.add_argument("pdf", type=Path, help="Input PDF path")
    ap.add_argument("--out", type=Path, default=None, help="Output PDF path (default: <input>_stamped.pdf)")
    ap.add_argument("--mode", choices=("received", "paid"), default="received")
    ap.add_argument("--date", default="MAY 08 2026")
    ap.add_argument("--plan", default="BCS 2707")
    args = ap.parse_args()

    pdf_bytes = args.pdf.read_bytes()
    if args.mode == "received":
        out_bytes = render_received_stamp(pdf_bytes, args.date, args.plan)
    else:
        out_bytes = render_paid_stamp(pdf_bytes)

    out = args.out or args.pdf.with_name(f"{args.pdf.stem}_stamped_{args.mode}.pdf")
    out.write_bytes(out_bytes)
    print(f"wrote {out}")
