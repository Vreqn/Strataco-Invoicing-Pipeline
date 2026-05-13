"""Unit tests for tools/_lib/pdf_merge.merge_pdfs_from_bytes — Step 7.

Builds trivial PDFs in memory via reportlab + pypdf so the test is
self-contained (doesn't depend on real invoices on disk).

Standalone: no pytest dependency. Run with `python tests/test_pdf_merge.py`.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def test_merge_two_single_page_pdfs() -> list[str]:
    failures: list[str] = []
    a = _make_pdf("alpha")
    b = _make_pdf("beta")
    merged = merge_pdfs_from_bytes([a, b])
    reader = PdfReader(io.BytesIO(merged))
    if len(reader.pages) != 2:
        failures.append(f"[two 1-page] expected 2 pages, got {len(reader.pages)}")
    return failures


def test_merge_preserves_multipage_order() -> list[str]:
    failures: list[str] = []
    a = _make_pdf("alpha", pages=3)   # 3 pages
    b = _make_pdf("beta", pages=2)    # 2 pages
    c = _make_pdf("gamma", pages=1)   # 1 page
    merged = merge_pdfs_from_bytes([a, b, c])
    reader = PdfReader(io.BytesIO(merged))
    if len(reader.pages) != 6:
        failures.append(f"[multipage] expected 6 pages, got {len(reader.pages)}")
    # Spot-check the text on the first and last page so order is preserved.
    first_text = reader.pages[0].extract_text() or ""
    last_text = reader.pages[-1].extract_text() or ""
    if "alpha" not in first_text:
        failures.append(f"[multipage] page 1 should mention alpha, got {first_text!r}")
    if "gamma" not in last_text:
        failures.append(f"[multipage] last page should mention gamma, got {last_text!r}")
    return failures


def test_single_pdf_round_trip() -> list[str]:
    failures: list[str] = []
    a = _make_pdf("solo", pages=2)
    merged = merge_pdfs_from_bytes([a])
    reader = PdfReader(io.BytesIO(merged))
    if len(reader.pages) != 2:
        failures.append(f"[single] expected 2 pages, got {len(reader.pages)}")
    return failures


def test_empty_list_raises() -> list[str]:
    failures: list[str] = []
    try:
        merge_pdfs_from_bytes([])
    except ValueError:
        return failures
    except Exception as exc:
        failures.append(f"[empty] expected ValueError, got {type(exc).__name__}: {exc}")
    else:
        failures.append("[empty] expected ValueError, got no exception")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("merge two 1-page PDFs", test_merge_two_single_page_pdfs),
        ("preserve multi-page order", test_merge_preserves_multipage_order),
        ("single-PDF round trip", test_single_pdf_round_trip),
        ("empty list raises", test_empty_list_raises),
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
