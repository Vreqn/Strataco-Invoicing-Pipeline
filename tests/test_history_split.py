"""Unit tests for the 0.3.0 history baseline split.

Verifies that the new scanned-vs-notified helpers in tools/_lib/history.py
do what their docstrings claim:
- read_notified_for_manager falls back to the legacy file when the
  notified file doesn't exist yet (post-split first run).
- write_scanned vs write_notified are independent; only the notified
  one feeds back into the next day's "new vs old" diff.
- compute_old_new behaviour is unchanged.

Standalone: no pytest dependency. Run with `python tests/test_history_split.py`.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import history


def test_read_notified_falls_back_to_legacy() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td) / "2026-05-10__ALICE.xls"
        notified = Path(td) / "2026-05-10__ALICE__notified.xls"
        # No notified file, no legacy file → empty list.
        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        if got:
            failures.append(f"[no files] expected [], got {got}")

        # Write only the legacy file → must fall back.
        history.write_today_for_manager(legacy, ["a.pdf", "b.pdf"], "2026-05-10")
        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        if sorted(got) != ["a.pdf", "b.pdf"]:
            failures.append(f"[legacy fallback] expected a/b, got {got}")

        # Write the notified file → must win over legacy.
        history.write_notified_for_manager(notified, ["c.pdf"], "2026-05-10")
        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        if got != ["c.pdf"]:
            failures.append(f"[notified wins] expected ['c.pdf'], got {got}")
    return failures


def test_send_failure_does_not_age_invoices() -> list[str]:
    """Simulate a Step 4 day where send_mail raises.

    Pre-fix: write_today_for_manager runs unconditionally, so tomorrow's
    diff classifies the failed-to-send invoice as "old".

    Post-fix: write_scanned runs always (diagnostic) but
    write_notified is gated on send success. Tomorrow's diff reads the
    notified file, so the failed-to-send invoice stays "new" until it's
    successfully emailed.
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        # Day 1: invoice arrives, send fails.
        day1_scanned = Path(td) / "2026-05-10__ALICE__scanned.xls"
        day1_notified = Path(td) / "2026-05-10__ALICE__notified.xls"
        legacy = Path(td) / "2026-05-09__ALICE.xls"
        previously_notified = history.read_notified_for_manager(
            day1_notified, legacy_xls=legacy,
        )
        today_files = ["invoice_001.pdf"]
        summary1 = history.compute_old_new(today_files, previously_notified)
        # Always write scanned.
        history.write_scanned_for_manager(day1_scanned, today_files, "2026-05-10")
        # Simulate send failure: do NOT write notified.

        if summary1.new != ["invoice_001.pdf"]:
            failures.append(
                f"[day 1 new] expected ['invoice_001.pdf'], got {summary1.new}"
            )

        # Day 2: same invoice still present, send succeeds.
        day2_scanned = Path(td) / "2026-05-11__ALICE__scanned.xls"
        day2_notified = Path(td) / "2026-05-11__ALICE__notified.xls"
        previously_notified = history.read_notified_for_manager(
            day2_notified, legacy_xls=day1_notified,
        )
        # Day 1's notified file was never written, so day2 should still see no baseline.
        if previously_notified:
            failures.append(
                f"[day 2 baseline] expected empty (day-1 send failed), got {previously_notified}"
            )

        summary2 = history.compute_old_new(today_files, previously_notified)
        if summary2.new != ["invoice_001.pdf"]:
            failures.append(
                f"[day 2 still new] expected still new, got new={summary2.new} old={summary2.old}"
            )

        # Day 2 send succeeds → write notified.
        history.write_scanned_for_manager(day2_scanned, today_files, "2026-05-11")
        history.write_notified_for_manager(day2_notified, today_files, "2026-05-11")

        # Day 3: same file, no new arrivals. Now it should classify as "old".
        day3_notified = Path(td) / "2026-05-12__ALICE__notified.xls"
        previously_notified = history.read_notified_for_manager(
            day3_notified, legacy_xls=day2_notified,
        )
        summary3 = history.compute_old_new(today_files, previously_notified)
        if summary3.new or summary3.old != ["invoice_001.pdf"]:
            failures.append(
                f"[day 3 ages out] expected old=['invoice_001.pdf'] new=[], "
                f"got old={summary3.old} new={summary3.new}"
            )
    return failures


def test_ap_split_parallel() -> list[str]:
    """Same semantics for the AP-baseline pair (Step 5)."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        scanned = Path(td) / "_latest__ALEX__scanned.xls"
        notified = Path(td) / "_latest__ALEX__notified.xls"
        legacy = Path(td) / "_latest__ALEX.xls"

        got = history.read_ap_notified_baseline(notified, legacy_xls=legacy)
        if got:
            failures.append(f"[ap empty] expected [], got {got}")

        history.write_ap_scanned_baseline(scanned, ["x.pdf"], "2026-05-10")
        # Notified not yet written → reading notified still returns empty (no legacy either).
        if history.read_ap_notified_baseline(notified, legacy_xls=legacy):
            failures.append("[ap notified missing] should still be empty")

        history.write_ap_notified_baseline(notified, ["x.pdf"], "2026-05-10")
        got = history.read_ap_notified_baseline(notified, legacy_xls=legacy)
        if got != ["x.pdf"]:
            failures.append(f"[ap notified after write] expected ['x.pdf'], got {got}")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("read_notified falls back to legacy", test_read_notified_falls_back_to_legacy),
        ("send failure keeps invoice 'new'", test_send_failure_does_not_age_invoices),
        ("AP split parallel semantics", test_ap_split_parallel),
    ]:
        fails = fn()
        status = "OK  " if not fails else "FAIL"
        print(f"{status}[{label}] ({len(fails)} failure{'s' if len(fails) != 1 else ''})")
        all_failures.extend(fails)

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
