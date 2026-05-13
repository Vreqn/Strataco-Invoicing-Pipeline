# Step 5 — Transfer manager-approved invoices to AP, apply Paid stamp

## Objective
For every invoice a manager has placed in their `Approved` folder, look up the AP from the Strata Plan, apply the **Paid stamp** (blue, two editable fields: Date and Check Number) so the AP can fill it in, and copy the stamped PDF to the AP's `Approved_Invoices` folder. Mark the manager's copy as `Processed - <name>`. Then send each AP a notification email comparing today vs. the rolling baseline.

## Behavioural changes vs. the old N8n flow
1. **No `Approved - ` prefix** on the destination filename — keep the original filename as-is.
2. **Paid stamp applied** at this hand-off (was not in the old flow).

## Schedule
06:40 Mon–Fri.

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx`.
- Each manager's `<STRATACO_ROOT>/Users/<Manager>/Invoices/Approved/*.pdf` (skipping files starting with `Processed -`).
- Each AP's rolling baseline XLS at `<STRATACO_ROOT>/_state/ap_approved_history/_latest__<APKEY>.xls` (optional).

## Outputs
- For each transferred invoice:
  - `<STRATACO_ROOT>/Users/<AP>/Approved_Invoices/<original_name>.pdf` (with Paid stamp).
  - `<STRATACO_ROOT>/Users/<Manager>/Invoices/Approved/Processed - <original_name>.pdf` (so it isn't re-transferred).
- One email per unique AP (override to `NOTIFY_OVERRIDE_EMAIL`).
- Refreshed baseline XLS for each AP.
- Row in `logs/daily_summary.csv`.

## Run
```
python steps/step_5_to_ap.py
```

## Tools used
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `plan_to_ap`, `unique_aps`, `unique_managers`, `base_plan_index`.
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — `plan_from_filename`.
- [tools/_lib/stamp.py](../tools/_lib/stamp.py) — `render_paid_stamp` (blue, 3 rows: Paid header + Date: + Check Number:).
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — atomic writes.
- [tools/_lib/history.py](../tools/_lib/history.py) — baseline read/write + old/new diff.
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `send_mail`.

## Edge cases
- **Suffixed plan in filename vs XLS**: `LMS4193` in filename when XLS has only `LMS4193C/O/P` — `_resolve_ap` only auto-routes if all variants share the same AP (mirrors the N8n flow's "base-plan-suffix-fallback").
- **Plan in filename not in XLS**: file is left untouched; logged.
- **Paid stamp render fails**: unstamped PDF is still transferred (with an error log). The accountant sees the PDF without a stamp and can stamp it manually.
- **Notification email fails**: logged; baseline is still written so subsequent diffs are correct.

## When something fails
- The most common issue is a manager's filename that lost its plan prefix (e.g. they renamed the file). Either rename it back manually before the next run, or add a special case to the filename regex in `plan_match.plan_from_filename`.
