# Step 7 — Monthly Invoice Aggregator

## Objective
For each active strata plan, scan `Strata_Plans/<plan_raw>/` for every Step-6-archived invoice whose filename targets a chosen month, merge those PDFs in check-number order into a single combined PDF, move the source PDFs into `Strata_Plans/<plan_raw>/Processed/{YYYY}/{MM} - {MonthName}/`, and append one row per (plan, month) to `_state/monthly_aggregations.csv`. The output is one combined PDF per plan per month that a manager can attach to a strata statement.

## Schedule
Monthly, on the **1st of each month** via Task Scheduler. The script defaults to aggregating the **previous calendar month** (America/Vancouver) regardless of the day it fires on.

## Inputs
- `<STRATACO_ROOT>/_state/strataplan_list_snapshot.xlsx` — today's snapshot of the master plan list. Step 7 calls `refresh_snapshot()` itself at startup before requiring it, so the monthly trigger works on days Step 1 didn't run (weekends, holidays). If the master XLS is unreadable AND today's marker is missing, Step 7 refuses to run.
- For each active plan: `<STRATACO_ROOT>/Strata_Plans/<plan_raw>/*.pdf` matching Step 6's archive convention `{check} - {MM} - {plan_norm} {MonthName} {YYYY} inv.pdf` (and the `... inv (1).pdf` collision-renamed variant). Under `--force` Step 7 will also re-scan `<STRATACO_ROOT>/Strata_Plans/<plan_raw>/Processed/{YYYY}/{MM} - {MonthName}/` if the root has no candidates, so a deleted-summary regenerate flow works.
- `<STRATACO_ROOT>/_state/monthly_aggregations.csv` — the audit ledger. Auto-created on the first run; the header is also written if an existing file is zero-bytes (an Excel-crashed-mid-save edge case).

## Outputs
- For each plan that had at least one invoice in the target month:
  - `<STRATACO_ROOT>/Strata_Plans/<plan_raw>/Processed/{YYYY}/{MM} - {MonthName}/{MM} - {plan_norm} {MonthName} {YYYY} inv.pdf` — the combined PDF, written with `safe_write_unique` so a re-run never overwrites an existing summary.
  - Every source PDF moved into the same `Processed/{YYYY}/{MM} - {MonthName}/` folder alongside the summary.
- One row per (plan, month) appended to `<STRATACO_ROOT>/_state/monthly_aggregations.csv`. See *Ledger* below.
- A "Monthly aggregation: N plans" summary email (sent even when N=0 so the operator sees the cron fired) and, if anything went unmatched, an "Monthly aggregation unmatched" email. Both routed to `NOTIFY_OVERRIDE_EMAIL` during the shadow phase.
- One row in `logs/daily_summary.csv` for `step_7`, plus a detailed `logs/step_7_<date>.log`.

## Run

```
python steps/step_7_aggregate.py                    # default: previous calendar month, all plans
python steps/step_7_aggregate.py --month 2026-04    # explicit target month
python steps/step_7_aggregate.py --plan BCS1234     # one plan (case-insensitive vs plan_norm)
python steps/step_7_aggregate.py --dry-run          # log what would happen; write nothing
python steps/step_7_aggregate.py --force            # bypass the ledger short-circuit; re-evaluate every plan
```

Flags compose. `--force` does NOT delete prior summaries — it only bypasses the ledger's "already done" check; existing summary files are still preserved via `safe_write_unique`'s `(1)`/`(2)` renaming. To truly redo a month from scratch the operator deletes the old summary file by hand first.

Bad `--month` values (malformed, current month, future month, more than 24 months old) are rejected before the lock is acquired.

