"""Diagnostic bundler — produces a single zip with the pipeline's state.

On-demand only. Run from the project root:

    python tools/collect_diagnostics.py

Writes a single zip (default `<log_dir>/diagnostics_<host>_<YYYYMMDD-HHMMSS>.zip`)
containing logs, queue listings, system metadata, env-var presence, and the
master Strataplan list — enough for a remote AI assistant to diagnose
failures across all six pipeline steps without touching the deployment
machine. Does NOT include any PDF contents (Krisztian sources specific
files manually if needed).

Soft-fails gracefully when STRATACO_ROOT or the master XLS is missing —
the bundle still gets written so the operator can see why.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import os
import platform
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import config, paths, safe_io  # noqa: E402


REQUIRED_ENV = [
    "STRATACO_ROOT",
    "TENANT_ID",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "MAILBOX_UPN",
    "NOTIFY_DEFAULT_EMAIL",
]
OPTIONAL_ENV = [
    "NOTIFY_OVERRIDE_EMAIL",
    "LOG_DIR",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_BASE_DELAY_SECONDS",
    "ZIP_MAX_ENTRIES",
    "ZIP_MAX_UNCOMPRESSED_BYTES",
    "ZIP_MAX_TOTAL_BYTES",
    "ZIP_MAX_RATIO",
]
STEPS = ["step_1", "step_2", "step_3", "step_4", "step_5", "step_6"]
TSV_HEADER = "filename\tsize_bytes\tmtime_iso\tpath_rel_to_root\n"
STRATA_PLANS_MAX_AGE_DAYS = 30
STRATA_PLANS_MAX_ENTRIES = 500


def _now_local() -> _dt.datetime:
    return _dt.datetime.now()


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _safe_root() -> Path | None:
    try:
        return config.strataco_root()
    except EnvironmentError:
        return None


def _safe_log_dir() -> Path:
    """Return log_dir() if available; otherwise fall back to project_root()/logs.

    log_dir() requires STRATACO_ROOT unless LOG_DIR is overridden, so we want
    a usable default even when env is broken.
    """
    try:
        return config.log_dir()
    except EnvironmentError:
        return config.project_root() / "logs"


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _listing_tsv(folder: Path, root: Path, recursive: bool = False) -> str:
    """Build a TSV of files in `folder`, paths reported relative to `root`."""
    lines = [TSV_HEADER.rstrip("\n")]
    if not folder.exists():
        return TSV_HEADER  # header only — caller's SUMMARY.md notes "no folder"
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    rows = []
    for p in iterator:
        if not p.is_file():
            continue
        try:
            st = p.stat()
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            rows.append((p.name, st.st_size, _iso(st.st_mtime), str(rel).replace("\\", "/")))
        except OSError:
            continue
    rows.sort(key=lambda r: r[2], reverse=True)
    for name, size, mtime, rel in rows:
        lines.append(f"{name}\t{size}\t{mtime}\t{rel}")
    return "\n".join(lines) + "\n"


def _strata_plans_recent_tsv(root: Path) -> str:
    """Last 30 days under Strata_Plans/, capped to 500 entries."""
    sp = root / "Strata_Plans"
    lines = [TSV_HEADER.rstrip("\n")]
    if not sp.exists():
        return TSV_HEADER
    cutoff = (_now_local() - _dt.timedelta(days=STRATA_PLANS_MAX_AGE_DAYS)).timestamp()
    rows = []
    for p in sp.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
            if st.st_mtime < cutoff:
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            rows.append((p.name, st.st_size, st.st_mtime, str(rel).replace("\\", "/")))
        except OSError:
            continue
    rows.sort(key=lambda r: r[2], reverse=True)
    rows = rows[:STRATA_PLANS_MAX_ENTRIES]
    for name, size, mtime, rel in rows:
        lines.append(f"{name}\t{size}\t{_iso(mtime)}\t{rel}")
    return "\n".join(lines) + "\n"


def _count_files(folder: Path) -> tuple[int, str]:
    """Return (file count, last-mtime ISO or '-')."""
    if not folder.exists():
        return (0, "-")
    latest = 0.0
    count = 0
    for p in folder.iterdir():
        if not p.is_file():
            continue
        try:
            mt = p.stat().st_mtime
            if mt > latest:
                latest = mt
            count += 1
        except OSError:
            continue
    return (count, _iso(latest) if latest else "-")


def _env_report() -> str:
    """Report SET/MISSING for each env var. Values are never included."""
    lines = ["# Env-var presence check", "# Values are intentionally NOT recorded.", ""]
    lines.append("[required]")
    for k in REQUIRED_ENV:
        v = os.getenv(k, "").strip()
        lines.append(f"{k}: {'SET' if v else 'MISSING'}")
    lines.append("")
    lines.append("[optional]")
    for k in OPTIONAL_ENV:
        v = os.getenv(k, "").strip()
        lines.append(f"{k}: {'SET' if v else 'MISSING'}")
    return "\n".join(lines) + "\n"


def _system_report() -> str:
    lines = [
        f"hostname: {socket.gethostname()}",
        f"platform: {platform.platform()}",
        f"python: {sys.version.splitlines()[0]}",
        f"python_executable: {sys.executable}",
        f"cwd: {os.getcwd()}",
        f"local_time: {_now_local().isoformat(timespec='seconds')}",
        f"utc_time: {_now_utc().isoformat(timespec='seconds')}",
    ]
    return "\n".join(lines) + "\n"


def _pip_freeze() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
        return f"# pip freeze exited {result.returncode}\n{result.stderr}"
    except Exception as exc:
        return f"# pip freeze failed: {type(exc).__name__}: {exc}\n"


def _read_text_tolerant(path: Path, max_bytes: int = 5 * 1024 * 1024) -> str:
    """Read a possibly-being-written log file without blocking on it."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except OSError as exc:
        return f"# could not read {path}: {exc}\n"


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _today_summary_rows(summary_csv_text: str) -> list[list[str]]:
    """Pull today's rows out of a daily_summary.csv text dump."""
    today = _today_iso()
    out: list[list[str]] = []
    for line in summary_csv_text.splitlines():
        parts = line.split(",")
        if parts and parts[0].strip() == today:
            out.append(parts)
    return out


