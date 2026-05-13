"""Extract the check number and date from the Paid stamp.

Step 6 needs both the check number and the date written into the Paid stamp
by the accountant. Two PDF shapes are supported in tiered order:

1. AcroForm /V values (the normal happy path post-0.12.0). Step 5 stamps a
   Paid form with widgets named `paid_check_number_<sid>` and
   `paid_date_<sid>`; the AP fills them in Acrobat and saves. The values
   live in `/V` and we read them straight out via pypdf.
2. Positioned text in the PAID stamp region. Covers PDFs that have been
   flattened by a text-preserving tool (Acrobat Pro Flatten Form Fields,
   Kofax, prior pipeline runs predating the AcroForm path).
3. Regex over the full text, guarded by proximity to the word "PAID".

When all three tiers come up empty AND the PDF has no extractable text at
all, the result carries an explicit `image_only` flag so Step 6 can
surface a clearer "looks like Microsoft Print to PDF" message in the
morning email.

Date extraction was scaffolded in earlier and removed in 0.1.4 because it
picked up vendor "Date:" labels and produced junk. Re-introducing it is
safe now that `_locate_paid_region` constrains the search to the stamp
itself — the same fix that made check-number extraction reliable in
0.3.0 protects the date the same way.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from pypdf import PdfReader

from tools._lib.pdf_text import extract_page_text, extract_page_words

logger = logging.getLogger(__name__)

_CHECK_REGEX = re.compile(
    r"Check\s*(?:Number|No\.?|#)\s*[:#]?\s*([A-Za-z0-9\-_/]+)",
    re.IGNORECASE,
)

# Match a "Date:" label followed by a date in any of the forms a human is
# likely to write: "MAY 08 2026", "May 8, 2026", "2026-05-08", "05/08/2026".
# The capture is kept loose — `parse_paid_date` does the strict parsing.
_DATE_REGEX = re.compile(
    r"Date\s*[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9,/\-\s]{4,30}?\d{4})",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}

# Bounding-box dimensions for the Paid stamp, mirrored from tools/_lib/stamp.py.
# Used to restrict Check Number extraction to the stamp region instead of any
# "Check Number:" label that may appear elsewhere on the page (e.g. vendor
# invoice metadata, prior received-stamp output).
_PAID_STAMP_WIDTH_PT = 210
_PAID_STAMP_HEIGHT_PT = 100
# Slack around the rendered stamp to absorb post-flatten rasterisation drift.
_PAID_REGION_SLACK_PT = 24


@dataclass
class PaidStampValues:
    check_number: str
    paid_date: str = ""
    note: str = ""
    image_only: bool = False

    @property
    def has_check_number(self) -> bool:
        return bool(self.check_number.strip())

    @property
    def has_paid_date(self) -> bool:
        return bool(self.paid_date.strip())


def _read_acroform_paid_values(pdf_bytes: bytes) -> tuple[str, str]:
    """Read `paid_check_number_<sid>` and `paid_date_<sid>` /V values.

    Returns `(check_number, paid_date)` or empty strings when the PDF has
    no AcroForm or the expected fields are absent / unset. Field names
    are emitted by `stamp.render_paid_stamp`.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        fields = reader.get_fields() or {}
    except Exception as exc:
        logger.warning("AcroForm read failed: %s", exc)
        return "", ""
    check = ""
    paid_date = ""
    for name, field in fields.items():
        raw_v = field.get("/V")
        if raw_v is None:
            continue
        value = str(raw_v).strip()
        if not value:
            continue
        if name.startswith("paid_check_number_"):
            check = value
        elif name.startswith("paid_date_"):
            paid_date = value
    return check, paid_date


def _find_label_value(words: list[dict], label_tokens: list[str]) -> str:
    """Look for `label_tokens` (e.g. ['Check', 'Number:']) in order on the same row,
    then return the text immediately to the right of the last label token.
    """
    if not words or not label_tokens:
        return ""
    needle = [t.lower().rstrip(":") for t in label_tokens]

    for i in range(len(words)):
        # Try to match all label tokens contiguously starting at i
        ok = True
        last = i
        for j, tok in enumerate(needle):
            if i + j >= len(words):
                ok = False
                break
            w = words[i + j]
            wt = (w.get("text") or "").lower().rstrip(":")
            if wt != tok:
                ok = False
                break
            last = i + j
        if not ok:
            continue

        anchor = words[last]
        anchor_top = anchor.get("top", 0)
        anchor_bottom = anchor.get("bottom", anchor_top + 12)
        anchor_x_end = anchor.get("x1", 0)

        # Collect words to the right of the label on roughly the same baseline
        candidates: list[tuple[float, str]] = []
        for w in words:
            top = w.get("top", 0)
            bottom = w.get("bottom", top + 12)
            x0 = w.get("x0", 0)
            if x0 <= anchor_x_end:
                continue
            # Same row tolerance: vertical overlap or center within row height
            if bottom < anchor_top - 4 or top > anchor_bottom + 4:
                continue
            candidates.append((x0, str(w.get("text") or "").strip()))

        if not candidates:
            continue
        candidates.sort()
        # Stop at a large horizontal gap (next column / next field)
        out: list[str] = []
        prev_x = candidates[0][0]
        for x0, txt in candidates:
            if out and x0 - prev_x > 60:  # ~ next column
                break
            if txt:
                out.append(txt)
            prev_x = x0
        joined = " ".join(out).strip()
        if joined:
            return joined
    return ""


