# Step 3 — Safety-net PDF sort for `_Unmatched/Invoices`

## Role (post-2026-05-13)

This step is a **safety net only**, just like Step 2. The email intake path (Step 1) handles subject + body + PDF text + filename matching inline AND inspects ZIPs in memory at intake (see `workflows/step_1_intake.md`). Email-originated PDFs and ZIP-contained PDFs are routed directly by Step 1 or held in the Inbox with the Outlook red flag — they no longer flow through `_Unmatched/Invoices/`.

Step 3 continues to run on its existing 06:20 cron so that:

1. If an operator drags a manually-fixed PDF into `_Unmatched/Invoices/`, the automation picks it up the next morning without anyone needing to remember to run the job by hand.
2. If a future Step 1 change ever leaves a PDF in `_Unmatched/`, the safety net drains it.

On a normal day this job logs "found 0 PDF(s) to sort" and exits in milliseconds. The follow-up decision to decommission Steps 2 and 3 once the logs prove they're idle is tracked as a `To-Speak-About.txt` entry.

## Objective
Identify the Strata Plan for every PDF still in `_Unmatched/Invoices` and route it. Match by filename first; if that fails, extract the PDF text and use the safe scoring algorithm. Apply the Received stamp and route to the manager.

## Schedule
06:20 Mon–Fri.

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx` (via today's snapshot).
- Every `*.pdf` in `<STRATACO_ROOT>/_Unmatched/Invoices/` not starting with `Processed-`. After this change, that means primarily PDFs extracted from ZIPs by Step 2.

## Outputs
- Matched PDFs at `<STRATACO_ROOT>/Users/<Manager>/Invoices/To_Approve/<plan> - <name>.pdf` with Received stamp applied.
- Original copy renamed to `Processed-YYYYMMDD-HHMMSS-<name>.pdf` in `_Unmatched/Invoices`.
- Files with no safe match are left in place untouched.

## Run
```
python steps/step_3_pdf_sort.py
```

## Tools used
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `load_plans`, `plan_to_manager`.
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — `match_from_filename`, `match_from_pdf_text` (safe scoring + C/O guard + name fallback, ported verbatim from N8n node 11).
- [tools/_lib/pdf_text.py](../tools/_lib/pdf_text.py) — `extract_full_text` via pdfplumber.
- [tools/_lib/stamp.py](../tools/_lib/stamp.py) — `render_received_stamp`.

## Edge cases
- **Scanned PDFs (no extractable text)**: `extract_full_text` returns "", `match_from_pdf_text` reports "No text extracted", file is left in place. In the email pipeline this case is already handled in Step 1 — the parent email stays in the Inbox flagged for the operator. In the safety-net context (manual drop), the operator renames the file with the right plan prefix and the next run picks it up via the filename-first path.
- **Ambiguous match**: when the top score does not beat the runner-up by ≥ 3, the file is left in place (logged: "Ambiguous").
- **"C/O" trap**: explicitly handled by the regex's `(?!\s*\/\s*[A-Z])` lookahead.
- **Suffixed plans**: `LMS4193C` in filename matches `LMS4193C` in XLS first; if filename has the base `LMS4193`, it falls back to base only when all suffix variants point to the same manager.

## When something fails
- A noisy PDF that always ends ambiguous: rename it manually with the right Strata Plan prefix and Step 5 (or you can re-run Step 3 after the rename) will pick it up.
- A real plan missing from `Strataplan_List.xlsx`: add the row, re-run.
