# Step 6 — Archive paid invoices into Strata Plan folders + send morning "Invoices summary"

## Objective
For each AP user, scan their `Paid_Invoices` folder. The accountant has filled in the Paid stamp (Date + Check Number) in Acrobat and saved the PDF in place (Ctrl+S) — Step 6 reads the values straight from the AcroForm fields, flattens the PDF as part of the archive write, and copies it into the matching `Strata_Plans/<plan>/` archive folder under the convention `{check} - {MM} - {PLAN} {MonthName} {YYYY} inv.pdf`. The archived copy is fully flat: no editable fields anywhere.

After the archive loop, Step 6 also produces the operator's single morning report: it scans every pipeline folder where the automation should have drained the queue, queries the Inbox for unhandled emails, and bundles everything into one `Invoices summary` email — see "Outputs" below.

## Behavioural changes vs. the old N8n flow
1. **No `Paid -` filename filter** — every PDF in `Paid_Invoices` is processed. `Processed -` prefixed files (legacy markers written by pre-0.17.0 runs) are skipped.
2. **Destination filename follows the client's archive convention** — `{check} - {MM} - {PLAN} {MonthName} {YYYY} inv.pdf`, with check number AND date both read from the flattened Paid stamp. The original vendor filename is dropped from the archive name.
3. **True move (0.17.0+)** — after a successful archive write the source PDF is deleted from `Paid_Invoices/`. No `Processed - <name>.pdf` marker is written back. Deduplication lives in the dup ledger (SHA-256); a second run on a leftover source is a safe no-op.

## Schedule
07:00 Mon–Fri.

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx`.
- Each AP's `<STRATACO_ROOT>/Users/<AP>/Paid_Invoices/*.pdf`.
- Each Strata Plan archive folder at `<STRATACO_ROOT>/Strata_Plans/<plan_raw>/` (auto-created if missing).

## Outputs
- For each archived invoice:
  - `<STRATACO_ROOT>/Strata_Plans/<plan_raw>/<check_number> - <MM> - <PLAN> <MonthName> <YYYY> inv.pdf`.
    Example: `12345 - 03 - BCS1234 March 2026 inv.pdf`. `<PLAN>` is the
    normalised, no-space form of the plan (`BCS1234`, not `BCS 1234`).
  - The source PDF in `Paid_Invoices/` is deleted after the archive write succeeds.
- One consolidated `Invoices summary` email to `config.notify_email()` (`NOTIFY_OVERRIDE_EMAIL` during shadow, `NOTIFY_DEFAULT_EMAIL` post-cutover). Always sent — a silent inbox is a real "Step 6 didn't fire" signal. Subject: `Invoices summary — X processed, Y action required, Z duplicate — YYYY-MM-DD`. Three sections:
  - `== Processed ==` — files archived this morning, grouped by AP.
  - `== Action Required ==` — four sub-sections, each omitted when empty:
    - **Paid invoices stuck (Step 6 couldn't archive)** — AP `Paid_Invoices/` files where the check number / date couldn't be read, the plan wasn't in the XLS, etc.
    - **Manager approvals stuck (Step 5 didn't pick up)** — files sitting in any manager's `Approved/` folder at 07:00.
    - **Unmatched intake files (Steps 1/2/3 couldn't route)** — non-`Processed-` files in `_Unmatched/Invoices/`. OS-generated metadata files (`.DS_Store`, AppleDouble `._*` sidecars, `Thumbs.db`, `desktop.ini`) are filtered out — they reach shared folders via file-server browsing and are never pipeline content.
    - **Inbox emails (unhandled)** — live Graph query of the Inbox root; or a degraded "query failed" notice when Graph is unreachable. The error case counts as +1 in the subject's `action required` total so the subject always reflects what the body says.
    - **Pipeline scan errors** — appears only when one or both filesystem scans hit a per-folder error (e.g. permission denied on a manager's `Approved/`). Each error string lists which folder failed; the rest of the scan still ran, so this sub-section coexists with the other Action Required rows when applicable.
  - `== Duplicates ==` — fingerprint matches caught today (filtered to rows whose `last_dup_date` equals today's date).
- Row in `logs/daily_summary.csv`.

## Run
```
python steps/step_6_paid_archive.py
```

## Tools used
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `plan_to_ap`, `unique_aps`, `unique_managers` (for the manager-`Approved/` scan).
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — `plan_from_filename`.
- [tools/_lib/stamp_read.py](../tools/_lib/stamp_read.py) — `extract_paid_stamp_values` reads `Check Number:` and `Date:` from the Paid stamp. Tiered: AcroForm `/V` (the normal Step 5/6 happy path) → positioned pdfplumber narrowed to the PAID region → regex fallback. `parse_paid_date` turns the raw date string into `(month, year)` for the archive filename. When the PDF is image-only (no AcroForm + no extractable text), the result carries an `image_only` flag and the morning email reports it explicitly.
- [tools/_lib/stamp.py](../tools/_lib/stamp.py) — `flatten_acroform` (via pikepdf) bakes the AcroForm field values as static text in the page content stream and strips the widget annotations. Used before each archive write so the Strata_Plans copy is fully flat.
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — atomic writes.
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `list_inbox_messages` (for the Inbox-stuck scan), `send_mail`.
- [tools/_lib/inbox_report.py](../tools/_lib/inbox_report.py) — `render_messages` formats Graph message dicts into the Action Required sub-section rows.
- [tools/_lib/paths.py](../tools/_lib/paths.py) — `unmatched_invoices`, `manager_approved`, `ap_paid_invoices`.

## Reasons a file ends up in the unmatched email
1. No Strata Plan parsable from the filename.
2. Plan in filename is not in `Strataplan_List.xlsx`.
3. Could not read `Check Number:` from the Paid stamp (accountant forgot to fill it in).
4. Could not read `Date:` from the Paid stamp (same root cause as #3).
5. PDF is image-only — typically because the operator ran Microsoft "Print to PDF" by hand. The morning email surfaces this explicitly and points the operator at the Ctrl+S workflow.
6. The `Strata_Plans/<plan>/` folder is missing AND can't be created (permissions issue).

## When something fails
- For "Could not read Check Number" / "Could not read Date": open the PDF in Acrobat, confirm both Paid-stamp fields actually have a value typed in, then save normally (Ctrl+S). Step 6 reads the AcroForm directly and flattens automatically on archive — no manual flatten step required. For the Date field, the format the stamp emits by default (`MAY 08 2026`) is the safest; long-form (`May 8, 2026`) and ISO (`2026-05-08`) also parse cleanly. As of 0.16.0 the Paid stamp prints a small grey `format:  MMM DD YYYY` caption in the PAID title band so whoever fills it sees the expected format — the caption sits off every field's read baseline and carries no digits, so it can't be mistaken for a value.
- For "PDF appears image-only (likely Microsoft 'Print to PDF')": the AP flattened the PDF by printing it. Since 0.17.0, there is no `Processed -` backup in the manager's `Approved/` folder (true-move). Contact the developer, who can recover the form-bearing copy from the dup ledger's `archive_path` and re-stamp it via Step 5. Once a fresh form-bearing copy is in `Paid_Invoices/`, have the AP fill the stamp fields, save with Ctrl+S, and re-run Step 6.
- For "Plan not in XLS": add the row, the next run picks it up.
