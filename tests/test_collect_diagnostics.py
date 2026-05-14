"""Regression test for tools/collect_diagnostics.py.

Builds a synthetic STRATACO_ROOT in a temp dir, points the bundler at it,
runs it programmatically, and verifies the resulting zip contains the
expected files + that env_check never leaks values + that --no-strataplan
honours its flag.
"""

from __future__ import annotations

import gc
import importlib
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_strataplan_xlsx(path: Path) -> None:
    """Create a minimal but valid Strataplan_List.xlsx with 2 managers + 1 AP."""
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
        "BCS 2707", "Mock Strata One", "123 Fake St",
        "Sue Smith", "sue@example.com",
        "Pat AP", "pat@example.com",
        1,
    ])
    ws.append([
        "LMS 4193", "Mock Strata Two", "456 Other St",
        "Joe Bloggs", "joe@example.com",
        "Pat AP", "pat@example.com",
        1,
    ])
    wb.save(str(path))


def _seed_snapshot(root: Path) -> None:
    """Seed the Step 1 snapshot + today's marker so the bundler has data to read.

    The bundler now consumes `_state/strataplan_list_snapshot.xlsx` rather than
    the master, so tests must seed that path. Also writes the marker with
    today's America/Vancouver date so a future `require_fresh_snapshot()` check
    (if any) would pass.
    """
    import datetime as _dt2
    snapshot_dir = root / "_state"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    _make_strataplan_xlsx(snapshot_dir / "strataplan_list_snapshot.xlsx")
    try:
        from zoneinfo import ZoneInfo
        today = _dt2.datetime.now(ZoneInfo("America/Vancouver")).strftime("%Y-%m-%d")
    except Exception:
        today = _dt2.datetime.now().strftime("%Y-%m-%d")
    (snapshot_dir / "strataplan_list_snapshot.ok").write_text(today, encoding="utf-8")


def _seed_filesystem(root: Path) -> None:
    """Lay out the folder tree the bundler scans, with a handful of dummy files."""
    (root / "_Unmatched" / "Invoices").mkdir(parents=True, exist_ok=True)
    (root / "_Unmatched" / "Invoices" / "stuck_invoice_one.pdf").write_bytes(b"\x00" * 100)
    (root / "_Unmatched" / "Invoices" / "stuck_invoice_two.pdf").write_bytes(b"\x00" * 200)

    for manager in ("Sue Smith", "Joe Bloggs"):
        ta = root / "Users" / manager / "Invoices" / "To_Approve"
        ap = root / "Users" / manager / "Invoices" / "Approved"
        ta.mkdir(parents=True, exist_ok=True)
        ap.mkdir(parents=True, exist_ok=True)
        (ta / f"{manager}_waiting.pdf").write_bytes(b"a" * 50)

    ap_root_inv = root / "Users" / "Pat AP" / "Approved_Invoices"
    ap_paid = root / "Users" / "Pat AP" / "Paid_Invoices"
    ap_root_inv.mkdir(parents=True, exist_ok=True)
    ap_paid.mkdir(parents=True, exist_ok=True)

    (root / "_state" / "toapprove_history").mkdir(parents=True, exist_ok=True)
    (root / "_state" / "ap_approved_history").mkdir(parents=True, exist_ok=True)

    (root / "Strata_Plans" / "BCS 2707").mkdir(parents=True, exist_ok=True)


