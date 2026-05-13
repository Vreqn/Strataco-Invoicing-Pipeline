# Strataco Invoicing System — Client Training Brief

> **Who this is for:** Operators, managers, and accountants who use the system day-to-day. It follows one invoice from inbox to permanent archive, and shows exactly when a human needs to step in.
>
> **For a visual version:** Section 5 has a ready-to-paste prompt for Claude.ai, Canva AI, or Google Slides AI.

---

## Section 1: Overview

Every weekday from 6:00 AM, the system pulls invoices from the shared inbox, figures out which strata plan each one belongs to, and routes it to the right person. The manager approves, the accountant records the payment, and the system files everything in the correct Strata Plan folder by the next morning — named, stamped, and retrievable.

Once a month, it also bundles each plan's paid invoices into a single **Monthly Summary** PDF the manager can attach to the board's statement in one click.

**Three human touchpoints, everything else is automatic:**

- **Manager** — decides whether to approve an invoice.
- **Accountant** — fills in Date and Check Number on approved invoices.
- **Front-Desk Operator** — only steps in when the system can't identify a plan (rare). They reply to the email with the plan number; the next morning's run picks it up.

---

## Section 2: The Life of an Invoice — Step by Step

### Step 1 — Invoice Arrives
**When:** 6:00 AM, weekdays

**What the system does:**
- Checks the shared inbox (`testinvsml@stratacomgmt.com`) for new emails with PDFs and ZIPs (signature images, Word docs, and Excel files are ignored on the way in)
- Tries three places, in order, to identify the Strata Plan:
  1. Email subject line
  2. Email body text
  3. The PDF content itself
- **Cross-checks each PDF against the subject** as a second opinion. The PDF's body text is checked first; if the text doesn't yield a confident answer, the filename is consulted as a fallback (vendors put plan IDs in filenames on purpose). If the body text and subject agree (or the PDF is a scanned image with no text), routes normally. If they disagree in a way the system can't safely resolve, **the email gets a red Outlook "to-do" flag** and stays in the inbox for an operator to review.
- **Sanity-checks PDF bytes.** A file named `something.pdf` that's actually a PNG or corrupted bytes is rejected and the email is flagged — the system won't drop garbage into the manager's queue.
- If matched and confirmed: stamps the PDF "Received" (red) and drops it in the right manager's folder
- If not matched at all: the email **stays in the inbox** for an operator to tag

**What you do:** Nothing in the normal case. If an email is sitting in the inbox the next morning **without** a red flag, see the **Recovery Workflow** below. If an email has a **red flag**, see the **Flag-Review Workflow** below.

---

### Step 2 — Unpacking ZIP Files
**When:** 6:10 AM

**What the system does:** Looks in the local `_Unmatched/Invoices/` folder for ZIP files that Step 1 saved there. Unpacks each one and writes the extracted PDFs back into the same `_Unmatched/Invoices/` folder so Step 3 can sort them. The original ZIP is renamed `Processed-YYYYMMDD-HHMMSS-<original>.zip` so it doesn't get re-unpacked on the next run.

*Note: Step 2 does NOT read from the email Inbox directly. Step 1 is responsible for downloading ZIP attachments from emails and placing them into `_Unmatched/Invoices/`. If Step 2 reports "found 0 zip(s)", it means either there were no ZIPs in the morning's email or Step 1 didn't save any — check the Step 1 log for `saved zip to _Unmatched` lines to confirm.*

**What you do:** Nothing.

---

### Step 3 — Sorting `_Unmatched` PDFs
**When:** 6:20 AM

**What the system does:** Reads PDFs from `_Unmatched/Invoices/` — both PDFs that Step 2 just extracted from ZIPs AND any PDFs that Step 1 dumped there directly. For each PDF it tries the filename first (`EPS 6008 - Invoice.pdf` routes to the EPS 6008 manager), then the PDF text as a fallback, then writes the file to that manager's `To_Approve/` with a "Received" stamp. PDFs it can't safely match are left in `_Unmatched` for manual sorting.

