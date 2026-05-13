"""Step 4 — Send each manager a daily 'pending approval' summary email.

Replaces "Step 4 - Send detailed emails to Managers" (N8n).

For each unique manager:
1. List PDFs currently in their To_Approve folder.
2. Read yesterday's per-manager history XLS.
3. Compute total / old / new and send an email summary.
4. Write today's history XLS so tomorrow's run has a baseline.

During the shadow phase, all emails are rerouted to NOTIFY_OVERRIDE_EMAIL.

Schedule: 06:30 Mon–Fri.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import graph, history, paths, strataplan_snapshot
from tools._lib.log import daily_log
from tools._lib.xls import load_plans, unique_managers

_STAMP = "step_4"


def _today_yesterday() -> tuple[str, str]:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return today, yesterday


def _build_email(manager_name: str, today_str: str, folder: Path, summary: history.OldNew) -> tuple[str, str]:
    subject = f"Invoices pending approval: {summary.total}"
    new_block = "\n".join(summary.new) if summary.new else "(none)"
    old_block = "\n".join(summary.old) if summary.old else "(none)"
    body = (
        f"Hi {manager_name},\n\n"
        f"As of {today_str}:\n"
        f"Total in To_Approve: {summary.total}\n"
        f"Old (yesterday or earlier): {summary.old_count}\n"
        f"New (added today): {summary.new_count}\n\n"
        f"New invoices:\n{new_block}\n\n"
        f"Old invoices:\n{old_block}\n\n"
        f"Folder: {folder}\n\n"
        f"— Strataco Automation"
    )
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
        managers = unique_managers(rows)
        run.info(f"loaded {len(managers)} unique managers")

        today_str, yesterday_str = _today_yesterday()

        for mgr in managers:
            folder = paths.manager_to_approve(mgr.manager_name)
            today_files = sorted(p.name for p in folder.glob("*.pdf")) if folder.exists() else []

            # Diff against the NOTIFIED baseline so a previously-failed send
            # keeps its invoices in the "new" bucket until they're successfully
            # emailed. Falls back to the legacy combined file on first run after
            # the 0.3.0 split landed.
            yesterday_notified = paths.toapprove_notified_file(yesterday_str, mgr.manager_key)
            yesterday_legacy = paths.toapprove_history_file(yesterday_str, mgr.manager_key)
            yesterday_files = history.read_notified_for_manager(
                yesterday_notified, legacy_xls=yesterday_legacy,
            )

            summary = history.compute_old_new(today_files, yesterday_files)
            run.info(
                f"manager={mgr.manager_name} total={summary.total} "
                f"new={summary.new_count} old={summary.old_count}"
            )

            subject, body = _build_email(mgr.manager_name, today_str, folder, summary)
            recipient = graph.resolve_recipient(mgr.manager_email)

            send_ok = False
            if not recipient:
                run.error(f"no recipient for manager {mgr.manager_name} — skipping email")
            else:
                try:
                    graph.send_mail(recipient, subject, body)
                    run.info(f"emailed {recipient} ({summary.total} pending)")
                    send_ok = True
                except Exception as exc:
                    run.error(f"send_mail to {recipient} failed: {exc}")

            # Always write the diagnostic "what we saw" snapshot.
            scanned_xls = paths.toapprove_scanned_file(today_str, mgr.manager_key)
            try:
                history.write_scanned_for_manager(scanned_xls, today_files, today_str)
            except Exception as exc:
                run.error(f"write scanned snapshot {scanned_xls} failed: {exc}")

            # Only advance the notified baseline when the send actually succeeded.
            if send_ok:
                notified_xls = paths.toapprove_notified_file(today_str, mgr.manager_key)
                try:
                    history.write_notified_for_manager(notified_xls, today_files, today_str)
                except Exception as exc:
                    run.error(f"write notified baseline {notified_xls} failed: {exc}")

            run.processed += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
