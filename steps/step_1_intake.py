"""Step 1 — Email intake.

Polls the testinvsml@stratacomgmt.com Inbox, identifies invoices by Strata
Plan in the email subject / body / PDF text, downloads PDF/ZIP attachments,
applies the Received stamp to PDFs that matched a manager, and routes
everything to the right folder under STRATACO_ROOT.

Matching order per email (first hit wins):
  1. Subject  — `pick_from_subject(subject)`
  2. Body     — `pick_from_subject(bodyPreview)` (reuses the same regex)
                Note: Graph's `bodyPreview` is the first ~255 chars of the
                message body. Plan numbers buried deeper in long emails
                won't be caught here; the PDF-text scan (step 3) is the
                fallback. A future enhancement could request the full
                `body` field and strip HTML before matching.
  3. PDF text — `match_from_pdf_text(extract_full_text(blob))`

Unidentified emails stay in the Inbox so the operator's reply-to-self
recovery loop works. A reply-to-self that lacks the PDF (because she hit
Reply without re-attaching it) is resolved via Microsoft Graph's
`conversationId`: Step 1 looks up the prior message in the same thread,
pulls its PDF, and stamps using the reply's matched plan.

Schedule: 06:00 Mon–Fri.

Replaces "Step 1 - Strataco Invoice - Subject Identification pdf and zip
Download and Sort - Claude - with Stamp" (N8n).
"""

from __future__ import annotations

import datetime as _dt
import enum
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make the project root importable when running from Task Scheduler
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import (
    dup_fingerprint,
    dup_ledger,
    graph,
    paths,
    plan_match,
    safe_io,
    strataplan_snapshot,
    zip_safe,
)
from tools._lib.log import daily_log
from tools._lib.pdf_text import extract_full_text
from tools._lib.stamp import render_received_stamp
from tools._lib.xls import PlanRow, load_plans, plan_to_manager

_STAMP = "step_1"

_PROCESSED_FOLDER_NAME = "processed_emails"
_DUPLICATE_FOLDER_NAME = "duplicate_emails"
_ACTION_REQUIRED_FOLDER_NAME = "Action_Required"


class RouteOutcome(enum.Enum):
    """Outcome of attempting to route a single PDF attachment through `_route_pdf`."""
    ROUTED = "routed"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    FAILED = "failed"


class PdfOutcome(enum.Enum):
    """Per-PDF cross-validation outcome when the subject already matched a plan.

    - AGREE: PDF text confidently identifies the same plan as the subject.
    - EMPTY: PDF has no extractable text (scanned image / no text layer).
      No evidence to contradict the subject; route on subject.
    - NO_PLAN: PDF has extractable text but contains no recognizable strata
      plan number at all (no plan-shaped token the matcher could detect). No
      evidence to contradict the subject; route on subject — same stance as
      EMPTY. This is the "vendor invoice that just never prints the plan
      number" case; flagging it would loop the front desk's reply-to-self
      recovery forever.
    - AMBIGUOUS: PDF text contains plan tokens but the matcher couldn't pick
      one safely — either an unmanaged plan number, or two equally-scored
      managed candidates. Don't trust either side; flag for review.
    - CLASH: PDF confidently identifies a *different* plan than the subject
      (only produced by the filename-fallback path; the PDF-text path now
      uses PDF_OVERRIDE instead).
    - PDF_OVERRIDE: PDF text confidently identifies a *different* plan than
      the subject AND the match is a direct managed-list hit (not a
      base-without-suffix fallback). The PDF is trusted over the subject:
      route to the PDF's plan rather than flagging.
    """
    AGREE = "agree"
    EMPTY = "empty"
    NO_PLAN = "no_plan"
    AMBIGUOUS = "ambiguous"
    CLASH = "clash"
    PDF_OVERRIDE = "pdf_override"


@dataclass
class PdfClassification:
    """Per-PDF classification result + the routing payload the caller needs."""
    outcome: PdfOutcome
    base_name: str
    blob: bytes
    # The plan the PDF claims (its top match), or "" when EMPTY/AMBIGUOUS.
    pdf_plan_norm: str = ""
    # The PlanRow for `pdf_plan_norm` if it's a known active plan with a manager,
    # else None. Required when we AUTO_SPLIT this PDF away from the subject's plan.
    pdf_plan_row: PlanRow | None = None
    # Diagnostic text from `match_from_pdf_text` (the `note` field) — used in logs.
    note: str = ""
    # Top detected tokens from the PDF, for diagnostic logging on clash/flag.
    detected: list[tuple[str, int]] = field(default_factory=list)


class EmailActionKind(enum.Enum):
    ROUTE_AS_SUBJECT = "route_as_subject"
    AUTO_SPLIT = "auto_split"
    FLAG_AND_HOLD = "flag_and_hold"


@dataclass
class EmailAction:
    kind: EmailActionKind
    # Human-readable reason for the action; used in info/error logs and (for
    # FLAG_AND_HOLD) in the daily-log line so operators can grep "why was this flagged?".
    reason: str = ""
    # Populated only when kind == AUTO_SPLIT. Maps PDF index (position in the
    # classification list) -> the PlanRow that PDF should be routed to.
    per_pdf_plan: dict[int, PlanRow] = field(default_factory=dict)


def _check_dup_status(
    blob: bytes,
    plan_norm: str,
    sender_domain: str,
    ledger: dup_ledger.Ledger,
) -> tuple[
    str,
    str,
    int | None,
    dup_ledger.FingerprintRow | None,
    dup_ledger.FingerprintRow | None,
]:
    """Compute fingerprint and consult the ledger. Pure — does NOT mutate.

    Returns `(sha256, invoice_number, amount_cents, duplicate_match,
    overridden_match)`:
      - `duplicate_match` is non-None when this fingerprint should be
        treated as a duplicate (blocked).
      - `overridden_match` is non-None when the fingerprint matched a
        row whose stage is `overridden` — the caller should route this
        arrival normally AND consume the override.

    Layer A (hash) and Layer B (plan + invoice# + amount_cents +
    sender_domain) are both checked. A blank `sender_domain` (only happens
    on non-email entry points; should not occur in this step) makes
    Layer B no-op.

    `find_by_hash()` returns rows regardless of stage; the filter below
    treats `overridden`-stage Layer A hits as not-a-duplicate (the
    operator has explicitly allowed this fingerprint through) but
    treats `superseded`-stage Layer A hits as a NORMAL duplicate — the
    old bytes were already processed by the pipeline before the override
    consumed the row, so a second arrival of those exact bytes is a real
    re-send.

    `find_by_semantic_key()` already excludes `overridden`/`superseded`
    rows from its active index. The override case is surfaced via the
    explicit `find_overridden_*` helpers so the caller can call
    `consume_override_and_insert` after a successful route.
    """
    sha = dup_fingerprint.sha256_of(blob)
    inv_num, amount = dup_fingerprint.compute_layer_b(blob, plan_norm)

    # Layer A duplicate check. Overridden -> route normally (override
    # consume); superseded -> treat as duplicate (the bytes were already
    # processed before the override fired); anything else -> duplicate.
    duplicate = ledger.find_by_hash(sha)
    if duplicate is not None and duplicate.current_stage == "overridden":
        duplicate = None
    if duplicate is None:
        # Layer B: semantic-key index already excludes overridden/superseded.
        duplicate = ledger.find_by_semantic_key(plan_norm, inv_num, amount, sender_domain)

    # Override checks (only consulted if no active duplicate found).
    overridden = None
    if duplicate is None:
        overridden = ledger.find_overridden_by_hash(sha)
        if overridden is None:
            overridden = ledger.find_overridden_by_semantic_key(
                plan_norm, inv_num, amount, sender_domain,
            )

    return sha, inv_num, amount, duplicate, overridden

# Heuristics from the N8n "9B" code: catch PDFs delivered as octet-stream with
# no extension when the subject/filename hints at an invoice.
_INVOICE_HINT_RE = re.compile(
    r"(invoice|inv\b|statement|bill|approved contractor|contractor invoice|remit|payment)",
    re.IGNORECASE,
)
_NON_FILE_TYPES = {"itemattachment", "referenceattachment"}


def _is_file_attachment(att: dict) -> bool:
    """True if `att` is a real downloadable file (not an item/reference attachment).

    Per workflows/step_1_intake.md, anything that isn't a PDF/ZIP still goes
    to `_Unmatched/Invoices/` so the operator can sort it manually — we just
    skip Graph's item/reference attachment types because they're pointers
    to other Graph objects, not files we can save.
    """
    odata_type = (att.get("@odata.type") or att.get("odataType") or "").lower()
    if any(t in odata_type for t in _NON_FILE_TYPES):
        return False
    return True


def _looks_like_pdf_or_zip(att: dict, email_subject: str) -> bool:
    """True when the attachment is plausibly an invoice PDF or invoice ZIP.

    Drives the manager-routing + stamping path. Non-PDF/ZIP file attachments
    fall through to `_Unmatched/Invoices/` instead of being dropped.
    """
    if not _is_file_attachment(att):
        return False

    name = str(att.get("name") or "").strip()
    ct = str(att.get("contentType") or "").lower()

    if re.search(r"\.(pdf|zip)(\b|$)", name, re.IGNORECASE):
        return True
    if "pdf" in ct or "zip" in ct or "x-zip-compressed" in ct or "compressed" in ct:
        return True

    no_ext = not re.search(r"\.[A-Za-z0-9]{1,8}$", name)
    is_unknown_type = (not ct) or "octet-stream" in ct
    if no_ext and is_unknown_type:
        hint = f"{name} {email_subject}"
        if _INVOICE_HINT_RE.search(hint):
            return True
    return False


