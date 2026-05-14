"""Port of the N8n plan-matching JS logic.

Three matching surfaces:
1. Email subject (Step 1) — `subject_candidates()` + `pick_from_subject()`
2. PDF filename (Steps 3, 5, 6) — `match_from_filename()`
3. PDF text body (Step 3) — `match_from_pdf_text()`

The PDF-text scoring is a near-verbatim port of node 11 in
"Step 3 - PDF Opening and Sorting" with its C/O guard, suffix fallback,
and strata-name fallback intact.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from tools._lib.xls import PlanRow

# ----------------------------------------------------------------------
# Common helpers
# ----------------------------------------------------------------------

_PLAN_NORM_RE = re.compile(r"[\s\-_./\\#№＃]+")


def norm_plan(value: str | None) -> str:
    if value is None:
        return ""
    s = str(value).upper().strip()
    return _PLAN_NORM_RE.sub("", s)


def pretty_plan(plan_norm: str) -> str:
    """'BCS2707' -> 'BCS 2707'."""
    if not plan_norm:
        return ""
    m = re.match(r"^([A-Z]{2,6})(\d{1,6}[A-Z]{0,3})$", str(plan_norm).upper())
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return str(plan_norm).upper()


_PLAN_BASE_RE = re.compile(r"^(.*\d)([A-Z])$")


def plan_base(plan_norm: str) -> str:
    """Strip a single trailing letter to expose the base for suffix-variant grouping.

    'LMS4193T' -> 'LMS4193'. 'EPS4280A' -> 'EPS4280'. 'BCS2707' -> 'BCS2707'.

    Matches the convention used by `xls.base_plan_index` so callers comparing
    two plans for "are they suffix variants of the same base?" stay aligned
    with Step 5's suffix-fallback indexer. Plans without a digit-then-letter
    tail (no-suffix plans, no-digit plans like GVCCA) are returned unchanged.

    Input is normalised (`strip().upper()`) so unnormalised callers don't
    silently get a false-negative when comparing bases.
    """
    if not plan_norm:
        return ""
    s = str(plan_norm).strip().upper()
    if not s:
        return ""
    m = _PLAN_BASE_RE.match(s)
    if m:
        return m.group(1)
    return s


# ----------------------------------------------------------------------
# Subject parsing (Step 1, node 6 + 8)
# ----------------------------------------------------------------------

_SUBJECT_RE = re.compile(
    r"\b([A-Z]{2,6})\s*[-#:/_ ]?\s*(\d{1,6})([A-Z]{1,2})?\b"
)


@dataclass
class SubjectCandidate:
    raw: str
    norm: str


def subject_candidates(subject: str) -> list[SubjectCandidate]:
    """Parse plan-like tokens from an email subject."""
    if not subject:
        return []
    up = str(subject).upper()
    out: list[SubjectCandidate] = []
    seen: set[str] = set()
    for m in _SUBJECT_RE.finditer(up):
        alpha = m.group(1)
        digits = m.group(2)
        suffix = m.group(3) or ""
        # PlanRow.plan_norm is built as "<prefix><digits><suffix>" (e.g. BCS2707A);
        # match that ordering here so the resulting candidate.norm can be looked
        # up directly in plan_map without a second normalization pass.
        norm = f"{alpha}{digits}{suffix}"
        if norm in seen:
            continue
        seen.add(norm)
        raw = f"{alpha} {digits}{suffix}".strip()
        out.append(SubjectCandidate(raw=raw, norm=norm))
    return out


def pick_from_subject(
    subject: str,
    plan_map: dict[str, PlanRow],
) -> tuple[SubjectCandidate | None, PlanRow | None]:
    """Pick the first subject candidate that exists in the plan map."""
    for c in subject_candidates(subject):
        if c.norm in plan_map:
            return c, plan_map[c.norm]
    return None, None


# Mirrors the empty-allowed_prefixes branch of `_build_plan_text_regex`, but the
# "STRATA" / "STRATA PLAN" lead-in is REQUIRED — we only treat a token as a plan
# reference when the document literally labels it one. An optional "No." / "#"
# is allowed both after the lead-in ("Strata Plan No. EPS6008") and after the
# prefix ("EPS No. 6008"). Keeps the C/O guard so "STRATA PLAN EPS6763 C/O ..."
# doesn't capture suffix "C".
_EXPLICIT_PLAN_RE = re.compile(
    r"\b(?:STRATA\s+PLAN\s+|STRATA\s+)"
    r"(?:(?:NO\.?|NUMBER|NUM\.?)\s*)?(?:#\s*)?"
    r"([A-Z]{2,6})\s*(?:[.\-_/]*\s*)?"
    r"(?:(?:NO\.?|NUMBER|NUM\.?)\s*)?(?:#\s*)?(\d{1,5})"
    r"(?:\s*[-_.]?\s*([A-Z]{1,3})(?!\s*\/\s*[A-Z]))?\b"
)


def find_explicit_plan_tokens(text: str) -> list[str]:
    """Plan tokens the PDF text *explicitly labels* as a strata plan.

    Unlike `match_from_pdf_text`, this does NOT depend on the managed plan list
    — it fires only when the text literally says "Strata Plan <token>" (or
    "Strata <token>"). Used to tell "PDF carries no plan number at all" (safe to
    route on the email subject) apart from "PDF names a plan, just not one we
    manage" (flag for review). Deliberately does not guess at bare letter+digit
    tokens: a PO number or account number on an ordinary invoice must not be
    mistaken for a plan reference, or the reply-to-self recovery loops again.

    Returns normalised tokens (e.g. "KAS9999"), de-duplicated, first-seen order.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _EXPLICIT_PLAN_RE.finditer(str(text).upper()):
        norm = f"{m.group(1)}{m.group(2)}{m.group(3) or ''}"
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


