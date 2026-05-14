# Strataco Invoicing System — Client Training Brief

> **Who this is for:** Front desk, managers, and accountants who use the system day-to-day. It follows one invoice from inbox to permanent archive, and shows exactly when a human needs to step in.
>
> **For a visual version:** Section 5 has a ready-to-paste prompt for Claude.ai, Canva AI, or Google Slides AI.

---

## Section 1: Overview

Every weekday from 6:00 AM, the system pulls invoices from the shared inbox, figures out which strata plan each one belongs to, and routes it to the right person. The manager approves, the accountant records the payment, and the system files everything in the correct Strata Plan folder by the next morning — named, stamped, and retrievable.

Once a month, it also bundles each plan's paid invoices into a single **Monthly Summary** PDF the manager can attach to the board's statement in one click.

**Three human touchpoints, everything else is automatic:**

- **Manager** — decides whether to approve an invoice.
- **Accountant** — fills in Date and Check Number on approved invoices.
- **Front desk** — only steps in when the system can't identify a plan (rare). They reply to the email with the plan number; the next morning's run picks it up.

> **Where you look, and where you don't.**
> The front desk only looks at Outlook (the shared Inbox plus any emails carrying a red flag) and the daily summary email that arrives shortly after 7:00 AM. That's it — no log files, no automation folders, no command-line anything. If something needs attention, it shows up as a red flag in the Inbox or as a line in the morning summary. If something looks wrong and the recipes in this brief don't cover it, **tell the developer (Krisztian)** — don't try to fix it yourself.

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
- **Cross-checks each PDF against the subject** as a second opinion. The PDF's body text is checked first; if the text doesn't yield a confident answer, the filename is consulted as a fallback (vendors put plan IDs in filenames on purpose). If the body text and subject agree (or the PDF is a scanned image with no text), routes normally. If they disagree in a way the system can't safely resolve, **the email gets a red Outlook "to-do" flag** and stays in the inbox for the front desk to review.
- **Sanity-checks PDF bytes.** A file named `something.pdf` that's actually a PNG or corrupted bytes is rejected and the email is flagged — the system won't drop garbage into the manager's queue.
- If matched and confirmed: stamps the PDF "Received" (red) and routes it into the right manager's folder.
- If not matched at all: the email **stays in the inbox** for the front desk to tag.

**What you do:** Nothing in the normal case. If an email is sitting in the inbox the next morning **without** a red flag, see the **Recovery Workflow** below. If an email has a **red flag**, see the **Flag-Review Workflow** below.

---

### Step 2 — Unpacking ZIP Files
**When:** 6:10 AM

**What the system does:** A safety-net pass that picks up any ZIP files sitting in the automation's holding area (rare now — Step 1 unpacks ZIPs in memory during intake as of 0.14.0). Unpacks them and hands the contents to Step 3 to sort.

**What you do:** Nothing.

---

### Step 3 — Sorting any leftover PDFs
**When:** 6:20 AM

**What the system does:** A safety-net pass that sorts any leftover PDFs in the automation's holding area into the right manager's queue. Rarely fires in normal operation now that Step 1 handles most matching during intake.

**What you do:** Nothing. If a PDF can't be sorted automatically, the developer handles it.

---

### Step 4 — Manager Reviews and Approves
**When:** Manager receives an email around 6:30 AM

**What the system does:** Sends each manager a daily summary listing every invoice waiting on them (new today + carried over from previous days). Nothing is approved automatically.

**What the Manager does:**
1. Read the morning email.
2. Open each invoice in your `To_Approve` folder.
3. Fill in any required Received-stamp fields (the red stamp at the top right).
4. Save with **Ctrl+S** (regular save — do NOT use Print-to-PDF or Export-as-PDF).
5. Drag the ones you approve into your `Approved` folder.
6. Leave the rest — they'll reappear in tomorrow's email.

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
1. Open the invoice in Adobe Acrobat from your `Approved_Invoices` folder.
2. Fill in the **Date** and **Check Number** fields on the blue Paid stamp.
3. Save with **Ctrl+S** (regular save — do NOT use Print-to-PDF or Export-as-PDF; Step 6 handles flattening).
4. Drag the file to your `Paid_Invoices` folder.

> **Critical:** Save with **Ctrl+S** — do not Print-to-PDF or Export-as-PDF. The automation reads the AcroForm field values directly and flattens the PDF at archive time; a Print-to-PDF copy turns text into an image that Step 6 can't read.

---

### Step 6 — Permanent Filing
**When:** 7:00 AM the following morning

**What the system does:** Reads the Check Number from each PDF in `Paid_Invoices`, renames the file using it, and files it in the correct Strata Plan archive.

**What you do:** Nothing.

**Watch for:** If the morning "Invoices summary" email lists your file under the **Action Required → Paid invoices stuck** section, the Paid stamp's Date or Check Number fields are probably blank. Open the PDF, fill them in, Ctrl+S, and the next morning's run picks it up.

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

**What the Front desk does:**

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

**What happened:** The system tried to route the email but spotted something it didn't feel safe resolving on its own. Three flavours of flag exist:

1. **The subject and the PDF point to different plans.** Either the vendor mistyped the subject line, or the PDF mentions another plan number in passing. The system doesn't know which one is right, so it leaves both alone for you to decide.

2. **The email has multiple PDFs for very similar plans** (e.g. one for `LMS 4193C`, another for `LMS 4193T`). These are different stratas with the same base number — vendors confuse them all the time. The system refuses to auto-route in case the PDFs themselves got mixed up.

