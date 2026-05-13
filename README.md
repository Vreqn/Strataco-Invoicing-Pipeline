# Strataco Invoice Automation — Operator Manual

This is the day-to-day reference for running, monitoring, and troubleshooting the Strataco invoice pipeline. For the design rationale and the original N8n source, see `reference/`. For the per-step procedure, see `workflows/`. For change history, see `ReleaseNotes.txt`.

---

## What this does

Six daily scheduled jobs move invoices through their lifecycle every weekday, and a seventh runs once a month to roll each plan's archive into a Summary PDF:

| Time  | Step | Purpose |
|-------|------|---------|
| 06:00 | 1 — intake          | Pull invoice attachments out of the inbox; identify the Strata Plan from email subject, body, or PDF text; stamp with a red Received stamp and route to the right manager's `To_Approve` folder. Unidentified emails stay in Inbox for the operator to handle (see "Recovery workflow" below). |
| 06:10 | 2 — unzip           | Open any ZIPs in `_Unmatched/Invoices` so Step 3 can sort the contents. |
| 06:20 | 3 — pdf_sort        | Identify the Strata Plan from PDFs left in `_Unmatched/Invoices` (primarily PDFs that came out of Step 2's ZIP extraction; single-PDF emails are already handled in Step 1). |
| 06:30 | 4 — pending_email   | Email each manager a daily summary of what's waiting in their `To_Approve` folder. |
| 06:40 | 5 — to_ap           | Move manager-approved invoices into the AP's `Approved_Invoices` folder, applying a blue Paid stamp the accountant fills in. |
| 07:00 | 6 — paid_archive    | After the accountant flattens the PDF with Date + Check Number filled in, archive it under `Strata_Plans/<plan>/<check_number> - <name>.pdf`. Then scans the pipeline for stuck files, queries the Inbox for unhandled emails, and sends the daily "Invoices summary" email — the operator's single morning surface for everything that needs attention. |
| monthly | 7 — aggregate     | For each plan, merge the previous month's archived invoices into one combined `Summary - <MM> - <plan> <Month> <YYYY> inv.pdf` and move the sources into `Strata_Plans/<plan>/Processed/<YYYY-MM>/`. Operator picks the day in Task Scheduler (typically 5th–10th of the new month). |

Every step writes one row to `logs/daily_summary.csv` (columns: `date`, `step`, `processed`, `need_review`, `errors`, `duration_sec`, `status` — `need_review` covers emails Step 1 deliberately left in the Inbox for operator action, distinct from `errors` which counts genuine exceptions) and a detailed log to `logs/step_N_<date>.log`. Step 7 additionally appends one row per (plan, month) to `_state/monthly_aggregations.csv` as an audit ledger.

**The morning "Invoices summary" email.** Step 6 at 07:00 sends a single consolidated email with three sections: **Processed** (what was archived this morning), **Action Required** (every place in the pipeline where the automation should have drained the queue but didn't — plus any unhandled emails sitting in the Inbox root), and **Duplicates** (fingerprint matches caught today). The Action Required section pulls from four sources: AP `Paid_Invoices/` files Step 6 couldn't archive, manager `Approved/` files Step 5 didn't pick up, files in `_Unmatched/Invoices/` Steps 1/2/3 couldn't route, and Inbox emails that didn't auto-match a Strata Plan. One email, the operator's first piece of automation output every day.

**Strataplan snapshot.** At 06:00, Step 1 copies the master `Strataplan_List.xlsx` to `_state/strataplan_list_snapshot.xlsx` and writes a date marker. Steps 3–6 read the snapshot, not the master, so all six steps in a single morning run see the exact same routing rules and Excel having the master open on the automation machine no longer blocks the pipeline. If Step 1's snapshot refresh fails, every downstream step halts that day and the failure surfaces in `daily_summary.csv` — stale routing is unsafe (a new manager assignment must take effect the day it's saved, not the next day).

---

## Recovery workflow — unidentified invoices

Step 1 tries three matchers in order: email subject, email body, then PDF text content. If all three fail, the email **stays in the Inbox** (it is not moved to `processed_emails`). No file is written to `_Unmatched/` for these — the email itself is the recovery surface.

When the operator sees an unhandled email in the Inbox, the recovery action is:

1. Hit **Reply** on the email.
2. Change the To: field from the vendor's address to `testinvsml@stratacomgmt.com`.
3. Edit the subject to include the strata number, e.g. `BCS 2707 — Re: Invoice attached`.
4. Send. **No need to re-attach the PDF.**

Next morning's 06:00 pass will see the reply, match the subject, look up the conversation thread via Microsoft Graph, find the original message, pull the PDF, stamp it, route it, and move **both** the reply and the original to `processed_emails`. Nothing left to clean up.

**The Reply gotcha.** The To: field auto-fills with the vendor's address. If the operator forgets to change it, the reply goes to the vendor with `BCS 2707` in the subject — harmless, just embarrassing. Re-send to the right address and try again.

**Optional polish — Outlook Quick Step.** In Outlook desktop, a one-time IT setup adds a "Tag for Automation" button to the Home ribbon. Clicking it opens a Reply compose window with To: already filled in to `testinvsml@stratacomgmt.com`, so the gotcha is eliminated. The normal Reply button is unaffected. Not available in OWA; skip if the operator is browser-only.

To set this up in Outlook desktop:

1. Home ribbon → Quick Steps group → "Create New" (or right-click an existing Quick Step → Manage Quick Steps → New).
2. Name: `Tag for Automation`.
3. Choose action: `Reply`.
4. Click "Show Options" → set To: to `testinvsml@stratacomgmt.com`. Optionally pre-fill subject with a template like `[STRATA] - `.
5. Save.

The button now appears in the ribbon. Select an unmatched email, click the button, type the strata number in the subject, Send.

---

## Project layout

```
Strataco Invoicing/
├── README.md           ← this file
├── ReleaseNotes.txt    ← change history
├── VERSION             ← single-line semver (e.g. 0.1.0)
├── HANDOFF.md          ← outstanding next-actions before deployment
├── CLAUDE.md           ← agent instructions (for AI assistants only)
├── .env                ← LOCAL machine config (NOT committed)
├── .env.example        ← template
├── requirements.txt    ← Python dependencies
│
├── steps/              ← THE 7 STEP SCRIPTS (six daily + one monthly Task Scheduler job)
├── workflows/          ← markdown SOPs, one per step
├── tools/_lib/         ← shared library imported by every step
├── tests/              ← regression tests (run with `python tests/<file>.py`, no pytest needed)
├── reference/          ← read-only originals: N8n .json, docs, stamp samples
├── To-Speak-About.txt  ← deferred policy/workflow questions for client discussion
└── logs/               ← daily_summary.csv, per-step .log, lockfiles
```

---

## Prerequisites

The Python pipeline runs on **the N8N server** (the centralized Strataco deploy machine). The N8N server keeps a local mirror of Strataco's real file server; a bidirectional sync runs twice a day (~08:00 and ~20:00), so anything the automations write lands on the real file server by the next user-facing workday, and any user actions taken during the day are visible to the next morning's pipeline run.

`STRATACO_ROOT` in `.env` points at the local mirror path on the N8N server, NOT at the network share. The Python code never crosses the network for routine reads/writes.

### A. Deploy machine (the N8N server)

**Install once:**

- **Python 3.11 or newer**, from python.org. Tick "Add Python to PATH" during install. Verify with `python --version` from a fresh shell.
- **Microsoft C++ Build Tools — only if `pip install` fails.** Most fresh Windows installs work without this because every package in `requirements.txt` ships pre-built wheels for Python 3.11+. But if step 2 of "One-time setup" below errors out with `Microsoft Visual C++ 14.0 or greater is required`, install the standalone "Build Tools for Visual Studio" from <https://visualstudio.microsoft.com/visual-cpp-build-tools/> and select the **Desktop development with C++** workload. (If the deploy machine's Python came bundled with Anaconda, you already have an equivalent compiler chain via Anaconda's own packages — skip this.)

**Network access:**

- Outbound HTTPS to `graph.microsoft.com` and `login.microsoftonline.com` for Microsoft Graph (mail, attachments, send-mail, Inbox queries). Have IT confirm the corporate firewall / proxy allows these.
- Filesystem access to the local mirror path (the value of `STRATACO_ROOT`). The mirror-sync infrastructure itself is **not** part of this automation — it's separate infra Krisztian's dad maintains.

**Azure / M365 side (one-time, requires tenant admin):**

This is the source of `TENANT_ID`, `CLIENT_ID`, and `CLIENT_SECRET` in `.env`. Without this step the pipeline can't authenticate, even if Python and all packages install cleanly.

1. Register a new Azure App in the M365 tenant that owns `testinvsml@stratacomgmt.com`.
2. Under **API permissions**, add **Application** (not Delegated) permissions for Microsoft Graph:
   - `Mail.Read`
   - `Mail.ReadWrite`
   - `Mail.Send`
   - `MailboxSettings.Read`
3. Click **Grant admin consent** for the tenant.
4. Generate a client secret. Copy the **value** immediately (Azure only shows it once).
5. Drop the tenant ID, client (application) ID, and secret value into `.env`.

You'll also need one-time OWA access to the mailbox to create the two Inbox subfolders described in "One-time setup" step 4.

### B. User workstations (managers, accountants, front-desk operator)

These users **never** touch the N8N server. Each one has the real Strataco file server mapped as a network drive and does their daily work in those folders. Nothing pipeline-specific gets installed on these machines — everything they need is already standard Strataco software:

- **Adobe Acrobat** — every accountant workstation has this. Required for Step 5's Paid-stamp fill-and-flatten step (type Date + Check Number into the blue Paid stamp's fields, then **File → Export as PDF** to bake the values in as plain text). Adobe Reader alone is not sufficient — flattening is the load-bearing step that lets Step 6 read the values back.
- **Outlook** (desktop or web) — every user has this. The front-desk operator additionally needs shared-inbox access to `testinvsml@stratacomgmt.com`.
- **File Explorer** — the only "tool" managers need to drag invoices between `To_Approve` and `Approved`.