# ----------------------------------------------------------------------
# Filename matching (Step 3 node 7, Step 5 node 8, Step 6 node 8)
# ----------------------------------------------------------------------

_FILENAME_PREFIX_RE = re.compile(
    r"^\s*(?:Processed\s*-\s*)?(?:Paid\b(?:\s*-\s*|\s+))?\s*([A-Z]{2,6}\s*[- ]?\s*\d{1,6}[A-Z]{0,3})",
    re.IGNORECASE,
)


def plan_from_filename(filename: str) -> tuple[str, str]:
    """Extract (raw, norm) plan from the start of a filename.

    Strips leading 'Processed - ' and 'Paid - ' / 'Paid ' if present, then
    grabs the first plan-like token. Returns ('', '') if nothing matches.
    """
    fn = str(filename or "").strip()
    m = _FILENAME_PREFIX_RE.match(fn)
    if not m:
        return "", ""
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    return raw, norm_plan(raw)


def match_from_filename(
    filename: str,
    plan_map: dict[str, PlanRow],
) -> PlanRow | None:
    """Look up the plan extracted from `filename` in the plan map."""
    _, plan_norm = plan_from_filename(filename)
    if not plan_norm:
        return None
    return plan_map.get(plan_norm)


_STEP6_ARCHIVE_RE = re.compile(
    r"^(?P<check>.+?)\s*-\s*"
    r"(?P<month>0[1-9]|1[0-2])\s*-\s*"
    r"(?P<plan>[A-Z]{2,6}\d{1,6}[A-Z]{0,3})\s+"
    r"(?P<monthname>[A-Za-z]+)\s+"
    r"(?P<year>\d{4})\s+inv"
    r"(?:\s*\(\d+\))?\.pdf$",
    re.IGNORECASE,
)


def parse_archive_filename(name: str) -> dict | None:
    """Inverse of `step_6_paid_archive._build_archive_name`.

    Recognises `{check} - {MM} - {PLAN} {MonthName} {YYYY} inv.pdf` and the
    collision-renamed `... inv (1).pdf` variant produced by
    `safe_io.safe_write_unique`. Returns `{check, month, year, plan_norm}` or
    `None`. Rejects names whose `monthname` and numeric `month` disagree so a
    Step-6 bug can't silently feed a misnamed Summary.

    Used by Step 7 to scan `Strata_Plans/<plan>/` for invoices from a target
    month. Does NOT match the Step-7 Summary output (`Summary - ...`) — that's
    filtered by name prefix at the call site as belt-and-suspenders.
    """
    import calendar

    if not name:
        return None
    stripped = str(name).strip()
    # Reject prefixes that aren't Step 6's archive emit. The "summary -"
    # rejection is defensive: Step 7's own output also lives in the same
    # folder, and a permissive parser would re-aggregate it on the next run
    # if the call-site filter ever drifted.
    low = stripped.lower()
    if low.startswith((
        "processed -", "processed-",
        "paid -", "paid-",
        "summary -", "summary-",
    )):
        return None
    m = _STEP6_ARCHIVE_RE.match(stripped)
    if not m:
        return None
    month = int(m.group("month"))
    if not 1 <= month <= 12:
        return None
    monthname = m.group("monthname").lower()
    if monthname != calendar.month_name[month].lower():
        return None
    return {
        "check": m.group("check").strip(),
        "month": month,
        "year": int(m.group("year")),
        "plan_norm": norm_plan(m.group("plan")),
    }


