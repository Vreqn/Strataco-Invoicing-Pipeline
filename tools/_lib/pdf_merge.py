"""Combine several PDF blobs into one PDF blob.

Used by Step 7 (Monthly Invoice Aggregator) to roll a month's worth of
individual check PDFs from one strata plan into a single Summary PDF.
The Step-6-archived invoices are post-flatten (the AcroForm stamp values
have been baked into page content), so no special widget-merge handling
is required — `PdfWriter.append_pages_from_reader` is sufficient.
"""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter


def merge_pdfs_from_bytes(pdf_blobs: list[bytes]) -> bytes:
    """Concatenate `pdf_blobs` in order into a single PDF.

    Raises `ValueError` if the input list is empty, or `pypdf` exceptions
    propagate if any blob can't be read (caller treats this as a per-plan
    abort and leaves the plan folder untouched).
    """
    if not pdf_blobs:
        raise ValueError("merge_pdfs_from_bytes requires at least one input")

    writer = PdfWriter()
    for blob in pdf_blobs:
        reader = PdfReader(io.BytesIO(blob))
        for page in reader.pages:
            writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
