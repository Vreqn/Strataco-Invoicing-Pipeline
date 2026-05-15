"""Step 3 — Sort unmatched PDFs by filename or PDF text.

Replaces "Step 3 - Strataco Invoice - PDF Opening and Sorting" (N8n). For
every PDF in `_Unmatched/Invoices` that doesn't start with `Processed -`:

1. Try to extract a Strata Plan from the filename. If found and in the XLS
   plan map, route to the manager's To_Approve folder.
2. Otherwise, extract PDF text and run `match_from_pdf_text` (verbatim port
   of the safe scoring algorithm from N8n node 11).
3. On a match, apply the Received stamp, write to manager's To_Approve,
   and write `Processed-YYYYMMDD-HHMMSS-<original>` back to _Unmatched.
4. Files with no safe match are left in place.

Schedule: 06:20 Mon–Fri.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import (
    dup_fingerprint,
    dup_ledger,
    paths,
    plan_match,
    safe_io,
    strataplan_snapshot,
)
from tools._lib.log import daily_log
from tools._lib.pdf_text import extract_full_text
from tools._lib.stamp import render_received_stamp, received_stamp_sha_matches
from tools._lib.xls import load_plans, plan_to_manager

_STAMP = "step_3"


def _today_received_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    return now.strftime("%b %d %Y").upper()


def _stamp_or_raw(
    pdf_bytes: bytes,
    received_str: str,
    plan_pretty: str,
    sha: str,
    run,
) -> bytes:
    try:
        return render_received_stamp(
            pdf_bytes,
            received_date=received_str,
            plan_pretty=plan_pretty,
            sha=sha,
        )
    except Exception as exc:
        run.error(f"stamp failed: {exc} — saving unstamped")
        return pdf_bytes



def _route_one(
    pdf_path: Path,
    rows: list,
    ledger: dup_ledger.Ledger,
    run,
    received_str: str,
) -> None:
    """Route one unmatched PDF to the correct manager's To_Approve folder.

    True-move: after a successful ledger write the source is deleted with no
    Processed- marker left behind. Ledger writes are fail-closed — source
    stays in _Unmatched/ for retry if the write fails.
    """
    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as exc:
        run.error(f"could not read {pdf_path}: {exc}")
        return

    # Filename match with base-plan-unique-manager fallback per
    # workflows/step_3_pdf_sort.md.
    row = plan_match.match_from_filename_with_base_fallback(pdf_path.name, rows)
    match_source = "filename" if row else ""

    if not row:
        text = extract_full_text(pdf_bytes)
        result = plan_match.match_from_pdf_text(text, rows)
        if result.plan_row and result.plan_norm:
            row = result.plan_row
            match_source = "pdf_text"
        else:
            run.info(f"no safe match for {pdf_path.name}: {result.note or 'no text'}")
            return

    plan_norm = row.plan_norm
    plan_pretty = plan_match.pretty_plan(plan_norm)

    # Duplicate-detection check BEFORE stamping.
    #   - find_by_hash returns rows regardless of stage; we treat
    #     overridden as "route normally" and superseded as a normal
    #     duplicate (those exact bytes were already processed).
    #   - find_by_semantic_key already excludes overridden/superseded.
    #   - find_overridden_* surface the override case so we can
    #     consume it after a successful route.
    sha = dup_fingerprint.sha256_of(pdf_bytes)
    inv_num, amount = dup_fingerprint.compute_layer_b(pdf_bytes, plan_norm)
    # No email at this entry point — Layer B is intentionally a no-op
    # here (blank sender_domain). Layer A still catches verbatim
    # resends of manually-dropped PDFs.
    duplicate = ledger.find_by_hash(sha)
    if duplicate is not None and duplicate.current_stage == "overridden":
        duplicate = None
    if duplicate is None:
        duplicate = ledger.find_by_semantic_key(plan_norm, inv_num, amount, "")
    overridden = None
    if duplicate is None:
        overridden = ledger.find_overridden_by_hash(sha)
        if overridden is None:
            overridden = ledger.find_overridden_by_semantic_key(
                plan_norm, inv_num, amount, "",
            )

    if duplicate is not None:
        # Fail-closed: if the ledger increment fails, leave source in
        # _Unmatched/ for the next run to retry.
        try:
            updated = ledger.increment_dup_count(duplicate.sha256)
            archive_hint = duplicate.archive_path or f"({duplicate.current_stage})"
            run.info(
                f"duplicate skipped: {pdf_path.name} "
                f"(sha={sha[:12]}..., matches {duplicate.sha256[:12]}..., "
                f"original at {archive_hint}, dup_count={updated.dup_count})"
            )
        except Exception as exc:
            run.error(
                f"ledger increment failed for {sha[:12]}...: {exc} — "
                f"leaving source for retry"
            )
            return
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception as exc:
            run.error(f"could not unlink duplicate {pdf_path}: {exc}")
        run.processed += 1
        return

    # New filename: keep original name, prefix with plan if not already present
    if pdf_path.name.upper().startswith(plan_norm) or pdf_path.name.upper().startswith(plan_pretty.upper()):
        new_name = pdf_path.name
    else:
        new_name = safe_io.sanitize_filename(f"{plan_pretty} - {pdf_path.name}")

    # Compute dest before stamping so a cross-day retry can detect the prior
    # write and reuse its bytes instead of re-stamping with a new date.
    dest = paths.manager_to_approve(row.manager_name) / new_name

    # Stamp — pass sha so field names are deterministic, enabling safe_write_unique
    # to detect an identical prior write on retry (content-idempotency).
    # If dest already exists AND was stamped from this same source (SHA matches),
    # reuse those bytes so a cross-day retry doesn't change the stamp date and
    # cause safe_write_unique to create a (1) collision copy instead of returning
    # the existing path. A different invoice at the same dest path (SHA mismatch)
    # gets a fresh stamp so safe_write_unique correctly produces a collision copy.
    if dest.exists():
        try:
            dest_bytes = dest.read_bytes()
            if received_stamp_sha_matches(dest_bytes, sha):
                stamped = dest_bytes
            else:
                stamped = _stamp_or_raw(pdf_bytes, received_str, plan_pretty, sha, run)
        except Exception as exc:
            run.error(f"could not read existing {dest} — re-stamping: {exc}")
            stamped = _stamp_or_raw(pdf_bytes, received_str, plan_pretty, sha, run)
    else:
        stamped = _stamp_or_raw(pdf_bytes, received_str, plan_pretty, sha, run)

    # Write to manager folder — safe_write_unique returns the existing path
    # unchanged when the file already contains identical bytes (retry after
    # ledger failure), or creates a (N) collision copy for a genuinely
    # different invoice with the same sanitized filename.
    try:
        written = safe_io.safe_write_unique(dest, stamped)
    except Exception as exc:
        run.error(f"could not write {dest}: {exc}")
        return

    # Fail-closed ledger upsert: source only deleted after ledger confirms
    # the destination write. If upsert fails, source stays in _Unmatched/
    # for retry (destination is already written — retry is idempotent via
    # safe_write_unique collision rename).
    new_row = dup_ledger.make_row(
        sha256=sha,
        plan_norm=plan_norm,
        invoice_number=inv_num,
        amount_cents=amount,
        current_stage="manager_queue",
    )
    try:
        if overridden is not None and overridden.sha256 != sha:
            try:
                ledger.consume_override_and_insert(
                    old_sha256=overridden.sha256,
                    new_row=new_row,
                )
                run.info(
                    f"consumed Layer B override for {pdf_path.name} "
                    f"(retired {overridden.sha256[:12]}..., inserted {sha[:12]}...)"
                )
            except ValueError as exc:
                run.info(
                    f"override at {overridden.sha256[:12]}... already consumed "
                    f"({exc}); inserting {sha[:12]}... as new"
                )
                ledger.upsert(new_row)
        else:
            ledger.upsert(new_row)
    except Exception as exc:
        run.error(
            f"ledger upsert failed for {sha[:12]}... ({pdf_path.name}): {exc} — "
            f"leaving source for retry"
        )
        return

    try:
        pdf_path.unlink(missing_ok=True)
    except Exception as exc:
        run.error(f"could not unlink original {pdf_path}: {exc}")

    run.processed += 1
    if written != dest:
        run.info(
            f"routed via {match_source}: {pdf_path.name} -> {written} "
            f"(collision-renamed from {dest.name})"
        )
    else:
        run.info(f"routed via {match_source}: {pdf_path.name} -> {written}")


def main() -> int:
    with daily_log(_STAMP) as run:
        if run.status == "skipped":
            return 0

        try:
            snapshot = strataplan_snapshot.require_fresh_snapshot()
        except strataplan_snapshot.SnapshotStaleError as exc:
            run.error(f"snapshot is not today's — refusing to run: {exc}")
            return 1

        rows = load_plans(snapshot)
        plan_map = plan_to_manager(rows)
        run.info(f"loaded {len(plan_map)} unique plans from snapshot")

        try:
            ledger = dup_ledger.load()
        except ValueError as exc:
            run.error(f"dup ledger corrupted — halting day: {exc}")
            return 1

        unmatched = paths.unmatched_invoices()
        if not unmatched.exists():
            run.info(f"unmatched dir does not exist yet: {unmatched}")
            return 0

        candidates = sorted(
            p for p in unmatched.glob("*.pdf")
            if not p.name.lower().startswith("processed-") and not p.name.lower().startswith("processed -")
        )
        run.info(f"found {len(candidates)} PDF(s) to sort")

        received_str = _today_received_str()

        for pdf_path in candidates:
            _route_one(pdf_path, rows, ledger, run, received_str)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