def _seed_logs(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    today = _dt.date.today().isoformat()
    (log_dir / "daily_summary.csv").write_text(
        "date,step,processed,errors,duration_sec,status\n"
        f"{today},step_1,5,0,2.3,ok\n"
        f"{today},step_2,1,0,0.4,ok\n"
        f"{today},step_3,4,1,3.1,error\n",
        encoding="utf-8",
    )
    (log_dir / f"step_1_{today}.log").write_text(
        "2026-05-11 06:00:00 | INFO | step_1 started\n"
        "2026-05-11 06:00:02 | INFO | processed 5 messages\n"
        "2026-05-11 06:00:02 | INFO | step_1 finished\n",
        encoding="utf-8",
    )
    (log_dir / f"step_3_{today}.log").write_text(
        "2026-05-11 06:20:00 | INFO | step_3 started\n"
        "2026-05-11 06:20:01 | ERROR | could not parse plan from foo.pdf\n"
        "2026-05-11 06:20:03 | INFO | step_3 finished\n",
        encoding="utf-8",
    )


def _run_bundler(out_path: Path, *, no_strataplan: bool = False) -> Path:
    """Programmatically invoke the bundler. Returns the actual zip path written."""
    for mod in ("tools._lib.config", "tools._lib.paths", "tools._lib.xls", "tools.collect_diagnostics"):
        if mod in sys.modules:
            del sys.modules[mod]
    cd = importlib.import_module("tools.collect_diagnostics")

    argv = ["--out", str(out_path)]
    if no_strataplan:
        argv.append("--no-strataplan")
    rc = cd.main(argv)
    if rc != 0:
        raise RuntimeError(f"bundler returned non-zero exit: {rc}")
    return out_path


def _zip_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def _zip_read(path: Path, suffix: str) -> bytes:
    """Read the first member whose name ends with `suffix`."""
    with zipfile.ZipFile(path) as zf:
        for n in zf.namelist():
            if n.endswith(suffix):
                return zf.read(n)
    raise KeyError(suffix)


def _set_env(root: Path, log_dir: Path) -> dict[str, str | None]:
    """Set STRATACO_ROOT and LOG_DIR; also set required env so config doesn't barf
    when the bundler reports presence. Returns prior values for cleanup."""
    keys = [
        "STRATACO_ROOT", "LOG_DIR",
        "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
        "MAILBOX_UPN", "NOTIFY_DEFAULT_EMAIL",
        "NOTIFY_OVERRIDE_EMAIL",
    ]
    prior = {k: os.environ.get(k) for k in keys}
    os.environ["STRATACO_ROOT"] = str(root)
    os.environ["LOG_DIR"] = str(log_dir)
    os.environ["TENANT_ID"] = "test-tenant"
    os.environ["CLIENT_ID"] = "test-client"
    os.environ["CLIENT_SECRET"] = "test-secret"
    os.environ["MAILBOX_UPN"] = "test@example.com"
    os.environ["NOTIFY_DEFAULT_EMAIL"] = "ap@example.com"
    os.environ["NOTIFY_OVERRIDE_EMAIL"] = "shadow@example.com"
    return prior


def _restore_env(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_happy_path() -> None:
    # openpyxl read_only mode keeps the workbook file handle open until GC;
    # ignore_cleanup_errors handles the Windows handle-lag on tempdir cleanup.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td) / "Strataco"
        root.mkdir()
        log_dir = root / "logs"
        _seed_filesystem(root)
        _seed_logs(log_dir)
        _make_strataplan_xlsx(root / "Strataplan_List.xlsx")
        _seed_snapshot(root)
        out = Path(td) / "bundle.zip"

        prior = _set_env(root, log_dir)
        try:
            final = _run_bundler(out)
        finally:
            _restore_env(prior)
            gc.collect()

        assert final.exists(), "[happy] bundle was not written"

        members = _zip_members(final)
        required_suffixes = [
            "/SUMMARY.md",
            "/VERSION",
            "/system.txt",
            "/env_check.txt",
            "/pip_freeze.txt",
            "/strataplan_list_snapshot.xlsx",
            "/logs/daily_summary.csv",
            "/queues/unmatched.tsv",
            "/queues/strata_plans_recent.tsv",
            "/state/toapprove_history.tsv",
            "/state/ap_approved_history.tsv",
        ]
        for suf in required_suffixes:
            assert any(m.endswith(suf) for m in members), (
                f"[happy] expected member ending in {suf!r} — got: {sorted(members)}"
            )

        assert any("queues/manager_SUE_SMITH__to_approve.tsv" in m for m in members), (
            f"[happy] expected manager Sue Smith to_approve listing — got: {sorted(members)}"
        )
        assert any("queues/ap_PAT_AP__approved_invoices.tsv" in m for m in members), (
            f"[happy] expected AP Pat AP approved_invoices listing — got: {sorted(members)}"
        )

        summary_md = _zip_read(final, "/SUMMARY.md").decode("utf-8")
        assert "_Unmatched/Invoices" in summary_md, "[happy] SUMMARY.md missing _Unmatched/Invoices row"
        assert "stuck_invoice_one.pdf" in _zip_read(final, "/queues/unmatched.tsv").decode("utf-8"), (
            "[happy] unmatched.tsv missing seeded file"
        )
        assert "step_3" in summary_md, "[happy] SUMMARY.md should reference step_3 (error today)"

        env_text = _zip_read(final, "/env_check.txt").decode("utf-8")
        forbidden_values = [
            "test-tenant", "test-client", "test-secret",
            "test@example.com", "ap@example.com", "shadow@example.com",
            str(root), str(log_dir),
        ]
        for v in forbidden_values:
            assert v not in env_text, f"[happy] env_check.txt leaked a value: {v!r}"
        assert "STRATACO_ROOT: SET" in env_text, "[happy] env_check.txt missing STRATACO_ROOT: SET marker"


def test_no_strataplan_flag() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td) / "Strataco"
        root.mkdir()
        log_dir = root / "logs"
        _seed_filesystem(root)
        _seed_logs(log_dir)
        _make_strataplan_xlsx(root / "Strataplan_List.xlsx")
        _seed_snapshot(root)
        out = Path(td) / "bundle.zip"

        prior = _set_env(root, log_dir)
        try:
            final = _run_bundler(out, no_strataplan=True)
        finally:
            _restore_env(prior)
            gc.collect()

        members = _zip_members(final)
        assert not any(m.endswith("/strataplan_list_snapshot.xlsx") for m in members), (
            "[no_strataplan] snapshot still in bundle despite --no-strataplan"
        )


def test_missing_strataco_root() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        out = Path(td) / "bundle.zip"
        log_dir = Path(td) / "logs"
        log_dir.mkdir()
        keys = [
            "STRATACO_ROOT", "LOG_DIR",
            "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
            "MAILBOX_UPN", "NOTIFY_DEFAULT_EMAIL", "NOTIFY_OVERRIDE_EMAIL",
        ]
        prior = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ["LOG_DIR"] = str(log_dir)
        try:
            final = _run_bundler(out)
        finally:
            _restore_env(prior)

        assert final.exists(), "[missing root] bundle was not written"

        members = _zip_members(final)
        for suf in ["/SUMMARY.md", "/system.txt", "/env_check.txt", "/pip_freeze.txt"]:
            assert any(m.endswith(suf) for m in members), f"[missing root] missing {suf}"
        assert not any("/queues/" in m for m in members), (
            "[missing root] queue listings should be absent when root is unset"
        )

        env_text = _zip_read(final, "/env_check.txt").decode("utf-8")
        assert "STRATACO_ROOT: MISSING" in env_text, (
            "[missing root] env_check.txt should report STRATACO_ROOT: MISSING"
        )

        summary_md = _zip_read(final, "/SUMMARY.md").decode("utf-8")
        assert "NOT SET" in summary_md, "[missing root] SUMMARY.md should call out STRATACO_ROOT NOT SET"
