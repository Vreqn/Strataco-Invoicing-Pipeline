"""Step 2 — Unzip ZIP attachments left in _Unmatched/Invoices.

Replaces "Step 2 - Strataco Invoice - .pdf and .zip attachment handling"
(N8n). For every ZIP in _Unmatched/Invoices:

1. Extract every entry; keep .pdf/.doc/.docx
2. Write extracted files back to _Unmatched/Invoices with name `<zipbase>__<inner>`
3. Rename the original ZIP to `Processed-YYYYMMDD-HHMMSS-<original>.zip`

Schedule: 06:10 Mon–Fri.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import config, paths, safe_io
from tools._lib.log import daily_log

_STAMP = "step_2"
_KEEP_EXT = (".pdf", ".doc", ".docx")
_STAGING_DIR_NAME = ".staging"


class _UnsafeZipError(Exception):
    """Raised when a ZIP fails the bomb-protection pre-flight."""


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _audit_zip(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject zip bombs and encrypted entries before reading anything.

    Raises `_UnsafeZipError` with a human-readable reason. Returns the list
    of entries that survived the policy and are safe to extract — i.e.
    non-directory entries with a kept extension, after the limits have
    been validated against the FULL archive (so a tiny PDF accompanying a
    1 GB binary still fails — encrypted/oversized entries are a whole-zip
    red flag, not a per-entry one).
    """
    infos = zf.infolist()
    if len(infos) > config.zip_max_entries():
        raise _UnsafeZipError(
            f"too many entries ({len(infos)} > {config.zip_max_entries()})"
        )

    max_per_entry = config.zip_max_uncompressed_bytes()
    max_total = config.zip_max_total_bytes()
    max_ratio = config.zip_max_ratio()

    total = 0
    for info in infos:
        # ZIP encryption flag — we have no password and won't prompt.
        if info.flag_bits & 0x1:
            raise _UnsafeZipError(f"encrypted entry {info.filename!r}")
        if info.file_size > max_per_entry:
            raise _UnsafeZipError(
                f"entry {info.filename!r} uncompressed size "
                f"{info.file_size} > {max_per_entry}"
            )
        compressed = max(info.compress_size, 1)
        if info.file_size / compressed > max_ratio:
            raise _UnsafeZipError(
                f"entry {info.filename!r} ratio "
                f"{info.file_size}/{compressed} > {max_ratio}"
            )
        total += info.file_size
        if total > max_total:
            raise _UnsafeZipError(
                f"total uncompressed size {total} > {max_total}"
            )

    kept: list[zipfile.ZipInfo] = []
    for info in infos:
        if info.is_dir():
            continue
        leaf = Path(info.filename).name
        if not leaf:
            continue
        if not leaf.lower().endswith(_KEEP_EXT):
            continue
        kept.append(info)
    return kept


def _process_zip(zip_path: Path, run) -> int:
    """Audit → extract to staging → atomically promote to _Unmatched/Invoices.

    Per-entry failures abort the whole zip's extraction (we don't want to
    partially process a malformed archive and lose the rest), and the
    original ZIP stays in place without the Processed- rename so a human can
    investigate.
    """
    out_dir = zip_path.parent
    zip_base = zip_path.stem

    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                kept = _audit_zip(zf)
            except _UnsafeZipError as exc:
                run.error(f"unsafe zip {zip_path.name}: {exc} — leaving in place")
                return 0

            if not kept:
                run.info(f"no .pdf/.doc/.docx entries in {zip_path.name}")
                # Still mark the ZIP processed so we don't retry it forever.
                _mark_processed(zip_path, run)
                return 0

            staging = out_dir / _STAGING_DIR_NAME / f"{zip_base}-{_now_stamp()}"
            staging.mkdir(parents=True, exist_ok=True)
            staged: list[tuple[Path, str]] = []  # (staged_path, public_name)
            try:
                for info in kept:
                    leaf = Path(info.filename).name
                    out_name = safe_io.sanitize_filename(f"{zip_base}__{leaf}")
                    staged_path = staging / out_name
                    try:
                        with zf.open(info, "r") as src:
                            data = src.read()
                        # Plain atomic_write_bytes into staging — collision-safe
                        # uniquification happens when we promote to out_dir.
                        safe_io.atomic_write_bytes(staged_path, data)
                        staged.append((staged_path, out_name))
                    except Exception as exc:
                        run.error(
                            f"entry {info.filename!r} in {zip_path.name}: {exc} — "
                            f"aborting zip"
                        )
                        raise
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                return 0
    except zipfile.BadZipFile as exc:
        run.error(f"bad zip {zip_path}: {exc}")
        return 0

    # Promote staged files into _Unmatched/Invoices/, uniquifying on collision.
    extracted = 0
    for staged_path, public_name in staged:
        public_path = out_dir / public_name
        try:
            data = staged_path.read_bytes()
            written = safe_io.safe_write_unique(public_path, data)
            extracted += 1
            if written != public_path:
                run.info(
                    f"extracted {staged_path.name} -> {written} "
                    f"(collision-renamed from {public_name})"
                )
            else:
                run.info(f"extracted {staged_path.name} -> {written}")
        except Exception as exc:
            run.error(f"promote {staged_path} -> {public_path}: {exc}")
    shutil.rmtree(staging, ignore_errors=True)

    _mark_processed(zip_path, run)
    return extracted


def _mark_processed(zip_path: Path, run) -> None:
    processed_name = safe_io.sanitize_filename(
        f"Processed-{_now_stamp()}-{zip_path.name}"
    )
    target = zip_path.parent / processed_name
    try:
        os.replace(zip_path, target)
        run.info(f"marked processed: {target}")
    except Exception as exc:
        run.error(f"could not rename {zip_path} -> {target}: {exc}")


def main() -> int:
    with daily_log(_STAMP) as run:
        if run.status == "skipped":
            return 0

        unmatched = paths.unmatched_invoices()
        if not unmatched.exists():
            run.info(f"unmatched dir does not exist yet: {unmatched}")
            return 0

        zips = sorted(
            p for p in unmatched.glob("*.zip")
            if not p.name.lower().startswith("processed-")
        )
        run.info(f"found {len(zips)} zip(s) in {unmatched}")
        for z in zips:
            extracted = _process_zip(z, run)
            run.processed += extracted
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