def _build_summary(
    *,
    root: Path | None,
    log_dir: Path,
    project_version: str,
    queue_counts: list[tuple[str, int, str]],
    lock_status: dict[str, bool],
    env_text: str,
    summary_csv_text: str,
    log_tails: dict[str, str],
    xls_present: bool,
    snapshot_marker_date: str,
    pointers: list[str],
) -> str:
    out: list[str] = []
    out.append("# Strataco Diagnostic Bundle\n")
    out.append(f"**Generated (local):** {_now_local().isoformat(timespec='seconds')}\n")
    out.append(f"**Generated (UTC):** {_now_utc().isoformat(timespec='seconds')}\n")
    out.append(f"**Host:** {socket.gethostname()}\n")
    out.append(f"**Project VERSION:** {project_version}\n")
    out.append(f"**Python:** {sys.version.splitlines()[0]}\n")
    out.append(f"**STRATACO_ROOT:** {root if root else 'NOT SET — only host metadata captured'}\n")
    out.append(f"**LOG_DIR:** {log_dir}\n")
    out.append(f"**Strataplan snapshot in bundle:** {'yes' if xls_present else 'no'}\n")
    out.append(f"**Strataplan snapshot marker date:** {snapshot_marker_date}\n")

    out.append("\n## Where to look first\n")
    if pointers:
        for p in pointers:
            out.append(f"- {p}\n")
    else:
        out.append("- No obvious red flags. Skim daily_summary.csv and the queue counts below.\n")

    out.append("\n## Queue counts\n")
    out.append("| Queue | Files | Last activity |\n")
    out.append("|---|---:|---|\n")
    for label, count, last in queue_counts:
        out.append(f"| {label} | {count} | {last} |\n")

    out.append("\n## Lockfiles\n")
    if lock_status:
        for step, held in lock_status.items():
            out.append(f"- {step}: {'**HELD** (process may still be running, or stale lockfile)' if held else 'free'}\n")
    else:
        out.append("- log_dir not present; cannot inspect lockfiles.\n")

    out.append("\n## Env-var presence\n")
    out.append("```\n")
    out.append(env_text)
    out.append("```\n")

    out.append("\n## Today's daily_summary.csv rows\n")
    today_rows = _today_summary_rows(summary_csv_text)
    if today_rows:
        out.append("```\n")
        for row in today_rows:
            out.append(",".join(row) + "\n")
        out.append("```\n")
    else:
        out.append("(no rows for today)\n")

    out.append("\n## Recent log tails (last 30 lines per step)\n")
    if not log_tails:
        out.append("(no per-step logs found)\n")
    for step, tail in log_tails.items():
        out.append(f"\n### {step}\n")
        out.append("```\n")
        out.append(tail.rstrip() + "\n")
        out.append("```\n")

    return "".join(out)


