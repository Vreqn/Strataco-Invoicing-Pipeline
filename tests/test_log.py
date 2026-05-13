"""Unit tests for tools/_lib/log.py.

Specifically covers the 0.2.0 fix to `daily_log`: when another run already
holds the per-step lockfile, the contextmanager must yield a `_Run` object
with `status == "skipped"` (instead of crashing with the pre-fix
`RuntimeError: generator didn't yield`).

Standalone: no pytest dependency. Run with `python tests/test_log.py`.
Exits 0 on success, 1 on failure.
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


def test_daily_log_skipped_on_lock_collision() -> list[str]:
    failures: list[str] = []
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

        # Late import so the env vars above are picked up.
        from tools._lib.log import daily_log

        lockfile = Path(td) / f".{step}.lock"

        # Hold the lock from a separate Lock instance to simulate a concurrent run.
        holder = portalocker.Lock(
            str(lockfile),
            mode="w",
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        )
        holder.acquire()
        try:
            # Now opening daily_log for the SAME step should yield a skipped run.
            entered = False
            saw_status = None
            try:
                with daily_log(step) as run:
                    entered = True
                    saw_status = run.status
                    # Body should be a no-op when status == "skipped". If a real run
                    # got through, it would touch run.processed; we don't.
            except RuntimeError as exc:
                failures.append(
                    f"[collision] daily_log raised RuntimeError (the pre-fix bug): {exc}"
                )
                return failures
            except Exception as exc:
                failures.append(
                    f"[collision] daily_log raised unexpected {type(exc).__name__}: {exc}"
                )
                return failures

            if not entered:
                failures.append("[collision] with-block body never executed")
            if saw_status != "skipped":
                failures.append(
                    f"[collision] expected run.status == 'skipped', got {saw_status!r}"
                )
        finally:
            try:
                holder.release()
            except Exception:
                pass
            # Detach the file handler so Windows can clean up the temp dir.
            _close_strataco_log_handlers(step)

    return failures


def main() -> int:
    fails = test_daily_log_skipped_on_lock_collision()
    status = "OK  " if not fails else "FAIL"
    print(f"{status}[daily_log skip on lock collision] ({len(fails)} failure{'s' if len(fails) != 1 else ''})")
    if fails:
        print("\nFAILURES:")
        for f in fails:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
