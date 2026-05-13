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

Standalone: no pytest dependency. Run with `python tests/test_strataplan_snapshot.py`.
Exits 0 on success, 1 on failure.
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


def test_refresh_writes_snapshot_and_marker() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        result = snap_mod.refresh_snapshot()

        if not result.exists():
            failures.append(f"[refresh] returned path does not exist: {result}")
        if result != paths.strataplan_snapshot_xlsx():
            failures.append(
                f"[refresh] returned path {result} != strataplan_snapshot_xlsx() "
                f"{paths.strataplan_snapshot_xlsx()}"
            )
        marker = paths.strataplan_snapshot_marker()
        if not marker.exists():
            failures.append(f"[refresh] marker not created at {marker}")
        else:
            got = marker.read_text(encoding="utf-8").strip()
            if got != _today_str():
                failures.append(
                    f"[refresh] marker contents {got!r} != today {_today_str()!r}"
                )

        # Snapshot bytes should match master bytes byte-for-byte.
        master_bytes = (tmp / "Strataplan_List.xlsx").read_bytes()
        snap_bytes = result.read_bytes()
        if master_bytes != snap_bytes:
            failures.append(
                f"[refresh] snapshot bytes differ from master "
                f"(master={len(master_bytes)} B, snapshot={len(snap_bytes)} B)"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_require_fresh_snapshot_happy() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        snap_mod.refresh_snapshot()

        path = snap_mod.require_fresh_snapshot()
        if not path.exists():
            failures.append(f"[require happy] returned path does not exist: {path}")
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_require_fresh_snapshot_stale_marker() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        _make_strataplan_xlsx(tmp / "Strataplan_List.xlsx")
        snap_mod = _reload_snapshot_module()
        snap_mod.refresh_snapshot()

        from tools._lib import paths
        # Backdate the marker to yesterday in Vancouver TZ — same clock the
        # production reader uses, so the test stays correct around midnight
        # or on non-Pacific CI.
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
        except Exception:
            now = _dt.datetime.now()
        yesterday = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        paths.strataplan_snapshot_marker().write_text(yesterday, encoding="utf-8")

        try:
            snap_mod.require_fresh_snapshot()
            failures.append("[stale marker] expected SnapshotStaleError, got none")
        except snap_mod.SnapshotStaleError:
            pass
        except Exception as exc:
            failures.append(
                f"[stale marker] expected SnapshotStaleError, "
                f"got {type(exc).__name__}: {exc}"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_require_fresh_snapshot_missing_marker() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        snap_mod = _reload_snapshot_module()
        # No refresh; no marker file.
        try:
            snap_mod.require_fresh_snapshot()
            failures.append("[missing marker] expected SnapshotStaleError, got none")
        except snap_mod.SnapshotStaleError:
            pass
        except Exception as exc:
            failures.append(
                f"[missing marker] expected SnapshotStaleError, "
                f"got {type(exc).__name__}: {exc}"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_refresh_raises_when_master_missing() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        snap_mod = _reload_snapshot_module()
        # No master file in the root.
        try:
            snap_mod.refresh_snapshot()
            failures.append("[missing master] expected SnapshotRefreshError, got none")
        except snap_mod.SnapshotRefreshError:
            pass
        except Exception as exc:
            failures.append(
                f"[missing master] expected SnapshotRefreshError, "
                f"got {type(exc).__name__}: {exc}"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_refresh_raises_when_master_not_xlsx() -> list[str]:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        # A file at the master path that is not a valid XLSX.
        master = tmp / "Strataplan_List.xlsx"
        master.write_bytes(b"this is not an xlsx, just some bytes")

        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        try:
            snap_mod.refresh_snapshot()
            failures.append("[bad xlsx] expected SnapshotRefreshError, got none")
        except snap_mod.SnapshotRefreshError:
            # Marker must NOT have been written — downstream steps would then
            # halt rather than read a corrupt snapshot.
            if paths.strataplan_snapshot_marker().exists():
                failures.append(
                    "[bad xlsx] marker was created even though refresh failed"
                )
        except Exception as exc:
            failures.append(
                f"[bad xlsx] expected SnapshotRefreshError, "
                f"got {type(exc).__name__}: {exc}"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def test_failed_retry_preserves_previous_good_snapshot() -> list[str]:
    """Regression test for the verify-before-publish atomicity fix.

    Scenario: Step 1 refreshes successfully (snapshot + today-marker on disk).
    Later in the same day the master goes bad (corrupted/truncated) and Step 1
    is re-run (testing or eventual same-day client flow). The corrupt bytes
    must not replace the previously-good snapshot, and the today-marker must
    still point at valid bytes — otherwise Steps 3-6 would silently consume
    corrupt data because their freshness check sees a today-dated marker.
    """
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="strataplan_snapshot_"))
    prior = _set_env(tmp)
    try:
        master = tmp / "Strataplan_List.xlsx"
        _make_strataplan_xlsx(master)
        good_master_bytes = master.read_bytes()

        snap_mod = _reload_snapshot_module()
        from tools._lib import paths

        # First run: succeeds.
        snap_mod.refresh_snapshot()
        if paths.strataplan_snapshot_xlsx().read_bytes() != good_master_bytes:
            failures.append("[first refresh] snapshot bytes do not match master")

        # Corrupt the master and re-run — second refresh must raise and must
        # NOT have touched the published snapshot or the marker.
        master.write_bytes(b"this is no longer valid xlsx data")
        try:
            snap_mod.refresh_snapshot()
            failures.append("[second refresh] expected SnapshotRefreshError, got none")
        except snap_mod.SnapshotRefreshError:
            pass

        if paths.strataplan_snapshot_xlsx().read_bytes() != good_master_bytes:
            failures.append(
                "[second refresh] published snapshot was replaced with bad bytes "
                "— verify-before-publish is broken"
            )

        # Marker date must still be today's (from the first successful run).
        if paths.strataplan_snapshot_marker().read_text(encoding="utf-8").strip() != _today_str():
            failures.append(
                "[second refresh] marker was disturbed by failed retry"
            )

        # require_fresh_snapshot must still return the good snapshot.
        try:
            path = snap_mod.require_fresh_snapshot()
            if path.read_bytes() != good_master_bytes:
                failures.append(
                    "[second refresh] require_fresh returned path with bad bytes"
                )
        except Exception as exc:
            failures.append(
                f"[second refresh] require_fresh unexpectedly raised: "
                f"{type(exc).__name__}: {exc}"
            )
    finally:
        _restore_env(prior)
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("refresh writes snapshot + marker", test_refresh_writes_snapshot_and_marker),
        ("require_fresh — happy path", test_require_fresh_snapshot_happy),
        ("require_fresh — stale marker", test_require_fresh_snapshot_stale_marker),
        ("require_fresh — missing marker", test_require_fresh_snapshot_missing_marker),
        ("refresh — master missing", test_refresh_raises_when_master_missing),
        ("refresh — master not xlsx", test_refresh_raises_when_master_not_xlsx),
        ("refresh — failed retry preserves previous good snapshot", test_failed_retry_preserves_previous_good_snapshot),
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