def _ext_for_attachment(att: dict, email_subject: str) -> str:
    name = str(att.get("name") or "")
    m = re.search(r"(\.[A-Za-z0-9]{1,8})$", name)
    if m:
        return m.group(1).lower()
    ct = str(att.get("contentType") or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "zip" in ct or "compressed" in ct:
        return ".zip"
    # No extension hints — guess from subject/name
    if "zip" in (name + " " + email_subject).lower():
        return ".zip"
    return ".pdf"


def _is_real_pdf(blob: bytes) -> bool:
    """True iff the bytes start with the PDF magic number (`%PDF-`).

    Catches the case where an attachment is named `*.pdf` but its contents are
    actually PNG / JPEG / HTML / Word bytes (mobile scanner glitches, manual
    rename, vendor screw-up). Without this check, such a file is classified
    EMPTY (text extraction returns nothing), routes via the EMPTY-trusts-subject
    branch of `_decide_email_action`, and lands in the manager's queue with
    a `.pdf` name but unreadable contents.

    Scanner-made PDFs (image-only, no text layer) still pass — the PDF wrapper
    has the magic header regardless of what's inside.
    """
    return blob[:5] == b"%PDF-"


def _today_received_str() -> str:
    # The N8n flow uses America/Vancouver MMM dd yyyy uppercase
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    return now.strftime("%b %d %Y").upper()


def _find_priors_with_attachments(
    conversation_id: str,
    exclude_ids: set[str],
    own_received_iso: str,
    run,
) -> list[dict]:
    """Return prior messages in `conversation_id` that have attachments, newest first.

    `exclude_ids` skips the reply itself plus any prior already consumed earlier in
    this run (prevents double-routing when there are multiple corrected replies
    against the same original).

    Returns an empty list on any error (logged via `run.error`). Graph
    failures are NOT silently swallowed — the caller and the operator need to
    see them.
    """
    if not conversation_id:
        return []
    try:
        msgs = graph.list_conversation_messages(conversation_id)
    except Exception as exc:
        run.error(
            f"conversation lookup failed for conv_id="
            f"{conversation_id[:12]}...: {exc}"
        )
        return []
    candidates = [
        m for m in msgs
        if m.get("id") not in exclude_ids
        and bool(m.get("hasAttachments"))
        and str(m.get("receivedDateTime") or "") < own_received_iso
    ]
    candidates.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    return candidates


def _pick_plan(
    subject: str,
    body_preview: str,
    plan_map: dict[str, PlanRow],
) -> tuple[PlanRow | None, str]:
    """Run subject then body matching. Returns (plan_row_or_None, match_source)."""
    _, row = plan_match.pick_from_subject(subject, plan_map)
    if row is not None:
        return row, "subject"
    _, row = plan_match.pick_from_subject(body_preview, plan_map)
    if row is not None:
        return row, "body"
    return None, ""


def _route_pdf(
    blob: bytes,
    base_name: str,
    plan_row: PlanRow,
    received_str: str,
    sender_domain: str,
    ledger: dup_ledger.Ledger,
    run,
) -> RouteOutcome:
    """Stamp and write a matched PDF to the manager's To_Approve folder.

    Consults the duplicate-detection ledger before stamping. If this fingerprint
    has been seen before (and is not overridden), increments `dup_count` and
    returns `DUPLICATE_SKIPPED` — nothing is written to disk. Otherwise the
    file is stamped, written, and a `manager_queue` ledger row is upserted.

    Returns `ROUTED` on a fresh route, `DUPLICATE_SKIPPED` on a known duplicate,
    `FAILED` on a genuine failure (bad plan_row, write error, etc.). Failure is
    logged.
    """
    if not plan_row.manager_name:
        # Active plan with no manager assigned — can't build a path. Don't crash
        # the whole step; log and treat as unroutable for this caller to handle.
        run.error(
            f"plan {plan_row.plan_norm} has no manager_name in the snapshot — "
            f"cannot route '{base_name}'. Fix Strataplan_List.xlsx."
        )
        return RouteOutcome.FAILED

    # Duplicate-detection check BEFORE any disk write.
    sha, inv_num, amount, duplicate, overridden = _check_dup_status(
        blob, plan_row.plan_norm, sender_domain, ledger
    )
    if duplicate is not None:
        try:
            updated = ledger.increment_dup_count(duplicate.sha256)
            archive_hint = duplicate.archive_path or f"({duplicate.current_stage})"
            run.info(
                f"duplicate skipped: {base_name} "
                f"(sha={sha[:12]}..., matches {duplicate.sha256[:12]}..., "
                f"original at {archive_hint}, dup_count={updated.dup_count})"
            )
        except Exception as exc:
            run.error(f"ledger increment failed for {sha[:12]}...: {exc}")
        return RouteOutcome.DUPLICATE_SKIPPED

    plan_pretty = plan_match.pretty_plan(plan_row.plan_norm)
    prefix = f"{plan_pretty} - "
    if base_name.upper().startswith(plan_row.plan_norm):
        out_name = safe_io.sanitize_filename(base_name)
    else:
        out_name = safe_io.sanitize_filename(prefix + base_name)
    try:
        dest_dir = paths.manager_to_approve(plan_row.manager_name)
    except Exception as exc:
        run.error(
            f"could not build manager folder for {plan_row.manager_name!r}: {exc}"
        )
        return RouteOutcome.FAILED
    dest_path = dest_dir / out_name

    try:
        stamped = render_received_stamp(
            blob,
            received_date=received_str,
            plan_pretty=plan_pretty,
        )
    except Exception as exc:
        run.error(f"stamp failed for {out_name}: {exc} — saving unstamped")
        stamped = blob

    try:
        written = safe_io.safe_write_unique(dest_path, stamped)
        if written != dest_path:
            run.info(f"saved {written} (collision-renamed from {dest_path.name})")
        else:
            run.info(f"saved {written}")
    except Exception as exc:
        run.error(f"write failed for {dest_path}: {exc}")
        return RouteOutcome.FAILED

    # Successful route — upsert fingerprint row so a same-day or future
    # duplicate of this PDF gets caught. If we matched an override row via
    # Layer B (different bytes, same semantic key), atomically retire that
    # row (one-shot override semantics) and insert the new fingerprint.
    new_row = dup_ledger.make_row(
        sha256=sha,
        plan_norm=plan_row.plan_norm,
        invoice_number=inv_num,
        amount_cents=amount,
        sender_domain=sender_domain,
        current_stage="manager_queue",
    )
    try:
        if overridden is not None and overridden.sha256 != sha:
            try:
                ledger.consume_override_and_insert(
                    old_sha256=overridden.sha256,
                    new_row=new_row,
                )
                run.info(
                    f"consumed Layer B override for {base_name} "
                    f"(retired {overridden.sha256[:12]}..., inserted {sha[:12]}...)"
                )
            except ValueError as exc:
                # Override was already consumed by another transaction —
                # fall back to a plain upsert so the new sha still gets
                # a row.
                run.info(
                    f"override at {overridden.sha256[:12]}... already consumed "
                    f"({exc}); inserting {sha[:12]}... as new"
                )
                ledger.upsert(new_row)
        else:
            ledger.upsert(new_row)
    except Exception as exc:
        run.error(f"ledger upsert failed for {sha[:12]}... ({base_name}): {exc}")
        # Don't fail the overall route — the PDF is already in the manager folder
        # and the ledger miss just means a future duplicate may slip through.
    return RouteOutcome.ROUTED


def _classify_pdf_against_subject(
    blob: bytes,
    base_name: str,
    subject_plan_norm: str,
    rows: list[PlanRow],
) -> PdfClassification:
    """Cross-validate a single PDF against the plan the subject matched.

    Evidence priority — PDF TEXT FIRST, filename as fallback. The body of the
    PDF is the most authoritative signal; the filename is only consulted when
    the body doesn't yield a confident match. This catches the "vendor renamed
    a BCS 3396 invoice to EPS 6008 invoice.pdf to match the wrong subject"
    case, while still letting filename rescue scans / OCR-marginal PDFs whose
    text matcher refuses to pick.

    Pure: no disk writes, no Graph calls, no ledger mutation. Reuses the
    in-memory `blob` we already downloaded; one pdfplumber pass to extract text.

    The four outcomes feed `_decide_email_action`. See PdfOutcome for semantics.
    """
    # Pass 1: PDF text (primary evidence).
    text = extract_full_text(blob)
    result = plan_match.match_from_pdf_text(text, rows)

    # Only treat a PDF match as "confident" when it points at an active row
    # that has a manager assigned (otherwise we couldn't route to it anyway).
    confident_row = result.plan_row if (
        result.plan_row is not None and result.plan_row.manager_name
    ) else None

    if confident_row is not None:
        if result.is_base_fallback:
            # PDF names the base plan (e.g. "BCS 2707") but the managed list only
            # has suffix variants (e.g. "BCS 2707A", "BCS 2707B"). Can't determine
            # which variant — flag for front-desk correction.
            return PdfClassification(
                outcome=PdfOutcome.AMBIGUOUS,
                base_name=base_name,
                blob=blob,
                pdf_plan_norm=confident_row.plan_norm,
                pdf_plan_row=confident_row,
                note=(
                    f"PDF identifies base plan "
                    f"{plan_match.pretty_plan(confident_row.plan_norm)} only; "
                    f"variant is ambiguous — flagged for front-desk correction."
                ),
                detected=result.detected,
            )
        if confident_row.plan_norm == subject_plan_norm:
            outcome = PdfOutcome.AGREE
        else:
            outcome = PdfOutcome.PDF_OVERRIDE
        return PdfClassification(
            outcome=outcome,
            base_name=base_name,
            blob=blob,
            pdf_plan_norm=confident_row.plan_norm,
            pdf_plan_row=confident_row,
            note=result.note,
            detected=result.detected,
        )

    # Pass 2: Filename fallback. PDF text gave us nothing usable (empty layer,
    # ambiguous, or matcher's safety guard refused to pick). Trust the filename
    # when it cleanly identifies an active managed plan — vendors put plan IDs
    # in filenames intentionally (statements, multi-month chase emails).
    fn_row = plan_match.match_from_filename_with_base_fallback(base_name, rows)
    if fn_row is not None and fn_row.manager_name:
        if fn_row.plan_norm == subject_plan_norm:
            outcome = PdfOutcome.AGREE
        else:
            outcome = PdfOutcome.CLASH
        return PdfClassification(
            outcome=outcome,
            base_name=base_name,
            blob=blob,
            pdf_plan_norm=fn_row.plan_norm,
            pdf_plan_row=fn_row,
            note=(
                f"matched by filename ({plan_match.pretty_plan(fn_row.plan_norm)}); "
                f"PDF text: {result.note}"
            ),
            detected=[(base_name, 1)],
        )

    # Pass 3: Neither PDF text nor filename gave a confident match. Three
    # sub-cases, distinguished so the email-level decision and the operator
    # logs can treat them differently:
    #   - EMPTY: no text layer at all (scanned image).
    #   - NO_PLAN: text exists but carries no strata plan number — neither a
    #     managed-plan token (`result.detected`) NOR a token the document
    #     explicitly labels "Strata Plan ..." (`find_explicit_plan_tokens`).
    #     No evidence to contradict the subject, so route on subject (same
    #     stance as EMPTY). Flagging this would loop the reply-to-self recovery.
    #   - AMBIGUOUS: text names a plan but it didn't resolve — a managed-prefix
    #     token the matcher couldn't pick, a genuine tie between two managed
    #     plans, OR a plan the PDF explicitly names that isn't in the managed
    #     list (e.g. "Strata Plan KAS 9999"). Strict-first: flag for review.
    #     `result.detected` only covers managed prefixes, so the explicit-
    #     wording scan is what catches unmanaged plans.
    explicit_tokens = plan_match.find_explicit_plan_tokens(text)
    note_low = (result.note or "").lower()
    if note_low.startswith("no text extracted"):
        outcome = PdfOutcome.EMPTY
        note = result.note
    elif not result.detected and not explicit_tokens:
        outcome = PdfOutcome.NO_PLAN
        note = "No strata plan number found in PDF text; routing on subject."
    else:
        outcome = PdfOutcome.AMBIGUOUS
        if not result.detected:
            note = (
                f"PDF names strata plan(s) not in the managed list: "
                f"{', '.join(explicit_tokens)}; flagged for review."
            )
        else:
            note = result.note
    return PdfClassification(
        outcome=outcome,
        base_name=base_name,
        blob=blob,
        pdf_plan_norm="",
        pdf_plan_row=None,
        note=note,
        detected=result.detected,
    )


def _decide_email_action(
    subject_plan_norm: str,
    classifications: list[PdfClassification],
) -> EmailAction:
    """Combine per-PDF classifications into a single email-level routing action.

    Decision matrix (see workflows/step_1_intake.md):

      - all AGREE                             → ROUTE_AS_SUBJECT
      - lone PDF, EMPTY or NO_PLAN            → ROUTE_AS_SUBJECT
      - multi-PDF, any NO_PLAN                → ROUTE_AS_SUBJECT (NO_PLAN PDFs
                                                are skipped in the routing loop;
                                                caller forwards the email to the
                                                manager so they see the extras)
      - any AMBIGUOUS                         → FLAG (strict-first)
      - any EMPTY/NO_PLAN mixed with any CLASH → FLAG (can't safely route it)
      - all PDFs confident, ≥1 CLASH:
        - all PDF plans equal (consensus)     → FLAG (subject vs unanimous PDFs)
        - multiple unique plans, shared base  → FLAG (suffix-variant failsafe)
        - multiple unique plans, distinct bases → AUTO_SPLIT (subject ignored)

    Notes:
      * "Confident" here means the PDF returned an active plan with a manager
        (PdfOutcome.AGREE or PdfOutcome.CLASH). EMPTY, NO_PLAN, and AMBIGUOUS
        are not confident.
      * EMPTY and NO_PLAN both route on the subject (lone or multi-PDF). In a
        multi-PDF email a NO_PLAN PDF is not stamped or routed — the caller
        skips it in the routing loop and forwards the whole email to the plan
        manager instead. The base-collision check covers the subject's plan
        implicitly: if a PDF's plan AGREES, its plan_norm equals
        subject_plan_norm, and that plan appears in `unique_plans`, so the
        base check catches a "subject plan and another PDF plan share a base"
        scenario too.
    """
    if not classifications:
        # No PDFs at all — degenerate but possible if caller passed a zip-only
        # email through. Route as subject; the existing non-PDF handler still
        # parks the zip in _Unmatched/.
        return EmailAction(EmailActionKind.ROUTE_AS_SUBJECT, "no PDFs to classify")

    outcomes = [c.outcome for c in classifications]

    _CLEAN_OUTCOMES = frozenset({
        PdfOutcome.AGREE, PdfOutcome.EMPTY, PdfOutcome.NO_PLAN, PdfOutcome.PDF_OVERRIDE,
    })

    if all(o in _CLEAN_OUTCOMES for o in outcomes):
        overridden = [c for c in classifications if c.outcome == PdfOutcome.PDF_OVERRIDE]

        if not overridden:
            # All AGREE / EMPTY / NO_PLAN — existing behaviour.
            agreed = sum(1 for o in outcomes if o == PdfOutcome.AGREE)
            empty = sum(1 for o in outcomes if o == PdfOutcome.EMPTY)
            no_plan = sum(1 for o in outcomes if o == PdfOutcome.NO_PLAN)
            return EmailAction(
                EmailActionKind.ROUTE_AS_SUBJECT,
                f"PDF cross-check OK ({agreed} agree, {empty} empty, "
                f"{no_plan} no plan # in PDF — routed under subject)",
            )

        # At least one PDF_OVERRIDE (PDF trusted over subject).
        # Check for suffix-variant ambiguity among the confident PDFs first.
        confident_pdfs = [
            c for c in classifications
            if c.outcome in (PdfOutcome.AGREE, PdfOutcome.PDF_OVERRIDE)
        ]
        unique_plans = sorted({c.pdf_plan_norm for c in confident_pdfs})
        bases = [plan_match.plan_base(p) for p in unique_plans]
        if len(set(bases)) < len(unique_plans):
            plan_summary = ", ".join(plan_match.pretty_plan(p) for p in unique_plans)
            return EmailAction(
                EmailActionKind.FLAG_AND_HOLD,
                f"suffix-variant clash across PDFs: {plan_summary} share a base",
            )

        # Build per_pdf_plan for every confident PDF so the routing loop uses
        # the right plan_row regardless of action.kind.
        per_pdf_plan: dict[int, PlanRow] = {}
        for idx, c in enumerate(classifications):
            if c.outcome in (PdfOutcome.AGREE, PdfOutcome.PDF_OVERRIDE):
                if c.pdf_plan_row is None:
                    return EmailAction(
                        EmailActionKind.FLAG_AND_HOLD,
                        f"internal: missing pdf_plan_row for {c.base_name} "
                        f"during PDF-override routing — flagging instead of risking misroute",
                    )
                per_pdf_plan[idx] = c.pdf_plan_row

        if len(unique_plans) <= 1:
            # All confident PDFs agree on one plan (the PDF's plan).
            plan_label = (
                plan_match.pretty_plan(unique_plans[0]) if unique_plans
                else plan_match.pretty_plan(subject_plan_norm)
            )
            return EmailAction(
                EmailActionKind.ROUTE_AS_SUBJECT,
                f"PDF overrides subject: routing to {plan_label} (PDF evidence trusted)",
                per_pdf_plan=per_pdf_plan,
            )

        # Multiple distinct-base plans across confident PDFs — AUTO_SPLIT.
        plan_summary = ", ".join(plan_match.pretty_plan(p) for p in unique_plans)
        return EmailAction(
            EmailActionKind.AUTO_SPLIT,
            f"auto-split across distinct strata bases: {plan_summary} "
            f"(PDF evidence trusted; subject {plan_match.pretty_plan(subject_plan_norm)} "
            f"may be partial)",
            per_pdf_plan=per_pdf_plan,
        )

    if any(o == PdfOutcome.AMBIGUOUS for o in outcomes):
        amb_names = [c.base_name for c in classifications if c.outcome == PdfOutcome.AMBIGUOUS]
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"ambiguous PDF text: {', '.join(amb_names)}",
        )

    # At this point: every PDF is AGREE / EMPTY / NO_PLAN / CLASH, and at least
    # one CLASH. An EMPTY or NO_PLAN PDF carries no plan of its own, so it can't
    # be safely routed when a sibling PDF clashes with the subject.
    if any(o in (PdfOutcome.EMPTY, PdfOutcome.NO_PLAN) for o in outcomes):
        no_evidence_names = [
            c.base_name for c in classifications
            if c.outcome in (PdfOutcome.EMPTY, PdfOutcome.NO_PLAN)
        ]
        clash_summary = [
            f"{c.base_name}->{c.pdf_plan_norm}"
            for c in classifications if c.outcome == PdfOutcome.CLASH
        ]
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"mixed evidence: PDF(s) {', '.join(no_evidence_names)} carry no "
            f"plan while PDF(s) {', '.join(clash_summary)} clash with subject "
            f"{plan_match.pretty_plan(subject_plan_norm)}",
        )

    # Every PDF is confident (AGREE or CLASH), at least one CLASH.
    unique_plans = sorted({c.pdf_plan_norm for c in classifications})

    if len(unique_plans) == 1:
        # All PDFs agree on a single plan that differs from the subject's plan
        # (otherwise we'd have been in the all-AGREE branch above).
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"consensus clash: every PDF says "
            f"{plan_match.pretty_plan(unique_plans[0])}, subject says "
            f"{plan_match.pretty_plan(subject_plan_norm)}",
        )

    # Multiple distinct plans across PDFs. Check for suffix variants of the
    # same base — vendors confuse these (LMS4193C vs LMS4193T, EPS4280 vs
    # EPS4280A). Strict-first: flag instead of auto-routing.
    bases = [plan_match.plan_base(p) for p in unique_plans]
    if len(set(bases)) < len(unique_plans):
        plan_summary = ", ".join(
            f"{plan_match.pretty_plan(p)}" for p in unique_plans
        )
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"suffix-variant clash across PDFs: {plan_summary} share a base",
        )

    # Distinct bases — safe to auto-split each PDF to its own plan.
    per_pdf_plan: dict[int, PlanRow] = {}
    for idx, c in enumerate(classifications):
        # Every PDF here is AGREE or CLASH, so pdf_plan_row is set.
        if c.pdf_plan_row is None:
            # Defensive: should never happen given the filters above.
            return EmailAction(
                EmailActionKind.FLAG_AND_HOLD,
                f"internal: missing pdf_plan_row for {c.base_name} "
                f"(plan {c.pdf_plan_norm}) during auto-split — flagging instead of risking misroute",
            )
        per_pdf_plan[idx] = c.pdf_plan_row

    plan_summary = ", ".join(plan_match.pretty_plan(p) for p in unique_plans)
    return EmailAction(
        EmailActionKind.AUTO_SPLIT,
        f"auto-split across distinct strata bases: {plan_summary} (subject "
        f"{plan_match.pretty_plan(subject_plan_norm)} ignored)",
        per_pdf_plan=per_pdf_plan,
    )


