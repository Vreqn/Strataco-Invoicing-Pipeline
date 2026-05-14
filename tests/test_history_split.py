"""Unit tests for the 0.3.0 history baseline split.

Verifies that the new scanned-vs-notified helpers in tools/_lib/history.py
do what their docstrings claim:
- read_notified_for_manager falls back to the legacy file when the
  notified file doesn't exist yet (post-split first run).
- write_scanned vs write_notified are independent; only the notified
  one feeds back into the next day's "new vs old" diff.
- compute_old_new behaviour is unchanged.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import history


def test_read_notified_falls_back_to_legacy() -> None:
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td) / "2026-05-10__ALICE.xls"
        notified = Path(td) / "2026-05-10__ALICE__notified.xls"

        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        assert got == [], f"[no files] expected [], got {got}"

        history.write_today_for_manager(legacy, ["a.pdf", "b.pdf"], "2026-05-10")
        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        assert sorted(got) == ["a.pdf", "b.pdf"], f"[legacy fallback] expected a/b, got {got}"

        history.write_notified_for_manager(notified, ["c.pdf"], "2026-05-10")
        got = history.read_notified_for_manager(notified, legacy_xls=legacy)
        assert got == ["c.pdf"], f"[notified wins] expected ['c.pdf'], got {got}"


def test_send_failure_does_not_age_invoices() -> None:
    """Simulate a Step 4 day where send_mail raises.

    Pre-fix: write_today_for_manager runs unconditionally, so tomorrow's
    diff classifies the failed-to-send invoice as "old".

    Post-fix: write_scanned runs always (diagnostic) but
    write_notified is gated on send success. Tomorrow's diff reads the
    notified file, so the failed-to-send invoice stays "new" until it's
    successfully emailed.
    """
    with tempfile.TemporaryDirectory() as td:
        day1_scanned = Path(td) / "2026-05-10__ALICE__scanned.xls"
        day1_notified = Path(td) / "2026-05-10__ALICE__notified.xls"
        legacy = Path(td) / "2026-05-09__ALICE.xls"
        previously_notified = history.read_notified_for_manager(
            day1_notified, legacy_xls=legacy,
        )
        today_files = ["invoice_001.pdf"]
        summary1 = history.compute_old_new(today_files, previously_notified)
        history.write_scanned_for_manager(day1_scanned, today_files, "2026-05-10")
        # Simulate send failure: do NOT write notified.

        assert summary1.new == ["invoice_001.pdf"], (
            f"[day 1 new] expected ['invoice_001.pdf'], got {summary1.new}"
        )

        day2_scanned = Path(td) / "2026-05-11__ALICE__scanned.xls"
        day2_notified = Path(td) / "2026-05-11__ALICE__notified.xls"
        previously_notified = history.read_notified_for_manager(
            day2_notified, legacy_xls=day1_notified,
        )
        assert not previously_notified, (
            f"[day 2 baseline] expected empty (day-1 send failed), got {previously_notified}"
        )

        summary2 = history.compute_old_new(today_files, previously_notified)
        assert summary2.new == ["invoice_001.pdf"], (
            f"[day 2 still new] expected still new, got new={summary2.new} old={summary2.old}"
        )

        history.write_scanned_for_manager(day2_scanned, today_files, "2026-05-11")
        history.write_notified_for_manager(day2_notified, today_files, "2026-05-11")

        day3_notified = Path(td) / "2026-05-12__ALICE__notified.xls"
        previously_notified = history.read_notified_for_manager(
            day3_notified, legacy_xls=day2_notified,
        )
        summary3 = history.compute_old_new(today_files, previously_notified)
        assert not summary3.new and summary3.old == ["invoice_001.pdf"], (
            f"[day 3 ages out] expected old=['invoice_001.pdf'] new=[], "
            f"got old={summary3.old} new={summary3.new}"
        )


def test_ap_split_parallel() -> None:
    """Same semantics for the AP-baseline pair (Step 5)."""
    with tempfile.TemporaryDirectory() as td:
        scanned = Path(td) / "_latest__ALEX__scanned.xls"
        notified = Path(td) / "_latest__ALEX__notified.xls"
        legacy = Path(td) / "_latest__ALEX.xls"

        got = history.read_ap_notified_baseline(notified, legacy_xls=legacy)
        assert got == [], f"[ap empty] expected [], got {got}"

        history.write_ap_scanned_baseline(scanned, ["x.pdf"], "2026-05-10")
        assert not history.read_ap_notified_baseline(notified, legacy_xls=legacy), (
            "[ap notified missing] should still be empty"
        )

        history.write_ap_notified_baseline(notified, ["x.pdf"], "2026-05-10")
        got = history.read_ap_notified_baseline(notified, legacy_xls=legacy)
        assert got == ["x.pdf"], f"[ap notified after write] expected ['x.pdf'], got {got}"
