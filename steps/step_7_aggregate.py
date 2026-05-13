"""Step 7 — Monthly Invoice Aggregator.

For each active strata plan, merges every Step-6-archived invoice from a
target month into a single combined PDF and moves the source PDFs into
`Strata_Plans/<plan>/Processed/{YYYY-MM}/`. Tracks every (plan, month)
attempt in `_state/monthly_aggregations.csv` for audit and idempotency.

Schedule
--------
Operator-chosen day in Task Scheduler — typically day 5–10 of the new
month. Default behaviour aggregates the PREVIOUS calendar month
(America/Vancouver). Override with `--month YYYY-MM` for reruns.

The monthly trigger may fire on a day Step 1's daily Mon–Fri schedule
didn't run (weekend, holiday). Step 7 therefore refreshes the
Strataplan snapshot itself before requiring it — if the master XLS is
readable, the refresh succeeds even on days Step 1 was off.

CLI
---
    python steps/step_7_aggregate.py
    python steps/step_7_aggregate.py --month 2026-05
    python steps/step_7_aggregate.py --plan BCS1234
    python steps/step_7_aggregate.py --dry-run
    python steps/step_7_aggregate.py --force

See `workflows/step_7_aggregate.md` for the full SOP.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as _dt
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import (
    aggregation_ledger,
    config,
    graph,
    paths,
    pdf_merge,
    plan_match,
    safe_io,
    strataplan_snapshot,
)
from tools._lib.log import daily_log
from tools._lib.xls import PlanRow, load_plans

_STAMP = "step_7"


@dataclass
class _Outcomes:
    processed: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    dry_run: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def _now_vancouver() -> _dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        return _dt.datetime.now()


def _previous_month_vancouver(today: _dt.date | None = None) -> tuple[int, int]:
    """(year, month) of the calendar month BEFORE `today` in America/Vancouver."""
    if today is None:
        today = _now_vancouver().date()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - _dt.timedelta(days=1)
    return last_of_prev.year, last_of_prev.month


def _parse_month_arg(value: str) -> tuple[int, int]:
    """`'2026-05'` -> `(2026, 5)`. Raises `argparse.ArgumentTypeError` on bad input."""
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--month must be YYYY-MM (got {value!r}): {exc}"
        ) from exc
    return parsed.year, parsed.month


def _validate_month(year: int, month: int, today: _dt.date | None = None) -> None:
    """Reject future months and absurdly historical (>24 mo) values."""
    if today is None:
        today = _now_vancouver().date()
    target = _dt.date(year, month, 1)
    first_of_this_month = today.replace(day=1)
    if target >= first_of_this_month:
        raise SystemExit(
            f"error: --month {year:04d}-{month:02d} is in the current or future month; "
            f"Step 7 only aggregates COMPLETED months"
        )
    months_back = (today.year - year) * 12 + (today.month - month)
    if months_back > 24:
        raise SystemExit(
            f"error: --month {year:04d}-{month:02d} is more than 24 months ago — "
            f"if this is intentional, edit the script's _MAX_BACK_MONTHS guard"
        )


_CHECK_SPLIT_RE = re.compile(r"^([^\d]*)(\d+)(.*)$")


def _check_sort_key(check: str) -> tuple:
    """Sort key that buckets alpha-prefixed checks separately from pure-numeric.

    `'12345'` → `('', 12345, '')`              — pure numeric
    `'00123'` → `('', 123, '')`                — leading zeros normalised
    `'AB-123'` → `('AB-', 123, '')`            — alpha-prefixed sequence
    `'DEP9'` → `('DEP', 9, '')`
    `'WIRE'` → `('~WIRE', 0, 'WIRE')`          — no digits — sort last

    Pure-numeric checks (empty prefix) sort first by value; alpha-prefixed
    sequences sort by prefix, then by their internal number. Mixing wire
    transfers labelled "AB-123" with regular checks "9876" used to put them
    at integer position 123 alongside pure-numeric — the new key keeps them
    in their own bucket.
    """
    s = str(check or "").strip()
    if not s:
        return ("~", 0, "")
    m = _CHECK_SPLIT_RE.match(s)
    if m:
        prefix, digits, suffix = m.groups()
        return (prefix.upper(), int(digits), suffix)
    # No digits at all — sort after everything else. The leading `~` is
    # greater than letters in ASCII, so these go to the bottom.
    return ("~" + s.upper(), 0, s)


def _build_summary_name(month: int, year: int, plan_norm: str) -> str:
    """`Summary - {MM} - {plan_norm} {MonthName} {YYYY} inv.pdf` — mirrors Step 6."""
    month_name = calendar.month_name[month]
    return safe_io.sanitize_filename(
        f"Summary - {month:02d} - {plan_norm} {month_name} {year} inv.pdf"
    )


def _summary_present(folder: Path, summary_path: Path) -> bool:
    """True if the expected Summary file (or any safe_write_unique variant) exists."""
    if summary_path.exists():
        return True
    stem = summary_path.stem
    suffix = summary_path.suffix
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith(f"{stem} (") and p.suffix == suffix:
            return True
    return False


def _safe_move(src: Path, dest: Path) -> Path:
    """Cross-collision-safe rename. Returns the actual destination path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        os.replace(src, dest)
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        cand = dest.parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            os.replace(src, cand)
            return cand
        n += 1


