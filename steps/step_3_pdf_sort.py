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
from tools._lib.stamp import render_received_stamp
from tools._lib.xls import load_plans, plan_to_manager

_STAMP = "step_3"


def _today_received_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    return now.strftime("%b %d %Y").upper()


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


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
            try:
                pdf_bytes = pdf_path.read_bytes()
            except Exception as exc:
                run.error(f"could not read {pdf_path}: {exc}")
                continue

            # Filename match with base-plan-unique-manager fallback per
            # workflows/step_3_pdf_sort.md.
            row = plan_match.match_from_filename_with_base_fallback(pdf_path.name, rows)
            match_source = "filename" if row else ""

            if not row:
                # Fall back to PDF text
                text = extract_full_text(pdf_bytes)
                result = plan_match.match_from_pdf_text(text, rows)
                if result.plan_row and result.plan_norm:
                    row = result.plan_row
                    match_source = "pdf_text"
                else:
                    run.info(f"no safe match for {pdf_path.name}: {result.note or 'no text'}")
                    continue

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
                try:
                    updated = ledger.increment_dup_count(duplicate.sha256)
                    archive_hint = duplicate.archive_path or f"({duplicate.current_stage})"
                    run.info(
                        f"duplicate skipped: {pdf_path.name} "
                        f"(sha={sha[:12]}..., matches {duplicate.sha256[:12]}..., "
                        f"original at {archive_hint}, dup_count={updated.dup_count})"
                    )
                except Exception as exc:
                    run.error(f"ledger increment failed for {sha[:12]}...: {exc}")

                # Rename in place so the glob skips it next run.
                dup_marker_name = safe_io.sanitize_filename(
                    f"Processed-{_now_stamp()}-DUPLICATE-{pdf_path.name}"
                )
                dup_marker_path = pdf_path.parent / dup_marker_name
                try:
                    safe_io.safe_write_unique(dup_marker_path, pdf_bytes)
                except Exception as exc:
                    run.error(
                        f"could not write duplicate marker for {pdf_path.name}: {exc} — "
                        f"leaving original in place (it WILL be re-detected next run)"
                    )
                    continue
                try:
                    pdf_path.unlink(missing_ok=True)
                except Exception as exc:
                    run.error(f"could not unlink original {pdf_path}: {exc}")
                run.processed += 1
                continue

            # New filename: keep original name, prefix with plan if not already present
            if pdf_path.name.upper().startswith(plan_norm) or pdf_path.name.upper().startswith(plan_pretty.upper()):
                new_name = pdf_path.name
            else:
                new_name = safe_io.sanitize_filename(f"{plan_pretty} - {pdf_path.name}")

            # Stamp
            try:
                stamped = render_received_stamp(
                    pdf_bytes,
                    received_date=received_str,
                    plan_pretty=plan_pretty,
                )
            except Exception as exc:
                run.error(f"stamp failed for {pdf_path.name}: {exc} — saving unstamped")
                stamped = pdf_bytes

            # Write to manager folder
            dest = paths.manager_to_approve(row.manager_name) / new_name
            try:
                written = safe_io.safe_write_unique(dest, stamped)
            except Exception as exc:
                run.error(f"could not write {dest}: {exc}")
                continue

            # Mark original as Processed only AFTER the dest write confirmed.
            processed_name = safe_io.sanitize_filename(
                f"Processed-{_now_stamp()}-{pdf_path.name}"
            )
            processed_path = pdf_path.parent / processed_name
            try:
                safe_io.safe_write_unique(processed_path, pdf_bytes)
            except Exception as exc:
                run.error(
                    f"could not write processed marker for {pdf_path.name}: {exc} — "
                    f"leaving original in place"
                )
                run.processed += 1
                run.info(f"routed via {match_source}: {pdf_path.name} -> {written}")
                continue
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception as exc:
                run.error(f"could not unlink original {pdf_path}: {exc}")

            # Upsert ledger row so a future duplicate of this PDF gets caught.
            # If we matched an override row via Layer B (different bytes, same
            # semantic key), atomically retire it.
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
                run.error(f"ledger upsert failed for {sha[:12]}... ({pdf_path.name}): {exc}")

            run.processed += 1
            if written != dest:
                run.info(
                    f"routed via {match_source}: {pdf_path.name} -> {written} "
                    f"(collision-renamed from {dest.name})"
                )
            else:
                run.info(f"routed via {match_source}: {pdf_path.name} -> {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