**What you do:** Nothing in most cases. If a PDF lands in `_Unmatched`, the system administrator renames it manually with the correct plan code so the next Step 3 run picks it up.

> Step 3 is the fallback sorter for any PDF in `_Unmatched/Invoices/` — not just ZIP-extracted ones. If Step 1 couldn't safely route a PDF directly to a manager but didn't flag the email, the PDF lands here for Step 3 to take another pass.

---

### Step 4 — Manager Reviews and Approves
**When:** Manager receives an email around 6:30 AM

**What the system does:** Sends each manager a daily summary listing every invoice waiting on them (new today + carried over from previous days). Nothing is approved automatically.

**What the Manager does:**
1. Read the morning email.
2. Open each invoice in your `To_Approve` folder.
3. Drag the ones you approve into your `Approved` folder.
4. Leave the rest — they'll reappear in tomorrow's email.

**Watch for:** Approved an invoice but forgot to move it? It just shows up again tomorrow. Nothing is lost.

---

### Step 5 — Approved Invoice Goes to Accounts Payable
**When:** 6:40 AM

**What the system does:**
- Picks up everything in each manager's `Approved` folder
- Applies a blue "Paid" stamp with two blank fields: **Date** and **Check Number**
- Delivers the stamped PDF to the accountant's `Approved_Invoices` folder
- Emails the accountant a list of new arrivals

**What the Accountant does:**
1. Open the invoice in Adobe Acrobat.
2. Fill in the **Date** and **Check Number** fields.
3. Save using **File → Export as PDF** (or **Print → Save as PDF**) — **NOT** regular Save.
4. Move the saved file to your `Paid_Invoices` folder.

> **Critical:** Regular Save leaves the fields editable, and the next step can't read the Check Number. Always Export or Print-to-PDF.

---

### Step 6 — Permanent Filing
**When:** 7:00 AM the following morning

**What the system does:** Reads the Check Number from each PDF in `Paid_Invoices`, renames the file using it, and files it in the correct Strata Plan archive.

**What you do:** Nothing.

**Watch for:** If the morning "Invoices summary" email lists your file under the **Action Required → Paid invoices stuck** section, the PDF was probably saved with regular Save in Step 5. Re-save it correctly using Export and the next morning's run picks it up.

---

### Step 7 — Monthly Summary
**When:** Once a month, scheduled by the administrator — usually the 5th to 10th of the new month. Always covers the **previous** calendar month.

**Example:** A run on May 7 produces summaries for April. A run on June 8 produces summaries for May.

**What the system does:**
- For every active Strata Plan, collects all invoices paid the previous month
- Merges them into one PDF in check-number order, e.g. `Summary - 04 - BCS1234 April 2026 inv.pdf`
- Saves it in the plan's main archive folder
- Moves the source PDFs into a tidy `Processed/2026-04/` subfolder
- Emails the administrator a recap

**What the Manager does:** Nothing automatic-side. When you send the monthly statement to the board, attach the one Summary PDF instead of a folder full of separate checks.

**Where to find it:** In the plan's archive folder (e.g. `Strata_Plans\BCS 1234`), look for a file starting with `Summary -`. The originals are one level deeper in `Processed\2026-04\`.

**Watch for:**
- **Two summaries in one month?** A late check landed after the original run. The new file ends in `(1).pdf` and contains just that late check. The original Summary is left untouched so any links you've already shared still work. Decide whether to forward the `(1)` separately, replace the original, or leave both.
- The Summary is a convenience for sharing, **not** a replacement for the originals. Every individual PDF is still on file in `Processed/`.

---

### Recovery Workflow — When the System Couldn't Identify a Plan
**When:** Any email still sitting in the inbox after the 6:00 AM run.

**What happened:** The system checked the subject, body, and PDF text. Nothing matched a known plan. It left the email alone so a human can tag it.

**What the Operator does:**

