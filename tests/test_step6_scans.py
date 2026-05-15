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
    with _RootContext():
        result = _scan_unmatched_intake()
        assert isinstance(result, _ScanResult), "is_scan_result"
        assert result.rows == [], "rows_empty"
        assert result.errors == [], "errors_empty"


def test_intake_empty_folder() -> None:
    with _RootContext() as root:
        (root / "_Unmatched" / "Invoices").mkdir(parents=True)
        result = _scan_unmatched_intake()
        assert result.rows == [], "rows_empty"
        assert result.errors == [], "errors_empty"


def test_intake_mixed_contents() -> None:
    """PDF + Processed marker + non-PDF: only the non-Processed entries surface."""
    with _RootContext() as root:
        folder = root / "_Unmatched" / "Invoices"
        folder.mkdir(parents=True)
        (folder / "foo.pdf").write_text("pdf bytes")
        (folder / "Processed - bar.pdf").write_text("marker")
        (folder / "baz.docx").write_text("docx bytes")
        result = _scan_unmatched_intake()
        names = {r["fileName"] for r in result.rows}
        assert len(result.rows) == 2, f"two_rows: got {len(result.rows)}: {names}"
        assert "foo.pdf" in names, "foo_present"
        assert "baz.docx" in names, "baz_present"
        assert "Processed - bar.pdf" not in names, "processed_filtered"
        assert all(r.get("localPath") for r in result.rows), "all_have_local_path"
        assert all(r.get("mtimeIso") for r in result.rows), "all_have_mtime"


def test_intake_os_junk_filtered() -> None:
    """OS metadata files (.DS_Store, AppleDouble sidecars, Thumbs.db,
    desktop.ini) reach shared folders via file-server browsing. They must
    never be surfaced as stuck intake — a real stuck PDF still must.

    The filter is scoped to those exact names: a genuinely stuck intake
    file that happens to be dot-hidden must STILL surface, so it isn't
    silently lost from the morning report."""
    with _RootContext() as root:
        folder = root / "_Unmatched" / "Invoices"
        folder.mkdir(parents=True)
        (folder / ".DS_Store").write_text("macos junk")
        (folder / "._foo.pdf").write_text("appledouble sidecar")
        (folder / "Thumbs.db").write_text("windows junk")
        (folder / "desktop.ini").write_text("windows junk")
        (folder / "LMS 123 - real invoice.pdf").write_text("real stuck pdf")
        # Dot-hidden but NOT a known junk name and NOT an AppleDouble
        # sidecar — must still be reported as stuck.
        (folder / ".hidden invoice.pdf").write_text("dot-hidden real pdf")
        result = _scan_unmatched_intake()
        names = {r["fileName"] for r in result.rows}
        assert names == {"LMS 123 - real invoice.pdf", ".hidden invoice.pdf"}, (
            f"real pdfs surface, junk filtered: got {names}"
        )
        assert result.errors == [], "no_errors"


def test_intake_unicode_filename() -> None:
    with _RootContext() as root:
        folder = root / "_Unmatched" / "Invoices"
        folder.mkdir(parents=True)
        weird = "facture_éàñü.pdf"
        (folder / weird).write_text("pdf bytes")
        result = _scan_unmatched_intake()
        assert len(result.rows) == 1, "one_row"
        assert result.rows[0]["fileName"] == weird, "filename_preserved"


def test_intake_iterdir_error() -> None:
    """A file at the _Unmatched/Invoices path: exists() True, iterdir() raises.

    This is the same shape as the production PermissionError case — a folder
    that exists but can't be read. We can't easily revoke read perms in a
    cross-platform way, so we lean on the platform-portable NotADirectoryError.
    """
    with _RootContext() as root:
        (root / "_Unmatched").mkdir()
        (root / "_Unmatched" / "Invoices").write_text("oops, not a directory")
        result = _scan_unmatched_intake()
        assert result.rows == [], "rows_empty"
        assert len(result.errors) == 1, f"one_error: got: {result.errors}"
        assert "_Unmatched/Invoices" in result.errors[0], (
            f"error_mentions_path: got: {result.errors[0]!r}"
        )


# ------------------------------------------------------- manager-stuck


def test_manager_missing_users_dir() -> None:
    with _RootContext():
        result = _scan_manager_stuck()
        assert isinstance(result, _ScanResult), "is_scan_result"
        assert result.rows == [], "rows_empty"
        assert result.errors == [], "errors_empty"