def _locate_paid_region(words: list[dict]) -> tuple[float, float, float, float] | None:
    """Find the rendered "PAID" header on page 1 and return a bounding box.

    Returns `(x_min, x_max, y_min, y_max)` in pdfplumber coordinates
    (origin top-left, y increases downward). The box covers the full Paid
    stamp area below the PAID title plus a generous slack to absorb
    Print-to-PDF rasterisation drift. Returns None when "PAID" is not found —
    callers should fall back to whole-page extraction with a warning.
    """
    if not words:
        return None
    for w in words:
        text = (w.get("text") or "").strip().upper()
        if text != "PAID":
            continue
        x0 = float(w.get("x0", 0))
        x1 = float(w.get("x1", 0))
        top = float(w.get("top", 0))
        center_x = (x0 + x1) / 2.0
        x_min = center_x - _PAID_STAMP_WIDTH_PT / 2 - _PAID_REGION_SLACK_PT
        x_max = center_x + _PAID_STAMP_WIDTH_PT / 2 + _PAID_REGION_SLACK_PT
        y_min = top - _PAID_REGION_SLACK_PT
        y_max = top + _PAID_STAMP_HEIGHT_PT + _PAID_REGION_SLACK_PT
        return x_min, x_max, y_min, y_max
    return None


def _words_in_region(
    words: list[dict],
    region: tuple[float, float, float, float],
) -> list[dict]:
    x_min, x_max, y_min, y_max = region
    out: list[dict] = []
    for w in words:
        x0 = float(w.get("x0", 0))
        x1 = float(w.get("x1", 0))
        top = float(w.get("top", 0))
        bottom = float(w.get("bottom", top + 12))
        if x1 < x_min or x0 > x_max:
            continue
        if bottom < y_min or top > y_max:
            continue
        out.append(w)
    return out


def extract_paid_stamp_values(pdf_bytes: bytes) -> PaidStampValues:
    """Tiered extraction of the Paid stamp's check number and date.

    1. AcroForm `/V` values — the normal happy path for PDFs the AP just
       filled in Acrobat and saved (the 0.12.0+ flow). **Authoritative**:
       if *any* `paid_*` field has a non-empty `/V`, that tier short-
       circuits and returns whatever it found. Partial fills (one value
       set, the other empty) honestly report `has_check_number=False`
       or `has_paid_date=False`; the caller surfaces the missing-field
       error and the operator fills the remaining field. Without this
       short-circuit, the positional tier could pick up a vendor "Date:"
       or "Check Number:" on the page and combine it with a real
       AcroForm value.

    2. Positioned text inside the PAID stamp region — covers PDFs that
       were flattened by a text-preserving tool (Acrobat Pro Flatten,
       Kofax, pre-0.12.0 pipeline runs).

    3. Regex over **page 1 text only** (not full document), guarded by
       PAID proximity. Page-1 scoping avoids matching against a "PAID"
       string on a remittance stub or terms page later in the PDF.

    Sets `image_only=True` when the PDF has no AcroForm AND **page 1**
    has no extractable text — the signature of a Microsoft "Print to
    PDF" rasterised page. Page-1-only scoping means a rasterised page 1
    + text-bearing page 2 (e.g. cover letter behind invoice) still
    correctly flags as image-only.
    """
    check, paid_date = _read_acroform_paid_values(pdf_bytes)
    if check or paid_date:
        # AcroForm is authoritative. Don't mix /V with positional fallback.
        return PaidStampValues(
            check_number=check.strip(),
            paid_date=paid_date.strip(),
            note="check_number/paid_date from AcroForm",
            image_only=False,
        )

    note = ""
    words: list[dict] = []
    try:
        words = extract_page_words(pdf_bytes, page_index=0)
    except Exception as exc:
        logger.warning("extract_page_words failed: %s", exc)

    region = _locate_paid_region(words)
    if region is not None:
        search_words = _words_in_region(words, region)
        if not search_words:
            logger.warning(
                "PAID header found but no words inside the stamp region"
            )
    else:
        if words:
            logger.warning(
                "PAID header not found — falling back to whole-page "
                "check-number search"
            )
            note = "PAID header missing — page-wide extraction"
        search_words = words

    check = _find_label_value(search_words, ["Check", "Number:"])
    if not check:
        check = _find_label_value(search_words, ["Check", "#"])
    if not check:
        check = _find_label_value(search_words, ["Check", "No."])

    page_one_text: str | None = None

    if not check:
        # Regex fallback over PAGE 1 text only — multi-page invoices often
        # have "PAID" / "Check Number:" on a remittance stub later in the
        # document, and a full-document scan would happily grab that.
        page_one_text = extract_page_text(pdf_bytes, 0)
        m = _CHECK_REGEX.search(page_one_text)
        if m and _check_in_paid_context(page_one_text, m.start(), m.end()):
            check = m.group(1).strip()
            note = (note + "; " if note else "") + (
                "check_number from regex fallback"
            )

    paid_date = _find_label_value(search_words, ["Date:"])
    if not paid_date:
        paid_date = _find_label_value(search_words, ["Date"])

    if not paid_date:
        if page_one_text is None:
            page_one_text = extract_page_text(pdf_bytes, 0)
        m = _DATE_REGEX.search(page_one_text)
        if m and _check_in_paid_context(page_one_text, m.start(), m.end()):
            paid_date = m.group(1).strip()
            note = (note + "; " if note else "") + (
                "paid_date from regex fallback"
            )

    image_only = False
    if not (check and paid_date):
        # When *both* AcroForm fields are absent AND page 1 has zero
        # extractable text, the PDF's first page is image-only — almost
        # always Microsoft "Print to PDF". Scope is page 1 because the
        # Paid stamp must live on page 1, and a text-bearing page 2
        # doesn't make a rasterised page 1 any more readable.
        if page_one_text is None:
            page_one_text = extract_page_text(pdf_bytes, 0)
        has_text = bool((page_one_text or "").strip())
        has_acroform = False
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            has_acroform = "/AcroForm" in reader.trailer.get("/Root", {})
        except Exception:
            pass
        if not has_text and not has_acroform:
            image_only = True

    return PaidStampValues(
        check_number=check.strip(),
        paid_date=paid_date.strip(),
        note=note,
        image_only=image_only,
    )