No Python, no scripts, no logs on these machines. The only humans technical enough to run code or read logs are the two system administrators (Krisztian + his dad).

### C. System administrators (Krisztian + his dad)

Maintain the deploy machine, the Strataplan list, and the Azure registration.

- **Remote access** (RDP / console) to the N8N server for installs, troubleshooting, and Task Scheduler maintenance.
- **Microsoft Excel** on at least one admin's workstation — for editing `Strataplan_List.xlsx`. The N8N server reads it via openpyxl and doesn't need Excel installed.
- **Tenant admin** for the Azure side, or a working relationship with whoever has it (for the one-time app-registration consent above).

---

## One-time setup

1. **Install Python 3.11 or newer** on the deployment machine.

2. **Install dependencies:**
   ```
   python -m pip install -r requirements.txt
   ```

3. **Create `.env`** by copying `.env.example` and filling in the real values. From the project root in PowerShell:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   The copy turns the template into a live `.env` — the tools only read `.env`, never `.env.example`. Edit the new file in Notepad and fill in the real values for the keys listed below.

   Required keys:

   | Key | What it is |
   |-----|-----------|
   | `STRATACO_ROOT` | Windows root of the Strataco folder tree (e.g. `D:\Strataco`). On the deployment machine, point this at whatever path the N8n Docker container is bind-mounted to so Python and N8n share the same files during the parallel phase. |
   | `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` | Azure app registration for Microsoft Graph (client-credentials flow). |
   | `MAILBOX_UPN` | The mailbox Step 1 polls (e.g. `testinvsml@stratacomgmt.com`). |
   | `NOTIFY_DEFAULT_EMAIL` | The real recipient for the daily Step 6 "Invoices summary" email (e.g. the AP supervisor). Required — Step 6 raises at startup if neither this nor `NOTIFY_OVERRIDE_EMAIL` is set. |
   | `NOTIFY_OVERRIDE_EMAIL` | All notification emails are rerouted here during the shadow phase. Set to empty when going live to send to real managers/APs. |

