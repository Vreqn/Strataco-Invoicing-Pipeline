"""Unit tests for tools/_lib/xls.py.

Covers the 0.2.0 additions:
- `_check_duplicate_routing` raises on active duplicate plan_norm with
  conflicting manager/AP, and stays silent when routing agrees.
- `_validate_component` re-raises sanitize_path_component errors with
  enough row context that an operator can locate the bad XLS row.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

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


def test_duplicate_routing_conflict_raises() -> None:
    # Same plan, two active rows, different managers → must raise.
    conflicting = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Jane Doe", "Alex AP"),
    ]
    with pytest.raises(ValueError):
        _check_duplicate_routing(conflicting)

    # Same plan, two active rows, different APs → must raise.
    conflicting_ap = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Sue Smith", "Other AP"),
    ]
    with pytest.raises(ValueError):
        _check_duplicate_routing(conflicting_ap)


def test_duplicate_routing_agreement_passes() -> None:
    # Same plan listed twice with identical routing → must NOT raise (legitimate dup).
    agreement = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("BCS2707", "Sue Smith", "Alex AP"),
    ]
    _check_duplicate_routing(agreement)

    # Same plan but one row inactive → must NOT raise.
    one_inactive = [
        _row("BCS2707", "Sue Smith", "Alex AP", active=True),
        _row("BCS2707", "Jane Doe", "Alex AP", active=False),
    ]
    _check_duplicate_routing(one_inactive)

    # Different plans, completely independent.
    distinct = [
        _row("BCS2707", "Sue Smith", "Alex AP"),
        _row("LMS4193", "Jane Doe", "Other AP"),
    ]
    _check_duplicate_routing(distinct)


def test_validate_component_passthrough_and_reject() -> None:
    # Empty string is allowed (active rows without an AP exist in real data).
    got = _validate_component("", "Strata Manager", "BCS2707")
    assert got == "", f"[empty passthrough] expected '', got {got!r}"

    # Clean name passes through unchanged.
    got = _validate_component("Sue Smith", "Strata Manager", "BCS2707")
    assert got == "Sue Smith", f"[clean passthrough] expected 'Sue Smith', got {got!r}"

    # Path-traversal in manager name surfaces with row context.
    with pytest.raises(ValueError):
        _validate_component("../etc/passwd", "Strata Manager", "BCS2707")
