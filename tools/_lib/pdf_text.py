"""PDF text extraction wrappers.

Used by Step 3 (plan matching from PDF body) and Step 6 (check number from
flattened paid stamp). pdfplumber gives us positioned word-level extraction
when we need it; falls back to pypdf for the simple full-text path.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)


def extract_full_text(pdf_path: Path | bytes) -> str:
    """Return the full plain-text content of every page concatenated."""
    parts: list[str] = []
    src = io.BytesIO(pdf_path) if isinstance(pdf_path, (bytes, bytearray)) else str(pdf_path)
    try:
        with pdfplumber.open(src) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    parts.append(t)
    except Exception as exc:
        logger.warning("pdfplumber failed on PDF: %s", exc)
        return ""
    return "\n".join(parts)


def extract_page_words(pdf_path: Path | bytes, page_index: int = 0) -> list[dict]:
    """Return word records `{text, x0, x1, top, bottom}` from one page.

    Used by stamp_read.py to find the value adjacent to "Check Number:".
    """
    src = io.BytesIO(pdf_path) if isinstance(pdf_path, (bytes, bytearray)) else str(pdf_path)
    with pdfplumber.open(src) as pdf:
        if page_index >= len(pdf.pages):
            return []
        page = pdf.pages[page_index]
        return page.extract_words(use_text_flow=True, keep_blank_chars=False) or []


def extract_page_text(pdf_path: Path | bytes, page_index: int = 0) -> str:
    """Return the plain-text content of a single page.

    `stamp_read.extract_paid_stamp_values` uses this to scope the regex
    fallback and image-only detection to page 1, so that a remittance
    stub or terms page later in the document can't seed false positives.
    """
    src = io.BytesIO(pdf_path) if isinstance(pdf_path, (bytes, bytearray)) else str(pdf_path)
    try:
        with pdfplumber.open(src) as pdf:
            if page_index >= len(pdf.pages):
                return ""
            return pdf.pages[page_index].extract_text() or ""
    except Exception as exc:
        logger.warning("pdfplumber failed on page %d: %s", page_index, exc)
        return ""