4. **Create two Inbox subfolders** in OWA for the mailbox in `MAILBOX_UPN`. Both are **required** — skip this and the pipeline runs but leaves emails sitting in the Inbox until you come back to fix it.
   - `processed_emails` — Step 1 moves handled emails here.
   - `duplicate_emails` — Step 1 moves emails whose attachments were all duplicates here. **Added in 0.9.0** — verify this folder exists when you upgrade.

   To create each: in OWA, right-click `Inbox` → `Create new subfolder` → type the exact name above (lowercase, with underscore). Verify both folders appear under Inbox before running Step 1.

5. **Verify everything boots:**
   ```
   python steps/step_2_unzip.py
   ```
   You should see "step_2 started" → "step_2 finished" in the console with a fresh row appended to `logs/daily_summary.csv`.

---

## Running a step manually

```
python steps/step_1_intake.py
python steps/step_2_unzip.py
python steps/step_3_pdf_sort.py
python steps/step_4_pending_email.py
python steps/step_5_to_ap.py
python steps/step_6_paid_archive.py               # also sends the morning "Invoices summary"
python steps/step_7_aggregate.py                  # monthly; defaults to previous calendar month
python steps/step_7_aggregate.py --month 2026-04  # explicit month
python steps/step_7_aggregate.py --dry-run        # see what would happen without writing
```

