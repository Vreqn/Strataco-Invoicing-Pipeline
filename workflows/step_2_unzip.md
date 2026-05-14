# Step 2 — Safety-net unzip for `_Unmatched/Invoices`

## Role (post-2026-05-13)

This step is a **safety net only**. The email intake path (Step 1) inspects ZIPs in memory at intake and routes contained PDFs directly to manager folders, or leaves the parent email in the Inbox with the Outlook red flag — see `workflows/step_1_intake.md`. Email-originated ZIPs no longer reach this step, and ZIPs are never written to `_Unmatched/Invoices/` by Step 1.

The step continues to run on its existing 06:10 cron for three reasons:

1. **Operator manual drops** — if a stuck file is dragged into `_Unmatched/Invoices/` for the automation to finish, the next morning's 06:10 sweep handles it without anyone needing to remember to kick it off by hand.
2. **Defense in depth** — if a future Step 1 change ever leaves a file in `_Unmatched/` (a partial-commit edge case, a new code path, etc.), the safety net catches it the next morning rather than letting it accumulate.
3. **Pairs with Step 6's `_Unmatched/` scan** — Step 6 already lists orphans in the daily summary email; Step 2/3 actually drain them once the operator has fixed the underlying file.

On a normal day this job logs "found 0 zip(s)" and exits in milliseconds.

The follow-up question of whether to decommission Steps 2 and 3 entirely once the logs prove they're idle is tracked as a `To-Speak-About.txt` entry.

## Objective
For every ZIP that landed in `_Unmatched/Invoices` (operator manual drop, or any other source), extract its `.pdf/.doc/.docx` contents into the same folder so Step 3 can sort them, and mark the original ZIP as processed.

## Schedule
06:10 Mon–Fri.

## Inputs
- Every `*.zip` in `<STRATACO_ROOT>/_Unmatched/Invoices/` whose name does NOT start with `Processed-`.

## Outputs
- For each kept entry, a file at `<STRATACO_ROOT>/_Unmatched/Invoices/<zipbase>__<inner>.<ext>`.
- Original ZIP renamed to `Processed-YYYYMMDD-HHMMSS-<original>.zip` (so it isn't re-processed).
- Row in `logs/daily_summary.csv`, detail in `logs/step_2_<date>.log`.

## Run
```
python steps/step_2_unzip.py
```

## Tools used
- Standard library `zipfile`.
- [tools/_lib/zip_safe.py](../tools/_lib/zip_safe.py) — `audit_zipfile` (lenient safety pre-flight + extension filtering); shared with the strict in-memory caller used by Step 1.
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — atomic writes + filename sanitisation.

## Edge cases
- **Bad ZIP**: logs an error and leaves the file alone (no rename) so the human can investigate.
- **Empty ZIP / no PDF/DOC/DOCX inside**: the original ZIP is still marked Processed-... so we don't keep retrying.
- **Concurrent write**: atomic writes in `safe_io.atomic_write_bytes` ensure Step 2 never reads a half-written ZIP. (Step 1 no longer writes ZIPs to this folder, but the same atomicity protects manual operator drops mid-copy.)

## When something fails
1. Check `logs/step_2_<date>.log`.
2. For a "bad zip" error, the file is left in place; open it manually and either fix or delete.
