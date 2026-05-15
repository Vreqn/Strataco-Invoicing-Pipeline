"""Step 5 — Move manager-approved invoices to AP, apply Paid stamp.

Replaces "Step 5 - Transfer Approved Invoices to AP" (N8n) with two
behavioural changes flagged in `Things to Change to current Flows.txt`:

1. The destination filename in the AP's Approved_Invoices folder is no
   longer prefixed with `Approved - `; the original filename is preserved.
2. A new **Paid stamp** (blue, two editable fields) is applied to the PDF
   at this hand-off so the accountant can fill it in (Date + Check Number)
   before saving as a flat PDF.

For each unique manager:
  - List PDFs in their Approved/ folder
  - Skip files that start with `Processed -`
  - Extract Strata Plan from the filename
  - Look up the AP for that plan (with base-plan suffix-fallback)
  - Apply Paid stamp, atomic-write to AP's Approved_Invoices/<original_name>.pdf
  - Write `Processed - <original_name>.pdf` back to the manager's Approved folder

Then for each unique AP:
  - List their Approved_Invoices/*.pdf
  - Compare to rolling baseline at _state/ap_approved_history/_latest__<APKEY>.xls
  - Send notification email
  - Overwrite baseline

Schedule: 06:40 Mon–Fri.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import (
    dup_fingerprint,
    dup_ledger,
    graph,
    history,
    paths,
    plan_match,
    safe_io,
    strataplan_snapshot,
)
from tools._lib.log import daily_log
from tools._lib.stamp import flatten_acroform, render_paid_stamp
from tools._lib.xls import (
    PlanRow,
    base_plan_index,
    load_plans,
    plan_to_ap,
    unique_aps,
    unique_managers,
)

_STAMP = "step_5"


def _today_yesterday() -> tuple[str, str]:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return today, yesterday


def _resolve_ap(plan_norm: str, plan_map: dict[str, PlanRow], base_index: dict[str, list[PlanRow]]) -> PlanRow | None:
    """Look up AP by exact plan; fall back to base-plan only if all variants share AP+path."""
    if plan_norm in plan_map:
        return plan_map[plan_norm]
    candidates = [r for r in base_index.get(plan_norm, []) if r.ap_name]
    if not candidates:
        return None
    routes = {(r.ap_name, str(paths.ap_approved_invoices(r.ap_name)), r.ap_email) for r in candidates}
    if len(routes) == 1:
        return candidates[0]
    return None


def _build_ap_email(ap: PlanRow, today_str: str, folder: Path, summary: history.OldNew) -> tuple[str, str]:
    subject = f"Approved invoices pending processing: {summary.total}"
    new_block = "\n".join(summary.new) if summary.new else "(none)"
    old_block = "\n".join(summary.old) if summary.old else "(none)"
    body = (
        f"Hi {ap.ap_name},\n\n"
        f"As of {today_str}:\n"
        f"Total in Approved_Invoices: {summary.total}\n"
        f"Old (already present last run): {summary.old_count}\n"
        f"New (added since last run): {summary.new_count}\n\n"
        f"New invoices:\n{new_block}\n\n"
        f"Old invoices:\n{old_block}\n\n"
        f"Folder: {folder}\n\n"
        f"— Strataco Automation"
    )
    return subject, body


def _transfer_one(
    pdf_path: Path,
    plan_map: dict[str, PlanRow],
    base_index: dict[str, list[PlanRow]],
    ledger: dup_ledger.Ledger,
    run,
) -> None:
    """Transfer one manager-approved PDF to the AP's Approved_Invoices folder.

    True-move: ledger update is fail-closed and moved to after the destination
    write, so the source is only deleted once both the AP copy is on disk and
    the ledger reflects the new stage. No Processed- marker is written.
    """
    name = pdf_path.name

    _, plan_norm = plan_match.plan_from_filename(name)
    if not plan_norm:
        run.info(f"no plan in filename, skipping: {name}")
        return

    ap_row = _resolve_ap(plan_norm, plan_map, base_index)
    if not ap_row:
        run.info(f"no AP match for plan {plan_norm} ({name})")
        return

    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as exc:
        run.error(f"read {pdf_path}: {exc}")
        return

    # Duplicate-detection safety net: a manager may have manually
    # dragged a PDF into Approved/ that Step 1 never saw. Catch it
    # before the accountant wastes a Paid stamp on it.
    sha = dup_fingerprint.sha256_of(pdf_bytes)
    inv_num, amount = dup_fingerprint.compute_layer_b(pdf_bytes, plan_norm)
    # Same as Step 3: no email context here, so Layer B is a no-op.
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

    # Was this sha already in the ledger at a normal stage?
    # Drives the "advance stage vs insert orphan" decision at ledger update.
    prior_same_sha = ledger.find_by_hash(sha)
    if prior_same_sha is not None and prior_same_sha.current_stage in ("overridden", "superseded"):
        prior_same_sha = None

    if duplicate is not None:
        # Fail-closed: if the ledger increment fails, leave source in place
        # for retry. No DUPLICATE marker written.
        try:
            updated = ledger.increment_dup_count(duplicate.sha256)
            archive_hint = duplicate.archive_path or f"({duplicate.current_stage})"
            run.info(
                f"duplicate skipped in Approved/: {name} "
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
            run.error(f"could not unlink {pdf_path} after dup detection: {exc}")
        return

    # Flatten the manager-filled Received-stamp values (and any vendor
    # /AcroForm fields the manager touched) into static page text BEFORE
    # adding the new editable Paid stamp. Fail closed: leave source for retry.
    try:
        flat_bytes = flatten_acroform(pdf_bytes)
    except Exception as exc:
        run.error(f"flatten failed for {name}: {exc} — leaving source for retry")
        return

    # Pass sha so Paid stamp field names are deterministic, enabling
    # safe_write_unique to detect an identical prior write on retry.
    try:
        stamped = render_paid_stamp(flat_bytes, sha=sha)
    except Exception as exc:
        run.error(f"paid stamp failed for {name}: {exc} — saving unstamped")
        stamped = flat_bytes

    ap_dest = paths.ap_approved_invoices(ap_row.ap_name) / safe_io.sanitize_filename(name)
    try:
        ap_written = safe_io.safe_write_unique(ap_dest, stamped)
    except Exception as exc:
        run.error(f"write {ap_dest}: {exc}")
        return

    # Fail-closed ledger update: source only deleted after ledger confirms
    # the destination write. Moving ledger update to after the write means
    # the stage never advances without a real AP copy on disk.
    new_row = dup_ledger.make_row(
        sha256=sha,
        plan_norm=plan_norm,
        invoice_number=inv_num,
        amount_cents=amount,
        current_stage="ap_queue",
    )
    try:
        if prior_same_sha is not None:
            ledger.update_stage(sha, "ap_queue")
        elif overridden is not None and overridden.sha256 != sha:
            try:
                ledger.consume_override_and_insert(
                    old_sha256=overridden.sha256,
                    new_row=new_row,
                )
                run.info(
                    f"consumed Layer B override at AP transfer for {name} "
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
            f"ledger update at AP transfer failed for {sha[:12]}... ({name}): {exc} — "
            f"leaving source for retry"
        )
        return

    try:
        pdf_path.unlink(missing_ok=True)
    except Exception as exc:
        run.error(f"could not unlink original {pdf_path}: {exc}")

    run.processed += 1
    if ap_written != ap_dest:
        run.info(
            f"transferred {name} ({plan_norm}) -> {ap_written} "
            f"(collision-renamed from {ap_dest.name})"
        )
    else:
        run.info(f"transferred {name} ({plan_norm}) -> {ap_written}")


def _transfer_phase(rows: list[PlanRow], ledger: dup_ledger.Ledger, run) -> None:
    """Manager Approved -> AP Approved_Invoices, applying the Paid stamp."""
    plan_map = plan_to_ap(rows)
    base_index = base_plan_index(rows)

    for mgr in unique_managers(rows):
        approved = paths.manager_approved(mgr.manager_name)
        if not approved.exists():
            continue
        for pdf_path in sorted(approved.glob("*.pdf")):
            name = pdf_path.name
            if name.lower().startswith("processed -") or name.lower().startswith("processed-"):
                continue
            _transfer_one(pdf_path, plan_map, base_index, ledger, run)


def _notification_phase(rows: list[PlanRow], today_str: str, run) -> None:
    """Send each AP a 'pending processing' email + roll the baselines.

    Uses the split scanned/notified baselines added in 0.3.0 so a failed
    send doesn't silently age unsent invoices from "new" into "old".
    """
    for ap in unique_aps(rows):
        folder = paths.ap_approved_invoices(ap.ap_name)
        today_files = sorted(p.name for p in folder.glob("*.pdf")) if folder.exists() else []

        notified_xls = paths.ap_approved_notified_baseline_file(ap.ap_key)
        legacy_xls = paths.ap_approved_baseline_file(ap.ap_key)
        baseline_files = history.read_ap_notified_baseline(
            notified_xls, legacy_xls=legacy_xls,
        )
        summary = history.compute_old_new(today_files, baseline_files)

        run.info(
            f"ap={ap.ap_name} total={summary.total} new={summary.new_count} old={summary.old_count}"
        )

        subject, body = _build_ap_email(ap, today_str, folder, summary)
        recipient = graph.resolve_recipient(ap.ap_email)

        send_ok = False
        if not recipient:
            run.error(f"no recipient for AP {ap.ap_name} — skipping email")
        else:
            try:
                graph.send_mail(recipient, subject, body)
                run.info(f"emailed {recipient} ({summary.total} pending)")
                send_ok = True
            except Exception as exc:
                run.error(f"send_mail to {recipient} failed: {exc}")

        scanned_xls = paths.ap_approved_scanned_baseline_file(ap.ap_key)
        try:
            history.write_ap_scanned_baseline(scanned_xls, today_files, today_str)
        except Exception as exc:
            run.error(f"write scanned baseline {scanned_xls}: {exc}")

        if send_ok:
            try:
                history.write_ap_notified_baseline(notified_xls, today_files, today_str)
            except Exception as exc:
                run.error(f"write notified baseline {notified_xls}: {exc}")


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
        run.info(f"loaded {len(rows)} plan rows from snapshot")

        try:
            ledger = dup_ledger.load()
        except ValueError as exc:
            run.error(f"dup ledger corrupted — halting day: {exc}")
            return 1

        today_str, _ = _today_yesterday()

        _transfer_phase(rows, ledger, run)
        _notification_phase(rows, today_str, run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
