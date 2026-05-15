"""Unit tests for tools/_lib/plan_match.py — the changes shipped in 0.3.0.

Covers the three plan_match fixes Krisztian green-lit out of the Codex
review (the other two — suffix double-count and base-fallback display —
went into To-Speak-About.txt as policy questions):

  (a) subject_candidates() normalisation now builds "BCS2707A", not
      the pre-fix "BCSA2707".
  (b) match_from_pdf_text() can match no-digit plans (GVCCA, TCLUB)
      that the pre-fix code silently dropped from `plan_to_row`.
  (e) match_from_filename_with_base_fallback() implements the
      base-plan-when-variants-share-manager rule the Step 3 workflow
      already promised but the code wasn't delivering.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib.plan_match import (
    find_explicit_plan_tokens,
    match_from_filename_with_base_fallback,
    match_from_pdf_text,
    pick_from_subject,
    plan_base,
    subject_candidates,
)
from tools._lib.xls import PlanRow


def _row(plan_norm: str, manager: str = "Sue Smith", ap: str = "Alex AP",
         strata_name: str = "", active: bool = True) -> PlanRow:
    return PlanRow(
        plan_norm=plan_norm,
        plan_raw=plan_norm,
        strata_name=strata_name,
        address="",
        manager_name=manager,
        manager_key=manager.upper().replace(" ", "_"),
        manager_email="",
        ap_name=ap,
        ap_key=ap.upper().replace(" ", "_"),
        ap_email="",
        status_active=active,
    )


def test_subject_normalization() -> None:
    cases = [
        ("Invoice for BCS 2707A", "BCS2707A"),
        ("BCS-2707 attached", "BCS2707"),
        ("LMS 4193C — please review", "LMS4193C"),
        ("EPS 6763", "EPS6763"),
    ]
    for subject, expected in cases:
        cands = subject_candidates(subject)
        norms = [c.norm for c in cands]
        assert expected in norms, (
            f"[normalize '{subject}'] expected {expected!r} among candidates, got {norms}"
        )

    # Pre-fix bug would have produced "BCSA2707" for "BCS 2707A".
    cands = [c.norm for c in subject_candidates("BCS 2707A")]
    assert "BCSA2707" not in cands, "[normalize bug check] still producing pre-fix 'BCSA2707'"

    plan_map = {"BCS2707A": _row("BCS2707A")}
    sc, row = pick_from_subject("Inv for BCS 2707A pls approve", plan_map)
    assert row is not None and row.plan_norm == "BCS2707A", (
        f"[pick_from_subject 'BCS 2707A'] expected BCS2707A row, got {row}"
    )


def test_no_digit_plan_matches_pdf_text() -> None:
    rows = [
        _row("GVCCA", manager="Sue Smith", strata_name="Granville CCA"),
        _row("BCS2707", manager="Jane Doe", strata_name="Some Other Plan"),
    ]
    text = (
        "Invoice from ACME Corp. Strata GVCCA at 123 Main St. "
        "Total $250.00. Please remit by month-end."
    )
    result = match_from_pdf_text(text, rows)
    assert result.plan_norm == "GVCCA", (
        f"[no-digit GVCCA] expected plan_norm=GVCCA, got {result.plan_norm!r} "
        f"(note={result.note!r})"
    )

    result_neg = match_from_pdf_text("Random invoice with no plan", rows)
    assert result_neg.plan_row is None, (
        f"[no-digit negative] expected no match, got {result_neg.plan_norm!r}"
    )


def test_subject_breathing_room() -> None:
    """Locks in the spaces/hyphens/case tolerance the matcher already has.

    Operators sometimes type strata numbers as 'BCS-2707' or 'BCS  2707' or
    'bcs2707' by accident. norm_plan() + the subject regex are forgiving by
    design — this test makes sure a future refactor can't quietly regress
    that without one of these failing.
    """
    plan_map = {
        "BCS2707": _row("BCS2707"),
        "NW567": _row("NW567"),
        "LMS4193A": _row("LMS4193A"),
        "GVCCA": _row("GVCCA"),
    }

    bcs_variants = [
        "Invoice from Acme BCS 2707 attached",
        "Invoice from Acme BCS  2707 attached",
        "Invoice from Acme BCS2707 attached",
        "Invoice from Acme BCS-2707 attached",
        "Invoice from Acme BCS - 2707 attached",
        "Invoice from Acme BCS_2707 attached",
        "Invoice from Acme bcs 2707 attached",
        "Invoice from Acme BCS#2707 attached",
        "Invoice from Acme BCS:2707 attached",
        "Invoice from Acme BCS/2707 attached",
    ]
    for subject in bcs_variants:
        _, row = pick_from_subject(subject, plan_map)
        assert row is not None and row.plan_norm == "BCS2707", (
            f"[breathing room BCS 2707] {subject!r} → "
            f"{row.plan_norm if row else None!r} (expected BCS2707)"
        )

    _, row = pick_from_subject("Re: NW 567 invoice", plan_map)
    assert row is not None and row.plan_norm == "NW567", (
        f"[breathing room 2-letter] 'NW 567' → "
        f"{row.plan_norm if row else None!r} (expected NW567)"
    )

    _, row = pick_from_subject("LMS-4193A invoice", plan_map)
    assert row is not None and row.plan_norm == "LMS4193A", (
        f"[breathing room suffix] 'LMS-4193A' → "
        f"{row.plan_norm if row else None!r} (expected LMS4193A)"
    )

    bad_inputs = [
        "Invoice 2707 BCS",
        "Invoice B2C7S0707",
        "Invoice from 12345 someone",
    ]
    for subject in bad_inputs:
        _, row = pick_from_subject(subject, plan_map)
        assert row is None, (
            f"[breathing room negative] {subject!r} unexpectedly matched "
            f"{row.plan_norm!r} — matcher is too loose"
        )


def test_filename_base_fallback() -> None:
    rows = [
        _row("LMS4193A", manager="Sue Smith"),
        _row("LMS4193B", manager="Sue Smith"),
        _row("BCS2707", manager="Jane Doe"),
    ]

    row = match_from_filename_with_base_fallback("LMS 4193 - invoice.pdf", rows)
    assert row is not None, "[base fallback shared manager] returned None"
    assert row.manager_name == "Sue Smith", (
        f"[base fallback shared manager] expected Sue, got {row.manager_name!r}"
    )
    assert row.plan_norm == "LMS4193", (
        f"[base fallback shared manager] plan_norm should be BASE 'LMS4193', "
        f"got {row.plan_norm!r}"
    )

    rows_split = [
        _row("BCS2707A", manager="Sue Smith"),
        _row("BCS2707B", manager="Jane Doe"),
    ]
    row = match_from_filename_with_base_fallback("BCS 2707 - whatever.pdf", rows_split)
    assert row is None, (
        f"[base fallback conflicting managers] expected None, got {row.manager_name!r}"
    )

    rows_exact = [
        _row("LMS4193A", manager="Sue Smith"),
        _row("LMS4193", manager="Jane Doe"),
    ]
    row = match_from_filename_with_base_fallback("LMS 4193 - exact.pdf", rows_exact)
    assert row is not None and row.manager_name == "Jane Doe", (
        f"[exact wins over fallback] expected Jane, got {row.manager_name if row else None!r}"
    )


def test_find_explicit_plan_tokens() -> None:
    """`find_explicit_plan_tokens` fires only when the text literally labels a
    token a strata plan — it must NOT depend on the managed plan list, and it
    must NOT guess at bare PO / account / invoice numbers.

    Drives Step 1's NO_PLAN vs AMBIGUOUS split: a PDF that explicitly names a
    plan we don't manage (e.g. "Strata Plan KAS 9999") gets flagged for review;
    an ordinary invoice with no "Strata Plan" wording routes on the subject.
    """
    # Explicitly-labelled plans are detected regardless of managed prefixes.
    assert find_explicit_plan_tokens("Strata Plan KAS 9999") == ["KAS9999"], (
        "[explicit KAS] expected ['KAS9999']"
    )
    assert find_explicit_plan_tokens("STRATA PLAN BCS 2707A — invoice") == ["BCS2707A"], (
        "[explicit BCS2707A] expected ['BCS2707A']"
    )
    assert find_explicit_plan_tokens("re: strata plan no. EPS6008 attached") == ["EPS6008"], (
        "[explicit with 'no.'] expected ['EPS6008']"
    )
    assert find_explicit_plan_tokens("Strata LMS-4193 statement") == ["LMS4193"], (
        "[explicit 'Strata <token>'] expected ['LMS4193']"
    )

    # De-duplicated, first-seen order.
    assert find_explicit_plan_tokens(
        "Strata Plan KAS 9999 ... see Strata Plan BCS 2707 ... Strata Plan KAS 9999 again"
    ) == ["KAS9999", "BCS2707"], "[dedup/order] expected ['KAS9999', 'BCS2707']"

    # No "Strata Plan" wording → nothing. Ordinary invoice tokens must NOT match,
    # or the reply-to-self recovery loop comes back.
    for negative in [
        "Invoice #4567, PO 12345, account AB1029. Total $500.00.",
        "BCS 2707 invoice attached",          # plan-shaped, but not labelled "Strata Plan"
        "Reference KAS9999 for your records",  # bare token, no label
        "",
    ]:
        assert find_explicit_plan_tokens(negative) == [], (
            f"[negative {negative!r}] expected [], got {find_explicit_plan_tokens(negative)}"
        )


def test_strict_suffix_matching() -> None:
    """Decision 05 / Change C: a PDF mentioning 'BCS 2707A' must only score
    BCS2707A, not also BCS2707. With both plans on the managed list, BCS2707A
    should win cleanly (no spurious tie that would produce an AMBIGUOUS result).
    """
    rows = [
        _row("BCS2707",  manager="Sue Smith"),
        _row("BCS2707A", manager="Sue Smith"),
        _row("BCS2707B", manager="Sue Smith"),
    ]
    text = "Invoice from ACME Corp. Strata Plan BCS 2707A. Total $250.00."
    result = match_from_pdf_text(text, rows)
    assert result.plan_norm == "BCS2707A", (
        f"[strict suffix] PDF says BCS2707A, expected match=BCS2707A, got {result.plan_norm!r} "
        f"(pre-fix double-count would tie BCS2707A with BCS2707 → ambiguous result)"
    )
    assert not result.is_base_fallback, (
        f"[strict suffix] BCS2707A is a direct managed-list hit; is_base_fallback must be False"
    )


def test_base_fallback_sets_flag() -> None:
    """Decision 04 / Change B: when the PDF says the base plan ('BCS 2707') but
    the managed list only has suffix variants ('BCS 2707A', 'BCS 2707B'),
    is_base_fallback must be True so the caller can flag for disambiguation.
    Contrast: when the base plan itself IS on the managed list, is_base_fallback
    must be False (direct hit).
    """
    rows_variants_only = [
        _row("BCS2707A", manager="Sue Smith"),
        _row("BCS2707B", manager="Sue Smith"),
    ]
    text = "Invoice from ACME Corp. Strata Plan BCS 2707. Total $250.00."
    result = match_from_pdf_text(text, rows_variants_only)
    assert result.plan_norm == "BCS2707", (
        f"[base fallback] expected plan_norm=BCS2707 (base), got {result.plan_norm!r}"
    )
    assert result.is_base_fallback is True, (
        f"[base fallback] expected is_base_fallback=True when only variants exist, got False"
    )

    # Contrast: base plan IS directly on the managed list → not a fallback.
    rows_with_base = [
        _row("BCS2707",  manager="Sue Smith"),
        _row("BCS2707A", manager="Sue Smith"),
    ]
    result_direct = match_from_pdf_text(text, rows_with_base)
    assert result_direct.plan_norm == "BCS2707", (
        f"[direct base hit] expected plan_norm=BCS2707, got {result_direct.plan_norm!r}"
    )
    assert result_direct.is_base_fallback is False, (
        f"[direct base hit] BCS2707 is directly on the list; is_base_fallback must be False"
    )


def test_plan_base() -> None:
    """Lock in plan_base behaviour: strip a single trailing letter.

    Drives the PDF-vs-subject suffix-variant failsafe in Step 1. The convention
    aligns with `xls.base_plan_index` (single trailing letter) so two plans
    count as 'suffix variants of the same base' iff `plan_base(a) == plan_base(b)`.
    """
    cases = [
        ("LMS4193C", "LMS4193"),
        ("LMS4193T", "LMS4193"),
        ("EPS4280A", "EPS4280"),
        ("BCS2707A", "BCS2707"),
        ("BCS2707B", "BCS2707"),
        ("BCS2707", "BCS2707"),
        ("EPS4280", "EPS4280"),
        ("LMS4193", "LMS4193"),
        ("GVCCA", "GVCCA"),
        ("lms4193c", "LMS4193"),
        ("", ""),
        (" LMS4193T ", "LMS4193"),
        ("\tBCS2707\n", "BCS2707"),
        ("  ", ""),
        ("4193T", "4193"),
        ("4193", "4193"),
    ]
    for plan, expected in cases:
        got = plan_base(plan)
        assert got == expected, f"[plan_base({plan!r})] expected {expected!r}, got {got!r}"

    suffix_variant_pairs = [
        ("LMS4193C", "LMS4193T"),
        ("EPS4280", "EPS4280A"),
        ("BCS2707A", "BCS2707B"),
    ]
    for a, b in suffix_variant_pairs:
        assert plan_base(a) == plan_base(b), (
            f"[suffix variants {a!r} and {b!r}] expected same base, "
            f"got {plan_base(a)!r} vs {plan_base(b)!r}"
        )

    distinct_base_pairs = [
        ("BCS2707", "BCS2800"),
        ("BCS2707", "EPS2707"),
        ("LMS4193", "LMS222"),
        ("GVCCA", "LMS4193"),
    ]
    for a, b in distinct_base_pairs:
        assert plan_base(a) != plan_base(b), (
            f"[distinct bases {a!r} and {b!r}] expected different bases, "
            f"both came back as {plan_base(a)!r}"
        )
