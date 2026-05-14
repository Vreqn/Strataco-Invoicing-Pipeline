"""Shared ZIP audit + extraction helpers.

Two entry points, one set of safety checks:

  - `audit_zipfile(zf, keep_exts)` — for the on-disk safety-net job
    (Step 2). Performs bomb / encryption / oversize checks against the
    full archive, then returns the `ZipInfo` entries that match
    `keep_exts`. Anything outside `keep_exts` is silently filtered out,
    matching the long-standing Step 2 behavior (a .txt file in a ZIP
    is dropped, not raised).

  - `audit_and_extract_pdfs(zip_bytes)` — for Step 1's in-memory
    intake inspection. Strict: anything that isn't a directory, a Mac
    resource fork, a skippable `.txt` companion file, or a `.pdf`
    raises `UnsafeZipError`. Step 1's caller turns the raise into a
    "this email needs the operator — leave it in the Inbox flagged"
    decision, which is the whole point of the change that introduced
    this module.

Both share `_audit_safety`, which enforces the byte/entry/ratio limits
from `tools._lib.config`. Encrypted entries are always rejected (we
have no password and won't prompt).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from tools._lib import config


class UnsafeZipError(Exception):
    """Raised when a ZIP fails the safety pre-flight.

    Surfaces a human-readable reason in `str(exc)` for the daily log.
    """


# Non-PDF entries that are never a real invoice and should not poison a
# ZIP. A `.txt` is informational by nature (e.g. TELUS Bill Analyzer
# staples a `manifest.txt` next to the invoice PDF) — skipped like the
# `__MACOSX/` resource-fork noise rather than raising. `.docx` / `.xlsx`
# are deliberately NOT here: they could be a real invoice the vendor
# should resend as PDF, so they keep poisoning the ZIP.
IGNORABLE_COMPANION_EXTS = (".txt",)


def _is_mac_metadata(filename: str) -> bool:
    """Skip macOS resource-fork crap that appears in ZIPs from Mac users.

    `__MACOSX/...` directory entries and `._foo.pdf` AppleDouble files
    are ZIP-level noise; they should not gate routing decisions.
    """
    # Normalize separators — ZIP paths use `/` but some tools write `\`.
    norm = filename.replace("\\", "/")
    if norm.startswith("__MACOSX/") or "/__MACOSX/" in norm:
        return True
    leaf = Path(norm).name
    if leaf.startswith("._"):
        return True
    return False


def _audit_safety(infos: list[zipfile.ZipInfo]) -> None:
    """Run the bomb / encryption / oversize checks against every entry.

    Raises `UnsafeZipError` with a human-readable reason. Counts the
    full archive, including entries that the caller will eventually
    filter out — an encrypted or oversized side file is a whole-zip
    red flag, not a per-entry one.
    """
    if len(infos) > config.zip_max_entries():
        raise UnsafeZipError(
            f"too many entries ({len(infos)} > {config.zip_max_entries()})"
        )

    max_per_entry = config.zip_max_uncompressed_bytes()
    max_total = config.zip_max_total_bytes()
    max_ratio = config.zip_max_ratio()

    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise UnsafeZipError(f"encrypted entry {info.filename!r}")
        if info.file_size > max_per_entry:
            raise UnsafeZipError(
                f"entry {info.filename!r} uncompressed size "
                f"{info.file_size} > {max_per_entry}"
            )
        compressed = max(info.compress_size, 1)
        if info.file_size / compressed > max_ratio:
            raise UnsafeZipError(
                f"entry {info.filename!r} ratio "
                f"{info.file_size}/{compressed} > {max_ratio}"
            )
        total += info.file_size
        if total > max_total:
            raise UnsafeZipError(
                f"total uncompressed size {total} > {max_total}"
            )


def audit_zipfile(
    zf: zipfile.ZipFile,
    keep_exts: tuple[str, ...],
) -> list[zipfile.ZipInfo]:
    """Lenient audit used by the Step 2 safety-net job.

    Runs the full safety pre-flight, then returns the entries whose
    leaf-name extension (case-insensitive) is in `keep_exts`.
    Directories, empty leafs, and Mac resource-fork files are filtered
    out silently. Any other extension is also filtered silently —
    matching the historical Step 2 behavior of "extract what we
    recognize, ignore the rest."
    """
    infos = zf.infolist()
    _audit_safety(infos)

    kept: list[zipfile.ZipInfo] = []
    for info in infos:
        if info.is_dir():
            continue
        leaf = Path(info.filename).name
        if not leaf:
            continue
        if _is_mac_metadata(info.filename):
            continue
        if not leaf.lower().endswith(keep_exts):
            continue
        kept.append(info)
    return kept


def audit_and_extract_pdfs(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    """Strict in-memory extraction used by Step 1 at email intake.

    Returns `[(inner_leaf_name, pdf_bytes), ...]` for each `.pdf` entry
    in the archive.

    Raises `UnsafeZipError`:
      - if the bytes aren't a valid ZIP (`BadZipFile`)
      - on bomb / encryption / oversize per `_audit_safety`
      - if the archive contains any non-PDF real-file entries (after
        filtering directories, Mac resource forks, and skippable
        `.txt` companion files per `IGNORABLE_COMPANION_EXTS`)

    The strict no-non-PDF policy is what lets Step 1 honor the "Inbox
    is the single source of truth" invariant for ZIP-bearing emails:
    a vendor who staples a `.docx` cover letter to a PDF invoice
    forces the whole email to stay in the Inbox with a red flag,
    where the operator handles it manually. A `.txt` is the one
    exception — it's never an invoice, so it's skipped rather than
    raised. See `workflows/step_1_intake.md` and the resolved
    2026-05-13 ZIP-orphan entry in `To-Speak-About.txt`.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise UnsafeZipError(f"bad zip: {exc}") from exc

    with zf:
        infos = zf.infolist()
        _audit_safety(infos)

        pdf_entries: list[zipfile.ZipInfo] = []
        disallowed: list[str] = []
        for info in infos:
            if info.is_dir():
                continue
            leaf = Path(info.filename).name
            if not leaf:
                continue
            if _is_mac_metadata(info.filename):
                continue
            if leaf.lower().endswith(".pdf"):
                pdf_entries.append(info)
            elif leaf.lower().endswith(IGNORABLE_COMPANION_EXTS):
                continue
            else:
                disallowed.append(leaf)

        if disallowed:
            raise UnsafeZipError(
                f"non-PDF entries: {', '.join(sorted(set(disallowed)))}"
            )

        results: list[tuple[str, bytes]] = []
        for info in pdf_entries:
            leaf = Path(info.filename).name
            try:
                with zf.open(info, "r") as src:
                    data = src.read()
            except Exception as exc:
                raise UnsafeZipError(
                    f"failed to read entry {leaf!r}: {exc}"
                ) from exc
            results.append((leaf, data))
        return results