1. Open the email in the shared inbox.
2. Hit **Reply**.
3. **Change the To: field** from the vendor's address to `testinvsml@stratacomgmt.com`. ← *most important step*
4. **Edit the subject** to include the strata number, e.g. `BCS 2707 — Invoice attached`.
5. Send. You do **not** need to re-attach the PDF.

Next morning, the 6:00 AM run sees your reply, reads the strata number, finds the original PDF in the thread, and routes everything. Both emails move to `processed_emails` automatically.

> **The Reply gotcha:** Outlook fills To: with the vendor's address by default. **Change it to `testinvsml@stratacomgmt.com`** before sending. If you forget and it goes to the vendor — no harm done, just resend to the right address.

**Optional — Outlook "Quick Step":** The administrator can add a one-click "Tag for Automation" button to your ribbon that opens a Reply with To: already filled in. Nice-to-have, not required.

**Alternative — ask the vendor:** Reply to the vendor asking "Which Strata Plan is this for?" When they answer in the thread, the next morning's run picks it up automatically.

**Watch for:**
- An email sitting in the inbox for several days = nobody has tagged it yet.
- **Always use Reply, not Forward.** Forward creates a new email the system can't link back to the original PDF.

---

### Flag-Review Workflow — When the System Wasn't Sure
**When:** Any email in the inbox that shows a **red Outlook flag** (the standard "Follow up" / "To-do" flag).

**What happened:** The system identified a plan from the subject, but when it cross-checked the PDF text (or while processing the batch) it found something it didn't feel safe resolving on its own. Three flavours of flag exist:

1. **The subject and the PDF point to different plans.** Either the vendor mistyped the subject line, or the PDF mentions another plan number in passing. The system doesn't know which one is right, so it leaves both alone for you to decide.

2. **The email has multiple PDFs for very similar plans** (e.g. one for `LMS 4193C`, another for `LMS 4193T`). These are different stratas with the same base number — vendors confuse them all the time. The system refuses to auto-route in case the PDFs themselves got mixed up.

3. **Partial commit on a multi-PDF email.** The system started routing the batch and got partway through before something failed (network glitch, missing manager in the plan list, file-write error). One or more PDFs are *already* in their manager's folder; the rest aren't. The email is flagged so you can finish the job manually. Look in the daily log (`logs/step_1_<date>.log`) for a line starting with `partial commit on` — it tells you how many landed before the failure.

**What the Operator does:**

For flavours #1 and #2 (clash / suffix variants):

1. **Open the flagged email.** The email and its attachments are all still in the inbox; nothing has been routed yet.
2. **Open each attached PDF** and confirm which Strata Plan it actually belongs to. The plan number is usually printed at the top of the invoice or in the property address block.
3. **Resolve the email** using one of:
   - **One plan applies to everything** → Hit **Reply**, change the **To:** to `testinvsml@stratacomgmt.com`, edit the subject to include the correct strata number, send. (Same as the Recovery Workflow above.) The next 6:00 AM run picks it up and routes it.
   - **Different plans apply to different PDFs** → Download each PDF, rename it with the correct plan code (e.g. `LMS 4193C - vendor invoice.pdf`), drop each one into the matching manager's `To_Approve` folder by hand. Then move the original email into `processed_emails`.
4. **Mark the Outlook flag complete.** Right-click the flag → **Mark Complete**. The red flag becomes a green checkmark, signalling that this one is handled.

For flavour #3 (partial commit) — different recipe because *some PDFs are already routed*:

1. **Open the daily log** at `logs/step_1_<date>.log` and search for `partial commit on '<the subject>'`. The log line tells you how many of the email's attachments routed before the failure.
2. **Check each manager folder** the email's PDFs *should have* gone to. The ones already routed are sitting there with the Received stamp; the failed ones aren't.
3. **Manually route the missing PDFs** the same way as flavour #2 above — download from the email, rename with the right plan code, drop into the manager's `To_Approve` folder by hand.
4. **Do NOT reply-to-self for this case.** Reply-to-self would try to route the *whole batch* again, and the already-routed PDFs would be caught by the duplicate detector — wasted cycles. Just place the missing ones manually.
5. **Move the original email** into `processed_emails`, then **Mark the Outlook flag complete.**

