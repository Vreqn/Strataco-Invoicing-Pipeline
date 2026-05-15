"""Unit tests for private helpers in steps/step_7_aggregate.py.

Follows the same pattern as test_check_sort_key.py — imports private helpers
directly to verify their behaviour in isolation.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steps.step_7_aggregate import _has_source_invoices


def _make(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"pdf")
    return p


def test_missing_dir_returns_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        summary = processed / "04 - BCS1234 April 2026 inv.pdf"
        assert not _has_source_invoices(processed, summary)


def test_empty_dir_returns_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = processed / "04 - BCS1234 April 2026 inv.pdf"
        assert not _has_source_invoices(processed, summary)


def test_only_summary_returns_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = _make(processed, "04 - BCS1234 April 2026 inv.pdf")
        assert not _has_source_invoices(processed, summary)


def test_only_summary_collision_variant_returns_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = processed / "04 - BCS1234 April 2026 inv.pdf"
        _make(processed, "04 - BCS1234 April 2026 inv (1).pdf")
        assert not _has_source_invoices(processed, summary)


def test_summary_plus_source_returns_true() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = _make(processed, "04 - BCS1234 April 2026 inv.pdf")
        _make(processed, "12345 - 04 - BCS1234 April 2026 inv.pdf")
        assert _has_source_invoices(processed, summary)


def test_only_source_no_summary_yet_returns_true() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = processed / "04 - BCS1234 April 2026 inv.pdf"
        _make(processed, "12345 - 04 - BCS1234 April 2026 inv.pdf")
        assert _has_source_invoices(processed, summary)


def test_subdirectory_not_counted_as_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        processed = Path(td) / "Processed" / "2026" / "04 - April"
        processed.mkdir(parents=True)
        summary = processed / "04 - BCS1234 April 2026 inv.pdf"
        (processed / "subdir").mkdir()
        assert not _has_source_invoices(processed, summary)