def test_manager_empty_users_dir() -> None:
    with _RootContext() as root:
        (root / "Users").mkdir()
        result = _scan_manager_stuck()
        assert result.rows == [], "rows_empty"
        assert result.errors == [], "errors_empty"


def test_manager_two_managers_stuck() -> None:
    with _RootContext() as root:
        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        bob = root / "Users" / "Bob" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        (alice / "foo.pdf").write_text("pdf")
        (bob / "bar.pdf").write_text("pdf")
        result = _scan_manager_stuck()
        assert len(result.rows) == 2, f"two_rows: got {len(result.rows)}"
        assert result.errors == [], "no_errors"
        by_mgr = {r["managerName"]: r["fileName"] for r in result.rows}
        assert by_mgr.get("Alice") == "foo.pdf", "alice_mapped"
        assert by_mgr.get("Bob") == "bar.pdf", "bob_mapped"
        assert all(r.get("localPath") for r in result.rows), "all_have_local_path"


def test_manager_processed_marker_filtered() -> None:
    with _RootContext() as root:
        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        (alice / "real.pdf").write_text("pdf")
        (alice / "Processed - marker.pdf").write_text("marker")
        result = _scan_manager_stuck()
        assert len(result.rows) == 1, "one_row"
        assert result.rows[0]["fileName"] == "real.pdf", "real_present"


def test_manager_ap_only_user_ignored() -> None:
    """A Users/<X>/Paid_Invoices/ entry without Invoices/Approved/ is silently skipped.

    Key correctness property: APs share the Users/ namespace with managers,
    but their on-disk shape differs. The manager scan must only surface
    folders with the manager-specific `Invoices/Approved/` subdir.
    """
    with _RootContext() as root:
        sarah_paid = root / "Users" / "Sarah" / "Paid_Invoices"
        sarah_paid.mkdir(parents=True)
        (sarah_paid / "irrelevant.pdf").write_text("pdf")

        alice = root / "Users" / "Alice" / "Invoices" / "Approved"
        alice.mkdir(parents=True)
        (alice / "stuck.pdf").write_text("pdf")

        result = _scan_manager_stuck()
        assert len(result.rows) == 1, "only_one_row"
        assert result.rows[0]["managerName"] == "Alice", "alice_only"
        assert all(r["managerName"] != "Sarah" for r in result.rows), "no_sarah"
        assert result.errors == [], "no_errors"


def test_manager_disk_only_no_xls_row() -> None:
    """The glob-based scan must surface stuck PDFs for managers that aren't
    in the XLS at all. This is the RISK 2 fix Codex flagged: when the
    Strataplan snapshot is empty/inactive, the old `unique_managers(rows)`-
    based scan would silently miss disk residue. The new scan can't be
    fooled by snapshot state."""
    with _RootContext() as root:
        ghost = root / "Users" / "GhostManager" / "Invoices" / "Approved"
        ghost.mkdir(parents=True)
        (ghost / "left_behind.pdf").write_text("pdf")
        result = _scan_manager_stuck()
        assert len(result.rows) == 1, "ghost_surfaced"
        assert result.rows[0]["managerName"] == "GhostManager", "ghost_name_from_disk"


def test_manager_users_iterdir_error() -> None:
    """A file at Users/ triggers iterdir error — same trick as intake."""
    with _RootContext() as root:
        (root / "Users").write_text("oops, not a directory")
        result = _scan_manager_stuck()
        assert result.rows == [], "rows_empty"
        assert len(result.errors) == 1, "one_error"
        assert "Users/" in result.errors[0], f"error_mentions_users: got: {result.errors[0]!r}"


def test_manager_per_folder_glob_error() -> None:
    """Monkeypatched glob raises for Alice; Bob is still scanned cleanly.

    Proves the per-manager error isolation: one bad folder appends a row to
    `errors` but does not abort the iteration. Without this, a single
    PermissionError on one manager would silently hide every other manager's
    stuck files in the morning report.
    """
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

        assert len(result.errors) == 1, f"one_error: got: {result.errors}"
        assert "Alice" in result.errors[0], "error_mentions_alice"
        assert all(r["managerName"] != "Alice" for r in result.rows), "alice_no_rows"
        assert any(r["managerName"] == "Bob" for r in result.rows), "bob_still_scanned"
