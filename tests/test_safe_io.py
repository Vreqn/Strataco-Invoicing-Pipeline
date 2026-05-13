"""Unit tests for tools/_lib/safe_io.py.

Covers the helpers added in the 0.2.0 P0/P1 fix pass:
- `sanitize_path_component` — rejects path-traversal, reserved names, etc.
- `assert_under_root` — rejects paths that resolve outside the given root.
- `safe_write_unique` — uniquifies on collision instead of overwriting.

Standalone: no pytest dependency. Run with `python tests/test_safe_io.py`.
Exits 0 if every case passes, 1 otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.safe_io import (
    assert_under_root,
    safe_write_unique,
    sanitize_path_component,
)


def _expect_value_error(label: str, fn, *args, **kwargs) -> list[str]:
    try:
        fn(*args, **kwargs)
    except ValueError:
        return []
    except Exception as exc:
        return [f"[{label}] expected ValueError, got {type(exc).__name__}: {exc}"]
    return [f"[{label}] expected ValueError, got no exception"]


def test_sanitize_path_component() -> list[str]:
    failures: list[str] = []

    cases_ok = [
        ("Sue Smith", "Sue Smith"),
        ("BCS 2707", "BCS 2707"),
        ("Krisztian Kadar", "Krisztian Kadar"),
    ]
    for raw, expected in cases_ok:
        try:
            got = sanitize_path_component(raw)
            if got != expected:
                failures.append(
                    f"[sanitize ok '{raw}'] expected {expected!r}, got {got!r}"
                )
        except Exception as exc:
            failures.append(f"[sanitize ok '{raw}'] unexpected {type(exc).__name__}: {exc}")

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
        failures.extend(_expect_value_error(f"sanitize bad '{label}'", sanitize_path_component, raw))

    return failures


def test_assert_under_root() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ok_path = root / "Users" / "Sue" / "Invoices"
        try:
            got = assert_under_root(ok_path, root)
            if not got.is_relative_to(root.resolve()):
                failures.append(f"[under_root ok] returned {got} not relative to {root}")
        except Exception as exc:
            failures.append(f"[under_root ok] unexpected {type(exc).__name__}: {exc}")

        bad_outside = root.parent / "Other" / "secret.txt"
        failures.extend(_expect_value_error("under_root outside", assert_under_root, bad_outside, root))

        bad_traversal = root / "Users" / ".." / ".." / "Other"
        failures.extend(_expect_value_error("under_root traversal", assert_under_root, bad_traversal, root))

    return failures


def test_safe_write_unique() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "invoice.pdf"
        a = safe_write_unique(target, b"first")
        if a != target or a.read_bytes() != b"first":
            failures.append(f"[unique first] expected {target}, got {a} (content={a.read_bytes()!r})")

        b = safe_write_unique(target, b"second")
        expected_b = root / "invoice (1).pdf"
        if b != expected_b or b.read_bytes() != b"second":
            failures.append(f"[unique second] expected {expected_b}, got {b}")
        if target.read_bytes() != b"first":
            failures.append("[unique second] original was overwritten")

        c = safe_write_unique(target, b"third")
        expected_c = root / "invoice (2).pdf"
        if c != expected_c or c.read_bytes() != b"third":
            failures.append(f"[unique third] expected {expected_c}, got {c}")

        no_ext = root / "noext"
        d = safe_write_unique(no_ext, b"x")
        e = safe_write_unique(no_ext, b"y")
        if e != root / "noext (1)" or e.read_bytes() != b"y":
            failures.append(f"[unique noext] expected {root / 'noext (1)'}, got {e}")

    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("sanitize_path_component", test_sanitize_path_component),
        ("assert_under_root", test_assert_under_root),
        ("safe_write_unique", test_safe_write_unique),
    ]:
        fails = fn()
        status = "OK  " if not fails else "FAIL"
        print(f"{status}[{label}] ({len(fails)} failure{'s' if len(fails) != 1 else ''})")
        all_failures.extend(fails)

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