def _unique_active_plans(rows: list[PlanRow]) -> list[PlanRow]:
    """One PlanRow per `plan_norm`, active rows only."""
    seen: set[str] = set()
    out: list[PlanRow] = []
    for r in rows:
        if not r.status_active or not r.plan_norm or r.plan_norm in seen:
            continue
        seen.add(r.plan_norm)
        out.append(r)
    return out


def _scan_candidates(folder: Path, year: int, month: int, plan_row: PlanRow):
    """Return (candidates, unmatched) for PDFs at the ROOT of `folder`.

    `candidates` is a list of (Path, check_string, name). `unmatched` is a list
    of dicts suitable for the unmatched-email body.
    """
    candidates: list[tuple[Path, str, str]] = []
    unmatched: list[dict] = []
    for pdf in sorted(folder.glob("*.pdf")):
        name = pdf.name
        # Never re-aggregate a prior Step 7 Summary output.
        if name.lower().startswith("summary -"):
            continue
        parsed = plan_match.parse_archive_filename(name)
        if parsed is None:
            unmatched.append({
                "fileName": name,
                "planFolder": plan_row.plan_norm,
                "reason": "Filename does not match Step 6 archive convention",
            })
            continue
        if (parsed["year"], parsed["month"]) != (year, month):
            continue   # wrong month — silent skip
        if parsed["plan_norm"].upper() != plan_row.plan_norm.upper():
            unmatched.append({
                "fileName": name,
                "planFolder": plan_row.plan_norm,
                "reason": (
                    f"Filename plan {parsed['plan_norm']!r} disagrees with "
                    f"folder plan {plan_row.plan_norm!r}"
                ),
            })
            continue
        candidates.append((pdf, parsed["check"], name))
    return candidates, unmatched


def _scan_processed_candidates(processed_dir: Path, year: int, month: int, plan_row: PlanRow):
    """Scan `Processed/{YYYY-MM}/` for files matching (year, month, plan).

    Used only under `--force` when the plan folder root is empty (the operator
    deleted the Summary and wants to regenerate it from the already-archived
    sources). Returns the same shape as `_scan_candidates` but without
    populating the `unmatched` list — unmatched files in `Processed/` are
    operator-side state and shouldn't generate noise on a force-redo.
    """
    candidates: list[tuple[Path, str, str]] = []
    if not processed_dir.exists():
        return candidates
    for pdf in sorted(processed_dir.glob("*.pdf")):
        parsed = plan_match.parse_archive_filename(pdf.name)
        if parsed is None:
            continue
        if (parsed["year"], parsed["month"]) != (year, month):
            continue
        if parsed["plan_norm"].upper() != plan_row.plan_norm.upper():
            continue
        candidates.append((pdf, parsed["check"], pdf.name))
    return candidates


# ---------------------------------------------------------------- per-plan flow

