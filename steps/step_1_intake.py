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
)
from tools._lib.log import daily_log
from tools._lib.pdf_text import extract_full_text
from tools._lib.stamp import render_received_stamp
from tools._lib.xls import PlanRow, load_plans, plan_to_manager

_STAMP = "step_1"

_PROCESSED_FOLDER_NAME = "processed_emails"
_DUPLICATE_FOLDER_NAME = "duplicate_emails"


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
    - AMBIGUOUS: PDF text contains plan tokens but the matcher couldn't pick
      one safely (e.g. two equally-scored candidates). Don't trust either side.
    - CLASH: PDF confidently identifies a *different* plan than the subject.
    """
    AGREE = "agree"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"
    CLASH = "clash"


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
        if confident_row.plan_norm == subject_plan_norm:
            outcome = PdfOutcome.AGREE
        else:
            outcome = PdfOutcome.CLASH
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

    # Pass 3: Neither PDF text nor filename gave a confident match. Distinguish
    # EMPTY (scanned/no text layer) from AMBIGUOUS (text exists but matcher
    # refused to pick) — operators want different logs for these.
    note_low = (result.note or "").lower()
    if note_low.startswith("no text extracted"):
        outcome = PdfOutcome.EMPTY
    else:
        # "Ambiguous: ..." or "Detected plan text, but no match in list..." —
        # both are unsafe-to-auto-pick. Strict-first: treat as AMBIGUOUS so
        # the email-level decision flags it for review when combined with a CLASH.
        outcome = PdfOutcome.AMBIGUOUS
    return PdfClassification(
        outcome=outcome,
        base_name=base_name,
        blob=blob,
        pdf_plan_norm="",
        pdf_plan_row=None,
        note=result.note,
        detected=result.detected,
    )


def _decide_email_action(
    subject_plan_norm: str,
    classifications: list[PdfClassification],
) -> EmailAction:
    """Combine per-PDF classifications into a single email-level routing action.

    Decision matrix (see workflows/step_1_intake.md):

      - all AGREE/EMPTY                       → ROUTE_AS_SUBJECT
      - any AMBIGUOUS                         → FLAG (strict-first)
      - any EMPTY mixed with any CLASH        → FLAG (can't safely route the empty one)
      - all PDFs confident, ≥1 CLASH:
        - all PDF plans equal (consensus)     → FLAG (subject vs unanimous PDFs)
        - multiple unique plans, shared base  → FLAG (suffix-variant failsafe)
        - multiple unique plans, distinct bases → AUTO_SPLIT (subject ignored)

    Notes:
      * "Confident" here means the PDF returned an active plan with a manager
        (PdfOutcome.AGREE or PdfOutcome.CLASH). EMPTY and AMBIGUOUS are not
        confident.
      * The base-collision check covers the subject's plan implicitly: if a
        PDF's plan AGREES, its plan_norm equals subject_plan_norm, and that
        plan appears in `unique_plans`, so the base check catches a "subject
        plan and another PDF plan share a base" scenario too.
    """
    if not classifications:
        # No PDFs at all — degenerate but possible if caller passed a zip-only
        # email through. Route as subject; the existing non-PDF handler still
        # parks the zip in _Unmatched/.
        return EmailAction(EmailActionKind.ROUTE_AS_SUBJECT, "no PDFs to classify")

    outcomes = [c.outcome for c in classifications]

    if all(o in (PdfOutcome.AGREE, PdfOutcome.EMPTY) for o in outcomes):
        agreed = sum(1 for o in outcomes if o == PdfOutcome.AGREE)
        empty = sum(1 for o in outcomes if o == PdfOutcome.EMPTY)
        return EmailAction(
            EmailActionKind.ROUTE_AS_SUBJECT,
            f"PDF cross-check OK ({agreed} agree, {empty} empty)",
        )

    if any(o == PdfOutcome.AMBIGUOUS for o in outcomes):
        amb_names = [c.base_name for c in classifications if c.outcome == PdfOutcome.AMBIGUOUS]
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"ambiguous PDF text: {', '.join(amb_names)}",
        )

    # At this point: every PDF is AGREE / EMPTY / CLASH, and at least one CLASH.
    if any(o == PdfOutcome.EMPTY for o in outcomes):
        empty_names = [c.base_name for c in classifications if c.outcome == PdfOutcome.EMPTY]
        clash_summary = [
            f"{c.base_name}->{c.pdf_plan_norm}"
            for c in classifications if c.outcome == PdfOutcome.CLASH
        ]
        return EmailAction(
            EmailActionKind.FLAG_AND_HOLD,
            f"mixed evidence: PDF(s) {', '.join(empty_names)} empty while "
            f"PDF(s) {', '.join(clash_summary)} clash with subject "
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

        # Duplicate-detection ledger — loaded once at startup, mutated in
        # place by `_route_pdf` and the operator-tool CLIs.
        try:
            ledger = dup_ledger.load()
            run.info(f"dup ledger loaded: {len(ledger.all_rows())} fingerprints on file")
        except ValueError as exc:
            run.error(f"dup ledger corrupted — halting day: {exc}")
            return 1

        received_str = _today_received_str()
        # Track which prior-message IDs we've consumed via the conversation-link
        # path so we don't try to route the same PDF a second time when Step 1
        # iterates to the original message in the same run.
        consumed_prior_ids: set[str] = set()
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
            if msg_id in consumed_prior_ids:
                run.info(
                    f"skip: '{(msg.get('subject') or '')[:60]}' already routed "
                    f"via conversation-link from a later reply in this run"
                )
                if processed_folder_id:
                    try:
                        graph.move_message_to_folder(msg_id, processed_folder_id)
                    except Exception as exc:
                        run.error(f"move-to-processed_emails failed: {exc}")
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
                exclude_ids = consumed_prior_ids | {msg_id}
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
                    # walking, but do NOT add to `consumed_prior_ids`.
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
                        consumed_prior_ids.add(prior_id)
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

    return 0


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
      1. List + filter attachments.
      2. For each PDF: download the bytes and classify against the subject's plan.
         For non-PDFs: defer (still get parked in _Unmatched/ on route paths).
      3. Decide an email-level action via `_decide_email_action`.
         - ROUTE_AS_SUBJECT: stamp every PDF with the subject's plan (legacy path).
         - AUTO_SPLIT: stamp each PDF with its own PDF-detected plan; subject ignored.
         - FLAG_AND_HOLD: set Outlook to-do flag, write nothing to disk, leave the
           email in the Inbox so the operator's red-flag review surfaces it.
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
    # saved to _Unmatched. The audit trail is the INFO log line. ZIPs are
    # preserved and saved to _Unmatched/Invoices on the happy path so Step 2
    # can unpack them.
    pdf_atts: list[tuple[dict, str]] = []   # (att, base_name)
    zip_atts: list[tuple[dict, str]] = []   # (att, base_name)
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

    # Pass 2: download + classify every PDF. Any download failure flags the
    # whole email — we can't make a safe routing decision without seeing the bytes.
    classifications: list[PdfClassification] = []
    download_failures: list[str] = []
    invalid_pdfs: list[str] = []  # bytes don't start with %PDF- (imposter file)
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
        if action.kind == EmailActionKind.AUTO_SPLIT:
            target_row = action.per_pdf_plan[idx]
            route_source = "pdf_text (auto-split)"
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

    # ZIPs go to _Unmatched/Invoices/ for Step 2 to unpack. Same write semantics
    # as the old non-PDF save path, but scoped to ZIPs only — non-PDF/non-ZIP
    # attachments are discarded in Pass 1.
    for a, base_name in zip_atts:
        try:
            blob = graph.download_attachment(msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
            outcomes.append(RouteOutcome.FAILED)
            continue
        out_name = safe_io.sanitize_filename(base_name)
        dest_path = paths.unmatched_invoices() / out_name
        try:
            written = safe_io.safe_write_unique(dest_path, blob)
            run.processed += 1
            outcomes.append(RouteOutcome.ROUTED)
            if written != dest_path:
                run.info(
                    f"saved zip to _Unmatched (for Step 2): {written} "
                    f"(collision-renamed from {dest_path.name})"
                )
            else:
                run.info(f"saved zip to _Unmatched (for Step 2): {written}")
        except Exception as exc:
            run.error(f"write failed for {dest_path}: {exc}")
            outcomes.append(RouteOutcome.FAILED)

    # Partial-commit guard: if *any* attachment failed after others succeeded,
    # the email otherwise looks like an ordinary unrouted message in the Inbox
    # (because `_email_destination` returns None on any FAILED). Set the
    # Outlook to-do flag so the operator notices that some content already
    # committed downstream and needs reconciliation. `_route_pdf`/_Unmatched
    # writes that already succeeded cannot be rolled back; the loud log + red
    # flag are the operator-facing signal.
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
        if action.kind == EmailActionKind.AUTO_SPLIT:
            target_row = action.per_pdf_plan[idx]
            route_label = "conversation-link (auto-split)"
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
      - Every PDF in the email must be auto-handleable: either matched-and-new
        (will route) or matched-and-duplicate (will skip via ledger).
      - If ANY PDF doesn't match, OR there's any non-PDF/ZIP attachment
        the matcher can't handle, leave the entire email in the Inbox.
        Do NOT write anything to disk. **Do NOT increment dup_count** —
        we haven't committed to processing this email yet.

    Honors the "Inbox is the single source of truth" principle: we never
    silently leave anything in _Unmatched/ that the operator wouldn't
    notice. A known duplicate is treated as auto-handleable (the system
    knows what to do with it: skip and increment the counter).
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
    # mutating the ledger. PDFs are downloaded in memory and tested for plan
    # match + duplicate status. ZIPs are deferred — saved to _Unmatched/Invoices/
    # for Step 2 to unpack, and they do NOT trigger the all-or-nothing rule
    # (Step 2 + Step 3 will handle their contents downstream). Signature images
    # and one-off office files (.docx, .xlsx) are discarded with an INFO log.
    pdf_outcomes: list[dict] = []  # {blob, base_name, plan_row|None, dup_existing|None, note}
    zip_atts: list[tuple[dict, str]] = []  # (att, base_name) — saved to _Unmatched on the happy path
    download_failures: list[str] = []
    invalid_pdfs: list[str] = []  # bytes don't start with %PDF- (imposter file)

    for a in kept:
        ext = _ext_for_attachment(a, subject)
        base_name = str(a.get("name") or "file").strip() or "file"
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", base_name):
            base_name = f"{base_name}{ext}"
        is_invoice_container = _looks_like_pdf_or_zip(a, subject)
        if ext == ".zip" and is_invoice_container:
            zip_atts.append((a, base_name))
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

        # PDF text is primary evidence; filename is a fallback when text doesn't
        # yield a confident match. This is the safer order — a vendor who
        # mislabels a file (filename says EPS6008, contents say BCS3396) gets
        # caught by the body content rather than blindly routed by filename.
        # Costs one text extraction per PDF, which we accept.
        text = extract_full_text(blob)
        result = plan_match.match_from_pdf_text(text, rows)
        plan_row = result.plan_row if (
            result.plan_row is not None and result.plan_row.manager_name
        ) else None
        if plan_row is not None:
            result_note = result.note or "matched by PDF text"
        else:
            fn_row = plan_match.match_from_filename_with_base_fallback(base_name, rows)
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
            "base_name": base_name,
            "plan_row": plan_row,
            "dup_existing": dup_existing,
            "note": note,
        })

    # Pass 2: decide whether the email is auto-handleable.
    # ZIPs are always auto-handleable (Step 2 unpacks them downstream), so they
    # don't gate the decision. Unmatched PDFs, download failures, and invalid
    # PDF bytes (imposter files) all gate it.
    any_unmatched_pdf = any(o["plan_row"] is None for o in pdf_outcomes)
    has_download_failure = bool(download_failures)
    has_invalid_pdfs = bool(invalid_pdfs)
    has_actionable_content = bool(pdf_outcomes) or bool(zip_atts)

    can_route_all = (
        has_actionable_content
        and not any_unmatched_pdf
        and not has_download_failure
        and not has_invalid_pdfs
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
        zip_names = [bn for _, bn in zip_atts]
        parts = []
        if matched:
            parts.append(f"would-have-routed PDFs: {', '.join(matched)}")
        if would_skip_as_dup:
            parts.append(f"would-have-skipped-as-duplicate: {', '.join(would_skip_as_dup)}")
        if unmatched:
            parts.append(f"unmatched PDFs: {', '.join(unmatched)}")
        if zip_names:
            parts.append(f"deferred ZIPs (not saved due to all-or-nothing): {', '.join(zip_names)}")
        if download_failures:
            parts.append(f"download failures: {', '.join(download_failures)}")
        if invalid_pdfs:
            parts.append(f"invalid PDF bytes (not real PDFs despite .pdf names): {', '.join(invalid_pdfs)}")
        detail = " | ".join(parts) if parts else "no actionable content"
        run.review(
            f"all-or-nothing: '{subject[:60]}' left in Inbox for manual handling — "
            f"{detail}"
        )
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

    for a, base_name in zip_atts:
        try:
            blob = graph.download_attachment(msg_id, str(a.get("id")))
        except Exception as exc:
            run.error(f"download failed for '{subject[:50]}'/{base_name}: {exc}")
            outcomes.append(RouteOutcome.FAILED)
            continue
        out_name = safe_io.sanitize_filename(base_name)
        dest_path = paths.unmatched_invoices() / out_name
        try:
            written = safe_io.safe_write_unique(dest_path, blob)
            run.processed += 1
            outcomes.append(RouteOutcome.ROUTED)
            if written != dest_path:
                run.info(
                    f"saved zip to _Unmatched (for Step 2): {written} "
                    f"(collision-renamed from {dest_path.name})"
                )
            else:
                run.info(f"saved zip to _Unmatched (for Step 2): {written}")
        except Exception as exc:
            run.error(f"write failed for {dest_path}: {exc}")
            outcomes.append(RouteOutcome.FAILED)

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
