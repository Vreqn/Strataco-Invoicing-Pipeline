"""Unit tests for tools/_lib/safe_io.py.

Covers the helpers added in the 0.2.0 P0/P1 fix pass:
- `sanitize_path_component` — rejects path-traversal, reserved names, etc.
- `assert_under_root` — rejects paths that resolve outside the given root.
- `safe_write_unique` — uniquifies on collision instead of overwriting.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools._lib.safe_io import (
    assert_under_root,
    safe_write_unique,
    sanitize_path_component,
)


def test_sanitize_path_component() -> None:
    cases_ok = [
        ("Sue Smith", "Sue Smith"),
        ("BCS 2707", "BCS 2707"),
        ("Krisztian Kadar", "Krisztian Kadar"),
    ]
    for raw, expected in cases_ok:
        got = sanitize_path_component(raw)
        assert got == expected, f"[sanitize ok '{raw}'] expected {expected!r}, got {got!r}"

    bad_cases = [
        ("traversal", ".."),
        ("traversal embedded", "../etc/passwd"),
        ("dot", "."),
        ("slash", "Sue/Smith"),
        ("backslash", "Sue\\Smith"),
        ("drive letter", "C:Users"),
        ("invalid char colon", "Manager: Sue"),
        ("invalid char pipe", "Sue|Smith"),
        ("trailing dot", "Sue."),
        ("reserved CON", "CON"),
        ("reserved con lowercase", "con.txt"),
        ("reserved COM1", "COM1"),
        ("empty", ""),
        ("whitespace only", "   "),
    ]
    for label, raw in bad_cases:
        with pytest.raises(ValueError):
            sanitize_path_component(raw)


def test_assert_under_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ok_path = root / "Users" / "Sue" / "Invoices"
        got = assert_under_root(ok_path, root)
        assert got.is_relative_to(root.resolve()), (
            f"[under_root ok] returned {got} not relative to {root}"
        )

        bad_outside = root.parent / "Other" / "secret.txt"
        with pytest.raises(ValueError):
            assert_under_root(bad_outside, root)

        bad_traversal = root / "Users" / ".." / ".." / "Other"
        with pytest.raises(ValueError):
            assert_under_root(bad_traversal, root)


def test_safe_write_unique() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "invoice.pdf"
        a = safe_write_unique(target, b"first")
        assert a == target and a.read_bytes() == b"first", (
            f"[unique first] expected {target}, got {a} (content={a.read_bytes()!r})"
        )

        b = safe_write_unique(target, b"second")
        expected_b = root / "invoice (1).pdf"
        assert b == expected_b and b.read_bytes() == b"second", (
            f"[unique second] expected {expected_b}, got {b}"
        )
        assert target.read_bytes() == b"first", "[unique second] original was overwritten"

        c = safe_write_unique(target, b"third")
        expected_c = root / "invoice (2).pdf"
        assert c == expected_c and c.read_bytes() == b"third", (
            f"[unique third] expected {expected_c}, got {c}"
        )

        no_ext = root / "noext"
        safe_write_unique(no_ext, b"x")
        e = safe_write_unique(no_ext, b"y")
        assert e == root / "noext (1)" and e.read_bytes() == b"y", (
            f"[unique noext] expected {root / 'noext (1)'}, got {e}"
        )


def test_safe_write_unique_retry_idempotent_on_collision_variant() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "invoice.pdf"
        safe_write_unique(target, b"first")
        v1 = safe_write_unique(target, b"second")
        assert v1 == root / "invoice (1).pdf"
        # Retry with the same data already in (1) — must return (1), not create (2).
        v1_retry = safe_write_unique(target, b"second")
        assert v1_retry == root / "invoice (1).pdf", (
            f"[retry collision] expected invoice (1).pdf, got {v1_retry}"
        )
        assert not (root / "invoice (2).pdf").exists(), (
            "[retry collision] (2) was spuriously written"
        )
