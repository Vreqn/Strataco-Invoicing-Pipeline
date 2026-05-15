"""Step 6 — Move paid invoices into the Strata Plan archive folder.

For each unique AP:
  - List PDFs in Paid_Invoices
  - Skip files starting with `Processed -` (legacy markers, left by earlier
    pipeline versions; harmless, but never pipeline content)
  - Extract Strata Plan from filename
  - Look up Strata Plan folder in the XLS
  - Read the Check Number and Date from the (AcroForm) Paid stamp
  - Flatten the AcroForm, atomic-write to
    Strata_Plans/<plan>/<check_number> - <MM> - <plan> <Month> <YYYY> inv.pdf
  - Update the dup-ledger to stage "archived"
  - Delete the source from Paid_Invoices (true move — no Processed- marker)

Dedup is provided by the dup-ledger (SHA-256 keyed). A failed ledger write
keeps the source in place for retry on the next run (fail-closed).

Send one daily "Invoices summary" email covering processed, unmatched, and
duplicate sections — always sent so a silent inbox means the cron did not
run. (Override recipient to NOTIFY_OVERRIDE_EMAIL during shadow phase.)

Schedule: 07:00 Mon–Fri.
"""

from __future__ import annotations

import calendar
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import (
    config,
    dup_fingerprint,
    dup_ledger,
    graph,
    inbox_report,
    paths,
    plan_match,
    safe_io,
    strataplan_snapshot,
)
from tools._lib.log import daily_log
from tools._lib.stamp import flatten_acroform
from tools._lib.stamp_read import (
    extract_paid_stamp_values,
    parse_paid_date,
    sanitize_check_number_for_filename,
)
from tools._lib.xls import (
    PlanRow,
    load_plans,
    plan_to_ap,
    unique_aps,
)

_STAMP = "step_6"


@dataclass
class _Outcomes:
    """Step 6's run results.

    `unmatched` holds AP `Paid_Invoices/` archive failures (this step's own
    work). `manager_stuck` and `unmatched_intake` come from filesystem scans
    of other pipeline stages where the automation should have drained the
    folder but didn't. The four are surfaced together in the morning email's
    `Action Required` section, plus the live Inbox query passed separately.
    """
    processed: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    manager_stuck: list[dict] = field(default_factory=list)
    unmatched_intake: list[dict] = field(default_factory=list)


@dataclass
class _ScanResult:
    """Result of one pipeline-residue filesystem scan.

    A single bad folder permission must not kill the whole morning email
    contract. Each scan reports what it could read in `rows` AND a per-item
    list of human-readable error strings in `errors`. The caller (Step 6's
    main) folds `errors` into the email's Action Required section so the
    operator can see the failure surface without having to grep step logs.
    """
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)



def _build_archive_name(
    check_number: str,
    month: int,
    year: int,
    plan_norm: str,
) -> str:
    """`{check} - {MM} - {PLAN} {MonthName} {YYYY} inv.pdf`.

    Used by both the archive-write path and the reconcile path that checks
    whether the archive already exists. The two MUST stay in lockstep —
    diverging formats cause every previously-processed file to throw a
    false "stale processed marker" error.
    """
    check_prefix = sanitize_check_number_for_filename(check_number)
    month_name = calendar.month_name[month]
    return safe_io.sanitize_filename(
        f"{check_prefix} - {month:02d} - {plan_norm} {month_name} {year} inv.pdf"
    )


def _is_processed(name: str) -> bool:
    n = name.lower().lstrip()
    return n.startswith("processed -") or n.startswith("processed-")


def _is_os_junk(name: str) -> bool:
    """OS-generated metadata files that are never pipeline content:
    .DS_Store and AppleDouble sidecars (macOS), Thumbs.db / desktop.ini
    (Windows). They reach shared folders via file-server browsing and
    must never be reported as 'stuck intake'.

    Scoped to these exact names — a genuinely stuck intake file that
    happens to be dot-hidden must still surface in the morning report,
    so this does NOT filter all dotfiles."""
    n = name.strip().lower()
    return (
        n == ".ds_store"
        or n.startswith("._")          # AppleDouble sidecars
        or n in {"thumbs.db", "desktop.ini"}
    )