Each script is independent. A failure in one does NOT block the others. See [workflows/step_7_aggregate.md](workflows/step_7_aggregate.md) for Step 7's full flag reference.

---

## Scheduling on Windows Task Scheduler

Register the six daily tasks. For each one:

- **Trigger**: Daily, Mon–Fri, at the time in the table above.
- **Action**: Start a program
  - Program/script: `python` (or the full path to your `python.exe`)
  - Arguments: `steps\step_N_xxx.py` (e.g. `steps\step_1_intake.py`)
  - Start in: the project root (`q:\AI Automation\Strataco Invoicing`)
- **Settings**: "Stop the task if it runs longer than 30 minutes" is a sensible safety net.

Step 7 (monthly) gets its own task with a **monthly trigger** instead of daily. Pick a day between the 5th and 10th of each month (late enough that prior-month invoices have settled, early enough that the operator hasn't forgotten). The script always aggregates the previous calendar month by default.

Per-step lockfiles in `logs/.step_N.lock` prevent a step from racing itself if a job runs long.

---

## Configuration reference

### Tunable constants

| Where | What | Default | When to change |
|-------|------|---------|----------------|
| `tools/_lib/stamp.py` | `WHITE_PCT_MIN` | 0.985 | Increase (e.g. 0.995) if stamps land on top of sparse text instead of true whitespace. |
| `tools/_lib/stamp.py` | `RED`, `BLUE` | tuned | Change if the stamp colours don't match the existing sample. |
| `tools/_lib/stamp.py` | `STAMP_WIDTH_PT`, `STAMP_HEIGHT_*` | 210×180 / 210×90 | Resize the stamps. |
| `.env` | `RETRY_MAX_ATTEMPTS` | 3 | Increase if Graph rate-limits you. |
| `.env` | `RETRY_BASE_DELAY_SECONDS` | 2 | Bigger backoff on flaky networks. |
| `.env` | `ZIP_MAX_ENTRIES` | 200 | Max files inside a single ZIP before Step 2 rejects it as unsafe. |
| `.env` | `ZIP_MAX_UNCOMPRESSED_BYTES` | 104857600 (100 MB) | Max uncompressed size of any single ZIP entry. |
| `.env` | `ZIP_MAX_TOTAL_BYTES` | 524288000 (500 MB) | Max total uncompressed size across all entries in one ZIP. |
| `.env` | `ZIP_MAX_RATIO` | 100 | Max compression ratio (uncompressed ÷ compressed) per entry — rejects zip-bombs. |

### Where the files live (under `STRATACO_ROOT`)

| Path | Contents |
|------|----------|
| `Strataplan_List.xlsx` | Master list — Strata Plan ↔ Manager ↔ AP. |
| `Users/<Manager>/Invoices/To_Approve/` | Stamped invoices waiting for manager review. |
| `Users/<Manager>/Invoices/Approved/` | Manager has approved; Step 5 will pick these up. |
| `Users/<AP>/Approved_Invoices/` | AP queue; the accountant fills in the Paid stamp. |
| `Users/<AP>/Paid_Invoices/` | Accountant has saved the flattened PDF here; Step 6 will pick these up. |
| `Strata_Plans/<plan>/` | Final archive, one folder per Strata Plan. Step 7's `Summary - ...` PDFs land here; the per-month source archives live under `Strata_Plans/<plan>/Processed/<YYYY-MM>/`. |
| `_Unmatched/Invoices/` | Staging area for: (1) ZIP attachments Step 1 saved here for Step 2 to unpack; (2) PDFs Step 2 extracted from those ZIPs, which Step 3 then sorts. As of 0.11.2, non-PDF/non-ZIP attachments (signature images, `.docx`, `.xlsx`) are **discarded at intake** with an INFO log line — they no longer accumulate here. Unidentified emails (any combination of unmatched PDFs or ZIPs without a subject-body plan match) are NOT written here — they stay in the Inbox for the operator's reply-to-self recovery. The "all-or-nothing" rule keeps `_Unmatched/` lean: it's an automation staging area, not a human inbox. |
| `_state/toapprove_history/` | Per-manager XLS of yesterday's queue, for the daily diff. |
| `_state/ap_approved_history/` | Per-AP rolling baseline XLS for the daily diff. |
| `_state/monthly_aggregations.csv` | Step 7 audit ledger — one row per (plan, target-month) per run. See [workflows/step_7_aggregate.md](workflows/step_7_aggregate.md). |
| `_state/invoice_fingerprints.csv` | Duplicate-detection ledger — one row per unique invoice fingerprint, forever. Upsert-in-place. See [workflows/duplicate_detection.md](workflows/duplicate_detection.md). |

Everything except `Strataplan_List.xlsx` is created automatically when needed.

---

## Daily monitoring

The first place to look every morning:

```
logs/daily_summary.csv
```

One row per (date, step). Columns: `date`, `step`, `processed`, `need_review`, `errors`, `duration_sec`, `status`. A clean morning shows six rows with `status=ok` and the expected counts. `need_review` is the count of emails Step 1 deliberately left in the Inbox for human action (strict-first flags, all-or-nothing holds, subject-matched-but-no-prior-conversation); `errors` only counts genuine exceptions the operator should be paged on. Pre-0.11.2 environments had a 6-column schema (no `need_review`) — the first 0.11.2+ step run migrates the file in place, padding historical rows with `0`. The CSV is read by `tools/collect_diagnostics.py` and tolerates either width.

The single morning email arrives shortly after Step 6 finishes at 07:00. If you stop receiving it entirely on weekday mornings, that's a real "Step 6 didn't fire" signal — check the Task Scheduler entry and `logs/step_6_<date>.log`.

When something looks off:

```
logs/step_N_<date>.log
```

Has the full traceback and per-file decisions for that day's run.

### Duplicate detections

Step 6's morning "Invoices summary" email always includes a `== Duplicates ==` section. On days with zero duplicates it renders `None today.`; on days with one or more it lists the plan, the original archive path (or current pipeline location), the extracted invoice number and amount, and how many times the fingerprint has been seen.

If you need to look up a specific duplicate, the ledger lives at `_state/invoice_fingerprints.csv` (one row per unique invoice; safe to open in Excel). Step logs contain lines like `duplicate skipped: <name> (sha=<short>..., matches <short>..., original at <path>, dup_count=N)` that give the per-attachment story.

To let a flagged duplicate through (vendor legitimately re-billed):

```
python tools/dup_override.py <sha256_prefix> --reason "vendor re-billed for credit applied"
```

See [workflows/duplicate_detection.md](workflows/duplicate_detection.md) for the full SOP.

---

## Troubleshooting

**"Missing required environment variable: STRATACO_ROOT"**
→ `.env` doesn't exist or doesn't have that key. See "One-time setup".

**"Inbox subfolder 'processed_emails' not found"**
→ Create it in OWA under Inbox. Step 1 still saves attachments; emails just won't be moved until the folder exists.

**"Inbox subfolder 'duplicate_emails' not found"**
→ Create it in OWA under Inbox. Until then, duplicates are still detected (ledger updates and dup_count increments), but the email itself stays in the Inbox.

**A duplicate was flagged but it's actually a new invoice**
→ Vendor re-issued the invoice (credit and rebill, corrected version). Find the sha256 prefix in the duplicate-summary email or in the step log (`duplicate skipped: <name> (sha=<short>...)`) and run:
```
python tools/dup_override.py <sha256_prefix> --reason "what changed"
```
Then the next time that PDF arrives through Step 1/3/5, it routes normally. The override is one-shot — to override again, re-run.

**A duplicate that should have been caught wasn't**
→ Two cases: either the Layer A hash differs (vendor regenerated the PDF — extraction of Layer B `(invoice_number, amount)` should have caught it if those labels were present) and Layer B extraction failed, OR the prior invoice never made it into the ledger (orphan). Run `python tools/dup_reconcile.py` to surface orphans.

**Step says `dup ledger corrupted — halting day`**
→ Open `_state/invoice_fingerprints.csv` in Excel. The error message names the malformed row. Either fix the bad column or delete the row entirely. Save. Re-run the step. The ledger is intentionally loud here — silent-treat-as-empty would let every already-archived invoice re-enter the pipeline.

**An invoice email keeps sitting in the Inbox (not in `processed_emails`)**
→ The automation couldn't identify a Strata Plan from the subject, body, or PDF text. Use the recovery workflow above: hit Reply, change To: to `testinvsml@stratacomgmt.com`, edit the subject to include the strata number, Send. Next 06:00 pass routes the original PDF via conversation-link. If the plan isn't recognized at all, check that it's in `Strataplan_List.xlsx`.

**A PDF keeps ending up in `_Unmatched/Invoices`**
→ A PDF (or a ZIP that Step 2 extracted into a PDF) made it to the sorting yard but Step 3 couldn't identify a Strata Plan from the filename or PDF text. Either the plan isn't parseable from those sources, or the plan isn't in `Strataplan_List.xlsx`. Rename the file with the right plan prefix and move it to the manager's `To_Approve` folder, or add the row to the XLS.

(As of 0.11.2, non-PDF/non-ZIP attachments — signature images, `.docx`, `.xlsx` — are **discarded at intake** in Step 1 with an INFO log line and never reach `_Unmatched/`. If a real invoice arrives as a Word doc, the vendor needs to resend as PDF.)

(Unmatched single-PDF or multi-PDF emails without identifying info DO NOT land here — they stay in the Inbox for the operator's reply-to-self recovery.)

**Stamp covers important text on the invoice**
→ See "Tunable constants" — increase `WHITE_PCT_MIN`. The algorithm picks the largest qualifying whitespace area; a higher threshold rejects sparsely-populated regions.

**Step 6 says "Could not read Check Number"**
→ Open the PDF in Acrobat. The Date and Check Number fields must be **flattened** (not editable form fields) — re-save with "Print → Save as PDF" so the values become regular text.

**A step shows `status=skipped` in `daily_summary.csv`**
→ The previous run of the same step was still active when the next cron fired. Check `logs/step_N_<date>.log` for what's making it slow. Stale lockfiles can be removed manually: `del logs\.step_N.lock`.

**A step's log says `previous run still active — skipping` but no other Python process is running**
→ A stale lockfile from a crash. Delete `logs/.step_N.lock` and re-run.

**Step 7 records `error` with `ledger-filesystem disagreement`**
→ The ledger says a (plan, month) was aggregated, but the Summary file or `Processed/{YYYY-MM}/` folder is missing on disk. Open the plan folder and inspect. Most common cause: someone manually deleted the Summary. Recovery: confirm the Processed/ folder has the right archives, then re-run with `--force` to regenerate from those.

**Step 7 records `error` with `move failed on ... rolled back successfully`**
→ A source PDF couldn't be moved into `Processed/` (typically Acrobat or another viewer has it open with an exclusive lock). The run cleanly rolled back so the plan is in its pre-run state. Close any viewers holding the file open and re-run Step 7.

**Step 7 produces a `Summary - ... inv (1).pdf`**
→ A late-arriving invoice was archived after the original Step 7 run completed for that month. The `(1)` Summary contains only the late check; the original Summary is unchanged. Decide whether to keep both, delete the original, or rename. See [workflows/step_7_aggregate.md](workflows/step_7_aggregate.md) "Late checks".

---

## Generating a diagnostic bundle

When something looks off and you want to hand the pipeline's full state to a remote AI assistant (a developer on a different machine), run:

```
python tools/collect_diagnostics.py
```

This produces a single self-contained zip at `logs/diagnostics_<host>_<YYYYMMDD-HHMMSS>.zip` covering all six steps in one snapshot:

- `SUMMARY.md` — human-readable overview with queue counts, lockfile status, today's `daily_summary.csv` rows, and the last 30 lines of each step's log.
- `logs/` — the last 7 days of step logs + the full `daily_summary.csv`.
- `queues/` — TSV listings (filename, size, mtime) for `_Unmatched/Invoices/`, every manager's `To_Approve` and `Approved` folders, every AP's `Approved_Invoices` and `Paid_Invoices` folders, and the last 30 days of `Strata_Plans/`.
- `state/` — listings of `_state/toapprove_history/` and `_state/ap_approved_history/`.
- `system.txt`, `pip_freeze.txt`, `env_check.txt` — host metadata + which env vars are SET/MISSING (values are never recorded).
- `Strataplan_List.xlsx` — a copy of the master plan list at the moment the bundle was taken.

PDF contents are NOT included by design — the listings tell a remote assistant what's stuck where, and you can send specific PDFs on request. The bundler also produces useful output when `STRATACO_ROOT` is unset (env-check still reports what's missing), so it's safe to run even when something is wrong with the basic configuration.

Flags:
- `--days N` — log retention window (default 7).
- `--out PATH` — override the output zip path.
- `--no-strataplan` — skip including the master XLS.

The script is on-demand only; it does NOT add a row to `daily_summary.csv` and does NOT acquire a step lockfile.

---

## Promoting from shadow to live

When you're ready to send notifications to real managers/APs:

1. Edit `.env`, set `NOTIFY_OVERRIDE_EMAIL=` (empty value).
2. Bump `VERSION` (1.0.0 is the conventional first production release).
3. Add a `ReleaseNotes.txt` entry describing the cutover.
4. Restart Task Scheduler jobs (or wait for tomorrow's cron).