def _check_in_paid_context(full_text: str, match_start: int, match_end: int) -> bool:
    """Confirm a regex match for "Check Number:" sits near the word "PAID".

    Looks for "PAID" within 300 characters before or 100 characters after the
    match (covers the typical stamp layout where PAID is on the line above
    Check Number, plus generous slack for column-wrap artefacts).
    """
    window_lo = max(0, match_start - 300)
    window_hi = min(len(full_text), match_end + 100)
    window = full_text[window_lo:window_hi].upper()
    return "PAID" in window


def sanitize_check_number_for_filename(check: str) -> str:
    """Trim and replace anything Windows would reject in a filename prefix."""
    s = (check or "").strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", "_", s)
    return s


def parse_paid_date(raw: str) -> tuple[int, int] | None:
    """Parse the date string from the Paid stamp into (month, year).

    Accepts the formats accountants are likely to type:
      * `"MAY 08 2026"` / `"May 08 2026"` — the default format
        `render_paid_stamp` emits.
      * `"May 8, 2026"` / `"May 8 2026"` — long-form with optional comma.
      * `"2026-05-08"` — ISO; unambiguous.
      * `"05/08/2026"` / `"5-8-2026"` — slash or dash separated. If one of
        the leading numbers exceeds 12, that one is treated as the day and
        the other as the month. If both are 1–12, MM/DD/YYYY is assumed
        (the convention on the stamp's home territory).

    Year must be 4 digits — 2-digit years are rejected rather than guessed.
    Returns `None` if the string can't be parsed into a valid (month, year).
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # Form 1: month name anywhere + 4-digit year. Catches "MAY 08 2026",
    # "May 8, 2026", "8 May 2026", "May, 2026", etc.
    up = s.upper()
    year_match = re.search(r"(?<!\d)(\d{4})(?!\d)", up)
    if year_match:
        year = int(year_match.group(1))
        if 1900 <= year <= 2999:
            for name, num in _MONTH_NAMES.items():
                if re.search(rf"\b{name}\b", up):
                    return num, year

    # Form 2: ISO `YYYY-MM-DD` or `YYYY/MM/DD`.
    m = re.match(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$", s)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        if 1900 <= year <= 2999 and 1 <= month <= 12:
            return month, year

    # Form 3: `MM/DD/YYYY` or `DD/MM/YYYY` (or `-` separated).
    m = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        year = int(m.group(3))
        if not (1900 <= year <= 2999):
            return None
        if a > 12 and 1 <= b <= 12:
            month = b
        elif b > 12 and 1 <= a <= 12:
            month = a
        elif 1 <= a <= 12 and 1 <= b <= 12:
            month = a  # MM/DD/YYYY default
        else:
            return None
        return month, year

    return None