def _archive_one(
    pdf_path: Path,
    plan_to_path: dict[str, PlanRow],
    out: _Outcomes,
    ledger: dup_ledger.Ledger,
    run,
    ap_name: str,
) -> None:
    name = pdf_path.name
    local_path = str(pdf_path)
    mtime_iso = _format_mtime(pdf_path)

    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as exc:
        run.error(f"read {pdf_path}: {exc}")
        return

    # `sha` is the chain SHA computed on the AP-folder bytes (pre-flatten).
    # `archive_sha256` is set later to the post-flatten bytes.
    sha = dup_fingerprint.sha256_of(pdf_bytes)

    # Ledger pre-check: if this PDF was already archived (e.g. a prior run
    # wrote the archive and deleted the source, then the source somehow
    # reappeared), clean it up without re-archiving — but only after verifying
    # the archive file's content matches the stored SHA256. Trusting path
    # existence alone could delete the source if the archive was replaced or
    # corrupted, leaving no good copy.
    existing = ledger.find_by_hash(sha)
    if existing is not None and existing.current_stage == "archived":
        if existing.archive_path and Path(existing.archive_path).exists():
            archive_path_obj = Path(existing.archive_path)
            sha_ok = False
            if existing.archive_sha256:
                try:
                    actual_sha = dup_fingerprint.sha256_of(archive_path_obj.read_bytes())
                    sha_ok = (actual_sha == existing.archive_sha256)
                except Exception as exc:
                    run.error(
                        f"could not read archive for SHA verification at "
                        f"{archive_path_obj}: {exc}"
                    )
            # sha_ok is False when archive_sha256 is unset (pre-0.12.1 ledger row)
            # or when reading failed. In both cases treat as unverifiable.
            if sha_ok:
                try:
                    pdf_path.unlink(missing_ok=True)
                except Exception as exc:
                    run.error(f"could not unlink leftover {pdf_path}: {exc}")
                run.info(
                    f"cleaned up leftover source {name} "
                    f"(archive verified at {existing.archive_path})"
                )
                return
            else:
                out.unmatched.append({
                    "fileName": name,
                    "reason": (
                        "Ledger says archived but archive SHA mismatch or unverifiable "
                        f"(archive at {existing.archive_path}) — investigate before deleting source"
                    ),
                    "planKey": "",
                    "apName": ap_name,
                    "localPath": local_path,
                    "mtimeIso": mtime_iso,
                })
                return
        out.unmatched.append({
            "fileName": name,
            "reason": (
                "Ledger says archived but archive file is missing — "
                "investigate before re-running"
            ),
            "planKey": "",
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    _, plan_norm = plan_match.plan_from_filename(name)
    if not plan_norm:
        out.unmatched.append({
            "fileName": name,
            "reason": "No Strata Plan found in filename",
            "planKey": "",
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    plan_row = plan_to_path.get(plan_norm)
    if not plan_row:
        out.unmatched.append({
            "fileName": name,
            "reason": "Plan in filename not found in Strataplan_List.xlsx",
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    paid_values = extract_paid_stamp_values(pdf_bytes)
    if paid_values.image_only:
        out.unmatched.append({
            "fileName": name,
            "reason": (
                "PDF appears image-only (likely Microsoft 'Print to PDF') — "
                "re-save by just hitting Ctrl+S, do not flatten manually"
            ),
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return
    if not paid_values.has_check_number:
        out.unmatched.append({
            "fileName": name,
            "reason": "Could not read Check Number from Paid stamp",
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    parsed_date = parse_paid_date(paid_values.paid_date)
    if parsed_date is None:
        out.unmatched.append({
            "fileName": name,
            "reason": "Could not read Date from Paid stamp",
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return
    month, year = parsed_date

    plan_folder = paths.strata_plan_folder(plan_row.plan_raw)
    if not plan_folder.exists():
        try:
            plan_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            out.unmatched.append({
                "fileName": name,
                "reason": f"Strata_Plans folder missing and could not be created: {exc}",
                "planKey": plan_norm,
                "apName": ap_name,
                "localPath": local_path,
                "mtimeIso": mtime_iso,
            })
            return

    archive_name = _build_archive_name(
        paid_values.check_number, month, year, plan_norm,
    )

    # Flatten the AcroForm fields into static text on the archived copy.
    # Done BEFORE the crash-gap scan so we can verify any existing archive
    # by content hash (not just by filename). Fail closed: leave source for retry.
    try:
        archive_bytes = flatten_acroform(pdf_bytes)
    except Exception as exc:
        run.error(
            f"flatten failed for {name}: {exc} — file stays in Paid_Invoices"
        )
        out.unmatched.append({
            "fileName": name,
            "reason": f"Archive flatten failed — leaving in Paid_Invoices: {exc}",
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    archive_sha = dup_fingerprint.sha256_of(archive_bytes)

    # Physical safety-net: a prior run may have written the archive but died
    # before the ledger write (crash gap). Verify by content SHA — not just
    # by filename — so a different invoice that happens to produce the same
    # archive filename (same check/plan/month) does NOT trigger crash-gap and
    # get silently discarded. Only enter recovery when the on-disk file is
    # byte-for-byte identical to what this source would produce.
    base_stem = Path(archive_name).stem
    base_suffix = Path(archive_name).suffix
    existing_archive: Path | None = None
    try:
        for candidate in plan_folder.iterdir():
            if not candidate.is_file():
                continue
            cname = candidate.name
            if cname == archive_name or (
                cname.startswith(f"{base_stem} (") and cname.endswith(base_suffix)
            ):
                try:
                    if dup_fingerprint.sha256_of(candidate.read_bytes()) == archive_sha:
                        existing_archive = candidate
                        break
                except Exception:
                    pass
    except Exception as exc:
        run.error(f"could not scan plan folder {plan_folder}: {exc}")

    if existing_archive is not None:
        try:
            if existing is not None:
                ledger.update_stage(
                    sha, "archived",
                    archive_path=str(existing_archive),
                    archive_sha256=archive_sha,
                )
            else:
                inv_num, amount = dup_fingerprint.compute_layer_b(pdf_bytes, plan_norm)
                ledger.upsert(dup_ledger.make_row(
                    sha256=sha,
                    plan_norm=plan_norm,
                    invoice_number=inv_num,
                    amount_cents=amount,
                    current_stage="archived",
                    archive_path=str(existing_archive),
                    archive_sha256=archive_sha,
                ))
        except Exception as exc:
            run.error(f"crash-gap ledger update failed for {name}: {exc}")
            out.unmatched.append({
                "fileName": name,
                "reason": (
                    f"Archive exists on disk but ledger update failed — "
                    f"investigate: {exc}"
                ),
                "planKey": plan_norm,
                "apName": ap_name,
                "localPath": local_path,
                "mtimeIso": mtime_iso,
            })
            return
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception as exc:
            run.error(f"could not unlink source after crash-gap recovery {pdf_path}: {exc}")
        run.info(f"crash-gap recovery: cleaned {name} -> {existing_archive}")
        return

    # archive_bytes and archive_sha are already computed above.
    # safe_write_unique handles both the normal case and the retry case:
    # identical bytes at archive_path → returns it unchanged (idempotent);
    # a different invoice with the same archive name → (N) collision copy.
    archive_path = plan_folder / archive_name
    try:
        archive_written = safe_io.safe_write_unique(archive_path, archive_bytes)
    except Exception as exc:
        run.error(f"write {archive_path}: {exc}")
        return

    # Fail-closed ledger update: source is only deleted after the ledger
    # confirms the destination write. A log-only error here would leave the
    # ledger out of sync while the source disappears silently.
    try:
        if existing is not None:
            ledger.update_stage(
                sha, "archived",
                archive_path=str(archive_written),
                archive_sha256=archive_sha,
            )
        else:
            inv_num, amount = dup_fingerprint.compute_layer_b(pdf_bytes, plan_norm)
            ledger.upsert(dup_ledger.make_row(
                sha256=sha,
                plan_norm=plan_norm,
                invoice_number=inv_num,
                amount_cents=amount,
                current_stage="archived",
                archive_path=str(archive_written),
                archive_sha256=archive_sha,
            ))
    except Exception as exc:
        run.error(f"ledger archive update failed for {sha[:12]}... ({name}): {exc}")
        out.unmatched.append({
            "fileName": name,
            "reason": f"Archived but ledger update failed — will retry on next run: {exc}",
            "planKey": plan_norm,
            "apName": ap_name,
            "localPath": local_path,
            "mtimeIso": mtime_iso,
        })
        return

    try:
        pdf_path.unlink(missing_ok=True)
    except Exception as exc:
        run.error(f"could not unlink AP source {pdf_path}: {exc}")

    out.processed.append({
        "fileName": name,
        "planRaw": plan_row.plan_raw,
        "apName": plan_row.ap_name,
        "checkNumber": paid_values.check_number,
        "destination": str(archive_written),
        "status": "Processed successfully",
    })
    run.info(f"archived {name} -> {archive_written}")



def _scan_unmatched_intake() -> _ScanResult:
    """Files stuck in `_Unmatched/Invoices/`.

    Steps 1/2/3 stage files here when they can't identify a Strata Plan.
    Anything still here at 07:00 is genuinely stuck — the automation isn't
    going to move it on its own; the operator must rename + relocate or
    add the missing plan to Strataplan_List.xlsx.

    A permission error on the staging folder is logged as a single scan
    error and the scan returns no rows. Step 6's email still sends with
    the error noted in Action Required so the operator can fix the perms.
    """
    result = _ScanResult()
    folder = paths.unmatched_invoices()
    if not folder.exists():
        return result
    try:
        entries = sorted(folder.iterdir())
    except Exception as exc:
        result.errors.append(f"_Unmatched/Invoices scan failed: {exc}")
        return result
    for p in entries:
        try:
            if not p.is_file():
                continue
        except Exception:
            # is_file() can raise on a symlink whose target is unreadable.
            # Skip the entry rather than tank the whole scan.
            continue
        if _is_processed(p.name):
            continue
        if _is_os_junk(p.name):
            continue
        result.rows.append({
            "fileName": p.name,
            "localPath": str(p),
            "mtimeIso": _format_mtime(p),
        })
    return result


def _scan_manager_stuck() -> _ScanResult:
    """PDFs sitting in any manager's `Approved/` folder.

    Step 5 at 06:40 drains this folder into the AP queue. Anything left at
    07:00 means Step 5 failed for that file (file locked, write error, or
    the file arrived after Step 5 ran).

    Glob-based — iterates `<root>/Users/*/Invoices/Approved/*.pdf` rather
    than reading the manager list from the Strataplan snapshot, so a
    stuck file surfaces even if the manager's XLS row is missing or
    inactive (XLS-disk drift). Per-manager errors land in `result.errors`
    so one bad folder doesn't hide sibling managers.

    AP-only Users/ entries (no `Invoices/Approved/` subdir) are silently
    skipped — they're a normal state, not an error.
    """
    result = _ScanResult()
    users_root = paths.root() / "Users"
    if not users_root.exists():
        return result
    try:
        user_dirs = sorted(users_root.iterdir())
    except Exception as exc:
        result.errors.append(f"Users/ scan failed: {exc}")
        return result
    for user_dir in user_dirs:
        try:
            if not user_dir.is_dir():
                continue
        except Exception:
            continue
        approved = user_dir / "Invoices" / "Approved"
        if not approved.exists():
            continue
        try:
            files = sorted(approved.glob("*.pdf"))
        except Exception as exc:
            result.errors.append(f"manager {user_dir.name}: {exc}")
            continue
        for p in files:
            if _is_processed(p.name):
                continue
            result.rows.append({
                "fileName": p.name,
                "managerName": user_dir.name,
                "localPath": str(p),
                "mtimeIso": _format_mtime(p),
            })
    return result


def _build_combined_summary_email(
    today_str: str,
    processed: list[dict],
    paid_failed: list[dict],
    manager_stuck: list[dict],
    unmatched_intake: list[dict],
    inbox_messages: list[dict],
    inbox_error: str | None,
    scan_errors: list[str],
    duplicates: list[dup_ledger.FingerprintRow],
) -> tuple[str, str]:
    """Build the daily "Invoices summary" email.

    Always sent — empty sections are rendered explicitly so a silent inbox
    means the cron did not fire.

    `duplicates` is pre-filtered by the caller to today's `last_dup_date`.
    Action Required rows include local filesystem paths (and inbox Msg ids
    for stuck emails) so the operator can open the file or email directly.
    The email goes to an internal recipient, not a vendor.

    `inbox_error` is non-None only when the Graph fetch failed; in that case
    `inbox_messages` is empty and the Inbox sub-section renders a degraded
    "query failed" notice instead of a count. `scan_errors` is the list of
    pipeline-scan failure strings collected by main(). Both degraded surfaces
    count toward `action_count` so the subject line never says "0 action
    required" while the body asks the operator to investigate. The rest of
    the email still sends so the morning report isn't lost to one transient
    Graph error or one bad folder permission.
    """
    action_count = (
        len(paid_failed)
        + len(manager_stuck)
        + len(unmatched_intake)
        + len(inbox_messages)
        + (1 if inbox_error else 0)
        + len(scan_errors)
    )

    subject = (
        f"Invoices summary — "
        f"{len(processed)} processed, "
        f"{action_count} action required, "
        f"{len(duplicates)} duplicate — {today_str}"
    )

    lines: list[str] = [f"Invoices summary — {today_str}", ""]

    lines.append(f"== Processed ({len(processed)}) ==")
    lines.append("")
    if processed:
        by_ap: dict[str, list[dict]] = {}
        for r in processed:
            by_ap.setdefault(r.get("apName") or "Unknown AP", []).append(r)
        for i, (ap, items) in enumerate(by_ap.items()):
            lines.append(f"AP: {ap}")
            lines.append("")
            for j, item in enumerate(items, 1):
                lines.append(f"{j}. {item['fileName']}")
                lines.append(f"   Status: {item['status']}")
                lines.append(f"   Strata Plan: {item['planRaw']}")
                lines.append(f"   Check Number: {item['checkNumber']}")
            if i < len(by_ap) - 1:
                lines.append("")
    else:
        lines.append("None today.")
    lines.append("")

    lines.append(f"== Action Required ({action_count}) ==")
    lines.append("")

    has_any_action = (
        paid_failed
        or manager_stuck
        or unmatched_intake
        or inbox_messages
        or inbox_error
        or scan_errors
    )

    if paid_failed:
        lines.append(
            f"-- Paid invoices stuck (Step 6 couldn't archive) ({len(paid_failed)}) --"
        )
        lines.append("")
        for i, r in enumerate(paid_failed, 1):
            lines.append(f"{i}. {r['fileName']}")
            if r.get("apName"):
                lines.append(f"   AP: {r['apName']}")
            lines.append(f"   Reason: {r['reason']}")
            if r.get("planKey"):
                lines.append(f"   Plan: {r['planKey']}")
            if r.get("localPath"):
                lines.append(f"   Path: {r['localPath']}")
            if r.get("mtimeIso"):
                lines.append(f"   Stuck since: {r['mtimeIso']}")
            lines.append("")

    if manager_stuck:
        lines.append(
            f"-- Manager approvals stuck (Step 5 didn't pick up) ({len(manager_stuck)}) --"
        )
        lines.append("")
        for i, r in enumerate(manager_stuck, 1):
            lines.append(f"{i}. {r['fileName']}")
            if r.get("managerName"):
                lines.append(f"   Manager: {r['managerName']}")
            if r.get("localPath"):
                lines.append(f"   Path: {r['localPath']}")
            if r.get("mtimeIso"):
                lines.append(f"   Stuck since: {r['mtimeIso']}")
            lines.append("")

    if unmatched_intake:
        lines.append(
            f"-- Unmatched intake files (Steps 1/2/3 couldn't route) ({len(unmatched_intake)}) --"
        )
        lines.append("")
        for i, r in enumerate(unmatched_intake, 1):
            lines.append(f"{i}. {r['fileName']}")
            if r.get("localPath"):
                lines.append(f"   Path: {r['localPath']}")
            if r.get("mtimeIso"):
                lines.append(f"   Stuck since: {r['mtimeIso']}")
            lines.append("")

    if inbox_error:
        lines.append("-- Inbox emails (unhandled) (query failed) --")
        lines.append("")
        lines.append(f"Inbox query failed: {inbox_error}")
        lines.append(
            "Operator: open Outlook directly to triage anything sitting in the Inbox root."
        )
        lines.append("")
    elif inbox_messages:
        lines.append(
            f"-- Inbox emails (unhandled) ({len(inbox_messages)}) --"
        )
        lines.append("")
        lines.extend(inbox_report.render_messages(inbox_messages))

    if scan_errors:
        lines.append(
            f"-- Pipeline scan errors (investigate folder permissions) ({len(scan_errors)}) --"
        )
        lines.append("")
        for i, err in enumerate(scan_errors, 1):
            lines.append(f"{i}. {err}")
        lines.append("")

    if not has_any_action:
        lines.append("None today.")
        lines.append("")

    lines.append(f"== Duplicates ({len(duplicates)}) ==")
    lines.append("")
    if duplicates:
        lines.append(
            f"{len(duplicates)} duplicate(s) were skipped today. The originals "
            "are already in the pipeline or archived — no action needed unless "
            "the vendor reports a re-billing issue."
        )
        lines.append("")
        for i, r in enumerate(duplicates, 1):
            original_loc = r.archive_path or f"(currently in pipeline: {r.current_stage})"
            lines.append(f"{i}. Plan {r.plan_norm or '(unknown)'}")
            lines.append(f"   Original: {original_loc}")
            if r.invoice_number:
                lines.append(f"   Invoice #: {r.invoice_number}")
            if r.amount_cents is not None:
                lines.append(f"   Amount: ${r.amount_cents / 100:,.2f}")
            lines.append(f"   First seen: {r.first_seen_date}")
            lines.append(f"   Total times seen: {r.dup_count + 1} (dup_count={r.dup_count})")
            lines.append(f"   sha256: {r.sha256[:16]}...")
            lines.append("")
    else:
        lines.append("None today.")

    body = "\n".join(lines).rstrip() + "\n"
    return subject, body


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
        plan_to_path = plan_to_ap(rows)  # uses ap_name to derive Strata_Plan path
        out = _Outcomes()

        try:
            ledger = dup_ledger.load()
        except ValueError as exc:
            run.error(f"dup ledger corrupted — halting day: {exc}")
            return 1

        for ap in unique_aps(rows):
            folder = paths.ap_paid_invoices(ap.ap_name)
            if not folder.exists():
                continue

            for pdf_path in sorted(folder.glob("*.pdf")):
                name = pdf_path.name
                if _is_processed(name):
                    continue
                if _is_os_junk(name):
                    continue
                _archive_one(pdf_path, plan_to_path, out, ledger, run, ap.ap_name)

        run.processed = len(out.processed)

        # Pipeline residue scans — these surface every folder where the
        # automation should have drained the queue but didn't. Run AFTER the
        # archive loop so Step 6's own successes are already accounted for.
        # Each scan helper is already defensive against per-folder errors;
        # the belt-and-braces try/except here protects against a future
        # contributor adding a non-defensive code path.
        scan_errors: list[str] = []

        try:
            intake_result = _scan_unmatched_intake()
        except Exception as exc:
            run.error(f"_scan_unmatched_intake raised: {exc}")
            scan_errors.append(f"_Unmatched/Invoices scan raised: {exc}")
        else:
            out.unmatched_intake = intake_result.rows
            scan_errors.extend(intake_result.errors)
            for err in intake_result.errors:
                run.error(f"unmatched_intake scan: {err}")

        try:
            manager_result = _scan_manager_stuck()
        except Exception as exc:
            run.error(f"_scan_manager_stuck raised: {exc}")
            scan_errors.append(f"Manager Approved/ scan raised: {exc}")
        else:
            out.manager_stuck = manager_result.rows
            scan_errors.extend(manager_result.errors)
            for err in manager_result.errors:
                run.error(f"manager_stuck scan: {err}")

        # Live Inbox query. A Graph failure here should NOT take down the
        # whole morning email — archive work has succeeded and the operator
        # still needs the rest of the report. Capture the error and let the
        # email builder render a degraded "query failed" notice.
        inbox_messages: list[dict] = []
        inbox_error: str | None = None
        try:
            inbox_messages = graph.list_inbox_messages()
        except Exception as exc:
            inbox_error = str(exc)
            run.error(f"list_inbox_messages failed: {exc}")

        # Recipient comes from config (NOTIFY_OVERRIDE_EMAIL during shadow phase,
        # NOTIFY_DEFAULT_EMAIL otherwise). The old hardcoded address was a
        # P0 leak — see 2026-05-10 Codex review.
        recipient = config.notify_email()
        today_str = _dt_today_str()
        dup_today = [
            r for r in ledger.all_rows()
            if r.last_dup_date == today_str
        ]
        subject, body = _build_combined_summary_email(
            today_str,
            out.processed,
            out.unmatched,
            out.manager_stuck,
            out.unmatched_intake,
            inbox_messages,
            inbox_error,
            scan_errors,
            dup_today,
        )
        try:
            graph.send_mail(recipient, subject, body)
            run.info(
                f"emailed invoices summary to {recipient} "
                f"(processed={len(out.processed)}, "
                f"paid_failed={len(out.unmatched)}, "
                f"manager_stuck={len(out.manager_stuck)}, "
                f"unmatched_intake={len(out.unmatched_intake)}, "
                f"inbox_stuck={len(inbox_messages)}, "
                f"inbox_error={'yes' if inbox_error else 'no'}, "
                f"scan_errors={len(scan_errors)}, "
                f"duplicates={len(dup_today)})"
            )
        except Exception as exc:
            run.error(f"send invoices summary to {recipient}: {exc}")
        for r in out.unmatched:
            run.error(f"paid_failed: {r['fileName']} — {r['reason']}")
        for r in out.manager_stuck:
            run.error(
                f"manager_stuck: {r['fileName']} — manager={r.get('managerName', '?')}"
            )
        for r in out.unmatched_intake:
            run.error(f"unmatched_intake: {r['fileName']}")

    return 0


def _dt_today_str() -> str:
    """Today's date as YYYY-MM-DD, in America/Vancouver."""
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    return now.date().isoformat()


def _format_mtime(p: Path) -> str:
    """File mtime as 'YYYY-MM-DD HH:MM' in America/Vancouver. '' on stat failure."""
    import datetime as _dt
    try:
        ts = p.stat().st_mtime
    except Exception:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = _dt.datetime.fromtimestamp(ts, ZoneInfo("America/Vancouver"))
    except Exception:
        dt = _dt.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