def _aggregate_one_plan(
    plan_row: PlanRow,
    year: int,
    month: int,
    ledger: aggregation_ledger.Ledger,
    args: argparse.Namespace,
    out: _Outcomes,
    run,
) -> None:
    plan = plan_row.plan_norm
    folder = paths.strata_plan_folder(plan_row.plan_raw)

    def _record(status: str, *, summary_filename: str = "", sources_merged: int = 0, notes: str = "") -> None:
        ledger.append(aggregation_ledger.make_row(
            plan, year, month, status,
            summary_filename=summary_filename,
            sources_merged=sources_merged,
            notes=notes,
        ))

    if not folder.exists():
        _record("skipped_no_folder")
        return

    summary_path = folder / _build_summary_name(month, year, plan)
    processed_dir = paths.strata_plan_processed_month(plan_row.plan_raw, year, month)

    candidates, unmatched_local = _scan_candidates(folder, year, month, plan_row)
    out.unmatched.extend(unmatched_local)
    ledger_done = ledger.is_done(plan, year, month)

    # Under --force, if root has no candidates, fall back to Processed/ so
    # "delete the old Summary, run --force" actually regenerates the Summary.
    candidates_from_processed = False
    if not candidates and args.force:
        processed_candidates = _scan_processed_candidates(processed_dir, year, month, plan_row)
        if processed_candidates:
            candidates = processed_candidates
            candidates_from_processed = True

    # Rules 1 & 2 — ledger says done, nothing new, NOT --force.
    if ledger_done and not candidates and not args.force:
        # Verify BOTH the Summary file AND Processed/ are intact. A
        # deleted/corrupt Summary shouldn't be silently treated as "done."
        summary_ok = _summary_present(folder, summary_path)
        processed_ok = processed_dir.exists() and any(p.is_file() for p in processed_dir.iterdir())
        if summary_ok and processed_ok:
            _record("skipped_already_done")
        else:
            run.error(
                f"{plan} {year}-{month:02d}: ledger says done but filesystem disagrees "
                f"(summary_present={summary_ok}, processed_populated={processed_ok}) — "
                "skipping; use --force after triage"
            )
            _record(
                "error",
                notes=f"ledger-filesystem disagreement (summary={summary_ok}, processed={processed_ok})",
            )
        return

    # Rule 3 — no candidates, not done — nothing to do
    if not candidates:
        _record("skipped_no_files")
        return

    # Rule 4 — candidates exist AND ledger says done -> late-check. --force
    # does NOT suppress this classification; it just bypassed rules 1/2.
    # Keeping `aggregated_late` for subsequent aggregations keeps the audit
    # trail unambiguous: the FIRST aggregation for a (plan, month) is
    # `aggregated`; everything after is `aggregated_late`.
    is_late = ledger_done

    candidates.sort(key=lambda t: _check_sort_key(t[1]))

    try:
        pdf_bytes_list = [pdf.read_bytes() for pdf, _, _ in candidates]
    except Exception as exc:
        run.error(f"{plan} {year}-{month:02d}: read failed: {exc} — leaving plan untouched")
        return

    try:
        merged_bytes = pdf_merge.merge_pdfs_from_bytes(pdf_bytes_list)
    except Exception as exc:
        run.error(f"{plan} {year}-{month:02d}: pdf merge failed: {exc} — leaving plan untouched")
        return

    if args.dry_run:
        run.info(
            f"[dry-run] {plan} {year}-{month:02d}: would write {summary_path.name} "
            f"({len(candidates)} files)"
            + (" [from Processed/]" if candidates_from_processed else "")
        )
        notes = ("would have aggregated_late" if is_late else "would have aggregated")
        if candidates_from_processed:
            notes += " (from Processed/)"
        _record(
            "dry_run",
            summary_filename=summary_path.name,
            sources_merged=len(candidates),
            notes=notes,
        )
        out.dry_run.append({
            "planRaw": plan_row.plan_raw,
            "planKey": plan,
            "wouldMerge": len(candidates),
            "wouldWrite": summary_path.name,
            "late": is_late,
            "fromProcessed": candidates_from_processed,
        })
        return

    try:
        summary_written = safe_io.safe_write_unique(summary_path, merged_bytes)
    except Exception as exc:
        run.error(f"{plan} {year}-{month:02d}: write {summary_path} failed: {exc}")
        return

    # Move sources to Processed/{YYYY-MM}/. If candidates already came from
    # Processed/ (the --force-redo case), skip the move loop entirely.
    #
    # On move failure: roll back successful moves AND delete the Summary just
    # written. Without rollback, a later non-force run would see the unmoved
    # source as a "new candidate", trigger the late-check branch, and write
    # `Summary ... (1).pdf` containing pages that already exist in the original
    # Summary — duplicate pages across the two files. Rolling back puts the
    # plan back into a clean "not done" state so the operator can investigate
    # (lockfile from Acrobat, permissions, etc.) and rerun.
    if not candidates_from_processed:
        moved: list[tuple[Path, Path]] = []
        move_failed_file: str | None = None
        move_failure_exc: str | None = None
        for pdf, _, name in candidates:
            dest = processed_dir / safe_io.sanitize_filename(name)
            try:
                final_dest = _safe_move(pdf, dest)
                moved.append((pdf, final_dest))
            except Exception as exc:
                move_failed_file = name
                move_failure_exc = str(exc)
                run.error(f"{plan} {year}-{month:02d}: move {pdf} -> {dest} failed: {exc}")
                break

        if move_failed_file is not None:
            rollback_failures: list[str] = []
            for original_src, moved_dest in moved:
                try:
                    os.replace(moved_dest, original_src)
                except Exception as exc:
                    rollback_failures.append(f"{moved_dest.name} ({exc})")
                    run.error(
                        f"{plan} {year}-{month:02d}: rollback "
                        f"{moved_dest} -> {original_src} failed: {exc}"
                    )
            try:
                summary_written.unlink(missing_ok=True)
            except Exception as exc:
                rollback_failures.append(f"summary deletion ({exc})")
                run.error(
                    f"{plan} {year}-{month:02d}: could not delete partial summary "
                    f"{summary_written}: {exc}"
                )
            if rollback_failures:
                notes = (
                    f"move failed on {move_failed_file}: {move_failure_exc}; "
                    f"rollback issues: {'; '.join(rollback_failures)} — "
                    "filesystem in mixed state, requires manual triage"
                )
            else:
                notes = (
                    f"move failed on {move_failed_file}: {move_failure_exc}; "
                    "rolled back successfully"
                )
            _record("error", notes=notes)
            return

    status = "aggregated_late" if is_late else "aggregated"
    if candidates_from_processed and is_late:
        notes = "re-aggregated from Processed/ (force, plan was previously done)"
    elif candidates_from_processed:
        notes = "re-aggregated from Processed/ (force)"
    else:
        notes = ""
    _record(
        status,
        summary_filename=summary_written.name,
        sources_merged=len(candidates),
        notes=notes,
    )
    out.processed.append({
        "planRaw": plan_row.plan_raw,
        "planKey": plan,
        "merged": len(candidates),
        "summary": str(summary_written),
        "processedDir": str(processed_dir),
        "late": is_late,
        "fromProcessed": candidates_from_processed,
    })
    run.info(
        f"aggregated {plan}: {len(candidates)} invoices -> {summary_written.name}"
        + (" [LATE]" if is_late else "")
        + (" [from Processed/]" if candidates_from_processed else "")
    )


