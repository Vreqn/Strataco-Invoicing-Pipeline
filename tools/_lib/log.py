"""Per-step file logger + rolling daily_summary.csv + per-step lockfile.

Usage from an entry-point:

    from tools._lib.log import daily_log

    def main():
        with daily_log("step_5") as run:
            run.info("started")
            for item in items:
                try:
                    do_work(item)
                    run.processed += 1
                except Exception as exc:
                    run.error(f"item {item}: {exc}")

If a previous run of the same step is still active, the context manager
exits cleanly with status="skipped" instead of stepping on it.
"""

from __future__ import annotations

import csv
import datetime as _dt
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import portalocker

from tools._lib import config

_SUMMARY_HEADER = ["date", "step", "processed", "need_review", "errors", "duration_sec", "status"]
# Old pre-need_review schema. Files matching this header are migrated in place
# the first time `_append_summary_row` opens them under the new version.
_SUMMARY_HEADER_LEGACY = ["date", "step", "processed", "errors", "duration_sec", "status"]


class _Run:
    def __init__(self, step: str, logger: logging.Logger):
        self.step = step
        self.logger = logger
        self.processed = 0
        self.need_review: list[str] = []
        self.errors: list[str] = []
        self.started_at = _dt.datetime.now()
        self.status = "ok"

    def info(self, msg: str) -> None:
        self.logger.info(msg)

    def warn(self, msg: str) -> None:
        self.logger.warning(msg)

    def review(self, msg: str) -> None:
        # A deliberate "left in Inbox for human review" outcome. Logged at WARNING
        # so it stands out from chatty INFO without polluting the error stream
        # (which is reserved for genuine exceptions the operator should be paged on).
        self.need_review.append(msg)
        self.logger.warning(f"NEED_REVIEW: {msg}")

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.logger.error(msg)


def _build_logger(step: str, log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    log_file = log_dir / f"{step}_{today}.log"

    logger = logging.getLogger(f"strataco.{step}")
    logger.setLevel(logging.INFO)
    # Avoid double handlers if the module is re-imported in tests
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "_strataco", False) for h in logger.handlers):
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        fh._strataco = True  # type: ignore[attr-defined]
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
        sh._strataco = True  # type: ignore[attr-defined]
        logger.addHandler(sh)
    return logger


def _append_summary_row(log_dir: Path, row: list) -> None:
    """Append a row to logs/daily_summary.csv under a Windows file lock.

    Safety properties:
      * Existence check happens INSIDE the lock — two concurrent first-time
        runs on a fresh log dir can't both write the header.
      * Migration is wrapped to detect Excel-lock failures and skip the
        append rather than corrupt the file by appending a 7-column row to
        a still-6-column file.
      * Whole append wrapped in `try/except OSError` so a held CSV (Excel,
        antivirus, etc.) doesn't crash the step from its `finally` block —
        the row is dropped, a WARNING is logged, the step exits cleanly.

    One-shot migration: if the file's first line is the legacy 6-column header,
    we rewrite the whole file inserting a `need_review` column of 0 into every
    historical row, then append the new row.
    """
    summary = log_dir / "daily_summary.csv"
    summary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(summary) + ".lock", timeout=30):
            if summary.exists():
                if not _migrate_summary_if_legacy(summary):
                    logging.getLogger("strataco.summary").warning(
                        "daily_summary.csv migration failed (file locked? Excel "
                        "open?) — skipping this row to avoid corruption"
                    )
                    return
            write_header = not summary.exists()
            with open(summary, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(_SUMMARY_HEADER)
                w.writerow(row)
    except OSError as exc:
        logging.getLogger("strataco.summary").warning(
            "daily_summary.csv append failed: %s — step continues", exc
        )


def _migrate_summary_if_legacy(summary: Path) -> bool:
    """Upgrade a pre-0.11.2 daily_summary.csv to the 7-column schema in place.

    Returns True on success or no-op (already migrated / unknown header),
    False when the migration could not complete (file locked, etc.). On
    False the caller should skip the append to avoid corrupting the file.
    """
    try:
        with open(summary, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return False
    if not rows or rows[0] == _SUMMARY_HEADER:
        return True
    if rows[0] != _SUMMARY_HEADER_LEGACY:
        # Unknown header — leave alone; we don't want to scramble unrecognised data.
        return True
    migrated = [_SUMMARY_HEADER]
    for r in rows[1:]:
        if len(r) == 6:
            # date, step, processed, errors, duration_sec, status
            migrated.append([r[0], r[1], r[2], "0", r[3], r[4], r[5]])
        else:
            # Anomalous row count — preserve as-is. Operator will see the oddity.
            migrated.append(r)
    tmp = summary.with_suffix(".csv.migrating")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(migrated)
        tmp.replace(summary)
    except OSError:
        # Excel or another process has the summary file open; clean up the
        # tmp file and report failure so the caller skips this append.
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


@contextmanager
def daily_log(step: str):
    """Acquire a per-step lockfile, set up logging, and write a summary row on exit.

    Yields a `_Run` object the caller mutates as work proceeds. Callers should
    bail out at the top of `main()` when `run.status == "skipped"` so a second
    overlapping run doesn't redo work the holder is already doing:

        with daily_log("step_5") as run:
            if run.status == "skipped":
                return 0
            ...
    """
    log_dir = config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    lockfile = log_dir / f".{step}.lock"

    # Acquire the per-step lock — non-blocking, yield a skipped run if another holder owns it.
    lock = portalocker.Lock(
        str(lockfile),
        mode="w",
        timeout=0,
        flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
    )
    try:
        lock.acquire()
    except portalocker.exceptions.LockException:
        logger = _build_logger(step, log_dir)
        logger.warning("previous %s run still active — skipping", step)
        _append_summary_row(
            log_dir,
            [_dt.date.today().isoformat(), step, 0, 0, 0, 0, "skipped"],
        )
        skipped = _Run(step, logger)
        skipped.status = "skipped"
        yield skipped
        return

    logger = _build_logger(step, log_dir)
    run = _Run(step, logger)
    logger.info("=== %s started ===", step)

    try:
        yield run
        run.status = "error" if run.errors else "ok"
    except Exception as exc:
        run.status = "error"
        run.error(f"unhandled exception: {exc}")
        logger.exception("unhandled exception in %s", step)
        raise
    finally:
        duration = (_dt.datetime.now() - run.started_at).total_seconds()
        logger.info(
            "=== %s finished — processed=%d need_review=%d errors=%d duration=%.1fs status=%s ===",
            step, run.processed, len(run.need_review), len(run.errors), duration, run.status,
        )
        _append_summary_row(
            log_dir,
            [
                _dt.date.today().isoformat(),
                step,
                run.processed,
                len(run.need_review),
                len(run.errors),
                round(duration, 1),
                run.status,
            ],
        )
        try:
            lock.release()
        except Exception:
            pass
