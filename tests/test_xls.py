"""Unit tests for tools/_lib/xls.py.

Covers the 0.2.0 additions:
- `_check_duplicate_routing` raises on active duplicate plan_norm with
  conflicting manager/AP, and stays silent when routing agrees.
- `_validate_component` re-raises sanitize_path_component errors with
  enough row context that an operator can locate the bad XLS row.

Standalone: no pytest dependency. Run with `python tests/test_xls.py`.
Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.xls import (
    PlanRow,
    _check_duplicate_routing,
    _validate_component,
)


def _row(plan_norm: str, manager: str, ap: str, active: bool = True) -> PlanRow:
    return PlanRow(
        plan_norm=plan_norm,
        plan_raw=plan_norm,
        strata_name="",
        address="",
        manager_name=manager,
        manager_key=manager.upper().replace(" ", "_"),
        manager_email="",
        ap_name=ap,
        ap_key=ap.upper().replace(" ", "_"),
        ap_email="",
        status_active=active,
    )


def _expect_value_error(label: str, fn, *args, **kwargs) -> list[str]:
    try:
        fn(*args, **kwargs)
    except ValueError:
        return []
    except Exception as exc:
        return [f"[{label}] expected ValueError, got {type(exc).__name__}: {exc}"]
    return [f"[{label}] expected ValueError, got no exception"]


def test_duplicate_routing_conflict_raises() -> list[str]:
    failures: list[str] = []

    # Same plan, two active rows, different managers → must raise.
    conflicting = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Jane Doe", "Alex AP"),
    ]
    failures.extend(_expect_value_error("manager conflict", _check_duplicate_routing, conflicting))

    # Same plan, two active rows, different APs → must raise.
    conflicting_ap = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Sue Smith", "Other AP"),
    ]
    failures.extend(_expect_value_error("ap conflict", _check_duplicate_routing, conflicting_ap))

    return failures


def test_duplicate_routing_agreement_passes() -> list[str]:
    failures: list[str] = []

    # Same plan listed twice with identical routing → must NOT raise (legitimate dup).
    agreement = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Sue Smith", "Alex AP"),
    ]
    try:
        _check_duplicate_routing(agreement)
    except Exception as exc:
        failures.append(f"[agreement] unexpected {type(exc).__name__}: {exc}")

    # Same plan but one row inactive → must NOT raise.
    one_inactive = [
        _row("BCS2707", "Sue Smith", "Alex AP", active=True),
        _row("BCS2707", "Jane Doe", "Alex AP", active=False),
    ]
    try:
        _check_duplicate_routing(one_inactive)
    except Exception as exc:
        failures.append(f"[inactive duplicate] unexpected {type(exc).__name__}: {exc}")

    # Different plans, completely independent.
    distinct = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("LMS4193", "Jane Doe", "Other AP"),
    ]
    try:
        _check_duplicate_routing(distinct)
    except Exception as exc:
        failures.append(f"[distinct] unexpected {type(exc).__name__}: {exc}")

    return failures


def test_validate_component_passthrough_and_reject() -> list[str]:
    failures: list[str] = []

    # Empty string is allowed (active rows without an AP exist in real data).
    got = _validate_component("", "Strata Manager", "BCS2707")
    if got != "":
        failures.append(f"[empty passthrough] expected '', got {got!r}")

    # Clean name passes through unchanged.
    got = _validate_component("Sue Smith", "Strata Manager", "BCS2707")
    if got != "Sue Smith":
        failures.append(f"[clean passthrough] expected 'Sue Smith', got {got!r}")

    # Path-traversal in manager name surfaces with row context.
    failures.extend(
        _expect_value_error(
            "traversal in manager",
            _validate_component, "../etc/passwd", "Strata Manager", "BCS2707",
        )
    )

    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("duplicate routing — conflict", test_duplicate_routing_conflict_raises),
        ("duplicate routing — agreement", test_duplicate_routing_agreement_passes),
        ("validate component", test_validate_component_passthrough_and_reject),
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