3. **"Partial commit" mentioned in the flag.** Rare — something went wrong partway through processing. **Don't try to finish it yourself.** Tell the developer (Krisztian) — partial commits need a closer look than a normal flag.

**What the Front desk does:**

For flavours #1 and #2 (clash / suffix variants):

1. **Open the flagged email.** The email and its attachments are all still in the inbox; nothing has been routed yet.
2. **Open each attached PDF** and confirm which Strata Plan it actually belongs to. The plan number is usually printed at the top of the invoice or in the property address block.
3. **Resolve the email** using one of:
   - **One plan applies to everything** → Hit **Reply**, change the **To:** to `testinvsml@stratacomgmt.com`, edit the subject to include the correct strata number, send. (Same as the Recovery Workflow above.) The next 6:00 AM run picks it up and routes it.
   - **Different plans apply to different PDFs** → Reply to the vendor and ask them to resend as separate emails — one per plan. When their replies arrive (each with one plan in the subject), the system handles them cleanly. *Policy note: pending a client decision on whether the system should auto-route these when each PDF clearly identifies its own variant.*
4. **Mark the Outlook flag complete.** Right-click the flag → **Mark Complete**. The red flag becomes a green checkmark, signalling that this one is handled.

For flavour #3 (partial commit): **escalate to the developer, don't resolve it yourself.** Leave the flag in place; the developer will tell you when it's clear.

**A note on conversation-link flags:** if a reply-to-self triggered the flag (you replied yesterday with the corrected subject; the system found the PDF in the thread and flagged it today because the PDF disagrees with your reply), you'll see **two messages** in the Inbox — the flagged reply AND the original PDF-bearing email, both untouched. Treat them as a pair: when you mark the reply's flag complete and move both into `processed_emails`, you're closing the whole thread.

**Watch for:**
- **Red flag means human eyes needed.** The system isn't trying to be obstinate — it's actively protecting you from a mis-route. Better a small interrupt now than a mis-filed invoice you discover next month.
- **Don't ignore the flag.** Flagged emails don't get retried automatically the next morning; they wait for you.
- **Never drop a PDF straight into a manager's folder.** Files only land in pipeline folders through the automation, never by hand — that's how the Received stamp gets applied. If a recipe seems to need a manual drop, escalate to the developer instead.

---

## Section 3: Roles at a Glance

| Role | Daily Task | When |
|------|-----------|------|
| **Front desk** | Check the shared inbox for un-tagged invoice emails AND any emails with a red Outlook flag. Tag un-matched ones via Reply-to-self; resolve flagged ones by confirming which plan(s) apply, then reply-to-self with the correct subject (or escalate the rare "partial commit" flag to the developer). Mark each flag complete when handled. | Once a morning |
| **Manager** | Review `To_Approve` folder. Move approved invoices to `Approved`. | After 6:30 AM email |
| **Manager (monthly)** | Grab the `Summary - … inv.pdf` from each plan's archive folder and attach it to the board's monthly statement. | Once a month |
| **Accountant** | For each invoice in `Approved_Invoices`: fill in Date + Check Number, Save (Ctrl+S), drag to `Paid_Invoices`. | During business day |
| **Developer (Krisztian)** | Keeps `Strataplan_List.xlsx` current. Reviews the monthly recap email. Investigates when the morning summary email or a flagged Inbox email surfaces something the front desk can't resolve. | As needed |

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

**A vendor sent an invoice as a Word doc or Excel file — what now?**
Ask them to resend as a PDF. The system only processes PDFs (and ZIPs of PDFs) — Word and Excel files are ignored on intake.

**Hit Reply but forgot to change the To: field — what now?**
The reply went to the vendor with your edited subject. They'll see something like `BCS 2707 — Re: Invoice attached` and probably ignore it. Just resend to `testinvsml@stratacomgmt.com`. No data lost.

**Can I use Forward instead of Reply?**
No. Reply links back to the original PDF in the email thread; Forward creates a new email the system may not be able to trace. Always Reply.

**An email has invoices for DIFFERENT properties — what do I do?**
The system handles this for you in most cases. If each PDF clearly identifies its own *distinct* strata plan (e.g. one for `BCS 2707`, one for `BCS 2800`), the morning run will **auto-split** them — each PDF goes to its own manager regardless of what the subject says. If the PDFs land on **similar plans that share a base** (e.g. `LMS 4193C` and `LMS 4193T`), the email gets **flagged with the red Outlook to-do flag** instead — vendors confuse those, so the system asks for a human check. Use the **Flag-Review Workflow** above to resolve it.

**An email has a red flag on it — what does that mean?**
The system tried to route the email but spotted something it didn't feel safe handling on its own. Open the email, read each attached PDF to confirm which plan(s) really apply, then either reply-to-self with the correct subject (clash / suffix-variant cases) OR — if the flag mentions "partial commit" — tell the developer. Mark the flag complete when done. See the **Flag-Review Workflow** for the full steps.

**An email is still sitting in the inbox AND has attachments — but no red flag. Is that suspicious?**
If you're not sure, tell the developer. Most likely it's an email the system simply couldn't identify (use the Recovery Workflow — reply-to-self with the plan number). On rare occasions the red flag itself failed to set — that's something the developer needs to look at, not you.

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

> Create a professional visual training document for non-technical staff (front desk, property managers, accountants) at a strata management company.
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
> **Recovery callout:** A separate sidebar or "When something goes wrong" panel — *not* a numbered step — showing the front desk's Reply workflow. Visually offset so it's clear this is a fallback, not a daily ritual.
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
