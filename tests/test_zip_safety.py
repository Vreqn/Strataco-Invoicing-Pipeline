"""Unit tests for the 0.4.0 zip-bomb guards in steps/step_2_unzip.py.

Builds synthetic ZIPs that violate each policy in turn and confirms
`_audit_zip` rejects them with a clear reason. Also verifies the happy
path: a small, well-formed ZIP with one PDF entry survives the audit.

Standalone: no pytest dependency. Run with `python tests/test_zip_safety.py`.
Exits 0 on success, 1 on failure.
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

from steps.step_2_unzip import _audit_zip, _UnsafeZipError


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
    # Local file headers
    i = 0
    while True:
        i = out.find(b"PK\x03\x04", i)
        if i < 0:
            break
        out[i + 6] |= 0x01
        i += 4
    # Central directory entries
    i = 0
    while True:
        i = out.find(b"PK\x01\x02", i)
        if i < 0:
            break
        out[i + 8] |= 0x01
        i += 4
    return bytes(out)


def _expect_unsafe(label: str, zip_bytes: bytes, reason_fragment: str) -> list[str]:
    failures: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
        zip_path = f.name
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                _audit_zip(zf)
                failures.append(f"[{label}] expected _UnsafeZipError, got success")
            except _UnsafeZipError as exc:
                if reason_fragment.lower() not in str(exc).lower():
                    failures.append(
                        f"[{label}] reason missing fragment {reason_fragment!r}: {exc}"
                    )
            except Exception as exc:
                failures.append(
                    f"[{label}] expected _UnsafeZipError, got "
                    f"{type(exc).__name__}: {exc}"
                )
    finally:
        os.unlink(zip_path)
    return failures


def test_happy_path_survives() -> list[str]:
    failures: list[str] = []
    zip_bytes = _build_zip([("invoice.pdf", b"%PDF-1.4 small body" * 8)])
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
        zip_path = f.name
    try:
        with zipfile.ZipFile(zip_path) as zf:
            kept = _audit_zip(zf)
            if len(kept) != 1 or kept[0].filename != "invoice.pdf":
                failures.append(f"[happy path] expected 1 kept entry 'invoice.pdf', got {[k.filename for k in kept]}")
    finally:
        os.unlink(zip_path)
    return failures


def test_too_many_entries_rejected() -> list[str]:
    # Default limit is 200; we'll build 201 small entries.
    entries = [(f"f{i}.pdf", f"body {i}".encode()) for i in range(201)]
    zip_bytes = _build_zip(entries)
    return _expect_unsafe("too many entries", zip_bytes, "too many entries")


def test_encrypted_entry_rejected() -> list[str]:
    zip_bytes = _force_encrypted_flag(_build_zip([("secret.pdf", b"x" * 100)]))
    return _expect_unsafe("encrypted entry", zip_bytes, "encrypted")


def test_oversized_entry_rejected() -> list[str]:
    """Build an entry whose uncompressed size header exceeds the default 100 MB."""
    # Build the ZIP manually: a real 100 MB file would take ages to deflate.
    # We exploit ZipInfo.file_size — the audit reads `info.file_size`, which
    # is the header value, not the actual extracted size. Set it artificially.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zinfo = zipfile.ZipInfo("huge.pdf")
        zinfo.compress_type = zipfile.ZIP_STORED
        zf.writestr(zinfo, b"placeholder")
    # Patch the file_size in the central directory entry after the fact would
    # be complex; instead use os.environ override to lower the cap for the
    # test, then a "normal" entry will trip the limit.
    os.environ["ZIP_MAX_UNCOMPRESSED_BYTES"] = "5"  # 5 bytes
    try:
        zip_bytes = _build_zip([("small.pdf", b"x" * 100)])
        return _expect_unsafe("oversized entry", zip_bytes, "uncompressed size")
    finally:
        del os.environ["ZIP_MAX_UNCOMPRESSED_BYTES"]


def test_high_ratio_rejected() -> list[str]:
    """A highly-compressible payload (lots of zeros) exceeds the default 100:1 ratio."""
    payload = b"\x00" * (100_000)  # very compressible
    zip_bytes = _build_zip([("bomb.pdf", payload)])
    # Default ratio is 100. 100k of zeros compresses to a few hundred bytes
    # → ratio > 100. Should fail.
    return _expect_unsafe("high ratio", zip_bytes, "ratio")


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("happy path survives", test_happy_path_survives),
        ("too many entries", test_too_many_entries_rejected),
        ("encrypted entry", test_encrypted_entry_rejected),
        ("oversized entry (override limit)", test_oversized_entry_rejected),
        ("high compression ratio", test_high_ratio_rejected),
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