# ---------------------------------------------------------------- email bodies

def _build_processed_email(rows: list[dict], year: int, month: int, preflight: str) -> tuple[str, str]:
    month_name = calendar.month_name[month]
    subject = f"Monthly aggregation: {len(rows)} plans ({month_name} {year})"
    if not rows:
        body = (
            f"Monthly aggregation for {month_name} {year}\n\n"
            f"{preflight}\n\n"
            "No new Summary files were written this run."
        )
        return subject, body

    lines = [f"Monthly aggregation for {month_name} {year}", "", preflight, ""]
    for i, item in enumerate(rows, 1):
        markers = []
        if item.get("late"):
            markers.append("LATE")
        if item.get("fromProcessed"):
            markers.append("from Processed/")
        marker = f" ({', '.join(markers)})" if markers else ""
        lines.append(f"{i}. {item['planKey']}{marker}")
        lines.append(f"   Merged: {item['merged']} invoices")
        lines.append(f"   Summary file: {Path(item['summary']).name}")
    lines.append("")
    lines.append("Source PDFs moved into Strata_Plans/<plan>/Processed/"
                 f"{year:04d}-{month:02d}/.")
    return subject, "\n".join(lines)


def _build_dry_run_email(rows: list[dict], year: int, month: int, preflight: str) -> tuple[str, str]:
    month_name = calendar.month_name[month]
    subject = f"DRY RUN — Monthly aggregation: would aggregate {len(rows)} plans ({month_name} {year})"
    if not rows:
        body = (
            f"DRY RUN — Monthly aggregation for {month_name} {year}\n\n"
            f"{preflight}\n\n"
            "No plans would have been aggregated this run. No files were written."
        )
        return subject, body

    lines = [
        f"DRY RUN — Monthly aggregation for {month_name} {year}",
        "",
        preflight,
        "",
        "The following plans WOULD have been aggregated if this had been a real run:",
        "",
    ]
    for i, item in enumerate(rows, 1):
        markers = []
        if item.get("late"):
            markers.append("would be LATE")
        if item.get("fromProcessed"):
            markers.append("from Processed/")
        marker = f" ({', '.join(markers)})" if markers else ""
        lines.append(f"{i}. {item['planKey']}{marker}")
        lines.append(f"   Would merge: {item['wouldMerge']} invoices")
        lines.append(f"   Would write: {item['wouldWrite']}")
    lines.append("")
    lines.append("No files were written. Re-run without --dry-run to perform the aggregation.")
    return subject, "\n".join(lines)


