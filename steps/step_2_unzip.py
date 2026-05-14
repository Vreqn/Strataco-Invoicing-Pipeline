"""Step 2 — Safety-net unzip job for _Unmatched/Invoices.

As of the 2026-05-13 ZIP-orphan fix, the email intake path (Step 1)
unpacks ZIPs in memory and routes their PDFs directly. Email-originated
ZIPs no longer reach this step — Step 1 either routes them, or holds
the parent email in the Inbox with the red flag.

This step exists as a safety net: if anything ever lands in
_Unmatched/Invoices/ (operator manual drop, debugging, a future Step 1
partial-commit edge case), the scheduled 06:10 run drains it. On a
normal day the folder is empty and this job logs "found 0 zip(s)".

For every ZIP in _Unmatched/Invoices:

1. Extract every entry; keep .pdf/.doc/.docx (lenient — anything else
   is silently filtered out, matching long-standing behaviour for the
   manual-drop case where a vendor's odd attachment shouldn't tank the
   whole archive).
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

from tools._lib import paths, safe_io
from tools._lib.log import daily_log
from tools._lib.zip_safe import UnsafeZipError as _UnsafeZipError, audit_zipfile

_STAMP = "step_2"
_KEEP_EXT = (".pdf", ".doc", ".docx")
_STAGING_DIR_NAME = ".staging"


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


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
                kept = audit_zipfile(zf, _KEEP_EXT)
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
