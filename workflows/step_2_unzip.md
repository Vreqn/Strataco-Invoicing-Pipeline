# Step 2 — Unzip ZIP attachments in _Unmatched/Invoices

## Objective
Open every ZIP that landed in `_Unmatched/Invoices` (because Step 1 couldn't decide a manager from the subject), extract its `.pdf/.doc/.docx` contents into the same folder so Step 3 can sort them, and mark the original ZIP as processed.

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
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — atomic writes + filename sanitisation.

## Edge cases
- **Bad ZIP**: logs an error and leaves the file alone (no rename) so the human can investigate.
- **Empty ZIP / no PDF/DOC/DOCX inside**: the original ZIP is still marked Processed-... so we don't keep retrying.
- **Concurrent write from Step 1**: atomic writes in `safe_io.atomic_write_bytes` ensure Step 2 never reads a half-written ZIP. Step 1 also writes ZIPs under a `.tmp.<pid>` name first.

## When something fails
1. Check `logs/step_2_<date>.log`.
2. For a "bad zip" error, the file is left in place; open it manually and either fix or delete.