def _pointers_from_summary(summary_csv_text: str) -> list[str]:
    """Build the "Where to look first" bullets from today's daily_summary.csv rows.

    Schema is auto-detected:
      - 7-column (0.11.2+):  date, step, processed, need_review, errors, duration_sec, status
      - 6-column (legacy):   date, step, processed,              errors, duration_sec, status

    On migration day the file may briefly contain rows of both widths.
    """
    out: list[str] = []
    today = _today_iso()
    for line in summary_csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or parts[0] != today:
            continue
        # 7-column row (new schema)
        if len(parts) >= 7:
            _date, step, _processed, need_review, errors, _duration, status = parts[:7]
            if status == "error":
                out.append(
                    f"`{step}` has status=error in today's summary (errors={errors}) — "
                    f"see logs/{step}_{today}.log"
                )
            elif status == "skipped":
                out.append(
                    f"`{step}` was SKIPPED — a previous run was still active, "
                    f"or a stale lockfile remains"
                )
            try:
                nr = int(need_review)
            except ValueError:
                nr = 0
            if nr > 0:
                out.append(
                    f"`{step}` has {nr} email(s) needing review (left in Inbox: "
                    f"strict flags + all-or-nothing + subject-but-no-prior) — "
                    f"see logs/{step}_{today}.log"
                )
        # 6-column row (pre-migration / legacy)
        elif len(parts) >= 6:
            _date, step, _processed, errors, _duration, status = parts[:6]
            if status == "error":
                out.append(
                    f"`{step}` has status=error in today's summary (errors={errors}) — "
                    f"see logs/{step}_{today}.log"
                )
            elif status == "skipped":
                out.append(
                    f"`{step}` was SKIPPED — a previous run was still active, "
                    f"or a stale lockfile remains"
                )
    return out


