"""Unit tests for `tools._lib.zip_safe`.

Exercises both entry points:

  - `audit_and_extract_pdfs` (strict, Step 1's in-memory caller)
  - `audit_zipfile`          (lenient, Step 2's on-disk caller)

Plus the shared safety pre-flight: bomb, encrypted, oversized,
too-many-entries.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail at module load.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

from tools._lib.zip_safe import (
    UnsafeZipError,
    _audit_safety,
    audit_and_extract_pdfs,
    audit_zipfile,
)


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP archive in memory with the given (name, bytes) entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_pdfs_returns_pdf_entries() -> None:
    data = _build_zip([
        ("inv_001.pdf", b"%PDF-1.4 first"),
        ("inv_002.pdf", b"%PDF-1.4 second"),
    ])
    out = audit_and_extract_pdfs(data)
    assert sorted(name for name, _ in out) == ["inv_001.pdf", "inv_002.pdf"]
    by_name = dict(out)
    assert by_name["inv_001.pdf"] == b"%PDF-1.4 first"
    assert by_name["inv_002.pdf"] == b"%PDF-1.4 second"


def test_extract_pdfs_rejects_non_pdf_entries() -> None:
    data = _build_zip([
        ("inv_001.pdf", b"%PDF-1.4 fake"),
        ("cover_letter.docx", b"PK fake docx bytes"),
    ])
    with pytest.raises(UnsafeZipError) as exc:
        audit_and_extract_pdfs(data)
    assert "non-PDF entries" in str(exc.value)
    assert "cover_letter.docx" in str(exc.value)


def test_extract_pdfs_ignores_mac_metadata() -> None:
    """`__MACOSX/` directory entries and `._foo.pdf` AppleDouble files
    should be filtered out silently — they're macOS artifacts, not real
    attachments, and Mac users send them constantly. The remaining real
    PDFs should still come back.
    """
    data = _build_zip([
        ("__MACOSX/", b""),
        ("__MACOSX/._inv_001.pdf", b"AppleDouble noise"),
        ("inv_001.pdf", b"%PDF-1.4 real"),
        ("._inv_001.pdf", b"more AppleDouble noise at root"),
    ])
    out = audit_and_extract_pdfs(data)
    assert [name for name, _ in out] == ["inv_001.pdf"]


def test_extract_pdfs_empty_zip_returns_empty_list() -> None:
    """An empty ZIP carries no useful content but is also not unsafe.
    Callers (Step 1) treat this as 'no contained PDFs' and the
    all-or-nothing decision continues using the other attachments."""
    data = _build_zip([])
    out = audit_and_extract_pdfs(data)
    assert out == []


def test_extract_pdfs_directory_only_zip_returns_empty_list() -> None:
    """A ZIP that only has directory entries (some tools do this) returns
    an empty list, not an error."""
    data = _build_zip([
        ("nested/", b""),
        ("nested/sub/", b""),
    ])
    out = audit_and_extract_pdfs(data)
    assert out == []


def test_extract_pdfs_uppercase_extension_still_pdf() -> None:
    """Case-insensitive extension matching — vendors send `.PDF` sometimes."""
    data = _build_zip([
        ("INVOICE.PDF", b"%PDF-1.4 shouty"),
    ])
    out = audit_and_extract_pdfs(data)
    assert [name for name, _ in out] == ["INVOICE.PDF"]


def test_extract_pdfs_rejects_bad_zip_bytes() -> None:
    with pytest.raises(UnsafeZipError) as exc:
        audit_and_extract_pdfs(b"this is not a zip file at all")
    assert "bad zip" in str(exc.value).lower()


def test_audit_safety_rejects_encrypted_entries() -> None:
    """The encryption check fires on `flag_bits & 0x1`. Going through
    `zipfile.writestr` rewrites flag_bits on write, so we test the
    safety pre-flight directly with a synthetic `ZipInfo` whose flag
    bit is set."""
    info = zipfile.ZipInfo("locked.pdf")
    info.flag_bits |= 0x1
    info.file_size = 100
    info.compress_size = 100
    with pytest.raises(UnsafeZipError) as exc:
        _audit_safety([info])
    assert "encrypted" in str(exc.value).lower()


def test_extract_pdfs_rejects_too_many_entries(monkeypatch) -> None:
    """Override the entry-count cap to a small number, then build a ZIP
    that exceeds it. Avoids needing a real bomb on disk."""
    monkeypatch.setenv("ZIP_MAX_ENTRIES", "3")
    entries = [(f"inv_{i:03d}.pdf", b"%PDF-1.4") for i in range(5)]
    with pytest.raises(UnsafeZipError) as exc:
        audit_and_extract_pdfs(_build_zip(entries))
    assert "too many entries" in str(exc.value).lower()


def test_audit_zipfile_lenient_keeps_pdf_doc_docx() -> None:
    """Step 2's on-disk caller uses `audit_zipfile` with a tuple of kept
    extensions. Non-keepers (including non-PDF files like .txt) are
    filtered silently, NOT raised. This is the long-standing Step 2
    behaviour that the safety-net job still relies on."""
    data = _build_zip([
        ("inv_001.pdf", b"%PDF-1.4 a"),
        ("inv_002.doc", b"old-school word"),
        ("inv_003.docx", b"PK fake docx"),
        ("readme.txt",  b"hello"),
    ])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        kept = audit_zipfile(zf, (".pdf", ".doc", ".docx"))
    names = sorted(Path(i.filename).name for i in kept)
    assert names == ["inv_001.pdf", "inv_002.doc", "inv_003.docx"]
