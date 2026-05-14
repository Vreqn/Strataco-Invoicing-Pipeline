"""Unit tests for Step 1's PDF cross-validation decision matrix.

Covers `_decide_email_action` — pure function, no Graph / no disk / no ledger.
Exercises every cell of the matrix laid out in workflows/step_1_intake.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

from steps.step_1_intake import (
    EmailActionKind,
    PdfClassification,
    PdfOutcome,
    _decide_email_action,
)
from tools._lib.xls import PlanRow


def _row(plan_norm: str, manager: str = "Sue Smith") -> PlanRow:
    return PlanRow(
        plan_norm=plan_norm,
        plan_raw=plan_norm,
        strata_name="",
        address="",
        manager_name=manager,
        manager_key=manager.upper().replace(" ", "_"),
        manager_email="",
        ap_name="Alex AP",
        ap_key="ALEX_AP",
        ap_email="",
        status_active=True,
    )


def _cls(outcome: PdfOutcome, plan: str = "", base_name: str = "x.pdf") -> PdfClassification:
    row = _row(plan) if plan else None
    return PdfClassification(
        outcome=outcome,
        base_name=base_name,
        blob=b"",
        pdf_plan_norm=plan,
        pdf_plan_row=row,
    )


def test_all_agree_routes_as_subject() -> None:
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf")],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[single AGREE] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[two AGREE] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )


def test_agree_and_empty_routes_as_subject() -> None:
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.EMPTY, base_name="scanned.pdf")],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[single EMPTY] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="clear.pdf"),
            _cls(PdfOutcome.EMPTY, base_name="scanned.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[AGREE + EMPTY] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )


def test_no_plan_routes_as_subject() -> None:
    """A PDF with extractable text but no strata plan number at all carries no
    evidence against the subject — route on the subject's plan (same stance as
    EMPTY). Flagging it would loop the front desk's reply-to-self recovery.
    """
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.NO_PLAN, base_name="plainvoice.pdf")],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[single NO_PLAN] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="clear.pdf"),
            _cls(PdfOutcome.NO_PLAN, base_name="plainvoice.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[AGREE + NO_PLAN] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.EMPTY, base_name="scanned.pdf"),
            _cls(PdfOutcome.NO_PLAN, base_name="plainvoice.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[EMPTY + NO_PLAN] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )


def test_no_plan_plus_clash_flags() -> None:
    """A NO_PLAN PDF can't be safely routed when a sibling PDF clashes with the
    subject — same failsafe as EMPTY + CLASH.
    """
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="clear.pdf"),
            _cls(PdfOutcome.NO_PLAN, base_name="plainvoice.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[NO_PLAN + CLASH] expected FLAG_AND_HOLD, got {a.kind}"
    )
    assert "plainvoice.pdf" in a.reason, (
        f"[NO_PLAN + CLASH] reason should name the no-plan PDF, got {a.reason!r}"
    )


def test_single_clash_flags() -> None:
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.CLASH, plan="BCS2800", base_name="x.pdf")],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[single CLASH] expected FLAG_AND_HOLD, got {a.kind}"
    )
    # Single CLASH is technically also a "consensus" of one PDF disagreeing.
    # Either phrasing is fine; just confirm the reason mentions both plans
    # when it doesn't already say "consensus".
    if "consensus" not in a.reason.lower():
        assert "BCS 2800" in a.reason and "BCS 2707" in a.reason, (
            f"[single CLASH] reason should mention both plans, got {a.reason!r}"
        )


def test_ambiguous_flags() -> None:
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.AMBIGUOUS, base_name="messy.pdf")],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[AMBIGUOUS] expected FLAG_AND_HOLD, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.AMBIGUOUS, base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[AGREE + AMBIGUOUS] expected FLAG_AND_HOLD, got {a.kind}"
    )


def test_empty_plus_clash_flags() -> None:
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="clear.pdf"),
            _cls(PdfOutcome.EMPTY, base_name="scanned.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[CLASH + EMPTY] expected FLAG_AND_HOLD, got {a.kind}"
    )
    assert "scanned.pdf" in a.reason and "mixed evidence" in a.reason.lower(), (
        f"[CLASH + EMPTY] reason should mention the empty PDF, got {a.reason!r}"
    )


def test_consensus_clash_flags() -> None:
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[consensus CLASH] expected FLAG_AND_HOLD, got {a.kind}"
    )
    assert "consensus" in a.reason.lower(), (
        f"[consensus CLASH] reason should mention consensus, got {a.reason!r}"
    )


def test_suffix_variants_flag() -> None:
    a = _decide_email_action(
        "LMS4193C",
        [
            _cls(PdfOutcome.AGREE, plan="LMS4193C", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="LMS4193T", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[LMS4193C + LMS4193T] expected FLAG_AND_HOLD, got {a.kind}"
    )
    assert "suffix-variant" in a.reason.lower(), (
        f"[LMS4193C + LMS4193T] reason should mention suffix variant, got {a.reason!r}"
    )

    a = _decide_email_action(
        "EPS4280",
        [
            _cls(PdfOutcome.AGREE, plan="EPS4280", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="EPS4280A", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[EPS4280 + EPS4280A] expected FLAG_AND_HOLD, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707A",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707A", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2707B", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[BCS2707A + BCS2707B] expected FLAG_AND_HOLD, got {a.kind}"
    )

    a = _decide_email_action(
        "BCS2707A",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2707B", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2707C", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.FLAG_AND_HOLD, (
        f"[BCS2707B + BCS2707C, no AGREE] expected FLAG_AND_HOLD, got {a.kind}"
    )


def test_distinct_bases_auto_split() -> None:
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="b.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.AUTO_SPLIT, (
        f"[BCS2707 + BCS2800] expected AUTO_SPLIT, got {a.kind} ({a.reason!r})"
    )
    assert a.per_pdf_plan.get(0) is not None and a.per_pdf_plan[0].plan_norm == "BCS2707", (
        f"[BCS2707 + BCS2800] per_pdf_plan[0] should route to BCS2707, got {a.per_pdf_plan}"
    )
    assert a.per_pdf_plan.get(1) is not None and a.per_pdf_plan[1].plan_norm == "BCS2800", (
        f"[BCS2707 + BCS2800] per_pdf_plan[1] should route to BCS2800, got {a.per_pdf_plan}"
    )

    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="EPS4280", base_name="b.pdf"),
            _cls(PdfOutcome.CLASH, plan="LMS222",  base_name="c.pdf"),
        ],
    )
    assert a.kind == EmailActionKind.AUTO_SPLIT, (
        f"[3 distinct bases] expected AUTO_SPLIT, got {a.kind} ({a.reason!r})"
    )
    plans_routed = {a.per_pdf_plan[i].plan_norm for i in range(3)}
    assert plans_routed == {"BCS2800", "EPS4280", "LMS222"}, (
        f"[3 distinct bases] expected all three plans routed, got {plans_routed}"
    )


def test_empty_classifications_routes_as_subject() -> None:
    """Degenerate case: caller passed no PDFs (e.g. zip-only email).

    The non-PDF handler in `_process_self_attachments` still parks the zip in
    _Unmatched/; the email-level action should be a no-op route-as-subject.
    """
    a = _decide_email_action("BCS2707", [])
    assert a.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
        f"[empty classifications] expected ROUTE_AS_SUBJECT, got {a.kind}"
    )