def _build_zip(
    *,
    out_path: Path,
    include_strataplan: bool,
    log_days: int,
) -> tuple[Path, list[str]]:
    """Build the diagnostic zip in memory, atomic-write to out_path.

    Returns (final_path, warnings).
    """
    warnings: list[str] = []
    root = _safe_root()
    log_dir = _safe_log_dir()

    if root is None:
        warnings.append("STRATACO_ROOT is not set - only host metadata captured.")

    project_version = "unknown"
    try:
        v_file = config.project_root() / "VERSION"
        if v_file.exists():
            project_version = v_file.read_text(encoding="utf-8").strip()
    except Exception as exc:
        warnings.append(f"could not read VERSION: {exc}")

    plans = []
    xls_path = None
    xls_present = False
    snapshot_marker_date = "(missing)"
    if root is not None:
        # Read the Step 1 snapshot, not the master — the snapshot is what
        # every other step actually consumed, so a diagnostic against the
        # master could disagree with what produced the day's behaviour.
        try:
            xls_path = paths.strataplan_snapshot_xlsx()
            if include_strataplan and xls_path.exists():
                xls_present = True
        except Exception as exc:
            warnings.append(f"could not resolve strataplan_snapshot_xlsx(): {exc}")

        try:
            marker = paths.strataplan_snapshot_marker()
            if marker.exists():
                snapshot_marker_date = marker.read_text(encoding="utf-8").strip() or "(empty)"
        except Exception as exc:
            warnings.append(f"could not read snapshot marker: {exc}")

        try:
            from tools._lib.xls import load_plans, unique_aps, unique_managers
            if xls_path is not None and xls_path.exists():
                plan_rows = load_plans(xls_path)
                plans = (unique_managers(plan_rows), unique_aps(plan_rows))
        except Exception as exc:
            warnings.append(f"could not load strataplan snapshot: {exc}")
            plans = []

    queue_counts: list[tuple[str, int, str]] = []
    queue_tsvs: list[tuple[str, str]] = []

    if root is not None:
        unmatched = paths.unmatched_invoices()
        c, last = _count_files(unmatched)
        queue_counts.append(("_Unmatched/Invoices", c, last))
        queue_tsvs.append(("queues/unmatched.tsv", _listing_tsv(unmatched, root)))

        if plans:
            managers, aps = plans
            for m in managers:
                ta = paths.manager_to_approve(m.manager_name)
                ap_app = paths.manager_approved(m.manager_name)
                c1, l1 = _count_files(ta)
                c2, l2 = _count_files(ap_app)
                queue_counts.append((f"Manager {m.manager_name} → To_Approve", c1, l1))
                queue_counts.append((f"Manager {m.manager_name} → Approved", c2, l2))
                queue_tsvs.append((f"queues/manager_{m.manager_key}__to_approve.tsv", _listing_tsv(ta, root)))
                queue_tsvs.append((f"queues/manager_{m.manager_key}__approved.tsv", _listing_tsv(ap_app, root)))
            for a in aps:
                ai = paths.ap_approved_invoices(a.ap_name)
                pi = paths.ap_paid_invoices(a.ap_name)
                c1, l1 = _count_files(ai)
                c2, l2 = _count_files(pi)
                queue_counts.append((f"AP {a.ap_name} → Approved_Invoices", c1, l1))
                queue_counts.append((f"AP {a.ap_name} → Paid_Invoices", c2, l2))
                queue_tsvs.append((f"queues/ap_{a.ap_key}__approved_invoices.tsv", _listing_tsv(ai, root)))
                queue_tsvs.append((f"queues/ap_{a.ap_key}__paid_invoices.tsv", _listing_tsv(pi, root)))
        else:
            warnings.append("Strataplan_List.xlsx not loaded — per-manager / per-AP listings skipped.")

        queue_tsvs.append(("queues/strata_plans_recent.tsv", _strata_plans_recent_tsv(root)))

        toap_hist = paths.toapprove_history_dir()
        ap_hist = paths.ap_approved_history_dir()
        queue_tsvs.append(("state/toapprove_history.tsv", _listing_tsv(toap_hist, root, recursive=True)))
        queue_tsvs.append(("state/ap_approved_history.tsv", _listing_tsv(ap_hist, root, recursive=True)))

    summary_csv_text = ""
    summary_csv_path = log_dir / "daily_summary.csv"
    if summary_csv_path.exists():
        summary_csv_text = _read_text_tolerant(summary_csv_path)

    log_files: list[tuple[str, Path]] = []
    lock_status: dict[str, bool] = {}
    if log_dir.exists():
        cutoff = (_now_local() - _dt.timedelta(days=log_days)).timestamp()
        for p in sorted(log_dir.iterdir()):
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".log") and name.startswith("step_"):
                try:
                    if p.stat().st_mtime >= cutoff:
                        log_files.append((f"logs/{name}", p))
                except OSError:
                    continue
        for step in STEPS:
            lock_status[step] = (log_dir / f".{step}.lock").exists()

    log_tails: dict[str, str] = {}
    today = _today_iso()
    for step in STEPS:
        candidate = log_dir / f"{step}_{today}.log"
        if candidate.exists():
            log_tails[step] = _tail(_read_text_tolerant(candidate), 30)

    pointers = _pointers_from_summary(summary_csv_text)
    if root is not None:
        unmatched_count = queue_counts[0][1] if queue_counts else 0
        if unmatched_count > 20:
            pointers.append(f"_Unmatched/Invoices holds {unmatched_count} files — unusually high; check Step 1 + Step 3 logs.")
    if any(lock_status.values()):
        for step, held in lock_status.items():
            if held:
                pointers.append(f"`{step}` lockfile is held — either a run is still active or a previous run crashed.")

    env_text = _env_report()
    system_text = _system_report()
    pip_text = _pip_freeze()

    summary_md = _build_summary(
        root=root,
        log_dir=log_dir,
        project_version=project_version,
        queue_counts=queue_counts,
        lock_status=lock_status,
        env_text=env_text,
        summary_csv_text=summary_csv_text,
        log_tails=log_tails,
        xls_present=xls_present,
        snapshot_marker_date=snapshot_marker_date,
        pointers=pointers,
    )

    buf = io.BytesIO()
    bundle_root = f"diagnostics_{socket.gethostname()}_{_now_local().strftime('%Y%m%d-%H%M%S')}"
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        def add(name: str, data: str | bytes) -> None:
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(f"{bundle_root}/{name}", data)

        add("SUMMARY.md", summary_md)
        add("VERSION", project_version + "\n")
        add("system.txt", system_text)
        add("env_check.txt", env_text)
        add("pip_freeze.txt", pip_text)

        if xls_present and xls_path is not None:
            try:
                add("strataplan_list_snapshot.xlsx", xls_path.read_bytes())
            except OSError as exc:
                warnings.append(f"could not read strataplan snapshot for inclusion: {exc}")

        if summary_csv_text:
            add("logs/daily_summary.csv", summary_csv_text)
        for arc, src in log_files:
            try:
                add(arc, _read_text_tolerant(src))
            except Exception as exc:
                warnings.append(f"could not include {src}: {exc}")
        if log_dir.exists():
            for step in STEPS:
                lock = log_dir / f".{step}.lock"
                if lock.exists():
                    add(f"logs/.{step}.lock", f"# present at {_iso(lock.stat().st_mtime)}\n")

        for arc, body in queue_tsvs:
            add(arc, body)

        if warnings:
            add("warnings.txt", "\n".join(warnings) + "\n")

    final = safe_io.safe_write_unique(out_path, buf.getvalue())
    return final, warnings


def _default_out_path() -> Path:
    log_dir = _safe_log_dir()
    host = safe_io.sanitize_filename(socket.gethostname())
    ts = _now_local().strftime("%Y%m%d-%H%M%S")
    return log_dir / f"diagnostics_{host}_{ts}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bundle Strataco pipeline diagnostics into a single zip.")
    parser.add_argument("--days", type=int, default=7, help="How many days of step logs to include (default 7).")
    parser.add_argument("--out", type=str, default=None, help="Output zip path (default: <log_dir>/diagnostics_<host>_<timestamp>.zip).")
    parser.add_argument("--no-strataplan", action="store_true", help="Skip including the master Strataplan_List.xlsx.")
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else _default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    final, warnings = _build_zip(
        out_path=out_path,
        include_strataplan=not args.no_strataplan,
        log_days=args.days,
    )
    print(f"diagnostic bundle written: {final}")
    for w in warnings:
        print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
