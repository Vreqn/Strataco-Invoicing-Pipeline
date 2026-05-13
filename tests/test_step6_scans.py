"""Unit tests for Step 6's pipeline-residue scan helpers.

Covers `_scan_unmatched_intake` and `_scan_manager_stuck` in
`steps/step_6_paid_archive.py`. Both must:

  - Return a `_ScanResult` with `rows` + `errors`.
  - Treat a missing folder as the empty case (no error).
  - Filter out `Processed -` marker files.
  - Collect filesystem errors into `errors` rather than raising — a single
    bad folder must not kill the whole morning email contract.
  - Identify managers from the directory segment, not the Strataplan XLS,
    so disk-vs-XLS drift can't silently hide stuck files.

Standalone: no pytest. Run with `python tests/test_step6_scans.py`.
Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Importing once is fine: the scan helpers call paths.root() / paths.unmatched_invoices()
# which re-read STRATACO_ROOT from the environment on every invocation, so
# tweaking os.environ between tests works without module reloads.
from steps.step_6_paid_archive import (
    _ScanResult,
    _scan_manager_stuck,
    _scan_unmatched_intake,
)


FAILED: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")
        FAILED.append(name)


class _RootContext:
    """Set STRATACO_ROOT to a temp dir for the duration of one test."""

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._prior: str | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self._prior = os.environ.get("STRATACO_ROOT")
        os.environ["STRATACO_ROOT"] = self._tmp.name
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        if self._prior is None:
            os.environ.pop("STRATACO_ROOT", None)
        else:
            os.environ["STRATACO_ROOT"] = self._prior
        assert self._tmp is not None
        self._tmp.cleanup()


# ---------------------------------------------------------------- intake


def test_intake_missing_folder() -> None:
    print("test_intake_missing_folder")
    with _RootContext():
        result = _scan_unmatched_intake()
        _check("is_scan_result", isinstance(result, _ScanResult))
        _check("rows_empty", result.rows == [])
        _check("errors_empty", result.errors == [])


def test_intake_empty_folder() -> None:
    print("test_intake_empty_folder")
    with _RootContext() as root:
        (root / "_Unmatched" / "Invoices").mkdir(parents=True)
        result = _scan_unmatched_intake()
        _check("rows_empty", result.rows == [])
        _check("errors_empty", result.errors == [])


def test_intake_mixed_contents() -> None:
    """PDF + Processed marker + non-PDF: only the non-Processed entries surface."""
    print("test_intake_mixed_contents")
    with _RootContext() as root:
        folder = root / "_Unmatched" / "Invoices"
        folder.mkdir(parents=True)
        (folder / "foo.pdf").write_text("pdf bytes")
        (folder / "Processed - bar.pdf").write_text("marker")
        (folder / "baz.docx").write_text("docx bytes")
        result = _scan_unmatched_intake()
        names = {r["fileName"] for r in result.rows}
        _check("two_rows", len(result.rows) == 2, f"got {len(result.rows)}: {names}")
        _check("foo_present", "foo.pdf" in names)
        _check("baz_present", "baz.docx" in names)
        _check("processed_filtered", "Processed - bar.pdf" not in names)
        _check("all_have_local_path", all(r.get("localPath") for r in result.rows))
        _check("all_have_mtime", all(r.get("mtimeIso") for r in result.rows))


def test_intake_unicode_filename() -> None:
    print("test_intake_unicode_filename")
    with _RootContext() as root:
        folder = root / "_Unmatched" / "Invoices"
        folder.mkdir(parents=True)
        weird = "facture_éàñü.pdf"
        (folder / weird).write_text("pdf bytes")
        result = _scan_unmatched_intake()
        _check("one_row", len(result.rows) == 1)
        _check("filename_preserved", result.rows[0]["fileName"] == weird)


def test_intake_iterdir_error() -> None:
    """A file at the _Unmatched/Invoices path: exists() True, iterdir() raises.

    This is the same shape as the production PermissionError case — a folder
    that exists but can't be read. We can't easily revoke read perms in a
    cross-platform way, so we lean on the platform-portable NotADirectoryError.
    """
    print("test_intake_iterdir_error")
    with _RootContext() as root:
        (root / "_Unmatched").mkdir()
        (root / "_Unmatched" / "Invoices").write_text("oops, not a directory")
        result = _scan_unmatched_intake()
        _check("rows_empty", result.rows == [])
        _check("one_error", len(result.errors) == 1, f"got: {result.errors}")
        _check(
            "error_mentions_path",
            "_Unmatched/Invoices" in result.errors[0],
            f"got: {result.errors[0]!r}",
        )


# ------------------------------------------------------- manager-stuck


def test_manager_missing_users_dir() -> None:
    print("test_manager_missing_users_dir")
    with _RootContext():
        result = _scan_manager_stuck()
        _check("is_scan_result", isinstance(result, _ScanResult))
        _check("rows_empty", result.rows == [])
        _check("errors_empty", result.errors == [])


def test_manager_empty_users_dir() -> None:
    print("test_manager_empty_users_dir")
    with _RootContext() as root:
        (root / "Users").mkdir()
        result = _scan_manager_stuck()
        _check("rows_empty", result.rows == [])
        _check("errors_empty", result.errors == [])


def test_manager_two_managers_stuck() -> None:
    print("test_manager_two_managers_stuck")
    with _RootContext() as root:
        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        bob = root / "Users" / "Bob" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        (alice / "foo.pdf").write_text("pdf")
        (bob / "bar.pdf").write_text("pdf")
        result = _scan_manager_stuck()
        _check("two_rows", len(result.rows) == 2, f"got {len(result.rows)}")
        _check("no_errors", result.errors == [])
        by_mgr = {r["managerName"]: r["fileName"] for r in result.rows}
        _check("alice_mapped", by_mgr.get("Alice") == "foo.pdf")
        _check("bob_mapped", by_mgr.get("Bob") == "bar.pdf")
        _check("all_have_local_path", all(r.get("localPath") for r in result.rows))


def test_manager_processed_marker_filtered() -> None:
    print("test_manager_processed_marker_filtered")
    with _RootContext() as root:
        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        (alice / "real.pdf").write_text("pdf")
        (alice / "Processed - marker.pdf").write_text("marker")
        result = _scan_manager_stuck()
        _check("one_row", len(result.rows) == 1)
        _check("real_present", result.rows[0]["fileName"] == "real.pdf")


def test_manager_ap_only_user_ignored() -> None:
    """A Users/<X>/Paid_Invoices/ entry without Invoices/Approved/ is silently skipped.

    Key correctness property: APs share the Users/ namespace with managers,
    but their on-disk shape differs. The manager scan must only surface
    folders with the manager-specific `Invoices/Approved/` subdir.
    """
    print("test_manager_ap_only_user_ignored")
    with _RootContext() as root:
        sarah_paid = root / "Users" / "Sarah" / "Paid_Invoices"
        sarah_paid.mkdir(parents=True)
        (sarah_paid / "irrelevant.pdf").write_text("pdf")

        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        (alice / "stuck.pdf").write_text("pdf")

        result = _scan_manager_stuck()
        _check("only_one_row", len(result.rows) == 1)
        _check("alice_only", result.rows[0]["managerName"] == "Alice")
        _check("no_sarah", all(r["managerName"] != "Sarah" for r in result.rows))
        _check("no_errors", result.errors == [])


def test_manager_disk_only_no_xls_row() -> None:
    """The glob-based scan must surface stuck PDFs for managers that aren't
    in the XLS at all. This is the RISK 2 fix Codex flagged: when the
    Strataplan snapshot is empty/inactive, the old `unique_managers(rows)`-
    based scan would silently miss disk residue. The new scan can't be
    fooled by snapshot state."""
    print("test_manager_disk_only_no_xls_row")
    with _RootContext() as root:
        # NO Strataplan_List.xlsx created in this test root. Yet:
        ghost = root / "Users" / "GhostManager" / "Invoices" / "Approved"
        ghost.mkdir(parents=True)
        (ghost / "left_behind.pdf").write_text("pdf")
        result = _scan_manager_stuck()
        _check("ghost_surfaced", len(result.rows) == 1)
        _check("ghost_name_from_disk", result.rows[0]["managerName"] == "GhostManager")


def test_manager_users_iterdir_error() -> None:
    """A file at Users/ triggers iterdir error — same trick as intake."""
    print("test_manager_users_iterdir_error")
    with _RootContext() as root:
        (root / "Users").write_text("oops, not a directory")
        result = _scan_manager_stuck()
        _check("rows_empty", result.rows == [])
        _check("one_error", len(result.errors) == 1)
        _check(
            "error_mentions_users",
            "Users/" in result.errors[0],
            f"got: {result.errors[0]!r}",
        )


def test_manager_per_folder_glob_error() -> None:
    """Monkeypatched glob raises for Alice; Bob is still scanned cleanly.

    Proves the per-manager error isolation: one bad folder appends a row to
    `errors` but does not abort the iteration. Without this, a single
    PermissionError on one manager would silently hide every other manager's
    stuck files in the morning report.
    """
    print("test_manager_per_folder_glob_error")
    with _RootContext() as root:
        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        bob = root / "Users" / "Bob" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        (alice / "alice.pdf").write_text("pdf")
        (bob / "bob.pdf").write_text("pdf")

        original_glob = Path.glob

        def fail_for_alice(self: Path, pattern: str):  # type: ignore[no-untyped-def]
            if "Alice" in str(self) and "Approved" in str(self):
                raise PermissionError("simulated perm denied on Alice/Approved")
            return original_glob(self, pattern)

        with patch.object(Path, "glob", fail_for_alice):
            result = _scan_manager_stuck()

        _check("one_error", len(result.errors) == 1, f"got: {result.errors}")
        _check("error_mentions_alice", "Alice" in result.errors[0])
        _check("alice_no_rows", all(r["managerName"] != "Alice" for r in result.rows))
        _check("bob_still_scanned", any(r["managerName"] == "Bob" for r in result.rows))


def main() -> int:
    test_intake_missing_folder()
    test_intake_empty_folder()
    test_intake_mixed_contents()
    test_intake_unicode_filename()
    test_intake_iterdir_error()
    test_manager_missing_users_dir()
    test_manager_empty_users_dir()
    test_manager_two_managers_stuck()
    test_manager_processed_marker_filtered()
    test_manager_ap_only_user_ignored()
    test_manager_disk_only_no_xls_row()
    test_manager_users_iterdir_error()
    test_manager_per_folder_glob_error()
    if FAILED:
        print(f"\nFAILED ({len(FAILED)}):")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
