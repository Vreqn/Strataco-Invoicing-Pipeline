"""Unit tests for tools/_lib/log.py.

Specifically covers the 0.2.0 fix to `daily_log`: when another run already
holds the per-step lockfile, the contextmanager must yield a `_Run` object
with `status == "skipped"` (instead of crashing with the pre-fix
`RuntimeError: generator didn't yield`).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
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


def test_daily_log_skipped_on_lock_collision() -> None:
    step = "test_collision_step"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        # Point both LOG_DIR and STRATACO_ROOT at the temp dir so the config
        # module's required-vars don't fail when log.py imports it.
        os.environ["LOG_DIR"] = td
        os.environ.setdefault("STRATACO_ROOT", td)
        os.environ.setdefault("TENANT_ID", "test")
        os.environ.setdefault("CLIENT_ID", "test")
        os.environ.setdefault("CLIENT_SECRET", "test")
        os.environ.setdefault("MAILBOX_UPN", "test@example.com")
        os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "test@example.com")

        from tools._lib.log import daily_log

        lockfile = Path(td) / f".{step}.lock"

        holder = portalocker.Lock(
            str(lockfile),
            mode="w",
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        )
        holder.acquire()
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