def _format_classification_for_log(c: PdfClassification) -> str:
    """Compact representation used in flag/clash log lines."""
    parts = [f"{c.base_name}:{c.outcome.value}"]
    if c.pdf_plan_norm:
        parts.append(f"pdf={c.pdf_plan_norm}")
    if c.note:
        parts.append(f"note={c.note!r}")
    if c.detected:
        top = ", ".join(f"{tok}x{cnt}" for tok, cnt in c.detected[:3])
        parts.append(f"detected=[{top}]")
    return " ".join(parts)


def main() -> int:
    with daily_log(_STAMP) as run:
        if run.status == "skipped":
            return 0

        try:
            snapshot = strataplan_snapshot.refresh_snapshot()
            run.info(f"snapshot refreshed: {snapshot.name}")
        except strataplan_snapshot.SnapshotRefreshError as exc:
            run.error(f"snapshot refresh failed — halting day: {exc}")
            return 1

        rows = load_plans(snapshot)
        plan_map = plan_to_manager(rows)
        run.info(f"loaded {len(plan_map)} unique plans from snapshot")

        try:
            messages = graph.list_inbox_messages(top=500)
        except Exception as exc:
            run.error(f"failed to list inbox messages: {exc}")
            return 1
        run.info(f"fetched {len(messages)} inbox messages")
        initial_inbox_ids: set[str] = {str(m.get("id") or "") for m in messages}
        initial_inbox_ids.discard("")

        processed_folder_id = graph.find_child_folder_id("Inbox", _PROCESSED_FOLDER_NAME)
        if not processed_folder_id:
            run.error(
                f"Inbox subfolder '{_PROCESSED_FOLDER_NAME}' not found — "
                f"create it and re-run. Skipping mail moves."
            )

        duplicate_folder_id = graph.find_child_folder_id("Inbox", _DUPLICATE_FOLDER_NAME)
        if not duplicate_folder_id:
            run.error(
                f"Inbox subfolder '{_DUPLICATE_FOLDER_NAME}' not found — "
                f"create it and re-run. Duplicate emails will stay in Inbox "
                f"until then; ledger still updates."
            )

        action_required_folder_id = graph.find_child_folder_id("Inbox", _ACTION_REQUIRED_FOLDER_NAME)
        if not action_required_folder_id:
            run.warn(
                f"Inbox subfolder '{_ACTION_REQUIRED_FOLDER_NAME}' not found — "
                f"create it in OWA and re-run. Inbox sweep will be skipped."
            )

        # Duplicate-detection ledger — loaded once at startup, mutated in
        # place by `_route_pdf` and the operator-tool CLIs.
        try:
            ledger = dup_ledger.load()
            run.info(f"dup ledger loaded: {len(ledger.all_rows())} fingerprints on file")
        except ValueError as exc:
            run.error(f"dup ledger corrupted — halting day: {exc}")
            return 1

        received_str = _today_received_str()
        # Prior-message IDs consumed via the conversation-link path. Membership
        # (regardless of value) means "don't re-route this prior's PDFs" — Step 1
        # must not route the same PDF again when it iterates to the original
        # message in the same run. The value carries the source email's move
        # disposition:
        #   None        -> the reply branch already moved the source out of the
        #                  Inbox; the outer-loop skip branch just logs + skips.
        #   <folder id> -> the reply branch committed the source's PDFs but its
        #                  Outlook move FAILED; the skip branch retries the move
        #                  once to that folder.
        consumed_priors: dict[str, str | None] = {}
        # Track prior-message IDs that the conversation-link path *flagged* (a
        # later reply pointed at this prior, but its PDFs clashed with the
        # reply's plan). The reply already has the red flag; the prior must
        # NOT be processed independently in this same run or its own subject/
        # body/PDF-text could route it under a different plan. Quarantine in
        # the outer loop: skip processing AND do NOT move — both messages
        # stay in the Inbox so the operator sees the whole thread.
        flagged_prior_ids: set[str] = set()

        for msg in messages:
            msg_id = msg.get("id") or ""
            if msg_id in consumed_priors:
                retry_folder_id = consumed_priors[msg_id]
                subject_preview = (msg.get("subject") or "")[:60]
                if retry_folder_id is None:
                    # The reply branch already moved this prior out of the Inbox,
                    # to its correct destination folder. Re-moving here would be
                    # a guaranteed double-move (the message is gone, Graph 404s).
                    run.info(
                        f"skip: '{subject_preview}' already routed and filed via "
                        f"conversation-link from a later reply in this run"
                    )
                else:
                    # The reply branch committed this prior's PDFs but its
                    # Outlook move failed — retry once so the source isn't left
                    # stranded in the Inbox.
                    try:
                        graph.move_message_to_folder(msg_id, retry_folder_id)
                        consumed_priors[msg_id] = None
                        run.info(
                            f"recovered: moved stranded conversation-link source "
                            f"'{subject_preview}' to its target folder on retry"
                        )
                    except Exception as exc:
                        run.error(
                            f"retry move of stranded conversation-link source "
                            f"'{subject_preview}' failed: {exc} — leaving in Inbox"
                        )
                continue
            if msg_id in flagged_prior_ids:
                run.info(
                    f"skip: '{(msg.get('subject') or '')[:60]}' was already "
                    f"flagged as a clash via conversation-link in this run; "
                    f"leaving in Inbox alongside its reply"
                )
                continue

            subject = str(msg.get("subject") or "")
            body_preview = str(msg.get("bodyPreview") or "")
            has_atts = bool(msg.get("hasAttachments"))
            conv_id = str(msg.get("conversationId") or "")
            received_iso = str(msg.get("receivedDateTime") or "")
            sender_domain = dup_fingerprint.extract_domain(msg.get("from"))

            plan_row, match_source = _pick_plan(subject, body_preview, plan_map)

            # Branch A: matched + has attachments → process this email's PDFs.
            # Branch B: matched + no attachments → pull from conversation thread.
            # Branch C: no match + has attachments → PDF text fallback.
            # Branch D: no match + no attachments → skip.

            if plan_row and has_atts:
                _process_self_attachments(
                    msg_id=msg_id,
                    subject=subject,
                    plan_row=plan_row,
                    match_source=match_source,
                    received_str=received_str,
                    sender_domain=sender_domain,
                    rows=rows,
                    ledger=ledger,
                    run=run,
                    processed_folder_id=processed_folder_id,
                    duplicate_folder_id=duplicate_folder_id,
                )

            elif plan_row and not has_atts:
                # Reply-to-self with no attachment: pull the PDF from a prior
                # message in the same conversation. Walk through priors newest
                # first; the first one we successfully route (or duplicate-skip)
                # from wins.
                exclude_ids = set(consumed_priors) | {msg_id}
                priors = _find_priors_with_attachments(
                    conv_id, exclude_ids, received_iso, run
                )
                if not priors:
                    run.review(
                        f"matched '{subject[:60]}' via {match_source} but no eligible "
                        f"prior message in conversation — leaving in Inbox"
                    )
                    continue

                used_prior_id: str | None = None
                used_prior_outcomes: list[RouteOutcome] = []
                flagged_via_prior = False
                for prior in priors:
                    prior_id = str(prior.get("id") or "")
                    if not prior_id:
                        continue
                    prior_sender_domain = dup_fingerprint.extract_domain(prior.get("from"))
                    result = _process_prior_attachments(
                        prior_msg_id=prior_id,
                        reply_subject=subject,
                        plan_row=plan_row,
                        match_source=match_source,
                        received_str=received_str,
                        prior_sender_domain=prior_sender_domain,
                        rows=rows,
                        ledger=ledger,
                        run=run,
                    )
                    if result.flagged:
                        # Strict-first: the prior we just walked to had PDFs
                        # whose plans disagree with the reply. Don't walk
                        # further priors (the first one we found is what the
                        # operator effectively pointed at). Flag the reply so
                        # the red flag shows up in their Inbox.
                        _flag_message_safely(msg_id, subject, run)
                        # Quarantine the prior so the outer loop doesn't
                        # later process it independently (it's already been
                        # classified; processing again could route its PDFs
                        # under a different plan if its own subject matches).
                        flagged_prior_ids.add(prior_id)
                        flagged_via_prior = True
                        break
                    # We "consume" a prior if any of its attachments produced
                    # a definitive outcome (routed or duplicate-skipped).
                    # Genuine failures advance to the next prior — UNLESS at
                    # least one PDF already committed (partial commit). In
                    # that case the prior's PDFs are partly downstream
                    # already; we don't want a later outer-loop iteration to
                    # move the source into `processed_emails` and hide the
                    # failure. Treat it like a clash: flag the reply and stop
                    # walking, but do NOT record it in `consumed_priors`.
                    any_committed = any(
                        o in (RouteOutcome.ROUTED, RouteOutcome.DUPLICATE_SKIPPED)
                        for o in result.outcomes
                    )
                    any_failed = any(o == RouteOutcome.FAILED for o in result.outcomes)
                    if any_committed and any_failed:
                        run.error(
                            f"partial commit on conversation-link reply "
                            f"'{subject[:60]}' (prior {prior_id[:12]}...): some "
                            f"PDFs committed before a failure. Flagging the reply; "
                            f"leaving both reply and source in Inbox. The "
                            f"committed PDFs are downstream and in the duplicate "
                            f"ledger — no rollback."
                        )
                        _flag_message_safely(msg_id, subject, run)
                        # Quarantine the prior in the outer loop for the
                        # same reason as the clash-flag path above.
                        flagged_prior_ids.add(prior_id)
                        flagged_via_prior = True
                        break
                    if any_committed:
                        # Record in `consumed_priors` *after* the move attempt
                        # below — its disposition value depends on whether the
                        # source move succeeds.
                        used_prior_id = prior_id
                        used_prior_outcomes = result.outcomes
                        break

                if flagged_via_prior:
                    # Reply stays in Inbox (with a red flag); prior stays
                    # wherever it was. Don't move either.
                    continue

                if used_prior_id is None:
                    run.review(
                        f"matched '{subject[:60]}' via {match_source} but none of the "
                        f"{len(priors)} prior message(s) in the conversation had a "
                        f"routable PDF — leaving in Inbox"
                    )
                    continue

                # Both the reply and the consumed prior get moved to the same
                # folder (processed_emails or duplicate_emails) based on the
                # prior's per-attachment outcomes.
                target_folder_id = _email_destination(
                    used_prior_outcomes, processed_folder_id, duplicate_folder_id
                )
                # `used_prior_outcomes` holds only ROUTED / DUPLICATE_SKIPPED
                # here (the any_failed case broke out earlier), so
                # `_email_destination` never returns None on this path — the
                # guard below stays for defensiveness.
                source_retry_folder_id: str | None = None
                if target_folder_id is not None:
                    for target_msg_id, label in (
                        (msg_id, "reply"),
                        (used_prior_id, "source"),
                    ):
                        try:
                            graph.move_message_to_folder(target_msg_id, target_folder_id)
                        except Exception as exc:
                            run.error(
                                f"move {label} msg {target_msg_id[:12]}... to "
                                f"target folder failed: {exc}"
                            )
                            if label == "source":
                                source_retry_folder_id = target_folder_id
                # Mark the prior consumed so the outer loop doesn't re-route its
                # PDFs. None -> source filed OK (skip it); a folder id -> source
                # move failed, the skip branch retries the move there.
                consumed_priors[used_prior_id] = source_retry_folder_id

            elif not plan_row and has_atts:
                _process_pdf_text_fallback(
                    msg_id=msg_id,
                    subject=subject,
                    received_str=received_str,
                    sender_domain=sender_domain,
                    rows=rows,
                    ledger=ledger,
                    run=run,
                    processed_folder_id=processed_folder_id,
                    duplicate_folder_id=duplicate_folder_id,
                )

            else:
                # No match, no attachments — non-actionable.
                if subject:
                    run.info(f"skip: '{subject[:60]}' — no plan match, no attachments")

        if action_required_folder_id:
            _sweep_inbox_to_action_required(action_required_folder_id, run, initial_inbox_ids)

    return 0