**A note on conversation-link flags:** if a reply-to-self triggered the flag (you replied yesterday with the corrected subject; the system found the PDF in the thread and flagged it today because the PDF disagrees with your reply), you'll see **two messages** in the Inbox — the flagged reply AND the original PDF-bearing email, both untouched. Treat them as a pair: when you mark the reply's flag complete and move both into `processed_emails`, you're closing the whole thread.

**Watch for:**
- **Red flag means human eyes needed.** The system isn't trying to be obstinate — it's actively protecting you from a mis-route. Better a small interrupt now than a mis-filed invoice you discover next month.
- **The PDF-cross-check is strict on purpose.** If the strata managers prove their labelling is consistent over time, the administrator can relax specific cases.
- **Don't ignore the flag.** Flagged emails don't get retried automatically the next morning; they wait for you.
- **Partial commits are not data loss.** Some PDFs are already where they need to be; the daily log tells you exactly which. The flag is asking you to finish what the system started, not redo it.

---

## Section 3: Roles at a Glance

| Role | Daily Task | When |
|------|-----------|------|
| **Front-Desk Operator** | Check the shared inbox for un-tagged invoice emails AND any emails with a red Outlook flag. Tag un-matched ones via Reply-to-self; resolve flagged ones by confirming which plan(s) apply, then either reply-to-self or manually drop each PDF into the right folder. Mark each flag complete when handled. | Once a morning |
| **Manager** | Review `To_Approve` folder. Move approved invoices to `Approved`. | After 6:30 AM email |
| **Manager (monthly)** | Grab the `Summary - … inv.pdf` from each plan's archive folder and attach it to the board's monthly statement. | Once a month |
| **Accountant** | For each invoice in `Approved_Invoices`: fill in Date + Check Number, save via **Export as PDF**, move to `Paid_Invoices`. | During business day |
| **System Administrator** | Check `_Unmatched` for un-sortable PDFs. Keep `Strataplan_List.xlsx` current. Review the monthly recap email. Periodically grep the daily logs for `FLAG_SET_FAILED:` and `partial commit on` — both indicate operator-visible issues the system has surfaced. | As needed |

---

## Section 4: Common Questions

**Invoice went to the wrong manager?**
The routing comes from the Strataplan List spreadsheet. Update the spreadsheet and the next invoice routes correctly.

**Forgot to move an invoice to Approved — is it lost?**
No. It reappears in tomorrow's email.

**See a file under "Paid invoices stuck" in the morning summary?**
The morning "Invoices summary" email has an **Action Required** section. If your file appears under **Paid invoices stuck**, almost always it means the PDF was saved with regular Save in Step 5. Re-open it, confirm the Check Number is filled, and re-save using **File → Export as PDF**. The next morning's run picks it up.

**An invoice email is still in the inbox the next morning?**
The system couldn't identify the plan. Use the Recovery Workflow: Reply, change To: to `testinvsml@stratacomgmt.com`, add the strata number to the subject, send.

**There's a PDF in `_Unmatched`. Where did it come from?**
Almost always from a ZIP that Step 2 unpacked but Step 3 couldn't identify by filename or PDF text. Rename it to include the plan code (e.g. `LMS1234 - invoice.pdf`) and drop it in the right manager's `To_Approve`.

*Note: As of the 0.11.2 update, Word docs, Excel files, and signature images are no longer saved to `_Unmatched` — they're discarded at intake (Step 1 logs each one). If a vendor sends a real invoice as a Word or Excel file, ask them to resend as PDF.*

**Hit Reply but forgot to change the To: field — what now?**
The reply went to the vendor with your edited subject. They'll see something like `BCS 2707 — Re: Invoice attached` and probably ignore it. Just resend to `testinvsml@stratacomgmt.com`. No data lost.

**Can I use Forward instead of Reply?**
No. Reply links back to the original PDF in the email thread; Forward creates a new email the system may not be able to trace. Always Reply.

