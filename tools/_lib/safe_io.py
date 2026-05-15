"""Safe I/O helpers: filename sanitization and atomic writes.

Atomic writes are critical for `_Unmatched/Invoices` where steps 1, 2, 3 may
all touch the same folder concurrently — we never want a downstream step to
see a half-written PDF.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Windows-invalid characters + control chars
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOT_SPACE_RE = re.compile(r"[. ]+$")
_MULTI_WHITESPACE_RE = re.compile(r"\s+")

# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Strip/replace any character that would break a Windows file name.

    Mirrors the cleanup logic from the N8n flow's node 10A.
    """
    if not name:
        return "file"
    # Drop any path separators — keep only the leaf
    leaf = str(name).replace("\\", "/").split("/")[-1]
    leaf = _INVALID_CHARS_RE.sub("_", leaf)
    leaf = _MULTI_WHITESPACE_RE.sub(" ", leaf).strip()
    leaf = _TRAILING_DOT_SPACE_RE.sub("", leaf)
    if not leaf:
        leaf = "file"
    if len(leaf) > max_length:
        # Preserve extension when truncating
        m = re.search(r"(\.[A-Za-z0-9]{1,8})$", leaf)
        ext = m.group(1) if m else ""
        base = leaf[: -len(ext)] if ext else leaf
        leaf = base[: max_length - len(ext)] + ext
    return leaf


def ensure_parent(path: Path) -> None:
    """Make sure the parent directory of `path` exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically.

    Writes to <path>.tmp.<pid>, fsyncs, then os.replace()s into the final name.
    Readers never see a partial file.
    """
    path = Path(path)
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def atomic_copy(src: Path, dst: Path) -> None:
    """Copy a file atomically (read fully, then atomic_write_bytes)."""
    src = Path(src)
    with open(src, "rb") as f:
        data = f.read()
    atomic_write_bytes(Path(dst), data)


def sanitize_path_component(name: str) -> str:
    """Sanitize a single path component sourced from external data (e.g. XLS).

    Stricter than `sanitize_filename`: rejects anything that could escape the
    intended directory or hit a Windows reserved device name. Raises
    `ValueError` rather than silently rewriting, because manager/AP names come
    from a curated spreadsheet — silent rewrites mask data-entry bugs.
    """
    raw = "" if name is None else str(name)
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("path component is empty")
    if cleaned in (".", ".."):
        raise ValueError(f"path component {cleaned!r} is not allowed")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"path component {cleaned!r} contains a path separator")
    if re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"path component {cleaned!r} looks like a drive letter")
    if _INVALID_CHARS_RE.search(cleaned):
        raise ValueError(f"path component {cleaned!r} contains invalid characters")
    if _TRAILING_DOT_SPACE_RE.search(cleaned):
        raise ValueError(f"path component {cleaned!r} ends with a dot or space")
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED_NAMES:
        raise ValueError(f"path component {cleaned!r} is a Windows reserved name")
    return cleaned


def assert_under_root(path: Path, root: Path) -> Path:
    """Resolve `path` and `root` and assert the path is inside the root tree.

    Returns the resolved path. Raises `ValueError` on escape. Use this as the
    last step of every path-builder that interpolates externally-sourced
    components, so a malicious or malformed manager/AP name can't redirect
    writes outside `STRATACO_ROOT`.
    """
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"path {resolved_path} escapes root {resolved_root}"
        ) from exc
    return resolved_path


def safe_write_unique(path: Path, data: bytes) -> Path:
    """Collision-safe atomic write.

    If `path` does not exist, behaves exactly like `atomic_write_bytes` and
    returns `path`. If it exists, appends ` (1)`, ` (2)`, … to the stem (before
    the extension) until an unused name is found, then writes there and
    returns the actual path written.

    Used everywhere a step saves an external file (invoice attachments, AP
    transfers, archived paid invoices) so a same-name duplicate from a
    different vendor never silently overwrites an earlier invoice.
    """
    path = Path(path)
    if not path.exists():
        atomic_write_bytes(path, data)
        return path
    # Idempotent retry: same content already at the target — return it in-place
    # rather than creating a collision copy. A different-content file at path
    # falls through to the collision-rename loop below.
    try:
        if path.read_bytes() == data:
            return path
    except OSError:
        pass
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            atomic_write_bytes(candidate, data)
            return candidate
        try:
            if candidate.read_bytes() == data:
                return candidate
        except OSError:
            pass
        counter += 1