def _sweep_inbox_to_action_required(folder_id: str, run, initial_ids: set[str]) -> int:
    """Move every originally-fetched Inbox message still present to Action_Required.

    Uses a re-fetch so messages already moved during the processing loop are
    naturally absent. Filters to `initial_ids` so emails that arrived after
    the initial fetch are left in the Inbox root for the next run.
    Returns the number successfully moved.
    """
    try:
        remaining = graph.list_inbox_messages(top=500)
    except Exception as exc:
        run.error(f"sweep: failed to list inbox messages: {exc}")
        return 0
    moved = 0
    for msg in remaining:
        msg_id = msg.get("id") or ""
        if not msg_id or msg_id not in initial_ids:
            continue  # new arrival or blank id — leave it for the next run
        try:
            graph.move_message_to_folder(msg_id, folder_id)
            moved += 1
        except Exception as exc:
            subject_preview = str(msg.get("subject") or "")[:60]
            run.error(f"sweep: failed to move '{subject_preview}' → Action_Required: {exc}")
    run.info(f"sweep: {moved} inbox message(s) → Action_Required")
    return moved


def _email_destination(
    outcomes: list[RouteOutcome],
    processed_folder_id: str | None,
    duplicate_folder_id: str | None,
) -> str | None:
    """Pick the right Outlook folder for an email given its per-attachment outcomes.

    Returns None when the email should stay in the Inbox (any genuine failure,
    or no outcomes at all).
    """
    if not outcomes:
        return None
    if any(o == RouteOutcome.FAILED for o in outcomes):
        return None
    any_routed = any(o == RouteOutcome.ROUTED for o in outcomes)
    any_dup = any(o == RouteOutcome.DUPLICATE_SKIPPED for o in outcomes)
    if not any_routed and any_dup:
        return duplicate_folder_id
    return processed_folder_id


