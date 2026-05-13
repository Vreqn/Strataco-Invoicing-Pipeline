# Step 1 — Email intake (subject + body + PDF text matching, conversation-link fallback, PDF cross-validation)

## Objective
Pull invoice attachments out of the testinvsml@stratacomgmt.com Inbox, identify the Strata Plan, route to the right manager's `To_Approve` folder (with the Received stamp applied to PDFs), and move the email into the `processed_emails` Inbox subfolder. **Unidentified emails stay in the Inbox** so the operator's reply-to-self recovery loop works. **Emails where the PDF text disagrees with the subject get the Outlook "Flag as to-do" set** so the operator can see a red flag in Outlook and intervene.

## Schedule
06:00 Mon–Fri (Windows Task Scheduler).

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx` — Strata Plan ↔ Manager ↔ AP master list.
- The mailbox's `Inbox` folder (latest 500 messages).
- An `Inbox/processed_emails` subfolder (must exist; created manually once).
- Azure app-reg credentials in `.env` (`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`).

## Matching order (first hit wins)
For each Inbox message:
1. **Subject** — `pick_from_subject(message.subject)` against the plan map.
2. **Body** — `pick_from_subject(message.bodyPreview)` reuses the same regex. NOTE: Graph returns `bodyPreview` as the first ~255 chars of the body, NOT the full body. Plan numbers buried deeper in long emails won't be matched here; the PDF-text scan is the safety net.
3. **PDF text + filename** (only if subject/body didn't match AND email has attachments) — for each PDF, `match_from_pdf_text(extract_full_text(blob))` runs first; on a confident match, that's the plan. If the body text is uninformative, `match_from_filename_with_base_fallback(base_name, rows)` is consulted as a fallback. Scored per-PDF independently. See "Mixed-batch handling" below for what happens when an email has multiple PDFs with different match outcomes.
4. **Conversation-link** (only if subject/body matched AND email has NO attachments) — look up other messages in the same `conversationId` newest-first, walk through them until a message with a routable PDF is found, route those PDFs using the matched plan. Move both the reply and the source message to `processed_emails`. The walk skips any prior IDs already consumed by an earlier reply in the same run.

## Attachment filtering (Pass 1)

Step 1 only downloads and processes **PDF and ZIP** attachments. Other attachments — signature images (Outlook-*.png, image001.png, ServiceBox-Navy.png), `.docx`, `.xlsx`, `~WRD*.jpg`, etc. — are **discarded at intake** with an INFO log line. They are not downloaded, not saved to disk, not counted as blockers.

ZIP attachments go to their own bucket: `_process_self_attachments` and `_process_pdf_text_fallback` both save them to `_Unmatched/Invoices/` on the happy path so Step 2 can unpack them. ZIPs do **not** gate `_decide_email_action` or the all-or-nothing rule — Step 2 + Step 3 handle their contents downstream.

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

Each PDF gets classified into one of four outcomes (`PdfOutcome` in `steps/step_1_intake.py`):

| Outcome | Condition |
|---|---|
| **AGREE** | PDF text confidently identifies the same plan as the subject. |
| **EMPTY** | PDF has no extractable text (scanned image / no text layer). No evidence either way. |
| **AMBIGUOUS** | PDF mentions plan tokens but the matcher can't pick one safely (e.g. two equally-scored candidates). |
| **CLASH** | PDF confidently identifies a *different* plan than the subject. |

The per-PDF classifications are combined into a single email-level action by `_decide_email_action`:

| Case | Action |
|---|---|
| Every PDF is AGREE or EMPTY | **ROUTE_AS_SUBJECT** — current behaviour, stamp using subject's plan. |
| Any PDF is AMBIGUOUS | **FLAG** — strict-first, don't trust ambiguous evidence. |
| Mix of EMPTY and CLASH | **FLAG** — can't safely route the empty PDF when its sibling disagrees. |
| All PDFs CLASH on the same plan (≠ subject) | **FLAG** — consensus clash, vendor likely mislabelled the subject. |
| Multiple PDFs, plan bases collide (e.g. `LMS4193C` + `LMS4193T`, `EPS4280` + `EPS4280A`) | **FLAG** — suffix-variant failsafe, easy to confuse. |
| Multiple PDFs, distinct plan bases (e.g. `BCS2707` + `BCS2800`) | **AUTO_SPLIT** — each PDF routes to its own plan, subject ignored. |

**FLAG behaviour:** sets the Outlook "Flag as to-do" on the message via `graph.flag_message`, leaves the email in the Inbox, writes **nothing** to disk (not even the ZIPs — the whole email stays put). The operator sees a red flag in Outlook, decides what to do, and marks it complete (green checkmark) when handled. Logged via `run.review()` (WARNING-level with `NEED_REVIEW:` prefix), which increments the `need_review` counter on the daily summary — NOT the `errors` counter, which is reserved for genuine exceptions.

**Suffix-variant detection** uses `plan_match.plan_base`, which strips a single trailing letter — the same convention as `xls.base_plan_index`. So `LMS4193C` and `LMS4193T` both reduce to base `LMS4193` (collision → flag), but `BCS2707` and `BCS2800` have distinct bases (`BCS2707` vs `BCS2800` → safe to auto-split).

**Strict-first stance:** when in doubt, flag for review. Mis-sorting silently is much worse than over-flagging. This policy can be relaxed (e.g. auto-route some suffix-variant cases, or trust the PDF outright on single-PDF clashes) once we have evidence from real operator usage that the strata managers' labelling is reliable.

## Outputs
- Matched PDFs (subject/body match) at `<STRATACO_ROOT>/Users/<Manager>/Invoices/To_Approve/<plan> - <name>.pdf` (Received stamp applied).
- ZIP attachments **on subject/body-matched emails AND on emails routed via the PDF-text-fallback happy path** at `<STRATACO_ROOT>/_Unmatched/Invoices/<name>.zip` for Step 2 to extract.
- Non-PDF/non-ZIP attachments (signature images, .docx, .xlsx) are **discarded at intake** with an INFO log — not saved anywhere.
- Subject/body-matched emails are moved to `Inbox/processed_emails`.
- Successfully PDF-text-fallback-routed emails (every PDF text-matched a plan AND any ZIPs saved cleanly) are routed and moved to `processed_emails`.
- **All-or-nothing leave-in-Inbox rule:** any email that doesn't get a subject/body match AND has an unmatched PDF, invalid PDF bytes, or download failure stays in the Inbox with nothing written to disk. ZIPs and discarded attachments do NOT trigger the rule. The email itself is the recovery surface; the operator replies-to-self with the corrected subject and the next morning's conversation-link pass routes everything in the thread together.
- One row appended to `logs/daily_summary.csv` (7 columns: `date, step, processed, need_review, errors, duration_sec, status`) and a detailed log at `logs/step_1_<date>.log`.

## All-or-nothing rule (for emails without a subject/body plan match)

When the subject and body don't yield a plan, Step 1 still tries to match each PDF by **PDF text first, filename second** (per `_classify_pdf_against_subject`'s order). But it commits the email's fate **all at once**:

- **Every PDF text- or filename-matched a plan AND any ZIPs save cleanly** → route the PDFs, save the ZIPs to `_Unmatched/Invoices/` for Step 2, move the email to `processed_emails`.
- **Anything less** (any unmatched PDF, invalid PDF bytes, download failure) → leave the entire email in the Inbox. **Nothing is written to disk.** Even the would-have-matched PDFs and the otherwise-saveable ZIPs are not committed.

ZIPs by themselves are always routable (Step 2 unpacks them downstream), and discarded attachments don't exist past Pass 1, so neither category gates the decision — only unmatched/invalid PDFs and download failures do. An email with one routable PDF and one ZIP commits both. An email with one ambiguous-text PDF and one ZIP commits neither.

Why all-or-nothing rather than save unmatched stuff to `_Unmatched/`? Because operators don't routinely check `_Unmatched/Invoices/` — their workflow looks at the Inbox. Saving something to `_Unmatched/` is effectively hiding it. Keeping the email in the Inbox preserves the "Inbox is the single source of truth" principle.

The all-or-nothing log line (via `run.review()`) lists each attachment's outcome: which PDFs would have routed, which didn't, which ZIPs were deferred (not saved due to all-or-nothing), and any invalid PDF bytes detected. IT can grep the log if the operator asks "which one was the problem?"

The rough edges of this design — multi-strata PDFs in one email, ZIPs without identifying info, non-invoice PDFs that get routed alongside invoice PDFs — are documented in `To-Speak-About.txt` for client policy direction.

## Partial-commit Outlook flag (both branches)

Both `_process_self_attachments` and `_process_pdf_text_fallback` carry a partial-commit guard: if any attachment in an email returned `FAILED` after others committed (PDF routed but ZIP write failed, or vice versa), `_flag_message_safely` sets the Outlook to-do marker and the log line `partial commit on '<subject>'` (or `... (pdf-text fallback)`) is written via `run.error()`. Without this, the email would otherwise look like ordinary unmatched mail despite having committed content downstream. Committed writes cannot be rolled back; the loud log + red flag are the operator-facing signal.

## Run
```
python steps/step_1_intake.py
```

## Tools used
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `list_inbox_messages`, `list_conversation_messages`, `list_attachments`, `download_attachment`, `find_child_folder_id`, `move_message_to_folder`, `flag_message`.
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `load_plans`, `plan_to_manager`.
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — `pick_from_subject`, `match_from_pdf_text`, `pretty_plan`, `plan_base`.
- [tools/_lib/pdf_text.py](../tools/_lib/pdf_text.py) — `extract_full_text` (pdfplumber on a bytes blob; no disk write).
- [tools/_lib/stamp.py](../tools/_lib/stamp.py) — `render_received_stamp` (red, 7 rows, 5 editable AcroForm fields).
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — `safe_write_unique`, `sanitize_filename`.

## Operator recovery workflow (unidentified invoices)

When the automation can't identify a strata plan from subject, body, or PDF text, the email stays in the Inbox. The operator handles it manually with one of two paths:

1. **Reply-to-self with corrected subject (primary path).** Hit Reply on the unmatched email, change the To: from the vendor's address to `testinvsml@stratacomgmt.com`, edit the subject to include the strata number (e.g. `BCS 2707 — Re: Invoice attached`), Send. **No PDF re-attachment needed.** Next 06:00 pass matches the subject, looks up the conversation, pulls the PDF from the original message, routes it, and moves both messages to `processed_emails`.

2. **Bounce to the vendor.** Reply to the vendor asking which strata. When the vendor responds in the same thread (with or without re-attaching the PDF), the response gets processed normally — if it carries identifying text in subject/body, the conversation-link path resolves the PDF from earlier in the thread.

The Reply gotcha: the To: field auto-fills with the vendor's address. If the operator forgets to change it, the reply goes to the vendor with `BCS 2707` in the subject — harmless, just re-send to the right address. An Outlook desktop **Quick Step** can pre-fill the To: field; see README for setup.

## Operator workflow for flagged emails (red flag in Outlook)

When the system can't decide between the subject and the PDF text — or when a multi-PDF email contains suffix-variant strata — it sets the Outlook "Flag as to-do" on the message. The operator sees a red flag next to the email in their Inbox view. The whole email stays in the Inbox; nothing is written to disk.

What the operator does:

1. **Open the flagged email** and check the daily log (`logs/step_1_<date>.log`) for the `pdf-classify:` lines — they show each PDF's classification (`AGREE`/`CLASH`/`EMPTY`/`AMBIGUOUS`), the plan the PDF claims, and the top detected tokens.
2. **Decide which plan(s) apply.** Two flavours of flag exist:
   - **Subject-vs-PDF clash** (single PDF, or consensus across PDFs): either the vendor mistyped the subject or the PDF mentions another plan in passing. Inspect the PDF, pick the right plan.
   - **Suffix-variant clash** (e.g. one PDF for `LMS 4193C`, another for `LMS 4193T` in the same email): vendors confuse these. Confirm each PDF's plan in the document, then route them separately.
3. **Resolve the email**, using one of:
   - **Reply-to-self with the corrected subject** (when one plan applies to everything): the next 06:00 pass picks it up and routes via the conversation-link path.
   - **Manually split**: drag each PDF to the correct manager's `To_Approve` folder by hand and apply the Received stamp via the editor's template. Then move the original email to `processed_emails`.
4. **Mark the Outlook flag complete.** The red flag becomes a green checkmark, signalling that this one is done.

## Edge cases
- **Inbox subfolder missing**: if `processed_emails` does not exist under Inbox, the step logs an error but still saves attachments. Mail does NOT get moved that day; create the folder and the next run handles the backlog.
- **Mislabeled PDFs (octet-stream, no extension)**: heuristic in `_looks_like_pdf_or_zip` keeps anything that smells like an invoice in the name or subject. Codex review noted this can drop a real PDF with weak Graph metadata (no extension, octet-stream, no invoice hint) — accepted as-is for now per `To-Speak-About.txt` triage; vendors in practice include filenames.
- **Imposter `.pdf` files (PNG/JPEG bytes in a PDF-named file)**: caught by `_is_real_pdf` magic-byte check after download. Email is flagged via `run.review()` and left in the Inbox; the operator can inspect.
- **Multiple attachments per email**: behaviour depends on the matching branch.
  - *Subject/body matched* → the new PDF cross-validation (see above) decides per-PDF, then `_decide_email_action` picks ROUTE_AS_SUBJECT / AUTO_SPLIT / FLAG_AND_HOLD for the email as a whole.
  - *No subject/body match* → the all-or-nothing rule from `_process_pdf_text_fallback` still applies. The email moves to `processed_emails` only if every PDF text- or filename-matched a plan and any ZIPs save cleanly; any unmatched PDF, invalid PDF bytes, or download failure forces leave-in-Inbox with nothing written. ZIPs and discarded non-PDF/non-ZIP attachments do NOT gate the rule.
- **Scanned (image-only) PDFs**: `extract_full_text` returns empty and `match_from_pdf_text` reports `"No text extracted (scanned PDF?)."`. Behaviour depends on the branch:
  - *Subject/body matched* → classified as `EMPTY`; routes on the subject's plan (no clash evidence on either side).
  - *No subject/body match* → treated as unmatched; the email stays in Inbox for operator recovery via reply-to-self.
- **Matched reply with no eligible prior in conversation**: logged as "no eligible prior message in conversation" or "none of the N prior message(s) had a routable PDF". Email stays in Inbox.
- **Conversation walks priors newest-first**: if the most recent attachment-bearing prior in the conversation has only non-PDF attachments (e.g. a Word doc), it's skipped and the walk continues to older messages. The first prior with a routable PDF wins.
- **Multiple corrected replies for the same conversation**: handled. Each consumed prior message ID is recorded in-run; subsequent replies in the same run exclude it from their search so the same original PDF isn't routed twice.
- **Prior message has multiple PDFs**: each prior PDF is cross-validated against the reply's plan using the same `_decide_email_action` matrix as `_process_self_attachments`. AGREE/EMPTY → route on reply's plan. Distinct-base CLASHes → auto-split per PDF. Suffix-variant collisions, consensus clash, ambiguity, or any partial commit → flag the reply (and quarantine the prior, see below).
- **Reply and original processed in the same run**: two cases.
  - *Happy path (all priors clean)* → the prior is added to `consumed_prior_ids` on the first successful PDF route, so a later loop iteration over the same original skips it and moves both to `processed_emails`.
  - *Clash or partial-commit path* → the prior is added to `flagged_prior_ids` instead, so a later loop iteration over the same original *skips processing* AND *does not move* it. Both the reply and the original stay in the Inbox; the reply has the red flag.
- **Partial commit on a multi-PDF email**: if some PDFs route to disk and a later PDF in the same batch returns `FAILED`, the email gets the Outlook to-do flag and a `partial commit on '<subject>'` error log. Committed files are already on disk and in the duplicate ledger — there's no rollback. Operator reconciles by hand. This applies to both `_process_self_attachments` and the conversation-link path.
- **Outlook flag-set fails (Graph error)**: `_flag_message_safely` logs `FLAG_SET_FAILED: ...` at error level and returns False. The email still stays in the Inbox per its branch's normal logic, but **without the red flag** — so it looks like an ordinary unmatched email. Admins should grep `FLAG_SET_FAILED:` in the daily log and investigate Graph `Mail.ReadWrite` scope.
- **Plan with no manager_name in the snapshot**: `_route_pdf` logs an error and treats the PDF as unroutable rather than crashing the step. In the cross-validation path, the PDF's match also gets demoted to "no confident plan" (we can't route to a manager-less plan).
- **Conversation lookup fails (Graph error)**: logged with the conversation ID prefix. The reply stays in Inbox. Not silently swallowed.
- **Stamp render failure**: the unstamped PDF is saved and an error is logged; downstream still works.

## When something fails
1. Read `logs/step_1_<date>.log` for the full traceback.
2. If Graph auth fails, run `python -c "from tools._lib.graph import get_access_token; print(get_access_token()[:20])"` to check the token — re-issue the client secret if needed.
3. If a particular email keeps failing, find its `id` in the log, open it in OWA, check for a non-standard attachment shape (item attachment, reference attachment) — these are intentionally skipped.