def _build_unmatched_email(rows: list[dict], year: int, month: int) -> tuple[str, str]:
    month_name = calendar.month_name[month]
    subject = f"Monthly aggregation unmatched: {len(rows)} files ({month_name} {year})"
    if not rows:
        return subject, "No unmatched files."
    lines = [
        f"Files inside Strata_Plans archive folders that could not be aggregated "
        f"for {month_name} {year}:",
        "",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['fileName']}")
        lines.append(f"   Plan folder: {r['planFolder']}")
        lines.append(f"   Reason: {r['reason']}")
        lines.append("")
    return subject, "\n".join(lines).rstrip()


# ---------------------------------------------------------------- main

def _parse_cli(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Step 7 — Monthly invoice aggregator. Merges each strata plan's "
            "archived paid invoices for a target month into one combined PDF, "
            "moves the sources into Processed/{YYYY-MM}/, and writes an audit "
            "row to _state/monthly_aggregations.csv."
        ),
    )
    ap.add_argument(
        "--month", type=_parse_month_arg, default=None, metavar="YYYY-MM",
        help="Target month (default: previous calendar month, America/Vancouver). "
             "Future months and >24-mo-historical values are rejected.",
    )
    ap.add_argument(
        "--plan", default=None, metavar="PLAN_NORM",
        help="Only aggregate this single plan (matched case-insensitively against "
             "plan_norm, e.g. BCS2707). Useful for re-running one plan after a fix.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Compute what would be merged/moved, log it, append a `dry_run` ledger "
             "row, but do not write any PDF or move any source file.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Bypass the ledger idempotency short-circuit. If the plan-folder root "
             "has no candidates AND Processed/{YYYY-MM}/ has matching files, "
             "regenerate the Summary from those Processed/ files (used after the "
             "operator deletes a Summary to redo from scratch). Existing Summary "
             "files are NEVER overwritten — safe_write_unique still produces a "
             "`(1)` variant if one already exists.",
    )
    return ap.parse_args(argv)


def _preflight_line(ledger: aggregation_ledger.Ledger, year: int, month: int,
                    total_active: int, run_dt: _dt.datetime) -> str:
    completed_plans = ledger.completed_plans_for(year, month)
    month_name = calendar.month_name[month]
    header = (
        f"Target month: {month_name} {year} (run on "
        f"{run_dt.strftime('%Y-%m-%d %H:%M America/Vancouver')})"
    )
    n = len(completed_plans)
    if n == 0:
        body = (
            f"Ledger: 0 of {total_active} active plans aggregated for "
            f"{month_name} {year} — will process all"
        )
    else:
        latest = ledger.latest_completed_timestamp(year, month) or "?"
        if n >= total_active and total_active > 0:
            body = (
                f"Ledger: {n} of {total_active} active plans aggregated for "
                f"{month_name} {year} (latest {latest}) — nothing to do "
                f"(pass --force to redo)"
            )
        else:
            body = (
                f"Ledger: {n} of {total_active} active plans aggregated for "
                f"{month_name} {year} (latest {latest}) — rerun will "
                "idempotently skip them"
            )
    return f"{header}\n{body}"


