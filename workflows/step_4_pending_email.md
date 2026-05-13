# Step 4 — Daily "pending approval" email per manager

## Objective
For each Strata Manager listed in `Strataplan_List.xlsx`, count the PDFs currently in their `To_Approve` folder, compare to yesterday, and email a summary listing what's old vs. new since yesterday's run.

## Schedule
06:30 Mon–Fri.

## Inputs
- `<STRATACO_ROOT>/Strataplan_List.xlsx`.
- Each manager's `<STRATACO_ROOT>/Users/<Manager>/Invoices/To_Approve/*.pdf`.
- Yesterday's per-manager history XLS at `<STRATACO_ROOT>/_state/toapprove_history/<YYYY-MM-DD>__<MANAGER_KEY>.xls` (optional; missing = treat all as new).

## Outputs
- One email per unique manager (recipient is overridden to `NOTIFY_OVERRIDE_EMAIL` during the shadow phase).
- Today's per-manager history XLS at `<STRATACO_ROOT>/_state/toapprove_history/<TODAY>__<MANAGER_KEY>.xls`.
- Row in `logs/daily_summary.csv`, detail in `logs/step_4_<date>.log`.

## Run
```
python steps/step_4_pending_email.py
```

## Tools used
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `load_plans`, `unique_managers`.
- [tools/_lib/history.py](../tools/_lib/history.py) — `read_yesterday_for_manager`, `compute_old_new`, `write_today_for_manager`.
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `send_mail`, `resolve_recipient`.

## Edge cases
- **Manager has zero invoices**: the email still goes out with `Total: 0`. (The manager email is a daily reminder rhythm, not just "you have work".)
- **Yesterday's history file missing (first run / weekend gap)**: every file is treated as "new". The current day's XLS is still written, so the next run is correct.
- **One manager, multiple emails (`,`-separated)**: the XLS preserves the raw cell; we pass it to `send_mail` where commas become `;` separators.

## When something fails
- A failed `send_mail` is logged but does NOT block the history XLS from being written — the next day's diff stays correct.
- If too many emails fail, check the Azure app reg has `Mail.Send` granted for the configured mailbox.
