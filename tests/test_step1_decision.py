"""Unit tests for Step 1's PDF cross-validation decision matrix.

Covers `_decide_email_action` — pure function, no Graph / no disk / no ledger.
Exercises every cell of the matrix laid out in workflows/step_1_intake.md.

Standalone: no pytest dependency. Run with `python tests/test_step1_decision.py`.
Exits 0 on success, 1 on failure.
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


def test_all_agree_routes_as_subject() -> list[str]:
    failures: list[str] = []

    # Single PDF, AGREE
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf")],
    )
    if a.kind != EmailActionKind.ROUTE_AS_SUBJECT:
        failures.append(f"[single AGREE] expected ROUTE_AS_SUBJECT, got {a.kind}")

    # Two PDFs, both AGREE
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.ROUTE_AS_SUBJECT:
        failures.append(f"[two AGREE] expected ROUTE_AS_SUBJECT, got {a.kind}")

    return failures


def test_agree_and_empty_routes_as_subject() -> list[str]:
    failures: list[str] = []

    # Subject matched, PDF is scanned (empty) — trust subject.
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.EMPTY, base_name="scanned.pdf")],
    )
    if a.kind != EmailActionKind.ROUTE_AS_SUBJECT:
        failures.append(f"[single EMPTY] expected ROUTE_AS_SUBJECT, got {a.kind}")

    # Multi-PDF: one AGREE, one EMPTY — the AGREE confirms, the EMPTY can't
    # contradict. Route on subject.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="clear.pdf"),
            _cls(PdfOutcome.EMPTY, base_name="scanned.pdf"),
        ],
    )
    if a.kind != EmailActionKind.ROUTE_AS_SUBJECT:
        failures.append(f"[AGREE + EMPTY] expected ROUTE_AS_SUBJECT, got {a.kind}")

    return failures


def test_single_clash_flags() -> list[str]:
    failures: list[str] = []

    # The original motivating case: subject says BCS 2707, PDF says BCS 2800.
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.CLASH, plan="BCS2800", base_name="x.pdf")],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[single CLASH] expected FLAG_AND_HOLD, got {a.kind}")
    if "consensus" not in a.reason.lower():
        # Single CLASH is technically also a "consensus" of one PDF disagreeing.
        # Either phrasing is fine; just confirm the reason mentions the clash.
        if "BCS 2800" not in a.reason or "BCS 2707" not in a.reason:
            failures.append(f"[single CLASH] reason should mention both plans, got {a.reason!r}")

    return failures


def test_ambiguous_flags() -> list[str]:
    failures: list[str] = []

    # Single AMBIGUOUS PDF — strict-first, flag.
    a = _decide_email_action(
        "BCS2707",
        [_cls(PdfOutcome.AMBIGUOUS, base_name="messy.pdf")],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[AMBIGUOUS] expected FLAG_AND_HOLD, got {a.kind}")

    # AMBIGUOUS in a multi-PDF email still trumps everything else.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.AMBIGUOUS, base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[AGREE + AMBIGUOUS] expected FLAG_AND_HOLD, got {a.kind}")

    return failures


def test_empty_plus_clash_flags() -> list[str]:
    failures: list[str] = []

    # Mix of EMPTY and CLASH: can't auto-route the EMPTY one when there's
    # disagreement on the other PDF. Strict-first -> FLAG.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="clear.pdf"),
            _cls(PdfOutcome.EMPTY, base_name="scanned.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[CLASH + EMPTY] expected FLAG_AND_HOLD, got {a.kind}")
    if "empty" not in a.reason.lower():
        failures.append(f"[CLASH + EMPTY] reason should mention the empty PDF, got {a.reason!r}")

    return failures


def test_consensus_clash_flags() -> list[str]:
    failures: list[str] = []

    # Two PDFs that BOTH say BCS 2800; subject says BCS 2707. Strict-first -> FLAG.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[consensus CLASH] expected FLAG_AND_HOLD, got {a.kind}")
    if "consensus" not in a.reason.lower():
        failures.append(f"[consensus CLASH] reason should mention consensus, got {a.reason!r}")

    return failures


def test_suffix_variants_flag() -> list[str]:
    failures: list[str] = []

    # The user's motivating case: LMS4193C and LMS4193T share base LMS4193.
    # Strict-first: FLAG — vendors confuse these easily.
    a = _decide_email_action(
        "LMS4193C",
        [
            _cls(PdfOutcome.AGREE, plan="LMS4193C", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="LMS4193T", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[LMS4193C + LMS4193T] expected FLAG_AND_HOLD, got {a.kind}")
    if "suffix-variant" not in a.reason.lower():
        failures.append(f"[LMS4193C + LMS4193T] reason should mention suffix variant, got {a.reason!r}")

    # EPS4280 (no suffix) + EPS4280A — same base, mixed bare/suffixed.
    a = _decide_email_action(
        "EPS4280",
        [
            _cls(PdfOutcome.AGREE, plan="EPS4280", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="EPS4280A", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[EPS4280 + EPS4280A] expected FLAG_AND_HOLD, got {a.kind}")

    # BCS2707A + BCS2707B — both suffixed, same base.
    # PDF #1 AGREEs with the subject (BCS2707A); PDF #2 CLASHes onto BCS2707B.
    # The shared base "BCS2707" should trip the suffix-variant failsafe.
    a = _decide_email_action(
        "BCS2707A",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707A", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2707B", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(f"[BCS2707A + BCS2707B] expected FLAG_AND_HOLD, got {a.kind}")

    # Subject-vs-PDF suffix collision with NO AGREE PDF: subject says BCS2707A,
    # two PDFs both CLASH onto sibling suffix variants. unique_plans has two
    # distinct entries (BCS2707B, BCS2707C) which collide on base BCS2707, so
    # the suffix-variant failsafe must still fire even though the subject's
    # own plan isn't in unique_plans.
    a = _decide_email_action(
        "BCS2707A",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2707B", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2707C", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.FLAG_AND_HOLD:
        failures.append(
            f"[BCS2707B + BCS2707C, no AGREE] expected FLAG_AND_HOLD, got {a.kind}"
        )

    return failures


def test_distinct_bases_auto_split() -> list[str]:
    failures: list[str] = []

    # Subject says BCS 2707, two PDFs: one BCS 2707, one BCS 2800.
    # Distinct bases (BCS2707 vs BCS2800) -> AUTO_SPLIT.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.AGREE, plan="BCS2707", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="b.pdf"),
        ],
    )
    if a.kind != EmailActionKind.AUTO_SPLIT:
        failures.append(f"[BCS2707 + BCS2800] expected AUTO_SPLIT, got {a.kind} ({a.reason!r})")
    elif a.per_pdf_plan.get(0) is None or a.per_pdf_plan[0].plan_norm != "BCS2707":
        failures.append(f"[BCS2707 + BCS2800] per_pdf_plan[0] should route to BCS2707, got {a.per_pdf_plan}")
    elif a.per_pdf_plan.get(1) is None or a.per_pdf_plan[1].plan_norm != "BCS2800":
        failures.append(f"[BCS2707 + BCS2800] per_pdf_plan[1] should route to BCS2800, got {a.per_pdf_plan}")

    # Three PDFs, three distinct bases — subject says one of them.
    a = _decide_email_action(
        "BCS2707",
        [
            _cls(PdfOutcome.CLASH, plan="BCS2800", base_name="a.pdf"),
            _cls(PdfOutcome.CLASH, plan="EPS4280", base_name="b.pdf"),
            _cls(PdfOutcome.CLASH, plan="LMS222",  base_name="c.pdf"),
        ],
    )
    if a.kind != EmailActionKind.AUTO_SPLIT:
        failures.append(f"[3 distinct bases] expected AUTO_SPLIT, got {a.kind} ({a.reason!r})")
    else:
        plans_routed = {a.per_pdf_plan[i].plan_norm for i in range(3)}
        if plans_routed != {"BCS2800", "EPS4280", "LMS222"}:
            failures.append(f"[3 distinct bases] expected all three plans routed, got {plans_routed}")

    return failures


def test_empty_classifications_routes_as_subject() -> list[str]:
    """Degenerate case: caller passed no PDFs (e.g. zip-only email).

    The non-PDF handler in `_process_self_attachments` still parks the zip in
    _Unmatched/; the email-level action should be a no-op route-as-subject.
    """
    failures: list[str] = []
    a = _decide_email_action("BCS2707", [])
    if a.kind != EmailActionKind.ROUTE_AS_SUBJECT:
        failures.append(f"[empty classifications] expected ROUTE_AS_SUBJECT, got {a.kind}")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("all AGREE -> ROUTE_AS_SUBJECT", test_all_agree_routes_as_subject),
        ("AGREE + EMPTY -> ROUTE_AS_SUBJECT", test_agree_and_empty_routes_as_subject),
        ("single CLASH -> FLAG", test_single_clash_flags),
        ("AMBIGUOUS -> FLAG", test_ambiguous_flags),
        ("CLASH + EMPTY -> FLAG", test_empty_plus_clash_flags),
        ("consensus CLASH -> FLAG", test_consensus_clash_flags),
        ("suffix variants -> FLAG (failsafe)", test_suffix_variants_flag),
        ("distinct bases -> AUTO_SPLIT", test_distinct_bases_auto_split),
        ("no PDFs -> ROUTE_AS_SUBJECT (no-op)", test_empty_classifications_routes_as_subject),
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
