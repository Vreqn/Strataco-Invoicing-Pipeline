# Step 1 — Email intake (subject + body + PDF text matching, conversation-link fallback, PDF cross-validation)

## Objective
Pull invoice attachments out of the testinvsml@stratacomgmt.com Inbox, identify the Strata Plan, route to the right manager's `To_Approve` folder (with the Received stamp applied to PDFs), and move the email into the `processed_emails` Inbox subfolder. **Unidentified emails stay in the Inbox** so the operator's reply-to-self recovery loop works. **Emails where the PDF text disagrees with the subject get the Outlook "Flag as to-do" set** so the operator can see a red flag in Outlook and intervene.

## Schedule
06:00 Mon–Fri (Windows Task Scheduler).

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx` — Strata Plan ↔ Manager ↔ AP master list.
- The mailbox's `Inbox` folder (latest 500 messages).
- An `Inbox/processed_emails` subfolder (must exist; created manually once).
- An `Inbox/duplicate_emails` subfolder (must exist; created manually once).
- An `Inbox/Action_Required` subfolder (must exist; created manually once — the front desk's daily work queue).
- Azure app-reg credentials in `.env` (`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`).

## Matching order (first hit wins)
For each Inbox message:
1. **Subject** — `pick_from_subject(message.subject)` against the plan map.
2. **Body** — `pick_from_subject(message.bodyPreview)` reuses the same regex. NOTE: Graph returns `bodyPreview` as the first ~255 chars of the body, NOT the full body. Plan numbers buried deeper in long emails won't be matched here; the PDF-text scan is the safety net.
3. **PDF text + filename** (only if subject/body didn't match AND email has attachments) — for each PDF, `match_from_pdf_text(extract_full_text(blob))` runs first; on a confident match, that's the plan. If the body text is uninformative, `match_from_filename_with_base_fallback(base_name, rows)` is consulted as a fallback. Scored per-PDF independently. See "Mixed-batch handling" below for what happens when an email has multiple PDFs with different match outcomes.
4. **Conversation-link** (only if subject/body matched AND email has NO attachments) — look up other messages in the same `conversationId` newest-first, walk through them until a message with a routable PDF is found, route those PDFs using the matched plan. Move both the reply and the source message to `processed_emails`. The walk skips any prior IDs already consumed by an earlier reply in the same run.

## Attachment filtering (Pass 1)

Step 1 only downloads and processes **PDF and ZIP** attachments. Other attachments — signature images (Outlook-*.png, image001.png, ServiceBox-Navy.png), `.docx`, `.xlsx`, `~WRD*.jpg`, etc. — are **discarded at intake** with an INFO log line. They are not downloaded, not saved to disk, not counted as blockers.

ZIP attachments are inspected in memory via `tools/_lib/zip_safe.py::audit_and_extract_pdfs`. Each contained PDF is downloaded into RAM, classified the same way a top-level PDF is, and becomes a full participant in `_decide_email_action` / the all-or-nothing rule. ZIPs themselves are **never written to disk by Step 1** — their useful payload (the PDFs inside) is routed directly to manager folders. Routed names use the convention `<zipbase>__<inner>.pdf` so the audit trail shows where the file came from.

The audit is strict: a ZIP that contains any non-PDF entries (`.docx`, `.xlsx`, etc.), is encrypted, exceeds the bomb-protection caps (`ZIP_MAX_*` in `.env`), or has corrupt bytes raises `UnsafeZipError`. Step 1 then keeps the parent email in the Inbox with the Outlook red flag and writes nothing to disk — the strictest interpretation of "Inbox is the single source of truth," and the resolution of the 2026-05-13 ZIP-orphan entry in `To-Speak-About.txt`. Two kinds of entry are silently ignored rather than tripping the strict check: macOS resource-fork files (`__MACOSX/`, `._foo.pdf`) so Mac-authored ZIPs pass, and `.txt` companion files (`IGNORABLE_COMPANION_EXTS` in `zip_safe.py`) since a `.txt` is never an invoice — e.g. TELUS Bill Analyzer staples a `manifest.txt` next to the invoice PDF, and the PDF still routes (0.14.2).

If a real invoice arrives as a `.docx` or `.xlsx`, the operator asks the vendor to resend as PDF (per client policy as of 0.11.2).

## PDF magic-byte validation (Pass 2)

After download, every PDF blob is checked for the `%PDF-` magic header via `_is_real_pdf(blob)`. A file named `invoice.pdf` whose bytes are actually PNG / JPEG / HTML / corrupted data would otherwise be classified `EMPTY` by `extract_full_text` and route under the EMPTY-trusts-subject branch of `_decide_email_action` — landing in the manager's `To_Approve` queue with a `.pdf` name and unreadable contents.

Scanner-made image-only PDFs still pass (the PDF wrapper carries the magic header regardless of what's inside).

When an imposter is detected:
  - In `_process_self_attachments`: added to a local `invalid_pdfs` list. After the download loop, if any are present, the email is flagged via `run.review()` (NOT `run.error()`) and `_flag_message_safely` sets the Outlook to-do marker. Nothing is written to disk.
  - In `_process_pdf_text_fallback`: same `invalid_pdfs` list; gates `can_route_all` alongside `download_failures`; the all-or-nothing detail string surfaces the invalid filenames.
  - In `_process_prior_attachments` (conversation-link replies): the invalid prior is appended to `download_failures` with an `(invalid PDF bytes)` suffix so the existing conversation-link flag path triggers and the reply gets flagged.

## PDF cross-validation (subject-matched branch)

Once subject (or body) has matched a plan, **every PDF attachment is still classified** against the matched plan. This catches two real problems: vendors typing the wrong plan in the subject while the PDF has the correct one, and multi-PDF emails where each PDF belongs to a different strata.

Evidence priority inside `_classify_pdf_against_subject` (as of 0.11.3 — order swapped after Codex review):

1. **PDF body text** — primary evidence. `extract_full_text(blob)` + `match_from_pdf_text(text, rows)`. If the matcher returns a confident active+managed plan, that's the outcome.
2. **Filename** — fallback when PDF text is uninformative (empty / ambiguous / safety guard refused to pick). `match_from_filename_with_base_fallback(base_name, rows)`. Vendor-authored filenames are intentional and rescue scans, OCR-marginal PDFs, and the matcher's safety-guard ambiguity cases. Same helper Step 3 uses.

If PDF text confidently identifies plan X and the filename says plan Y, **PDF text wins** — outcome is the body's plan, not the filename's. This catches the "vendor renamed invoice to match the wrong subject" case.

Each PDF gets classified into one of five outcomes (`PdfOutcome` in `steps/step_1_intake.py`):

| Outcome | Condition |
|---|---|
| **AGREE** | PDF text confidently identifies the same plan as the subject. |
| **EMPTY** | PDF has no extractable text (scanned image / no text layer). No evidence either way. |
| **NO_PLAN** | PDF has extractable text but carries **no strata plan number at all** — neither a managed-plan token (`match_from_pdf_text`) **nor** a token the document explicitly labels "Strata Plan …" (`plan_match.find_explicit_plan_tokens`). When it's the *lone* PDF this carries no evidence either way and is treated like EMPTY (route on subject — the genuine "vendor invoice that never prints the plan #" case). When it has *siblings*, it is skipped in routing and the email is forwarded to the plan manager — see the email-level action table below. |
| **AMBIGUOUS** | PDF text names a plan but it didn't resolve — a managed-prefix token the matcher couldn't pick, two equally-scored managed candidates, **or** a plan the PDF explicitly labels "Strata Plan …" whose prefix/number isn't in `Strataplan_List.xlsx` (e.g. "Strata Plan KAS 9999"). The explicit-wording scan is what catches unmanaged-*prefix* plans — `match_from_pdf_text`'s detector only sees managed prefixes. |
| **CLASH** | PDF confidently identifies a *different* managed plan than the subject. |

The per-PDF classifications are combined into a single email-level action by `_decide_email_action`:

| Case | Action |
|---|---|
| Every PDF is AGREE | **ROUTE_AS_SUBJECT** — stamp using subject's plan. |
| Lone PDF, EMPTY or NO_PLAN | **ROUTE_AS_SUBJECT** — a single PDF with no evidence against the subject is trusted to the subject's plan. |
| Multi-PDF, any NO_PLAN | **ROUTE_AS_SUBJECT + FORWARD_TO_MANAGER** — AGREE/EMPTY PDFs are stamped and filed; NO_PLAN PDF siblings are skipped (not stamped). The full original email (all attachments) is forwarded to the plan manager, then moved to `processed_emails`. The manager decides what to do with the plan-less attachment. |
| Multi-PDF, AGREE + EMPTY only (no NO_PLAN) | **ROUTE_AS_SUBJECT** — a scanned sibling is trusted to the subject. |
| Any PDF is AMBIGUOUS | **FLAG** — strict-first, don't trust ambiguous evidence. |
| Mix of EMPTY/NO_PLAN and CLASH | **FLAG** — can't safely route a no-plan PDF when its sibling disagrees with the subject. |
| All PDFs CLASH on the same plan (≠ subject) | **FLAG** — consensus clash, vendor likely mislabelled the subject. |
| Multiple PDFs, plan bases collide (e.g. `LMS4193C` + `LMS4193T`, `EPS4280` + `EPS4280A`) | **FLAG** — suffix-variant failsafe, easy to confuse. |
| Multiple PDFs, distinct plan bases (e.g. `BCS2707` + `BCS2800`) | **AUTO_SPLIT** — each PDF routes to its own plan, subject ignored. |

**FLAG behaviour:** sets the Outlook "Flag as to-do" on the message via `graph.flag_message`, leaves the email in the Inbox, writes **nothing** to disk (not even the ZIPs — the whole email stays put). The operator sees a red flag in Outlook, decides what to do, and marks it complete (green checkmark) when handled. Logged via `run.review()` (WARNING-level with `NEED_REVIEW:` prefix), which increments the `need_review` counter on the daily summary — NOT the `errors` counter, which is reserved for genuine exceptions.

**Suffix-variant detection** uses `plan_match.plan_base`, which strips a single trailing letter — the same convention as `xls.base_plan_index`. So `LMS4193C` and `LMS4193T` both reduce to base `LMS4193` (collision → flag), but `BCS2707` and `BCS2800` have distinct bases (`BCS2707` vs `BCS2800` → safe to auto-split).

**Strict-first stance:** when in doubt, flag for review. Mis-sorting silently is much worse than over-flagging. This policy can be relaxed (e.g. auto-route some suffix-variant cases, or trust the PDF outright on single-PDF clashes) once we have evidence from real operator usage that the strata managers' labelling is reliable.

## Outputs
- Matched PDFs (subject/body match OR fallback) at `<STRATACO_ROOT>/Users/<Manager>/Invoices/To_Approve/<plan> - <name>.pdf` (Received stamp applied). ZIP-contained PDFs use `<zipbase>__<inner>.pdf` as their base name so the audit trail preserves the source ZIP.
- ZIP attachments are inspected in memory and their PDFs routed directly — **the ZIP file itself is never saved to `_Unmatched/Invoices/` by Step 1**. (The directory continues to exist for the Step 2/3 safety-net jobs that drain operator manual drops.)
- Non-PDF/non-ZIP attachments (signature images, .docx, .xlsx) are **discarded at intake** with an INFO log — not saved anywhere. If such extras appear alongside a successfully routed invoice, the full email is forwarded to the plan manager.
- Subject/body-matched emails are moved to `Inbox/processed_emails`. If the email contained extras (non-PDF attachments or NO_PLAN PDF siblings), it is also forwarded to the plan manager before being moved.
- Successfully PDF-text-fallback-routed emails (every PDF — top-level OR ZIP-contained — text- or filename-matched a plan; every ZIP passed the safety audit) are routed and moved to `processed_emails`.
- **All-or-nothing leave-in-Inbox rule:** any email that doesn't get a subject/body match AND has an unmatched PDF, invalid PDF bytes, an unsafe ZIP (non-PDF entries, bomb, encrypted), or a download failure stays in the Inbox (temporarily — see end-of-run sweep below) **with the Outlook red flag set** and nothing written to disk. Discarded non-PDF/non-ZIP attachments do NOT trigger the rule. The email itself is the recovery surface; the operator replies-to-self with the corrected subject and the next morning's conversation-link pass routes everything in the thread together.
- **End-of-run inbox sweep:** after the main processing loop finishes, Step 1 calls `list_inbox_messages()` one final time and moves every remaining message to `Inbox/Action_Required`. This covers unmatched emails (with red flags set), any general correspondence, and anything else left over. The front desk monitors `Action_Required` as her daily work queue — the main Inbox stays clean for new arrivals. If `Action_Required` doesn't exist, the sweep is skipped with a warning and those emails remain in the Inbox until the folder is created.
- One row appended to `logs/daily_summary.csv` (7 columns: `date, step, processed, need_review, errors, duration_sec, status`) and a detailed log at `logs/step_1_<date>.log`.

## All-or-nothing rule (for emails without a subject/body plan match)

When the subject and body don't yield a plan, Step 1 still tries to match each PDF by **PDF text first, filename second** (per `_classify_pdf_against_subject`'s order). ZIPs are inspected in memory and each contained PDF goes through the same matcher. The email's fate is committed **all at once**:

- **Every PDF (top-level or ZIP-contained) text- or filename-matched a plan, every ZIP passed the safety audit** → route the PDFs, move the email to `processed_emails`. ZIPs themselves are not written to disk; their contained PDFs have already been routed.
- **Anything less** (any unmatched PDF, invalid PDF bytes, unsafe ZIP, download failure) → leave the entire email in the Inbox **with the Outlook red flag**. **Nothing is written to disk.** Even the would-have-matched PDFs are not committed.

Discarded non-PDF/non-ZIP attachments (signature PNGs, `.docx` attached at the top level, `.xlsx`) don't exist past Pass 1, so they don't gate the decision. ZIP-contained `.docx`/`.xlsx` etc. DO gate the decision — the strict `zip_safe` audit treats them as unsafe. ZIP-contained `.txt` files do NOT gate — they're skipped like macOS resource-fork noise (0.14.2).

Why all-or-nothing rather than save unmatched stuff to `_Unmatched/`? Because operators don't routinely check `_Unmatched/Invoices/` — their workflow looks at the Inbox. Saving something to `_Unmatched/` is effectively hiding it. Keeping the email in the Inbox preserves the "Inbox is the single source of truth" principle. The 2026-05-13 ZIP-orphan fix removed the last remaining ZIP exemption from this rule.

The all-or-nothing log line (via `run.review()`) lists each attachment's outcome: which PDFs would have routed (with `<zipbase>__<inner>` naming for ZIP-contained ones), which didn't and why, which ZIPs failed the safety audit and what tripped it, and any invalid PDF bytes detected. IT can grep the log if the operator asks "which one was the problem?"

**Outlook red flag on the leave-in-Inbox path.** Whenever the email that hits the all-or-nothing branch carried any PDF-shaped content the system couldn't fully resolve — at least one classified PDF (matched or unmatched, top-level or from a ZIP), a download failure, invalid PDF bytes, or an unsafe ZIP — Step 1 calls `_flag_message_safely` so the message picks up the same red flag the operator already trains on for subject↔PDF clashes. Emails that hit this branch with **only** non-invoice attachments (signature PNGs, top-level `.docx`, `.xlsx`) stay unflagged: a discard-only email has nothing for the operator to review.

The rough edges of this design — multi-strata PDFs in one email, non-invoice PDFs that get routed alongside invoice PDFs — are documented in `To-Speak-About.txt` for client policy direction.

## Partial-commit Outlook flag (both branches)

Both `_process_self_attachments` and `_process_pdf_text_fallback` carry a partial-commit guard: if any attachment in an email returned `FAILED` after others committed (PDF routed but ZIP write failed, or vice versa), `_flag_message_safely` sets the Outlook to-do marker and the log line `partial commit on '<subject>'` (or `... (pdf-text fallback)`) is written via `run.error()`. Without this, the email would otherwise look like ordinary unmatched mail despite having committed content downstream. Committed writes cannot be rolled back; the loud log + red flag are the operator-facing signal.

## Run
```
python steps/step_1_intake.py
```

## Tools used
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `list_inbox_messages`, `list_conversation_messages`, `list_attachments`, `download_attachment`, `find_child_folder_id`, `move_message_to_folder`, `flag_message`, `forward_message`.
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `load_plans`, `plan_to_manager`.
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — `pick_from_subject`, `match_from_pdf_text`, `pretty_plan`, `plan_base`.
- [tools/_lib/pdf_text.py](../tools/_lib/pdf_text.py) — `extract_full_text` (pdfplumber on a bytes blob; no disk write).
- [tools/_lib/stamp.py](../tools/_lib/stamp.py) — `render_received_stamp` (red, 7 rows, 5 editable AcroForm fields).
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — `safe_write_unique`, `sanitize_filename`.
- [tools/_lib/zip_safe.py](../tools/_lib/zip_safe.py) — `audit_and_extract_pdfs` (in-memory strict ZIP extraction), `UnsafeZipError`.

## Front-desk recovery workflow (unidentified invoices)

When the automation can't identify a strata plan from subject, body, or PDF text, the email ends up in `Inbox/Action_Required` (moved there by the end-of-run sweep after the red flag is set). The front-desk recipe lives in [`docs/client_training_brief.md`](../docs/client_training_brief.md) under "Recovery Workflow."

System side: the unidentified email is the recovery surface. No file is written to disk. The reply-to-self (the corrected forward the front desk sends) goes to the Inbox, where the next 06:00 pass's conversation-link path picks it up: the matched-subject reply finds its prior message in the same `conversationId`, pulls the PDF from that prior, routes it, and moves both messages to `processed_emails`.

## Front-desk workflow for flagged emails

The front-desk recipe for resolving red-flagged emails lives in [`docs/client_training_brief.md`](../docs/client_training_brief.md) under "Flag-Review Workflow." This file documents the system behaviour that produces the flag; the front desk's response is canonical there.

Per the universal "no manual drops" rule in CLAUDE.md, no recipe — for the front desk or the developer — ever drops a PDF directly into a manager folder. The Received stamp is only applied during Step 1; manual drops strip it and silently break Step 5/6. Recovery for any flagged email is always: get the email back into the Inbox with a corrected subject (reply-to-self for the front desk; the same recipe or a manual `python steps/step_1_intake.py` run for the developer) so Step 1 re-routes it.

For the rare partial-commit flag, the front desk escalates to the developer rather than resolving it themselves (see Edge cases below for the system-side detail).

## Edge cases
- **Inbox subfolder missing**: if `processed_emails` does not exist under Inbox, the step logs an error but still saves attachments. Mail does NOT get moved that day; create the folder and the next run handles the backlog.
- **Mislabeled PDFs (octet-stream, no extension)**: heuristic in `_looks_like_pdf_or_zip` keeps anything that smells like an invoice in the name or subject. Codex review noted this can drop a real PDF with weak Graph metadata (no extension, octet-stream, no invoice hint) — accepted as-is for now per `To-Speak-About.txt` triage; vendors in practice include filenames.
- **Imposter `.pdf` files (PNG/JPEG bytes in a PDF-named file)**: caught by `_is_real_pdf` magic-byte check after download. Email is flagged via `run.review()` and left in the Inbox; the operator can inspect.
- **Multiple attachments per email**: behaviour depends on the matching branch.
  - *Subject/body matched* → per-PDF cross-validation decides each PDF's fate; `_decide_email_action` picks ROUTE_AS_SUBJECT / AUTO_SPLIT / FLAG_AND_HOLD for the email as a whole. When the action is ROUTE_AS_SUBJECT or AUTO_SPLIT **and** the email had extras (non-PDF attachments discarded in Pass 1, or NO_PLAN PDF siblings skipped in routing), the full original email is forwarded to the plan manager and then moved to `processed_emails`.
  - *No subject/body match* → the all-or-nothing rule from `_process_pdf_text_fallback` still applies. The email moves to `processed_emails` only if every PDF (top-level or ZIP-contained) text- or filename-matched a plan and every ZIP passed the in-memory safety audit; any unmatched PDF, invalid PDF bytes, unsafe ZIP, or download failure forces leave-in-Inbox with nothing written. Discarded non-PDF/non-ZIP top-level attachments do NOT gate the rule.
- **Scanned (image-only) PDFs**: `extract_full_text` returns empty and `match_from_pdf_text` reports `"No text extracted (scanned PDF?)."`. Behaviour depends on the branch:
  - *Subject/body matched* → classified as `EMPTY`; routes on the subject's plan (no clash evidence on either side).
  - *No subject/body match* → treated as unmatched; the email stays in Inbox for operator recovery via reply-to-self.
- **PDF with text but no plan number at all**: `extract_full_text` returns text, but neither `match_from_pdf_text` (`result.detected` empty) nor `plan_match.find_explicit_plan_tokens` (no "Strata Plan …" wording) finds anything. When the *subject/body matched*, this is classified as `NO_PLAN` and routes on the subject's plan — same stance as `EMPTY`. Before 0.15.0 this was lumped into `AMBIGUOUS` and flagged, which deadlocked the reply-to-self recovery: the corrected reply went through the conversation-link path, re-ran the same cross-check against the same plan-less PDF, and re-flagged it forever. `NO_PLAN` breaks that loop.
- **PDF references an unmanaged strata plan**: the PDF text contains a plan it isn't in `Strataplan_List.xlsx` (a closed plan, a plan another firm manages, or a vendor typo). Two detection paths: (1) the plan's *prefix* IS managed but the number isn't — `match_from_pdf_text` detects it, `result.detected` is non-empty → `AMBIGUOUS`; (2) the plan's prefix is **not** managed at all (e.g. "KAS" when no KAS plans exist) — `match_from_pdf_text` can't see it, but `find_explicit_plan_tokens` catches the literal "Strata Plan KAS 9999" wording → `AMBIGUOUS`. Either way the email is **flagged and held** in the Inbox. This is deliberate: such an invoice has no manager/AP mapping, so even a corrected reply-to-self can't route it through Steps 3–6. The detector only fires on the explicit "Strata Plan …" phrasing — a bare unlabelled token (or a PO/account number) is **not** treated as a plan, to avoid re-flagging ordinary invoices. What to do with these invoices is a policy question — see the `To-Speak-About.txt` entry "PDF references an unmanaged strata plan" (2026-05-13).
- **Matched reply with no eligible prior in conversation**: logged as "no eligible prior message in conversation" or "none of the N prior message(s) had a routable PDF". Email stays in Inbox.
- **Conversation walks priors newest-first**: if the most recent attachment-bearing prior in the conversation has only non-PDF attachments (e.g. a Word doc), it's skipped and the walk continues to older messages. The first prior with a routable PDF wins.
- **Multiple corrected replies for the same conversation**: handled. Each consumed prior message ID is recorded in-run; subsequent replies in the same run exclude it from their search so the same original PDF isn't routed twice.
- **Prior message has multiple PDFs**: each prior PDF is cross-validated against the reply's plan using the same `_decide_email_action` matrix as `_process_self_attachments`. AGREE/EMPTY/NO_PLAN → route on reply's plan. Distinct-base CLASHes → auto-split per PDF. Suffix-variant collisions, consensus clash, ambiguity, or any partial commit → flag the reply (and quarantine the prior, see below). The NO_PLAN handling is what makes the reply-to-self recovery actually terminate for a PDF that carries no plan number — see the "PDF with text but no plan number at all" edge case above.
- **Reply and original processed in the same run**: two cases.
  - *Happy path (all priors clean)* → the **reply branch is the sole owner of moving the consumed prior**: it moves both the reply and the prior to their correct destination folder (`processed_emails` *or* `duplicate_emails`, per `_email_destination`), then records the prior in `consumed_priors` with the move *disposition* as the value. When the outer loop later reaches that same prior, the skip branch acts on that disposition — it never blindly re-moves (that would be a guaranteed double-move: the prior is already out of the Inbox, so Graph 404s, and a blind re-move would also wrongly hardcode `processed_emails` for priors that belong in `duplicate_emails`):
    - *disposition `None`* (source move succeeded) → the skip branch only logs and continues.
    - *disposition = a folder id* (the reply branch's source move **failed**) → the skip branch retries the move once to that folder. If the retry succeeds, the source is filed and the disposition flips to `None`; if it also fails, the source stays in the Inbox and **both** failures are in `run.errors` (so the daily summary shows `status=error`). A doubly-failed move is an infrastructure problem, not a data problem — the PDFs are already routed; on the next run the source is reprocessed and filed to `duplicate_emails` since its PDFs are in the ledger.
  - *Clash or partial-commit path* → the prior is added to `flagged_prior_ids` instead, so a later loop iteration over the same original *skips processing* AND *does not move* it. Both the reply and the original stay in the Inbox; the reply has the red flag.
- **Partial commit on a multi-PDF email**: if some PDFs route to disk and a later PDF in the same batch returns `FAILED`, the email gets the Outlook to-do flag and a `partial commit on '<subject>'` error log. Committed files are already on disk and in the duplicate ledger — there's no rollback. The front desk escalates this flag to the developer rather than resolving it themselves (per CLAUDE.md's universal no-manual-drops rule — even the developer doesn't drop files by hand; recovery is "fix the upstream cause and re-trigger Step 1" or a targeted ledger surgery for the failed file). This applies to both `_process_self_attachments` and the conversation-link path.
- **Outlook flag-set fails (Graph error)**: `_flag_message_safely` logs `FLAG_SET_FAILED: ...` at error level and returns False. The email still stays in the Inbox per its branch's normal logic, but **without the red flag** — so it looks like an ordinary unmatched email. Admins should grep `FLAG_SET_FAILED:` in the daily log and investigate Graph `Mail.ReadWrite` scope.
- **Plan with no manager_name in the snapshot**: `_route_pdf` logs an error and treats the PDF as unroutable rather than crashing the step. In the cross-validation path, the PDF's match also gets demoted to "no confident plan" (we can't route to a manager-less plan).
- **Conversation lookup fails (Graph error)**: logged with the conversation ID prefix. The reply stays in Inbox. Not silently swallowed.
- **Stamp render failure**: the unstamped PDF is saved and an error is logged; downstream still works.

## When something fails
1. Read `logs/step_1_<date>.log` for the full traceback.
2. If Graph auth fails, run `python -c "from tools._lib.graph import get_access_token; print(get_access_token()[:20])"` to check the token — re-issue the client secret if needed.
3. If a particular email keeps failing, find its `id` in the log, open it in OWA, check for a non-standard attachment shape (item attachment, reference attachment) — these are intentionally skipped.