def match_from_filename_with_base_fallback(
    filename: str,
    rows: list[PlanRow],
) -> PlanRow | None:
    """Filename match with a base-plan fallback when all variants agree on manager.

    Per workflows/step_3_pdf_sort.md: "LMS4193C in filename matches LMS4193C in
    XLS first; if filename has the base LMS4193, it falls back to base only
    when all suffix variants point to the same manager." This mirrors Step 5's
    `_resolve_ap` for AP routing, but keyed on manager_name instead.

    When the fallback fires we return a row whose `plan_norm` / `plan_raw` are
    rewritten to the BASE plan (not the picked variant), so the caller stamps
    the file with what the operator actually wrote rather than auto-picking a
    suffix on their behalf. The picked-variant question is a policy call
    documented in To-Speak-About.txt; until it's answered, preserve intent.
    """
    from dataclasses import replace

    _, plan_norm = plan_from_filename(filename)
    if not plan_norm:
        return None

    # Build a lookup keyed by plan_norm restricted to active rows with a manager.
    active = [r for r in rows if r.status_active and r.manager_name]
    exact = next((r for r in active if r.plan_norm == plan_norm), None)
    if exact is not None:
        return exact

    # Base fallback: collect variants whose plan_norm starts with `<plan_norm><suffix>`
    # where <suffix> is 1-3 trailing letters.
    suffix_re = re.compile(rf"^{re.escape(plan_norm)}[A-Z]{{1,3}}$")
    variants = [r for r in active if suffix_re.match(r.plan_norm)]
    if not variants:
        return None

    managers = {r.manager_name for r in variants}
    if len(managers) != 1:
        return None

    # All variants share a manager — safe to route. Rewrite plan_norm/plan_raw
    # so the caller's display uses the base ("LMS 4193") not a variant.
    representative = variants[0]
    pretty_base = pretty_plan(plan_norm)
    return replace(representative, plan_norm=plan_norm, plan_raw=pretty_base)


# ----------------------------------------------------------------------
# PDF text matching (Step 3 node 11)
#
# Port of the safe scoring algorithm:
# - Strict prefix regex built from prefixes that actually appear in the XLS
# - C/O guard: "EPS6763 C/O" must NOT capture suffix "C"
# - Score: exact match (10), suffix-in-pdf-base-in-list (9),
#          base-not-in-list-but-variants-share-manager (7),
#          name-fallback (6)
# - Pick top only if it beats second by >= 3
# ----------------------------------------------------------------------


def _escape_re(s: str) -> str:
    return re.escape(s)


def _build_plan_text_regex(allowed_prefixes: set[str]) -> re.Pattern[str]:
    """Build the strict regex that only matches prefixes seen in the XLS."""
    if not allowed_prefixes:
        return re.compile(
            r"\b(?:STRATA\s+PLAN\s+|STRATA\s+)?"
            r"([A-Z]{2,6})\s*(?:[.\-_/]*\s*)?"
            r"(?:(?:NO\.?|NUMBER|NUM\.?)\s*)?(?:#\s*)?(\d{1,5})"
            r"(?:\s*[-_.]?\s*([A-Z]{1,3})(?!\s*\/\s*[A-Z]))?\b"
        )
    alt = "|".join(
        _escape_re(p)
        for p in sorted(allowed_prefixes, key=lambda x: (-len(x), x))
    )
    return re.compile(
        r"\b(?:STRATA\s+PLAN\s+|STRATA\s+)?"
        rf"({alt})\s*(?:[.\-_/]*\s*)?"
        r"(?:(?:NO\.?|NUMBER|NUM\.?)\s*)?(?:#\s*)?(\d{1,5})"
        r"(?:\s*[-_.]?\s*([A-Z]{1,3})(?!\s*\/\s*[A-Z]))?\b"
    )


def _strata_name_score(plan_row: PlanRow, text_up: str) -> int:
    """Words in the strata name that also appear in the PDF text."""
    name = (plan_row.strata_name or "").upper()
    if not name:
        return 0
    words = [w for w in re.split(r"[^A-Z0-9]+", name) if len(w) >= 4]
    if not words:
        return 0
    hits = sum(1 for w in words if w in text_up)
    return min(12, hits * 3)


@dataclass
class PdfMatchResult:
    plan_norm: str
    plan_row: PlanRow | None
    note: str
    detected: list[tuple[str, int]]  # (token, count) sorted desc by count