## Tools used
- [tools/_lib/strataplan_snapshot.py](../tools/_lib/strataplan_snapshot.py) — `require_fresh_snapshot()` (same gate every other step uses).
- [tools/_lib/xls.py](../tools/_lib/xls.py) — `load_plans` to enumerate active plans.
- [tools/_lib/plan_match.py](../tools/_lib/plan_match.py) — new `parse_archive_filename` to invert Step 6's archive filename into `(check, month, year, plan_norm)`.
- [tools/_lib/paths.py](../tools/_lib/paths.py) — new `strata_plan_processed_month(plan_raw, year, month)` and `monthly_aggregations_csv()`.
- [tools/_lib/pdf_merge.py](../tools/_lib/pdf_merge.py) — new `merge_pdfs_from_bytes(list[bytes]) -> bytes` (thin pypdf wrapper).
- [tools/_lib/aggregation_ledger.py](../tools/_lib/aggregation_ledger.py) — new CSV-backed audit ledger.
- [tools/_lib/safe_io.py](../tools/_lib/safe_io.py) — `safe_write_unique` and `sanitize_filename`.
- [tools/_lib/graph.py](../tools/_lib/graph.py) — `send_mail`.

## Ledger — `_state/monthly_aggregations.csv`

Every (plan, target-month) attempt appends one row. The ledger is the answer to "did April actually finish for every plan?" and the script's own idempotency check consults it before doing work.

**Schema** (one header row, one append per (plan, month) per run):

```
run_date,run_timestamp,plan_norm,target_year,target_month,status,summary_filename,sources_merged,notes
```

**Status values**:

| status | meaning |
|---|---|
| `aggregated` | New summary written this run. |
| `aggregated_late` | A `(1)` summary written because a prior `aggregated` row already exists for this (plan, month) — the late-check case. |
| `dry_run` | Compute-only run via `--dry-run`. Informational; does NOT count as "done." |
| `skipped_already_done` | Idempotency guard fired — ledger AND filesystem both agree the month is done. |
| `skipped_no_files` | Plan folder exists but had zero matching invoices for this month. |
| `skipped_no_folder` | Active plan but `Strata_Plans/<plan>/` doesn't exist yet. |
| `error` | Read/merge/write failed, OR ledger says done but filesystem disagrees. The (plan, month) is unchanged. |

**Pre-flight summary** — at startup the script logs ONE line summarising the ledger's view of the target month, so the operator can confirm what the script believes it's about to do:

```
[step_7] Target month: April 2026 (run on 2026-06-07 14:32 America/Vancouver)
[step_7] Ledger: 0 of 47 active plans aggregated for April 2026 — will process all
        (or)  Ledger: 12 of 47 active plans aggregated for April 2026 (latest 2026-06-07T14:32:01) — rerun will idempotently skip them
        (or)  Ledger: 47 of 47 active plans aggregated for April 2026 (latest 2026-06-07T14:32:01) — nothing to do (pass --force to redo)
```

This is the answer to "is the script confused about which month it's processing?" The target is printed front-and-centre before any work begins.

## Idempotency rules

Consulted in order, per plan, after the per-plan candidate scan:

1. **Ledger says done AND no new candidates AND summary file present AND `Processed/{YYYY}/{MM} - {MonthName}/` has files** → append `skipped_already_done`. Move on.
2. **Ledger says done AND no new candidates BUT summary or Processed/ is missing** → append `error` with `notes=ledger-filesystem disagreement` (the notes spell out which side is missing). Don't process. Operator triages.
3. **Ledger has no `aggregated` row AND candidates exist** → first-time aggregation; on success append `aggregated`.
4. **Ledger says done AND new candidates exist in root** → late-check case (see *Late checks* below); write the additive `(1)` summary; append `aggregated_late`. Holds even under `--force` (the audit trail stays unambiguous).
5. **`--force`** bypasses rules 1 and 2; when the plan-folder root has no candidates, `--force` falls back to scanning `Processed/{YYYY}/{MM} - {MonthName}/` so the operator can delete a summary and regenerate it from already-archived sources. Files in `Processed/` are NOT moved a second time.

## Move-failure rollback

