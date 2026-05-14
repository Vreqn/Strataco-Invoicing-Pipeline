"""Unit tests for the zip-bomb guards (originally 0.4.0 in
steps/step_2_unzip.py; lifted into tools/_lib/zip_safe.py for the
2026-05-13 ZIP-orphan fix so both Step 1 and Step 2 share them).

Builds synthetic ZIPs that violate each policy in turn and confirms
`audit_zipfile` rejects them with a clear reason. Also verifies the
happy path: a small, well-formed ZIP with one PDF entry survives the
audit.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing config doesn't fail.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "x@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

import pytest

from tools._lib.zip_safe import (
    UnsafeZipError as _UnsafeZipError,
    audit_zipfile,
)

_KEEP_EXT = (".pdf", ".doc", ".docx")


def _audit_zip(zf):
    """Back-compat wrapper that mirrors the old `step_2_unzip._audit_zip`
    signature so the rest of this file's assertions read unchanged."""
    return audit_zipfile(zf, _KEEP_EXT)


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Create an in-memory ZIP with the given entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zinfo = zipfile.ZipInfo(name)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zinfo, data)
    return buf.getvalue()


def _force_encrypted_flag(zip_bytes: bytes) -> bytes:
    """Set the 'encrypted' bit (bit 0 of the general purpose bit flag) on
    every file header in the ZIP, without doing any actual encryption.

    Python's stdlib zipfile can't write encrypted entries, but the audit
    only inspects ZipInfo.flag_bits — which is populated from the file
    header's GP bit flag field. Flip the bit in both the local file header
    (signature 0x04034b50, GP flag at offset +6) and every central directory
    entry (signature 0x02014b50, GP flag at offset +8). This produces a
    ZIP that *claims* to be encrypted, which is exactly what _audit_zip
    must refuse to read.
    """
    out = bytearray(zip_bytes)
    i = 0
    while True:
        i = out.find(b"PK\x03\x04", i)
        if i < 0:
            break
        out[i + 6] |= 0x01
        i += 4
    i = 0
    while True:
        i = out.find(b"PK\x01\x02", i)
        if i < 0:
            break
        out[i + 8] |= 0x01
        i += 4
    return bytes(out)


def _assert_unsafe(zip_bytes: bytes, reason_fragment: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
        zip_path = f.name
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with pytest.raises(_UnsafeZipError) as excinfo:
                _audit_zip(zf)
            assert reason_fragment.lower() in str(excinfo.value).lower(), (
                f"reason missing fragment {reason_fragment!r}: {excinfo.value}"
            )
    finally:
        os.unlink(zip_path)


def test_happy_path_survives() -> None:
    zip_bytes = _build_zip([("invoice.pdf", b"%PDF-1.4 small body" * 8)])
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
        zip_path = f.name
    try:
        with zipfile.ZipFile(zip_path) as zf:
            kept = _audit_zip(zf)
            assert len(kept) == 1 and kept[0].filename == "invoice.pdf", (
                f"[happy path] expected 1 kept entry 'invoice.pdf', got {[k.filename for k in kept]}"
            )
    finally:
        os.unlink(zip_path)


def test_too_many_entries_rejected() -> None:
    entries = [(f"f{i}.pdf", f"body {i}".encode()) for i in range(201)]
    zip_bytes = _build_zip(entries)
    _assert_unsafe(zip_bytes, "too many entries")


def test_encrypted_entry_rejected() -> None:
    zip_bytes = _force_encrypted_flag(_build_zip([("secret.pdf", b"x" * 100)]))
    _assert_unsafe(zip_bytes, "encrypted")


def test_oversized_entry_rejected() -> None:
    """Build an entry whose uncompressed size header exceeds the default 100 MB."""
    os.environ["ZIP_MAX_UNCOMPRESSED_BYTES"] = "5"
    try:
        zip_bytes = _build_zip([("small.pdf", b"x" * 100)])
        _assert_unsafe(zip_bytes, "uncompressed size")
    finally:
        del os.environ["ZIP_MAX_UNCOMPRESSED_BYTES"]


def test_high_ratio_rejected() -> None:
    """A highly-compressible payload (lots of zeros) exceeds the default 100:1 ratio."""
    payload = b"\x00" * (100_000)
    zip_bytes = _build_zip([("bomb.pdf", payload)])
    _assert_unsafe(zip_bytes, "ratio")
