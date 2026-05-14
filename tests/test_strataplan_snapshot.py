"""Unit tests for tools/_lib/strataplan_snapshot.py.

Covers:
- `refresh_snapshot` writes both the snapshot xlsx and the marker (today's date)
- `require_fresh_snapshot` returns the snapshot path when marker is today
- `require_fresh_snapshot` raises `SnapshotStaleError` on yesterday/missing marker
- `refresh_snapshot` raises `SnapshotRefreshError` when master is missing
- `refresh_snapshot` raises `SnapshotRefreshError` when master bytes are not XLSX

The Windows-only `CreateFileW`-with-share-flags behaviour is the load-bearing
part for production but cannot be reliably reproduced cross-platform in a unit
test (we'd need a second process holding an exclusive Excel handle). It is
covered by manual verification on the deployment machine — see HANDOFF.md.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def _make_strataplan_xlsx(path: Path) -> None:
    """Create a minimal but valid Strataplan_List.xlsx."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Strata Plan", "Strata Name", "Address",
        "Strata Manager", "Manager email",
        "AP Name", "AP email",
        "Status",
    ])
    ws.append([
        "BCS 2707", "Mock Strata", "123 Fake St",
        "Sue Smith", "sue@example.com",
        "Pat AP", "pat@example.com",
        1,
    ])
    wb.save(str(path))


def _set_env(root: Path) -> dict[str, str | None]:
    """Set STRATACO_ROOT + required config vars; return prior values."""
    keys = [
        "STRATACO_ROOT", "LOG_DIR",
        "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
        "MAILBOX_UPN", "NOTIFY_DEFAULT_EMAIL", "NOTIFY_OVERRIDE_EMAIL",
    ]
    prior = {k: os.environ.get(k) for k in keys}
    os.environ["STRATACO_ROOT"] = str(root)
    os.environ["LOG_DIR"] = str(root / "logs")
    os.environ.setdefault("TENANT_ID", "test-tenant")
    os.environ.setdefault("CLIENT_ID", "test-client")
    os.environ.setdefault("CLIENT_SECRET", "test-secret")
    os.environ.setdefault("MAILBOX_UPN", "test@example.com")
    os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "ap@example.com")
    os.environ.setdefault("NOTIFY_OVERRIDE_EMAIL", "shadow@example.com")
    return prior


def _restore_env(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _reload_snapshot_module():
    """Fresh import so cached STRATACO_ROOT picks up the test env."""
    for mod in (
        "tools._lib.config",
        "tools._lib.paths",
        "tools._lib.strataplan_snapshot",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("tools._lib.strataplan_snapshot")


def _today_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d")
    except Exception:
        return _dt.datetime.now().strftime("%Y-%m-%d")


def test_refresh_writes_snapshot_and_marker() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        result = snap_mod.refresh_snapshot()

        assert result.exists(), f"[refresh] returned path does not exist: {result}"
        assert result == paths.strataplan_snapshot_xlsx(), (
            f"[refresh] returned path {result} != strataplan_snapshot_xlsx() "
            f"{paths.strataplan_snapshot_xlsx()}"
        )
        marker = paths.strataplan_snapshot_marker()
        assert marker.exists(), f"[refresh] marker not created at {marker}"
        got = marker.read_text(encoding="utf-8").strip()
        assert got == _today_str(), (
            f"[refresh] marker contents {got!r} != today {_today_str()!r}"
        )

        master_bytes = (tmp / "Strataplan_List.xlsx").read_bytes()
        snap_bytes = result.read_bytes()
        assert master_bytes == snap_bytes, (
            f"[refresh] snapshot bytes differ from master "
            f"(master={len(master_bytes)} B, snapshot={len(snap_bytes)} B)"
        )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_require_fresh_snapshot_happy() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        snap_mod.refresh_snapshot()

        path = snap_mod.require_fresh_snapshot()
        assert path.exists(), f"[require happy] returned path does not exist: {path}"
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_require_fresh_snapshot_stale_marker() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        snap_mod.refresh_snapshot()

        from tools._lib import paths
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
        except Exception:
            now = _dt.datetime.now()
        yesterday = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        paths.strataplan_snapshot_marker().write_text(yesterday, encoding="utf-8")

        with pytest.raises(snap_mod.SnapshotStaleError):
            snap_mod.require_fresh_snapshot()
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_require_fresh_snapshot_missing_marker() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        snap_mod = _reload_snapshot_module()
        with pytest.raises(snap_mod.SnapshotStaleError):
            snap_mod.require_fresh_snapshot()
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_refresh_raises_when_master_missing() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        snap_mod = _reload_snapshot_module()
        with pytest.raises(snap_mod.SnapshotRefreshError):
            snap_mod.refresh_snapshot()
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_refresh_raises_when_master_not_xlsx() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        master = tmp / "Strataplan_List.xlsx"
        master.write_bytes(b"this is not an xlsx, just some bytes")

        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        with pytest.raises(snap_mod.SnapshotRefreshError):
            snap_mod.refresh_snapshot()

        # Marker must NOT have been written — downstream steps would then
        # halt rather than read a corrupt snapshot.
        assert not paths.strataplan_snapshot_marker().exists(), (
            "[bad xlsx] marker was created even though refresh failed"
        )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_retry_preserves_previous_good_snapshot() -> None:
    """Regression test for the verify-before-publish atomicity fix.

    Scenario: Step 1 refreshes successfully (snapshot + today-marker on disk).
    Later in the same day the master goes bad (corrupted/truncated) and Step 1
    is re-run (testing or eventual same-day client flow). The corrupt bytes
    must not replace the previously-good snapshot, and the today-marker must
    still point at valid bytes — otherwise Steps 3-6 would silently consume
    corrupt data because their freshness check sees a today-dated marker.
    """
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        master = tmp / "Strataplan_List.xlsx"
        _make_strataplan_xlsx(master)
        good_master_bytes = master.read_bytes()

        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        snap_mod.refresh_snapshot()
        assert paths.strataplan_snapshot_xlsx().read_bytes() == good_master_bytes, (
            "[first refresh] snapshot bytes do not match master"
        )

        master.write_bytes(b"this is no longer valid xlsx data")
        with pytest.raises(snap_mod.SnapshotRefreshError):
            snap_mod.refresh_snapshot()

        assert paths.strataplan_snapshot_xlsx().read_bytes() == good_master_bytes, (
            "[second refresh] published snapshot was replaced with bad bytes "
            "— verify-before-publish is broken"
        )

        assert paths.strataplan_snapshot_marker().read_text(encoding="utf-8").strip() == _today_str(), (
            "[second refresh] marker was disturbed by failed retry"
        )

        path = snap_mod.require_fresh_snapshot()
        assert path.read_bytes() == good_master_bytes, (
            "[second refresh] require_fresh returned path with bad bytes"
        )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