If a source PDF can't be moved into `Processed/{YYYY}/{MM} - {MonthName}/` mid-batch (e.g. Acrobat has the file locked, a permissions error, the disk filled), Step 7:

1. Logs the failure with the specific source and the OS error.
2. Rolls back every successful move from this run (`os.replace` back into the plan folder root).
3. Deletes the just-written summary so it doesn't represent a partial state.
4. Appends an `error` ledger row whose `notes` field documents the failed file and any rollback issues.

The plan is left in its pre-run state. The operator inspects the failed file (close it in Acrobat, fix permissions, etc.) and reruns. Without this rollback, a stuck source would be silently merged into the original summary AND re-merged into a `... (1).pdf` on the next run — duplicate pages across the two files.

## Late checks

Operator runs Step 7 on the 1st. Step 6 archives a late check on day 9 (an April 30 invoice flattened on May 8). Operator re-runs Step 7 for April.

**What happens**:
- The original `04 - BCS1234 April 2026 inv.pdf` is left untouched.
- A second summary `04 - BCS1234 April 2026 inv (1).pdf` is written, containing only the late check.
- The late PDF is moved to `Processed/2026/04 - April/`.
- The ledger gets an `aggregated_late` row.

This is the policy ship for now. The decision is open in [To-Speak-About.txt](../To-Speak-About.txt) — the alternative behaviours are (a) destructive re-merge into a single summary and (b) hard refusal until `--force`.

## Reasons a file ends up in the unmatched email

1. Filename in `Strata_Plans/<plan>/` doesn't match Step 6's archive convention — i.e. it parses as neither `{check} - {MM} - {plan} {MonthName} {YYYY} inv.pdf` nor the `... inv (1).pdf` collision-renamed variant. Examples: an operator manually saved a vendor PDF here, or a stray copy from another folder.
2. Filename's plan disagrees with the folder's plan — e.g. `BCS9999 ...` sitting inside `Strata_Plans/BCS 1234/`.

In both cases Step 7 leaves the file in place; the operator's job is to move/rename/delete it before the next run.

## When something fails

- **"snapshot is not today's"**: Step 1 hasn't refreshed the snapshot today. Run Step 1 first.
- **"ledger says done but Processed/{YYYY}/{MM} - {MonthName}/ is empty"**: the ledger and filesystem disagree. Either (a) someone manually deleted the Processed folder, or (b) the previous run wrote the ledger row but failed to move the sources. Inspect `_state/monthly_aggregations.csv` and the plan folder; run with `--force` once the situation is understood.
- **"ledger file is corrupted"**: the CSV has a malformed row (non-numeric year/month/sources_merged). Open it in Excel, find the bad row, fix it, save as CSV-UTF-8, retry.
- **`... (1).pdf` appearing unexpectedly**: a late-check ran. Check the ledger for the `aggregated_late` row to see when and what.
- **PDF merge raised**: the source PDF may be corrupt or password-protected. Open it in Acrobat to confirm. If it's broken, replace it from the AP's `Paid_Invoices/` if available; otherwise skip the plan that month manually (move the bad file out, run, move it back for review).
- **Move-to-Processed failed for some files**: the summary is already durably written, but some sources are still in the plan folder. They'll be re-merged on the next run and produce a `... (1).pdf` — acceptable. Investigate the file-system error (permissions, in-use lock from Acrobat).

## Verification quick reference

Operator-side manual checks after a real run:
1. `Strata_Plans/<plan>/Processed/<YYYY>/<MM> - <MonthName>/<MM> - <plan> <MonthName> <YYYY> inv.pdf` exists.
2. `Strata_Plans/<plan>/Processed/<YYYY>/<MM> - <MonthName>/` contains the source PDFs alongside the summary.
3. `_state/monthly_aggregations.csv` shows one new row per plan with `status=aggregated`.
4. The summary email body lists each plan and its merged-invoice count.