**An email has invoices for DIFFERENT properties — what do I do?**
The system handles this for you in most cases. If each PDF clearly identifies its own *distinct* strata plan (e.g. one for `BCS 2707`, one for `BCS 2800`), the morning run will **auto-split** them — each PDF goes to its own manager regardless of what the subject says. If the PDFs land on **similar plans that share a base** (e.g. `LMS 4193C` and `LMS 4193T`), the email gets **flagged with the red Outlook to-do flag** instead — vendors confuse those, so the system asks for a human check. Use the **Flag-Review Workflow** above to resolve it.

**An email has a red flag on it — what does that mean?**
The system found one of three things and didn't feel safe routing on its own: a subject-vs-PDF conflict, two PDFs for very-similar plans (suffix variants like `LMS 4193C` vs `LMS 4193T`), or a *partial commit* where some PDFs already routed before a failure stopped the batch. Open the email, check the daily log (`logs/step_1_<date>.log`) for the matching `pdf-classify:` or `partial commit on` line, then either reply-to-self with the correct subject (clash / suffix-variant cases) OR manually route the missing PDFs (partial-commit case). Mark the flag complete when done. See the **Flag-Review Workflow** for the full steps.

**The red flag is gone but the email is still in the inbox — did something go wrong?**
Possibly. Check the daily log for a line starting with `FLAG_SET_FAILED:`. That means the system *tried* to set the red flag but Microsoft Graph rejected the request (usually a permission scope issue), so the email is sitting unflagged when it should have one. Ask the administrator to investigate. In the meantime, treat any email in the inbox that has attachments AND a subject line that *looks* matched as suspicious — it may be one of the un-flagged clashes the system was trying to surface.

**New Strata Plan coming on?**
Tell the administrator. The plan needs to be added to `Strataplan_List.xlsx` with the right Manager and Accounts Payable contact *before* invoices start arriving.

**Skipped a Monthly Summary run?**
Nothing is lost. The administrator can re-run any past month with `--month YYYY-MM`. The originals stay in the plan folder until the run completes.

**Two Summary files for one month — one ends in `(1).pdf`?**
A late check landed after the original run. The `(1)` contains just that one check; the original is untouched so any link you've already sent the board still works. Forward the `(1)`, replace the original, or leave both — your call.

---

## Section 5: Design Brief for Visual Tool

> Paste the prompt below into **Claude.ai**, **Canva AI**, or **Google Slides AI** along with the contents of this document to generate a visual version.

---

**Prompt:**

> Create a professional visual training document for non-technical staff (front-desk operators, property managers, accountants) at a strata management company.
>
> **Layout:** Horizontal swimlane process flow with three lanes:
> - Lane 1 — **The Invoice** (document moving through the process)
> - Lane 2 — **Automated System** (grey/blue boxes, what runs automatically)
> - Lane 3 — **Your Action** (warm accent colour, where a person acts)
>
> **Daily strip:** Steps 1-6 across the top, each labelled with its time (6:00 AM to 7:00 AM next morning).
>
> **Monthly band:** Step 7 in a visually distinct band below — different colour, different orientation — so readers see it's separate from the daily rhythm.
>
> **Highlight human touchpoints:**
> - Step 4 — Manager approves invoices
> - Step 5 — Accountant fills Date + Check Number, saves via Export-to-PDF
> - Step 7 — Manager attaches Summary PDF to monthly board statement
>
> **Recovery callout:** A separate sidebar or "When something goes wrong" panel — *not* a numbered step — showing the operator's Reply workflow. Visually offset so it's clear this is a fallback, not a daily ritual.
>
> **Most prominent element on the Recovery callout:** the "Change To: to `testinvsml@stratacomgmt.com`" instruction. Bold, coloured, icon or warning indicator. This is the single most error-prone step in the entire system.
>
> **After the swimlane:**
> - A "Roles at a Glance" table
> - A "Common Questions" Q&A section
>
> **Style:** Clean, corporate, minimal jargon, large fonts for slide or A4 print.
>
> **Content:** [paste the full content of this document here]