def match_from_pdf_text(
    text: str,
    rows: list[PlanRow],
    enable_name_fallback: bool = True,
) -> PdfMatchResult:
    if not text or not text.strip():
        return PdfMatchResult("", None, "No text extracted (scanned PDF?).", [])

    text_up = text.upper().replace("№", "#").replace("＃", "#")

    # Build indexes from active rows only
    plan_to_row: dict[str, PlanRow] = {}
    base_to_managers: dict[str, set[str]] = defaultdict(set)
    base_to_aps: dict[str, set[str]] = defaultdict(set)
    base_to_plans: dict[str, list[str]] = defaultdict(list)
    num_to_plans: dict[str, list[str]] = defaultdict(list)
    allowed_prefixes: set[str] = set()

    for r in rows:
        if not r.status_active:
            continue
        # No-digit plans (e.g. GVCCA, TCLUB) — index them so the dedicated
        # detection loop below can match them. They don't participate in the
        # digit-based prefix/base/num indexes.
        if not any(c.isdigit() for c in r.plan_norm):
            plan_to_row.setdefault(r.plan_norm, r)
            continue
        m = re.match(r"^([A-Z]{2,6})(\d{1,5})([A-Z]{0,3})$", r.plan_norm)
        if not m:
            continue
        prefix, num = m.group(1), m.group(2)
        base = f"{prefix}{num}"
        plan_to_row.setdefault(r.plan_norm, r)
        allowed_prefixes.add(prefix)
        if r.manager_name:
            base_to_managers[base].add(r.manager_name)
        if r.ap_name:
            base_to_aps[base].add(r.ap_name)
        base_to_plans[base].append(r.plan_norm)
        num_to_plans[num].append(r.plan_norm)

    base_to_unique_manager = {
        b: list(s)[0] for b, s in base_to_managers.items() if len(s) == 1
    }

    # Detect plan tokens from text
    plan_re = _build_plan_text_regex(allowed_prefixes)
    detected: dict[str, int] = defaultdict(int)
    numbers_seen: set[str] = set()
    for m in plan_re.finditer(text_up):
        prefix, num, suf = m.group(1) or "", m.group(2) or "", (m.group(3) or "").strip()
        full = f"{prefix}{num}{suf}"
        base = f"{prefix}{num}"
        detected[full] += 1
        detected[base] += 1
        numbers_seen.add(num)

    # No-digit plans (e.g. GVCCA / TCLUB)
    for plan in plan_to_row:
        if any(c.isdigit() for c in plan):
            continue
        hits = re.findall(rf"\b{re.escape(plan)}\b", text_up)
        if hits:
            detected[plan] += len(hits)

    # Score
    scores: dict[str, int] = defaultdict(int)
    for tok, cnt in detected.items():
        if tok in plan_to_row:
            scores[tok] += 10 * cnt
            continue
        m = re.match(r"^([A-Z]{2,6})(\d{1,5})([A-Z]{1,3})$", tok)
        if m:
            base = f"{m.group(1)}{m.group(2)}"
            if base in plan_to_row:
                scores[base] += 9 * cnt
                continue
        m2 = re.match(r"^([A-Z]{2,6})(\d{1,5})$", tok)
        if m2:
            base = tok
            if base not in plan_to_row and base in base_to_unique_manager:
                # Synthesize a row that points to that manager for routing.
                # We don't have the exact PlanRow but we'll resolve via the
                # caller using the unique-manager map.
                scores[base] += 7 * cnt

    # Optional name-based fallback
    if enable_name_fallback:
        for num in numbers_seen:
            if any(num in k for k in scores):
                continue
            plans = num_to_plans.get(num, [])
            scored = sorted(
                ((p, _strata_name_score(plan_to_row[p], text_up)) for p in plans),
                key=lambda x: x[1],
                reverse=True,
            )
            scored = [s for s in scored if s[1] > 0]
            if not scored:
                continue
            top, second = scored[0], (scored[1] if len(scored) > 1 else None)
            clean_win = top[1] >= 6 and (second is None or top[1] >= second[1] + 3)
            if clean_win:
                scores[top[0]] += 6

    detected_sorted = sorted(detected.items(), key=lambda x: x[1], reverse=True)[:10]

    if not scores:
        return PdfMatchResult(
            "", None,
            "Detected plan text, but no match in list (or not safe to auto-pick).",
            detected_sorted,
        )

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    if second is None or top[1] >= second[1] + 3:
        plan_norm = top[0]
        row = plan_to_row.get(plan_norm)
        if row is None:
            # base-without-suffix fallback: take the first variant's row,
            # but override the manager/ap to the unique one if present.
            variants = base_to_plans.get(plan_norm, [])
            if variants:
                row = plan_to_row[variants[0]]
        return PdfMatchResult(plan_norm, row, "", detected_sorted)
    return PdfMatchResult(
        "", None,
        f"Ambiguous: {top[0]} vs {second[0]}. Leaving unmatched.",
        detected_sorted,
    )