def _ensure_snapshot(run) -> Path | None:
    """Refresh the snapshot ourselves, then assert it's today's.

    Step 7 runs monthly on operator-chosen days; the daily Step 1 trigger may
    not have fired today (weekend, holiday). Refreshing here lets Step 7 work
    on any day the master XLS is readable. Returns the snapshot path or None
    on failure (caller exits with code 1).
    """
    try:
        strataplan_snapshot.refresh_snapshot()
    except Exception as exc:
        # Refresh failure isn't fatal IF today's marker already exists from
        # an earlier Step 1 or Step 7 run today. Try require_fresh_snapshot
        # below; only fail if that also fails.
        run.warn(f"snapshot refresh failed: {exc} — falling back to existing snapshot if today's")

    try:
        return strataplan_snapshot.require_fresh_snapshot()
    except strataplan_snapshot.SnapshotStaleError as exc:
        run.error(f"snapshot is not today's and refresh did not succeed — refusing to run: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv if argv is not None else sys.argv[1:])

    # Resolve target month early so --month validation errors don't bother
    # acquiring the per-step lock.
    today = _now_vancouver().date()
    if args.month is None:
        year, month = _previous_month_vancouver(today)
    else:
        year, month = args.month
    _validate_month(year, month, today)

    with daily_log(_STAMP) as run:
        if run.status == "skipped":
            return 0

        snapshot = _ensure_snapshot(run)
        if snapshot is None:
            return 1

        try:
            ledger = aggregation_ledger.load()
        except ValueError as exc:
            run.error(f"ledger file is corrupted — refusing to run: {exc}")
            return 1

        rows = load_plans(snapshot)
        plans = _unique_active_plans(rows)
        if args.plan:
            wanted = args.plan.upper()
            plans = [p for p in plans if p.plan_norm.upper() == wanted]
            if not plans:
                run.warn(f"--plan {args.plan!r} did not match any active plan_norm")

        run_dt = _now_vancouver()
        preflight = _preflight_line(ledger, year, month, len(_unique_active_plans(rows)), run_dt)
        for line in preflight.splitlines():
            run.info(f"[preflight] {line}")

        out = _Outcomes()
        for plan_row in plans:
            try:
                _aggregate_one_plan(plan_row, year, month, ledger, args, out, run)
            except Exception as exc:
                run.error(f"{plan_row.plan_norm} {year}-{month:02d}: unhandled: {exc}")

        run.processed = len(out.processed)

        # Recipient comes from config (NOTIFY_OVERRIDE_EMAIL during shadow phase,
        # NOTIFY_DEFAULT_EMAIL otherwise). Send the processed-summary email even
        # when zero plans had activity so the operator sees the cron fired.
        # Dry-run gets a distinct subject line so operators don't confuse it
        # with a real run that produced nothing.
        recipient = config.notify_email()
        if args.dry_run:
            subject, body = _build_dry_run_email(out.dry_run, year, month, preflight)
        else:
            subject, body = _build_processed_email(out.processed, year, month, preflight)
        try:
            graph.send_mail(recipient, subject, body)
            count = len(out.dry_run) if args.dry_run else len(out.processed)
            run.info(f"emailed processed summary to {recipient} ({count})")
        except Exception as exc:
            run.error(f"send processed summary to {recipient}: {exc}")

        if out.unmatched:
            subject_u, body_u = _build_unmatched_email(out.unmatched, year, month)
            try:
                graph.send_mail(recipient, subject_u, body_u)
                run.info(f"emailed unmatched summary to {recipient} ({len(out.unmatched)})")
            except Exception as exc:
                run.error(f"send unmatched summary to {recipient}: {exc}")
            for r in out.unmatched:
                run.error(f"unmatched: {r['fileName']} ({r['planFolder']}) — {r['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
