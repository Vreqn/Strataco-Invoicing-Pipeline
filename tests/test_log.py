"""Unit tests for tools/_lib/log.py.

Covers the 0.2.0 fix to `daily_log`: when another run already holds the
per-step lockfile, the contextmanager must yield a `_Run` object with
`status == "skipped"` (instead of crashing with the pre-fix
`RuntimeError: generator didn't yield`).

Also covers the 0.16.x bounded-retry fix: a *momentary* lock collision
(e.g. the collect_diagnostics probe) resolves within the retry window
instead of forcing a spurious skip.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portalocker


def _close_strataco_log_handlers(step: str) -> None:
    """Detach FileHandlers so Windows can delete the underlying .log file."""
    logger = logging.getLogger(f"strataco.{step}")
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)


def _set_test_env(td: str) -> None:
    """Point LOG_DIR + STRATACO_ROOT at the temp dir so config's required-vars
    don't fail when log.py imports it."""
    os.environ["LOG_DIR"] = td
    os.environ.setdefault("STRATACO_ROOT", td)
    os.environ.setdefault("TENANT_ID", "test")
    os.environ.setdefault("CLIENT_ID", "test")
    os.environ.setdefault("CLIENT_SECRET", "test")
    os.environ.setdefault("MAILBOX_UPN", "test@example.com")
    os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "test@example.com")


def _hold_lock(lockfile: Path) -> portalocker.Lock:
    holder = portalocker.Lock(
        str(lockfile),
        mode="w",
        timeout=0,
        flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
    )
    holder.acquire()
    return holder


def test_daily_log_skipped_on_lock_collision(monkeypatch) -> None:
    """A lock held for longer than the retry window yields status='skipped'."""
    step = "test_collision_step"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _set_test_env(td)

        import tools._lib.log as log_mod
        from tools._lib.log import daily_log

        # Shrink the retry window so the test stays fast — the holder below
        # never releases, so daily_log must wait the full window then skip.
        monkeypatch.setattr(log_mod, "LOCK_ACQUIRE_TIMEOUT_S", 0.3)
        monkeypatch.setattr(log_mod, "LOCK_CHECK_INTERVAL_S", 0.05)

        lockfile = Path(td) / f".{step}.lock"
        holder = _hold_lock(lockfile)
        try:
            entered = False
            saw_status = None
            with daily_log(step) as run:
                entered = True
                saw_status = run.status

            assert entered, "with-block body never executed"
            assert saw_status == "skipped", (
                f"expected run.status == 'skipped', got {saw_status!r}"
            )
        finally:
            try:
                holder.release()
            except Exception:
                pass
            _close_strataco_log_handlers(step)


def test_daily_log_rides_out_momentary_collision(monkeypatch) -> None:
    """A lock released *within* the retry window must NOT cause a skip.

    Regression for the collect_diagnostics probe race: the probe holds the
    lock for microseconds; daily_log must retry and acquire it, not skip a
    real run.
    """
    step = "test_transient_step"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _set_test_env(td)

        import tools._lib.log as log_mod
        from tools._lib.log import daily_log

        # Window comfortably longer than the holder's brief grip.
        monkeypatch.setattr(log_mod, "LOCK_ACQUIRE_TIMEOUT_S", 3.0)
        monkeypatch.setattr(log_mod, "LOCK_CHECK_INTERVAL_S", 0.05)

        lockfile = Path(td) / f".{step}.lock"
        holder = _hold_lock(lockfile)

        # Release the lock shortly after daily_log starts retrying.
        def _release_soon() -> None:
            time.sleep(0.3)
            holder.release()

        releaser = threading.Thread(target=_release_soon)
        releaser.start()
        try:
            entered = False
            saw_status = None
            with daily_log(step) as run:
                entered = True
                saw_status = run.status

            assert entered, "with-block body never executed"
            assert saw_status != "skipped", (
                f"momentary collision should not skip — got status {saw_status!r}"
            )
        finally:
            releaser.join()
            _close_strataco_log_handlers(step)