def _flag_message_safely(msg_id: str, subject: str, run) -> bool:
    """Set the Outlook to-do flag on a message. Returns True on success.

    The email already stays in the Inbox in every caller's branch, so a failed
    flag-set doesn't lose data — but it does destroy the *visual signal* the
    operator is trained to look for. A clash that should have a red flag and
    doesn't looks like a normal unmatched email in the daily review.

    On failure: log loudly with a `FLAG_SET_FAILED:` prefix so admins can grep
    the daily log for degradations. Return False so the caller can take
    follow-up action (e.g. mention in a summary email, count toward a daily
    counter) — current callers don't act on the return yet, but the surface
    is wired up for it.
    """
    try:
        graph.flag_message(msg_id)
        return True
    except Exception as exc:
        run.error(
            f"FLAG_SET_FAILED: '{subject[:60]}' (msg {msg_id[:12]}...) could not be "
            f"flagged in Outlook: {exc}. This email will sit in the Inbox WITHOUT "
            f"a red flag and look like an ordinary unmatched email. Investigate "
            f"Graph permissions / Mail.ReadWrite scope."
        )
        return False


def _process_self_attachments(
    *,
    msg_id: str,
    subject: str,
    plan_row: PlanRow,
    match_source: str,
    received_str: str,
    sender_domain: str,
    rows: list[PlanRow],
    ledger: dup_ledger.Ledger,
    run,
    processed_folder_id: str | None,
    duplicate_folder_id: str | None,
) -> None:
    """Matched email with its own attachments — cross-validate, then stamp/route or flag.

    Flow:
      1. List + filter attachments. Track any non-PDF/ZIP that gets discarded.
      2. For each PDF: download the bytes and classify against the subject's plan.
         For non-PDFs: defer (still get parked in _Unmatched/ on route paths).
      3. Decide an email-level action via `_decide_email_action`.
         - ROUTE_AS_SUBJECT: stamp every AGREE/EMPTY PDF with the subject's plan.
           NO_PLAN PDFs in a multi-attachment email are skipped (not stamped).
         - AUTO_SPLIT: stamp each PDF with its own PDF-detected plan; subject ignored.
         - FLAG_AND_HOLD: set Outlook to-do flag, write nothing to disk, leave the
           email in the Inbox so the operator's red-flag review surfaces it.
      4. If extras were present (discarded non-PDFs or NO_PLAN PDF siblings) and
         routing had no failures, forward the full original email to the plan
         manager and move to processed_emails.
    """
    try:
        attachments = graph.list_attachments(msg_id)
    except Exception as exc:
        run.error(f"list_attachments({msg_id}) failed: {exc}")
        return

    kept = [a for a in attachments if _is_file_attachment(a)]
    if not kept:
        run.info(f"skip: '{subject[:60]}' has no downloadable attachments")
        return

    # Pass 1: split into PDFs, ZIPs, and discards. Email signature decorations
    # (image001.png, Outlook-*.png, ServiceBox-Navy.png, etc.) and one-off
    # office files (.docx, .xlsx) are discarded here — not downloaded, not
    # saved to _Unmatched. The audit trail is the INFO log line.
    #
    # ZIPs are inspected in memory in Pass 2b: their contained PDFs become
    # full participants in the email-level decision. ZIPs themselves are
    # never written to disk by Step 1 — see the resolved 2026-05-13 ZIP-
    # orphan entry in `To-Speak-About.txt`.
    pdf_atts: list[tuple[dict, str]] = []   # (att, base_name)
    zip_atts: list[tuple[dict, str]] = []   # (att, base_name)
    has_non_pdf_extras = False
    for a in kept:
        ext = _ext_for_attachment(a, subject)
        base_name = str(a.get("name") or "file").strip() or "file"
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", base_name):
            base_name = f"{base_name}{ext}"
        is_invoice_container = _looks_like_pdf_or_zip(a, subject)
        if ext == ".pdf" and is_invoice_container:
            pdf_atts.append((a, base_name))
        elif ext == ".zip" and is_invoice_container:
            zip_atts.append((a, base_name))
        else:
            ct = str(a.get("contentType") or "").strip() or "unknown"
            run.info(f"discarded non-PDF/ZIP attachment: '{base_name}' ({ct})")
            has_non_pdf_extras = True

    # Pass 2a: download + classify every top-level PDF. Any download failure
    # flags the whole email — we can't make a safe routing decision without
    # seeing the bytes.
    classifications: list[PdfClassification] = []
    download_failures: list[str] = []
    invalid_pdfs: list[str] = []  # bytes don't start with %PDF- (imposter file)
    unsafe_zips: list[str] = []   # ZIPs that failed the audit (bomb / encrypted / non-PDF entries)
    for a, base_name in pdf_atts:
        try:
            blob = graph.download_attachment(msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
            download_failures.append(base_name)
            continue
        if not _is_real_pdf(blob):
            invalid_pdfs.append(base_name)
            continue
        cls = _classify_pdf_against_subject(blob, base_name, plan_row.plan_norm, rows)
        classifications.append(cls)

    # Pass 2b: download + audit each ZIP in memory, then classify each
    # contained PDF the same way top-level PDFs are classified above. The
    # strict zip_safe.audit_and_extract_pdfs rejects any non-PDF entry,
    # any bomb, and any encrypted ZIP — those become unsafe_zips, which
    # flag the email and force it to stay in the Inbox.
    for a, base_name in zip_atts:
        try:
            zip_blob = graph.download_attachment(msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
            download_failures.append(base_name)
            continue
        try:
            inner = zip_safe.audit_and_extract_pdfs(zip_blob)
        except zip_safe.UnsafeZipError as exc:
            run.error(
                f"unsafe zip in '{subject[:50]}'/{base_name}: {exc} — "
                f"flagging email, nothing written to disk"
            )
            unsafe_zips.append(f"{base_name} ({exc})")
            continue
        if not inner:
            run.info(f"zip '{base_name}' contained no PDF entries — ignored")
            continue
        zip_stem = Path(base_name).stem
        for inner_leaf, inner_bytes in inner:
            inner_base_name = f"{zip_stem}__{inner_leaf}"
            if not _is_real_pdf(inner_bytes):
                invalid_pdfs.append(inner_base_name)
                continue
            # Classify using inner_leaf ONLY so the ZIP filename's plan
            # tokens can't influence the filename-fallback matcher. Then
            # rebrand the classification's base_name with the
            # `<zipbase>__<inner>` prefix so the routed file + audit log
            # still trace back to the source ZIP. (Codex review 2026-05-13.)
            cls = _classify_pdf_against_subject(
                inner_bytes, inner_leaf, plan_row.plan_norm, rows,
            )
            cls.base_name = inner_base_name
            classifications.append(cls)

    if download_failures:
        run.error(
            f"flagging '{subject[:60]}': download failed for "
            f"{', '.join(download_failures)} — cannot cross-validate without bytes"
        )
        _flag_message_safely(msg_id, subject, run)
        return

    if invalid_pdfs:
        run.review(
            f"flagging '{subject[:60]}': invalid PDF bytes (not real PDFs "
            f"despite .pdf names): {', '.join(invalid_pdfs)}"
        )
        _flag_message_safely(msg_id, subject, run)
        return

    if unsafe_zips:
        run.review(
            f"flagging '{subject[:60]}': unsafe ZIP attachment(s): "
            f"{', '.join(unsafe_zips)}"
        )
        _flag_message_safely(msg_id, subject, run)
        return

    # A NO_PLAN PDF in a multi-attachment email is an "extra" — it isn't an
    # invoice we can stamp and file, so it stays in the original email which
    # gets forwarded to the manager. A lone NO_PLAN PDF routes on the subject
    # (vendor invoice without a plan number in the PDF) and is not an extra.
    has_no_plan_pdf = (
        any(c.outcome == PdfOutcome.NO_PLAN for c in classifications)
        and len(classifications) > 1
    )

    # Pass 3: decide.
    action = _decide_email_action(plan_row.plan_norm, classifications)

    if action.kind == EmailActionKind.FLAG_AND_HOLD:
        run.review(f"flagging '{subject[:60]}': {action.reason}")
        for c in classifications:
            run.info(f"  pdf-classify: {_format_classification_for_log(c)}")
        _flag_message_safely(msg_id, subject, run)
        # Strict-first: do not save any of this email's attachments to
        # _Unmatched/ — including ZIPs. The whole email stays put so the red
        # flag is the single, complete signal that this message needs human
        # handling.
        return

    # ROUTE_AS_SUBJECT or AUTO_SPLIT — both write PDFs to disk and park non-PDFs.
    run.info(f"'{subject[:60]}' → {action.kind.value}: {action.reason}")
    outcomes: list[RouteOutcome] = []

    for idx, c in enumerate(classifications):
        if c.outcome == PdfOutcome.NO_PLAN and len(classifications) > 1:
            # Multi-attachment email: NO_PLAN PDFs are not routed here.
            # The whole email is forwarded to the manager (see below) so
            # they can decide what to do with the plan-less attachment.
            run.info(f"skip routing NO_PLAN PDF '{c.base_name}' — forwarding email to manager")
            continue
        if action.kind == EmailActionKind.AUTO_SPLIT:
            target_row = action.per_pdf_plan[idx]
            route_source = "pdf_text (auto-split)"
        elif idx in action.per_pdf_plan:
            target_row = action.per_pdf_plan[idx]
            route_source = "pdf_text (override)"
        else:
            target_row = plan_row
            route_source = match_source
        outcome = _route_pdf(
            c.blob, c.base_name, target_row, received_str, sender_domain, ledger, run,
        )
        outcomes.append(outcome)
        if outcome == RouteOutcome.ROUTED:
            run.processed += 1
            run.info(
                f"routed via {route_source}: {c.base_name} "
                f"-> {plan_match.pretty_plan(target_row.plan_norm)}"
            )
        elif outcome == RouteOutcome.DUPLICATE_SKIPPED:
            run.processed += 1

    # ZIPs were inspected in Pass 2b — their contained PDFs are already in
    # `classifications` and routed by the loop above. ZIPs themselves are
    # never written to disk by Step 1; their useful payload is already in
    # the right manager folders.

    # Partial-commit guard: if *any* attachment failed after others succeeded,
    # the email otherwise looks like an ordinary unrouted message in the Inbox
    # (because `_email_destination` returns None on any FAILED). Set the
    # Outlook to-do flag so the operator notices that some content already
    # committed downstream and needs reconciliation. `_route_pdf` writes that
    # already succeeded cannot be rolled back; the loud log + red flag are
    # the operator-facing signal.
    if any(o == RouteOutcome.FAILED for o in outcomes):
        succeeded = sum(
            1 for o in outcomes
            if o in (RouteOutcome.ROUTED, RouteOutcome.DUPLICATE_SKIPPED)
        )
        failed = sum(1 for o in outcomes if o == RouteOutcome.FAILED)
        run.error(
            f"partial commit on '{subject[:60]}': {succeeded} of "
            f"{len(outcomes)} attachments committed before {failed} failure(s) — "
            f"flagging email for operator reconciliation (committed files are "
            f"already on disk and in the duplicate ledger; no rollback)"
        )
        _flag_message_safely(msg_id, subject, run)

    # Forward the original email (with all attachments) to the plan manager
    # when the email contained extras — non-invoice non-PDF files discarded
    # in Pass 1, or plan-less PDF siblings that weren't routed above. The
    # manager can see everything that came in and decide what to save.
    # Only fires when routing had no failures (a partial-commit flag already
    # signals the operator; no need to also forward an incomplete email).
    forward_needed = has_non_pdf_extras or has_no_plan_pdf
    forwarded = False
    forward_blocked = False  # True when forward was needed but couldn't be delivered
    if forward_needed and not any(o == RouteOutcome.FAILED for o in outcomes):
        # Collect unique (email, name) targets. AUTO_SPLIT, PDF_OVERRIDE with
        # per_pdf_plan, and plain ROUTE_AS_SUBJECT all share this path.
        if action.per_pdf_plan:
            seen_fwd: set[str] = set()
            fwd_targets: list[tuple[str, str]] = []
            for split_row in action.per_pdf_plan.values():
                if split_row and split_row.manager_email and split_row.manager_email not in seen_fwd:
                    seen_fwd.add(split_row.manager_email)
                    fwd_targets.append((split_row.manager_email, split_row.strata_name))
        else:
            fwd_targets = (
                [(plan_row.manager_email, plan_row.strata_name)]
                if plan_row.manager_email else []
            )

        if not fwd_targets:
            run.warn(
                f"FORWARD_SKIPPED: no manager email for "
                f"{plan_match.pretty_plan(plan_row.plan_norm)} — extras not forwarded"
            )
            forward_blocked = True
        else:
            any_fwd_ok = False
            for fwd_email, fwd_name in fwd_targets:
                try:
                    fwd_comment = (
                        f"The invoice for {fwd_name} has been filed automatically. "
                        f"The original email contained additional attachments — "
                        f"forwarding for your review."
                    )
                    graph.forward_message(msg_id, graph.resolve_recipient(fwd_email), fwd_comment)
                    run.info(f"FORWARDED_TO_MANAGER: {fwd_email} — extras present alongside invoice")
                    any_fwd_ok = True
                except Exception as exc:
                    run.error(f"forward_to_manager failed for '{subject[:50]}': {exc}")
                    forward_blocked = True
            if any_fwd_ok and not forward_blocked:
                forwarded = True

    # forward_blocked: extras needed forwarding but couldn't be delivered (Graph
    # error or no manager email on record). Keep the email in the Inbox with a
    # red flag so the operator can see the extras and action them manually.
    # This takes priority over the normal destination logic.
    if forward_blocked:
        _flag_message_safely(msg_id, subject, run)
        target_folder_id = None
    elif forwarded:
        # Forwarded successfully: always processed (covers all-NO_PLAN edge case
        # where outcomes is empty and _email_destination would return None).
        target_folder_id = processed_folder_id
    else:
        target_folder_id = _email_destination(outcomes, processed_folder_id, duplicate_folder_id)
    if target_folder_id is not None:
        try:
            graph.move_message_to_folder(msg_id, target_folder_id)
        except Exception as exc:
            run.error(f"move email '{subject[:50]}' to target folder failed: {exc}")


@dataclass
class PriorProcessingResult:
    """Return value of `_process_prior_attachments`: per-PDF outcomes plus a flag bit.

    `flagged` is True when the prior's PDFs disagreed with the reply's plan
    in a way the strict-first decision matrix wouldn't auto-resolve (clash,
    suffix variants, ambiguous text, etc.). Callers should set the Outlook
    to-do flag on the REPLY message and stop walking further priors.
    """
    outcomes: list[RouteOutcome] = field(default_factory=list)
    flagged: bool = False


def _process_prior_attachments(
    *,
    prior_msg_id: str,
    reply_subject: str,
    plan_row: PlanRow,
    match_source: str,
    received_str: str,
    prior_sender_domain: str,
    rows: list[PlanRow],
    ledger: dup_ledger.Ledger,
    run,
) -> PriorProcessingResult:
    """Pull PDFs from a prior message in the same conversation and route them.

    The reply gave us the plan; the prior message carries the PDF. The
    `prior_sender_domain` is the vendor's domain (extracted from the prior
    message's `From:`) — NOT the reply's, which is the operator's own address
    and would collapse every reply-to-self recovery into one fake "vendor".

    Each prior PDF is cross-validated against the reply's plan using the same
    decision matrix as `_process_self_attachments`. A clash, suffix-variant
    pair, or ambiguous PDF in the prior sets `result.flagged=True` so the
    caller can flag the reply and stop walking further priors.
    """
    try:
        attachments = graph.list_attachments(prior_msg_id)
    except Exception as exc:
        run.error(f"list_attachments({prior_msg_id}) failed: {exc}")
        return PriorProcessingResult()

    kept = [a for a in attachments if _is_file_attachment(a)]
    if not kept:
        run.info(f"prior message in conversation has no downloadable attachments")
        return PriorProcessingResult()

    # Pass 1: classify each routable PDF. Non-PDFs in the prior are ignored
    # (the existing behaviour — the conversation-link path is PDF-only).
    classifications: list[PdfClassification] = []
    download_failures: list[str] = []
    for a in kept:
        ext = _ext_for_attachment(a, reply_subject)
        base_name = str(a.get("name") or "file").strip() or "file"
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", base_name):
            base_name = f"{base_name}{ext}"

        if ext != ".pdf" or not _looks_like_pdf_or_zip(a, reply_subject):
            run.info(f"prior attachment '{base_name}' is not a PDF — ignoring")
            continue

        try:
            blob = graph.download_attachment(prior_msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download from prior message failed for {base_name}: {exc}")
            download_failures.append(base_name)
            continue

        if not _is_real_pdf(blob):
            run.error(
                f"prior '{base_name}' has .pdf name but bytes are not a real PDF — "
                f"cannot cross-validate against reply '{reply_subject[:50]}'"
            )
            download_failures.append(f"{base_name} (invalid PDF bytes)")
            continue

        classifications.append(
            _classify_pdf_against_subject(blob, base_name, plan_row.plan_norm, rows)
        )

    if download_failures:
        run.error(
            f"flagging conversation-link reply '{reply_subject[:60]}': download failed "
            f"for prior PDFs {', '.join(download_failures)} — cannot cross-validate"
        )
        return PriorProcessingResult(flagged=True)

    if not classifications:
        return PriorProcessingResult()

    action = _decide_email_action(plan_row.plan_norm, classifications)

    if action.kind == EmailActionKind.FLAG_AND_HOLD:
        run.review(
            f"flagging conversation-link reply '{reply_subject[:60]}' "
            f"(prior {prior_msg_id[:12]}...): {action.reason}"
        )
        for c in classifications:
            run.info(f"  pdf-classify: {_format_classification_for_log(c)}")
        return PriorProcessingResult(flagged=True)

    # ROUTE_AS_SUBJECT or AUTO_SPLIT.
    outcomes: list[RouteOutcome] = []
    for idx, c in enumerate(classifications):
        # Mirror the self-attachment guard: skip plan-less PDFs in multi-PDF
        # priors so boilerplate siblings aren't stamped and filed as invoices.
        if c.outcome == PdfOutcome.NO_PLAN and len(classifications) > 1:
            run.info(
                f"skip routing NO_PLAN PDF '{c.base_name}' in prior "
                f"(source msg {prior_msg_id[:12]}...) — not an invoice"
            )
            continue
        if action.kind == EmailActionKind.AUTO_SPLIT:
            target_row = action.per_pdf_plan[idx]
            route_label = "conversation-link (auto-split)"
        elif idx in action.per_pdf_plan:
            target_row = action.per_pdf_plan[idx]
            route_label = "conversation-link (pdf_text override on prior)"
        else:
            target_row = plan_row
            route_label = f"conversation-link ({match_source} match on reply)"

        outcome = _route_pdf(
            c.blob, c.base_name, target_row,
            received_str, prior_sender_domain, ledger, run,
        )
        outcomes.append(outcome)
        if outcome == RouteOutcome.ROUTED:
            run.processed += 1
            run.info(
                f"routed via {route_label}: {c.base_name} "
                f"-> {plan_match.pretty_plan(target_row.plan_norm)} "
                f"(source msg {prior_msg_id[:12]}...)"
            )
        elif outcome == RouteOutcome.DUPLICATE_SKIPPED:
            run.processed += 1
            run.info(
                f"{route_label} duplicate: {c.base_name} already in pipeline "
                f"(source msg {prior_msg_id[:12]}...)"
            )

    # Forward the prior email to the plan manager when a NO_PLAN sibling was
    # skipped — mirrors the has_no_plan_pdf forward block in
    # _process_self_attachments so managers see all attachments that arrived.
    has_no_plan_prior_pdf = (
        any(c.outcome == PdfOutcome.NO_PLAN for c in classifications)
        and len(classifications) > 1
    )
    if has_no_plan_prior_pdf and not any(o == RouteOutcome.FAILED for o in outcomes):
        if action.per_pdf_plan:
            seen_fwd: set[str] = set()
            fwd_targets: list[tuple[str, str]] = []
            for r in action.per_pdf_plan.values():
                if r and r.manager_email and r.manager_email not in seen_fwd:
                    seen_fwd.add(r.manager_email)
                    fwd_targets.append((r.manager_email, r.strata_name))
        else:
            fwd_targets = (
                [(plan_row.manager_email, plan_row.strata_name)]
                if plan_row.manager_email else []
            )
        for fwd_email, fwd_name in fwd_targets:
            try:
                comment = (
                    f"The invoice for {fwd_name} has been filed automatically. "
                    f"The original email contained additional attachments — "
                    f"forwarding for your review."
                )
                graph.forward_message(prior_msg_id, graph.resolve_recipient(fwd_email), comment)
                run.info(
                    f"FORWARDED_TO_MANAGER: {fwd_email} — "
                    f"NO_PLAN sibling in prior (source msg {prior_msg_id[:12]}...)"
                )
            except Exception as exc:
                run.error(
                    f"forward_to_manager (prior) failed for '{reply_subject[:50]}': {exc}"
                )

    return PriorProcessingResult(outcomes=outcomes)


def _process_pdf_text_fallback(
    *,
    msg_id: str,
    subject: str,
    received_str: str,
    sender_domain: str,
    rows: list[PlanRow],
    ledger: dup_ledger.Ledger,
    run,
    processed_folder_id: str | None,
    duplicate_folder_id: str | None,
) -> None:
    """Subject/body didn't match — try to identify the plan from PDF text.

    All-or-nothing rule (with duplicate-aware extension):
      - Every PDF in the email — top-level OR extracted from a ZIP — must be
        auto-handleable: either matched-and-new (will route) or matched-and-
        duplicate (will skip via ledger).
      - If ANY PDF doesn't match, OR a ZIP fails the safety audit (non-PDF
        entries, bomb, encrypted), OR any download fails, OR any .pdf bytes
        aren't a real PDF, leave the entire email in the Inbox with the
        Outlook red flag. Do NOT write anything to disk. **Do NOT increment
        dup_count** — we haven't committed to processing this email yet.

    Honors the "Inbox is the single source of truth" principle: we never
    silently leave anything in _Unmatched/ that the operator wouldn't
    notice. ZIPs are inspected in memory (zip_safe.audit_and_extract_pdfs)
    and their contained PDFs are full participants in the all-or-nothing
    decision — there is no longer a ZIP exemption. See the resolved
    2026-05-13 ZIP-orphan entry in `To-Speak-About.txt`.
    """
    try:
        attachments = graph.list_attachments(msg_id)
    except Exception as exc:
        run.error(f"list_attachments({msg_id}) failed: {exc}")
        return

    kept = [a for a in attachments if _is_file_attachment(a)]
    if not kept:
        run.info(f"skip: '{subject[:60]}' has no downloadable attachments")
        return

    # Pass 1: classify each attachment WITHOUT writing anything to disk or
    # mutating the ledger. Top-level PDFs are downloaded in memory and tested
    # for plan match + duplicate status. ZIPs are downloaded, audited
    # (zip_safe.audit_and_extract_pdfs), and each contained PDF is classified
    # the same way — its synthetic base_name is `<zipbase>__<inner>.pdf` so
    # the audit trail shows where it came from. Signature images and one-off
    # office files (.docx, .xlsx) are discarded with an INFO log.
    pdf_outcomes: list[dict] = []  # {blob, base_name, plan_row|None, dup_existing|None, note}
    zip_atts: list[tuple[dict, str]] = []  # (att, base_name) — inspected in Pass 1, never written to disk
    download_failures: list[str] = []
    invalid_pdfs: list[str] = []   # bytes don't start with %PDF- (imposter file)
    unsafe_zips: list[str] = []    # ZIPs that failed zip_safe.audit_and_extract_pdfs

    def _classify_one(
        blob: bytes,
        classification_name: str,
        route_name: str | None = None,
    ) -> None:
        """Plan-match (PDF text first, filename fallback) + dup lookup; append
        to `pdf_outcomes`. Encapsulated so top-level PDFs and ZIP-contained
        PDFs go through the exact same logic.

        `classification_name` is what the filename-fallback matcher sees:
        the inner leaf for ZIP-contained PDFs, so the ZIP filename's plan
        tokens cannot influence routing (Codex review 2026-05-13).
        `route_name` is what gets stored in `pdf_outcomes` and used for
        the routed filename / audit trail; for ZIP-contained PDFs it's
        the `<zipbase>__<inner>` prefixed form. Defaults to
        `classification_name` for top-level PDFs.
        """
        if route_name is None:
            route_name = classification_name
        # PDF text is primary evidence; filename is a fallback when text doesn't
        # yield a confident match. This is the safer order — a vendor who
        # mislabels a file (filename says EPS6008, contents say BCS3396) gets
        # caught by the body content rather than blindly routed by filename.
        text = extract_full_text(blob)
        result = plan_match.match_from_pdf_text(text, rows)
        plan_row = result.plan_row if (
            result.plan_row is not None and result.plan_row.manager_name
        ) else None
        if plan_row is not None:
            result_note = result.note or "matched by PDF text"
        else:
            fn_row = plan_match.match_from_filename_with_base_fallback(
                classification_name, rows,
            )
            if fn_row is not None and fn_row.manager_name:
                plan_row = fn_row
                result_note = (
                    f"matched by filename ({plan_match.pretty_plan(fn_row.plan_norm)}); "
                    f"PDF text: {result.note}"
                )
            else:
                result_note = result.note or "no plan in pdf text or filename"

        dup_existing = None
        if plan_row is not None:
            # Non-mutating ledger lookup. We'll commit (route or increment) in
            # Pass 2 only if can_route_all is satisfied. The override field
            # is unused at Pass 1 — _route_pdf will re-check during Pass 2
            # and consume the override under the lock.
            _, _, _, dup_existing, _ = _check_dup_status(
                blob, plan_row.plan_norm, sender_domain, ledger,
            )

        note = result_note
        if dup_existing is not None:
            note = f"{note} (duplicate of {dup_existing.sha256[:12]}...)"

        pdf_outcomes.append({
            "blob": blob,
            "base_name": route_name,
            "plan_row": plan_row,
            "dup_existing": dup_existing,
            "note": note,
        })

    for a in kept:
        ext = _ext_for_attachment(a, subject)
        base_name = str(a.get("name") or "file").strip() or "file"
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", base_name):
            base_name = f"{base_name}{ext}"
        is_invoice_container = _looks_like_pdf_or_zip(a, subject)

        if ext == ".zip" and is_invoice_container:
            zip_atts.append((a, base_name))
            try:
                zip_blob = graph.download_attachment(msg_id, str(a.get("id")))
            except Exception as exc:
                run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
                download_failures.append(base_name)
                continue
            try:
                inner = zip_safe.audit_and_extract_pdfs(zip_blob)
            except zip_safe.UnsafeZipError as exc:
                run.error(
                    f"unsafe zip in '{subject[:50]}'/{base_name}: {exc} — "
                    f"flagging email, nothing written to disk"
                )
                unsafe_zips.append(f"{base_name} ({exc})")
                continue
            if not inner:
                run.info(f"zip '{base_name}' contained no PDF entries — ignored")
                continue
            zip_stem = Path(base_name).stem
            for inner_leaf, inner_bytes in inner:
                inner_base_name = f"{zip_stem}__{inner_leaf}"
                if not _is_real_pdf(inner_bytes):
                    invalid_pdfs.append(inner_base_name)
                    continue
                # `inner_leaf` for classification (ZIP filename's plan
                # tokens must NOT influence the matcher); `inner_base_name`
                # for routing/audit trail. (Codex review 2026-05-13.)
                _classify_one(inner_bytes, inner_leaf, inner_base_name)
            continue

        if ext != ".pdf" or not is_invoice_container:
            ct = str(a.get("contentType") or "").strip() or "unknown"
            run.info(f"discarded non-PDF/ZIP attachment: '{base_name}' ({ct})")
            continue

        try:
            blob = graph.download_attachment(msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
            download_failures.append(base_name)
            continue

        if not _is_real_pdf(blob):
            invalid_pdfs.append(base_name)
            continue

        _classify_one(blob, base_name)

    # Pass 2: decide whether the email is auto-handleable.
    # ZIP-contained PDFs are already in `pdf_outcomes`; ZIPs themselves no
    # longer participate as separate entities. Unmatched PDFs, download
    # failures, invalid PDF bytes, and unsafe ZIPs all gate the decision.
    any_unmatched_pdf = any(o["plan_row"] is None for o in pdf_outcomes)
    has_download_failure = bool(download_failures)
    has_invalid_pdfs = bool(invalid_pdfs)
    has_unsafe_zips = bool(unsafe_zips)
    has_actionable_content = bool(pdf_outcomes)

    can_route_all = (
        has_actionable_content
        and not any_unmatched_pdf
        and not has_download_failure
        and not has_invalid_pdfs
        and not has_unsafe_zips
    )

    if not can_route_all:
        # All-or-nothing: nothing routed, nothing saved to disk, dup_count
        # NOT incremented. Build a detailed log line so IT can diagnose if
        # the operator asks "which one didn't match?".
        matched = [
            o["base_name"] for o in pdf_outcomes
            if o["plan_row"] is not None and o["dup_existing"] is None
        ]
        would_skip_as_dup = [
            o["base_name"] for o in pdf_outcomes
            if o["plan_row"] is not None and o["dup_existing"] is not None
        ]
        unmatched = [
            f"{o['base_name']} [{o['note']}]"
            for o in pdf_outcomes if o["plan_row"] is None
        ]
        parts = []
        if matched:
            parts.append(f"would-have-routed PDFs: {', '.join(matched)}")
        if would_skip_as_dup:
            parts.append(f"would-have-skipped-as-duplicate: {', '.join(would_skip_as_dup)}")
        if unmatched:
            parts.append(f"unmatched PDFs (top-level or ZIP-contained): {', '.join(unmatched)}")
        if download_failures:
            parts.append(f"download failures: {', '.join(download_failures)}")
        if invalid_pdfs:
            parts.append(f"invalid PDF bytes (not real PDFs despite .pdf names): {', '.join(invalid_pdfs)}")
        if unsafe_zips:
            parts.append(f"unsafe ZIPs: {', '.join(unsafe_zips)}")
        detail = " | ".join(parts) if parts else "no actionable content"
        run.review(
            f"all-or-nothing: '{subject[:60]}' left in Inbox for manual handling — "
            f"{detail}"
        )
        # Every Inbox email carrying PDF-shaped content the system couldn't fully
        # resolve gets the red flag so the operator's daily review surfaces it.
        # ZIP-contained PDFs count as PDF content here — an email whose only
        # attachment was a silent-content ZIP triggers the flag. Pure signature
        # / .docx-only emails leave every bucket empty and pass through silently.
        has_pdf_content = (
            bool(pdf_outcomes)
            or bool(download_failures)
            or bool(invalid_pdfs)
            or bool(unsafe_zips)
        )
        if has_pdf_content:
            _flag_message_safely(msg_id, subject, run)
        return

    # Happy path: every PDF is auto-handleable (matched or duplicate). Commit
    # each PDF outcome, save each ZIP to _Unmatched, then move the email.
    outcomes: list[RouteOutcome] = []
    for o in pdf_outcomes:
        outcome = _route_pdf(
            o["blob"], o["base_name"], o["plan_row"],
            received_str, sender_domain, ledger, run,
        )
        outcomes.append(outcome)
        if outcome == RouteOutcome.ROUTED:
            run.processed += 1
            run.info(f"routed via pdf_text: {o['base_name']} ({o['note']})")
        elif outcome == RouteOutcome.DUPLICATE_SKIPPED:
            run.processed += 1

    # ZIPs were inspected in Pass 1 — their contained PDFs are already in
    # `pdf_outcomes` and routed by the loop above. ZIPs themselves are
    # never written to disk by Step 1.

    # Partial-commit guard (mirrors the subject-matched path): if any
    # attachment failed after others committed, the email otherwise looks like
    # an ordinary unrouted message in the Inbox (because `_email_destination`
    # returns None on any FAILED). Set the Outlook to-do flag so the operator
    # notices that some content already committed downstream and needs
    # reconciliation. Committed writes cannot be rolled back; the loud log +
    # red flag are the operator-facing signal.
    if any(o == RouteOutcome.FAILED for o in outcomes):
        succeeded = sum(
            1 for o in outcomes
            if o in (RouteOutcome.ROUTED, RouteOutcome.DUPLICATE_SKIPPED)
        )
        failed = sum(1 for o in outcomes if o == RouteOutcome.FAILED)
        run.error(
            f"partial commit on '{subject[:60]}' (pdf-text fallback): "
            f"{succeeded} of {len(outcomes)} attachments committed before "
            f"{failed} failure(s) — flagging email for operator reconciliation "
            f"(committed files are already on disk and in the duplicate "
            f"ledger; no rollback)"
        )
        _flag_message_safely(msg_id, subject, run)

    target_folder_id = _email_destination(outcomes, processed_folder_id, duplicate_folder_id)
    if target_folder_id is not None:
        try:
            graph.move_message_to_folder(msg_id, target_folder_id)
        except Exception as exc:
            run.error(f"move email '{subject[:50]}' to target folder failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
