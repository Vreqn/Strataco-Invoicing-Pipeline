# Step 3 — Sort PDFs left in `_Unmatched/Invoices` (post-ZIP extraction)

## Objective
Identify the Strata Plan for every PDF still in `_Unmatched/Invoices` and route it. Match by filename first; if that fails, extract the PDF text and use the safe scoring algorithm. Apply the Received stamp and route to the manager.

**Context — what's actually in `_Unmatched/Invoices/` after this change?** Step 1 now does subject + body + PDF text matching inline (and pulls PDFs from prior messages via conversation-link when the operator replies-to-self without re-attaching). As a result, single-PDF invoice emails are almost always routed by Step 1 directly — they no longer land here. The main feeder for this step is now Step 2: ZIPs that get unzipped at 06:10. Non-PDF attachments (Word, images) also pass through if Step 1 saved them here for manual sort, but Step 3 doesn't try to match those — only `*.pdf` files.

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
- **Scanned PDFs (no extractable text)**: `extract_full_text` returns "", `match_from_pdf_text` reports "No text extracted", file is left in place. For single-PDF emails this case is already handled in Step 1 by leaving the email in Inbox; here it applies to PDFs that came out of ZIPs and so don't have a parent email to fall back on — operator renames the file manually or asks the vendor.
- **Ambiguous match**: when the top score does not beat the runner-up by ≥ 3, the file is left in place (logged: "Ambiguous").
- **"C/O" trap**: explicitly handled by the regex's `(?!\s*\/\s*[A-Z])` lookahead.
- **Suffixed plans**: `LMS4193C` in filename matches `LMS4193C` in XLS first; if filename has the base `LMS4193`, it falls back to base only when all suffix variants point to the same manager.

## When something fails
- A noisy PDF that always ends ambiguous: rename it manually with the right Strata Plan prefix and Step 5 (or you can re-run Step 3 after the rename) will pick it up.
- A real plan missing from `Strataplan_List.xlsx`: add the row, re-run.
