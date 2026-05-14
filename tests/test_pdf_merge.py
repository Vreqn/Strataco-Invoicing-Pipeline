"""Unit tests for tools/_lib/pdf_merge.merge_pdfs_from_bytes — Step 7.

Builds trivial PDFs in memory via reportlab + pypdf so the test is
self-contained (doesn't depend on real invoices on disk).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pypdf import PdfReader
from reportlab.pdfgen import canvas as rl_canvas

from tools._lib.pdf_merge import merge_pdfs_from_bytes


def _make_pdf(text: str, pages: int = 1) -> bytes:
    """Build a tiny multi-page PDF in memory."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"{text} - page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_merge_two_single_page_pdfs() -> None:
    a = _make_pdf("alpha")
    b = _make_pdf("beta")
    merged = merge_pdfs_from_bytes([a, b])
    reader = PdfReader(io.BytesIO(merged))
    assert len(reader.pages) == 2, f"[two 1-page] expected 2 pages, got {len(reader.pages)}"


def test_merge_preserves_multipage_order() -> None:
    a = _make_pdf("alpha", pages=3)
    b = _make_pdf("beta", pages=2)
    c = _make_pdf("gamma", pages=1)
    merged = merge_pdfs_from_bytes([a, b, c])
    reader = PdfReader(io.BytesIO(merged))
    assert len(reader.pages) == 6, f"[multipage] expected 6 pages, got {len(reader.pages)}"
    first_text = reader.pages[0].extract_text() or ""
    last_text = reader.pages[-1].extract_text() or ""
    assert "alpha" in first_text, f"[multipage] page 1 should mention alpha, got {first_text!r}"
    assert "gamma" in last_text, f"[multipage] last page should mention gamma, got {last_text!r}"


def test_single_pdf_round_trip() -> None:
    a = _make_pdf("solo", pages=2)
    merged = merge_pdfs_from_bytes([a])
    reader = PdfReader(io.BytesIO(merged))
    assert len(reader.pages) == 2, f"[single] expected 2 pages, got {len(reader.pages)}"


def test_empty_list_raises() -> None:
    with pytest.raises(ValueError):
        merge_pdfs_from_bytes([])
