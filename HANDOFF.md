# Handoff — Next Steps

Living document. Whoever picks this up next (a future Claude session, Krisztian, or Attila) starts here. Update this file whenever a step is completed or new blockers appear, then keep it short.

**Current version:** see `VERSION` (0.11.3 — Step 1 intake: TELUS ZIP fix, signature-image discard, PDF-text-first/filename-fallback matching, magic-byte PDF validation, `need_review` counter, CSV migration safety, partial-commit flag guard in PDF-text fallback path; pre-deployment, shadow phase).

---

## To start a fresh session

The full design is captured here (in priority order):
1. `README.md` — operator manual; how the system runs day-to-day.
2. `workflows/step_*.md` — per-step procedure and tools used.
3. `tools/_lib/*.py` — the actual implementation.
4. `reference/` — original N8n exports and stamp samples for reference only.
5. `To-Speak-About.txt` — deferred policy/workflow questions awaiting client discussion (do not implement these unilaterally; see CLAUDE.md for the convention).
6. `tests/` — regression test suite; run with `pytest tests/` (CLAUDE.md is the source of truth on test conventions).

Skip `ReleaseNotes.txt` unless you're investigating regressions; it's history, not state.

---

## Open work — in order

### 1. Fill in `.env` with real credentials
Status: **blocked on Azure access**

- Copy `.env.example` → `.env` on the dev machine.
- Get an Azure app registration in the M365 tenant that owns `testinvsml@stratacomgmt.com`.
- Grant Application permissions: `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `MailboxSettings.Read` (admin consent required).
- Drop `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` into `.env`.
- Verify with: `python -c "from tools._lib.graph import get_access_token; print(get_access_token()[:20])"` — should print a token prefix, not raise.

### 2. Set `STRATACO_ROOT` to the correct Windows path
Status: **blocked on access to the deployment machine**

- On the dev machine: any folder works (e.g. `D:\Strataco-dev`). Hand-create `Strataplan_List.xlsx` there with one or two test rows.
- On the deployment machine: point at whatever Windows path the existing N8n Docker container is bind-mounted to, so the Python and N8n flows read/write the same files during the parallel/shadow phase.

### 3. Test the stamp visual placement on real invoices
Status: ready as soon as #1 + #2 are done

- Pick 5–10 representative invoices (different layouts, different amounts of whitespace).
- Run: `python -m tools._lib.stamp <invoice.pdf> --mode received --date "MAY 08 2026" --plan "BCS 2707"`
- Open the output. Confirm: stamp lands on actual whitespace, AcroForm fields are clickable in Acrobat, colours match `reference/stamp_samples/Recieved Stamp Ex #1.pdf`.
- If the stamp lands on top of text on a sparse-but-not-empty area, raise `WHITE_PCT_MIN` in `tools/_lib/stamp.py` (e.g. 0.985 → 0.995). One-line change; bump PATCH version.
- Repeat for `--mode paid`.

### 4. Smoke-test each step end-to-end
Status: ready after #3

- Drop a sample email with a PDF attachment in the test mailbox; run Step 1; verify the file shows up under the right manager's `To_Approve` with a Received stamp on page 1 and the email moved to `processed_emails`.
- Drop a ZIP in `_Unmatched/Invoices`; run Step 2; verify the contents extracted, ZIP renamed `Processed-...zip`.
- Drop unmatched PDFs (some with plan in filename, some only in PDF body text); run Step 3; verify routing.
- Run Step 4 twice (today and "tomorrow" — bump the system clock or rename the history XLS); verify the old/new diff in the email body.
- Place a stamped invoice in a manager's `Approved/` with the Received fields filled in; run Step 5; verify it lands in the AP's `Approved_Invoices` **without** the `Approved -` prefix, that the Received stamp values are visible but **no longer editable** (Step 5 flattens them), and that the Paid stamp's Date + Check Number fields ARE editable in Acrobat.
- Open one Step 5 output, fill in Date + Check Number, save with Ctrl+S (no flatten — Step 6 flattens automatically on archive); place in `Paid_Invoices`; run Step 6; verify it lands at `Strata_Plans/<plan>/<check_number> - <name>.pdf` with no editable form fields anywhere.
- After Steps 1–6 have produced a real month's worth of `Strata_Plans/<plan>/...` archives, run `python steps/step_7_aggregate.py --month <YYYY-MM> --dry-run` to preview, then drop `--dry-run` for the real run. Verify: one `Summary - {MM} - {plan} {Month} {YYYY} inv.pdf` per active plan in the plan folder, every source moved into `Strata_Plans/<plan>/Processed/{YYYY-MM}/`, and one `aggregated` row per plan in `_state/monthly_aggregations.csv`.

### 5. Register the daily Task Scheduler jobs (Steps 1–6)
Status: ready after #4

Use the times in `README.md` ("Scheduling on Windows Task Scheduler"). Once the daily summary CSV shows six clean rows for a day or two, the daily migration is functionally complete.

**Heads-up:** if you're carrying a 0.9.3-era Task Scheduler config across, the old `step_8_inbox_sweep.py` entry at 18:00 was removed in 0.11.0. Disable / delete it — Step 6 at 07:00 now sends the consolidated morning report covering both pipeline-stuck files AND unhandled Inbox emails.

### 6. Register the monthly Task Scheduler job (Step 7)
Status: ready after #5

Pick a recurring day between the 5th and 10th of the month (operator's call — late enough that prior-month invoices have settled, early enough that no one's forgotten). Trigger: Monthly. Action: `python steps/step_7_aggregate.py` (no flags — it defaults to the previous calendar month). After the first month-end run, inspect `_state/monthly_aggregations.csv` to confirm every active plan has an `aggregated` row.

### 7. Cutover from shadow to live
Status: ready after #6 + a successful shadow run

- Set `NOTIFY_OVERRIDE_EMAIL=` (empty) in `.env`.
- Bump `VERSION` to 1.0.0.
- Add a `ReleaseNotes.txt` entry.
- Disable the corresponding N8n flows.

---

## Known issues / caveats

- **Stamp placement on dense invoices**: the integral-image search may pick a "mostly white" area that still contains scattered text (e.g. Subtotal/Total numbers). Tune `WHITE_PCT_MIN` per #3 above.
- **PDF text extraction on scanned invoices**: `pdfplumber` returns no text from image-only PDFs. For single-PDF emails the matcher chain in Step 1 (subject → body → PDF text) ends without a hit, the email is left in the Inbox, and the operator handles it via the reply-to-self recovery workflow documented in README ("Recovery workflow — unidentified invoices"). For PDFs that came out of a ZIP (extracted by Step 2 into `_Unmatched/Invoices/`), Step 3 leaves them in place (logged as "No text extracted"); manual rename is the workaround.
- **Two Python interpreters on the dev machine**: `pip` and `python` may point at different installs. Always use `python -m pip install ...` to keep them in sync.

---

## Future optimization (not now)

**Possible consolidation of Steps 2 + 3 into Step 1.** Step 1 now does subject → body → PDF text matching inline against the in-memory PDF blob (this change rolled out alongside the "unidentified emails stay in Inbox" behavior). As a result, Step 3's only remaining real-world input is PDFs that Step 2 extracted out of ZIPs — single-PDF emails are fully handled in Step 1. A future iteration *could* unzip ZIPs in-memory inside Step 1 and eliminate Steps 2 and 3 entirely as separate scheduled jobs.

**Not recommended until the system has been running stably in production for a meaningful period.** The current 6-step structure works, ships on time, and is straightforward to debug step-by-step (each step is a self-contained Python script with its own log file). Consolidation is purely a tidiness win — it doesn't unlock any new capability.

If a future maintainer does take this on, be aware:
- A monthly Step 7 (`steps/step_7_aggregate.py`) already exists. Don't blindly renumber Steps 4/5/6 down — Step 7 is in Task Scheduler with its own monthly trigger.
- Renumbering is one option. Others: leave a numbering gap (1, 4, 5, 6, 7), or keep Step 3 as a thin file that's a no-op most days. Either preserves Step 7's identity.
- Task Scheduler entries on the deployment machine reference the step file paths by name; whichever option is chosen, those Task Scheduler triggers need to be updated to match.

---

## Implemented: Step 7 — Monthly Invoice Aggregator

`steps/step_7_aggregate.py` is the seventh and final step in the pipeline. Runs once a month on an operator-chosen day (typically the 5th to the 10th of the new month) and rolls each strata plan's prior-month archived invoices into one combined Summary PDF, moving the sources into `Strata_Plans/<plan>/Processed/{YYYY-MM}/`. Every (plan, month) attempt is logged to `_state/monthly_aggregations.csv` as a permanent audit trail.

**Schedule.** Operator picks the day in Task Scheduler; the script always defaults to the PREVIOUS calendar month (America/Vancouver). `--month YYYY-MM` overrides for reruns. The monthly trigger may fire on days Step 1's Mon–Fri schedule didn't run, so Step 7 calls `refresh_snapshot()` itself at startup — works on weekends and holidays as long as the master XLS is readable.

**Idempotency.** Five rules per plan; the load-bearing ones are documented in `workflows/step_7_aggregate.md`. The ledger is the source of truth for "has this (plan, month) been aggregated yet?" and the script consults it before doing work. Re-runs idempotently skip already-done plans; the pre-flight log line shows the operator the ledger's view of the target month before any work begins ("Ledger: 12 of 47 active plans aggregated for April 2026").

**Late-check policy.** If Step 7 has already aggregated a month and a NEW invoice for that month appears later (Step 6 archived it after Step 7 ran), the next Step 7 run writes an additive `Summary - ... inv (1).pdf` containing only the late check, leaving the original Summary untouched. Ledger row: `aggregated_late`. Whether the destructive re-merge would be preferable is logged in `To-Speak-About.txt` for client discussion.

**Move-failure rollback.** If any source PDF fails to move into `Processed/` (e.g. Acrobat has it locked), Step 7 rolls back the successful moves AND deletes the just-written Summary. The plan returns to its pre-run state, ledger row is `error` with the failure details, and the operator triages without risking duplicate pages across `Summary` and `Summary (1)`.

**`--force` semantics.** Bypasses the idempotency short-circuit. When the plan-folder root is empty AND `--force` is set, Step 7 falls back to regenerating the Summary from `Processed/{YYYY-MM}/` contents (no second move, since the files are already where they belong). Existing Summary files are NEVER overwritten — `safe_write_unique` produces a `(1)`/`(2)` variant.

Files:
- New: `steps/step_7_aggregate.py`, `workflows/step_7_aggregate.md`
- New libraries: `tools/_lib/pdf_merge.py`, `tools/_lib/aggregation_ledger.py`
- Extensions: `tools/_lib/plan_match.py` (`parse_archive_filename`), `tools/_lib/paths.py` (`strata_plan_processed_month`, `monthly_aggregations_csv`)
- New tests: `tests/test_aggregate_filename.py` (8), `tests/test_pdf_merge.py` (4), `tests/test_aggregation_ledger.py` (8), `tests/test_check_sort_key.py` (6)
- Smoke (in `.tmp/smoke_step7.py`): 9 end-to-end cases against a scratch `STRATACO_ROOT`. Disposable per CLAUDE.md.
- Codex adversarial review at `codex-router-out/2026-05-11-step7-review/` produced 10 findings; 9 were fixed before shipping. See `ReleaseNotes.txt` 0.7.0 entry for the full list.

No new `.env` keys.

---

## Implemented: Strataplan_List.xlsx working-copy snapshot

`tools/_lib/strataplan_snapshot.py` owns the snapshot lifecycle. Step 1 calls `refresh_snapshot()` at the top of its run; Steps 3, 4, 5, 6 call `require_fresh_snapshot()` and halt the day if today's marker is missing. The bundler (`tools/collect_diagnostics.py`) reads the snapshot too, so the diagnostic view matches what the steps actually saw.

**Reading past Excel's lock.** The realistic baseline at 06:00 is that the operator left the master open in Excel from the previous day. The snapshot module reads the master with `CreateFileW` and `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` (via `ctypes`, no new dependency). Excel opens xlsx files with those same share flags, so Windows lets a matching-share read through even while Excel holds the file. The bytes we get are whatever Excel last saved to disk; unsaved in-memory edits are invisible, which matches the user's "edit, save, close" protocol. `shutil.copy2` is not used because it opens the source with no share flags and would fail on a normal morning.

**Halt-on-failure semantics.** If today's refresh fails for any reason (master missing, read error, bytes don't validate as XLSX), Step 1 returns 1 and the marker stays at yesterday (or never written). Steps 3–6 see the stale marker and refuse to run, surfacing `snapshot is not today's — refusing to run` in the daily summary. Stale routing is worse than a delayed run: re-routings (manager on vacation, new AP) must take effect the day they're saved on the server. Step 2 (unzip) is independent of the snapshot and still runs.

**Verify-before-publish atomicity.** `refresh_snapshot()` stages the new bytes to `<snapshot>.tmp.<pid>.xlsx`, validates the staged file with openpyxl, then `os.replace()`s into the published path, and rewrites the marker last. If validation fails, the previously-good snapshot remains intact — so a same-day re-run (testing today, or a future intraday client refresh) where the master had become corrupt can never replace good bytes under a still-today marker.

**Sync context.** The local `Strataplan_List.xlsx` is mirrored from the server by a nightly sync at 20:00. By the time Step 1 fires at 06:00, the local copy is ~10 hours fresh and nobody is actively editing it on the automation machine.

Files:
- New: `tools/_lib/strataplan_snapshot.py`, `tests/test_strataplan_snapshot.py`
- Paths added: `paths.strataplan_snapshot_xlsx()` and `paths.strataplan_snapshot_marker()` (at `_state/strataplan_list_snapshot.xlsx` and `_state/strataplan_list_snapshot.ok`)
- Steps 1/3/4/5/6 and `collect_diagnostics.py` updated to consume the snapshot instead of `Strataplan_List.xlsx` directly. Only `refresh_snapshot()` reads the master.

---

## Tests to run when real data arrives

These are the validations we deliberately deferred until production-shaped invoices and access exist. Synthetic versions of these would lock in the wrong assumptions.

1. **Plan-match accuracy on real invoices** — once 5–10 real invoices land, run `steps/step_3_pdf_sort.py` against them in a sandbox `STRATACO_ROOT` and compare manager assignments against the N8n flow's output for the same invoices. This is the test that actually validates the `tools/_lib/plan_match.py` port.
2. **Stamp positioning across diverse layouts** — same set of real invoices, run the chain (Received → Paid) and visually verify the hard-rule fields (subtotal / VAT / total / line items / invoice number) stay clear.
3. **Real flattened Paid stamp end-to-end** — get one accountant to fill in + flatten a Paid stamp for real; run Step 6 against it; verify the Check Number is read correctly and the file lands at `Strata_Plans/<plan>/<check_number> - <name>.pdf`. (Synthetic version of this lives in `tests/test_stamp_read.py` and covers the extractor logic; what's left is the real Print-to-PDF round-trip.)
4. **Live environment cutover** — once `STRATACO_ROOT` points at the real folder tree on the deployment machine and `.env` has Azure creds, run each step manually in sequence (Step 2 → 1 → 3 → 4 → 5 → 6) and inspect output before scheduling.
5. **Step 7 against a real month of archives** — after Steps 1–6 have run in shadow for at least one complete month, run `python steps/step_7_aggregate.py --dry-run --month <YYYY-MM>` against the real `Strata_Plans/` tree and inspect the proposed merges. Spot-check the check-number sort order, confirm no plans are missing from the dry-run summary, then drop `--dry-run` for the real run. Verify the resulting Summary PDFs by opening a couple in Acrobat: page count = sum of source page counts, page order = check-number ascending.

---

## Reference artifacts

- `reference/stamp_samples/Recieved Stamp Ex #1.pdf` — example stamp produced by the original N8n / 192.168 service; the visual target for our Python port.
- `reference/stamp_samples/python_renders/example_received_stamp.pdf` and `.../example_paid_stamp.pdf` — what our `tools/_lib/stamp.py` produces today. Open both alongside the original to spot drift after any stamp-module change. Regenerate with the two `python -m tools._lib.stamp ...` commands in `README.md`.

---

## Things NOT to touch without discussion

- The PDF text matching algorithm in `tools/_lib/plan_match.py` (the `match_from_pdf_text` function) is a near-verbatim port of the safe scoring logic from N8n's Step 3 node 11. The C/O guard, suffix fallback, and name fallback rules were debugged in production over months. Changes here can silently misroute invoices.
- The `Processed -` and `Processed-YYYYMMDD-HHMMSS-` filename conventions. The N8n flow uses these to know what's already been handled; the Python scripts rely on the same conventions for the duration of the shadow phase. Don't rename or remove the prefix logic without coordinating with the N8n side.
- The Step 6 archive filename format `{check} - {MM} - {plan_norm} {MonthName} {YYYY} inv.pdf`. Step 7's `parse_archive_filename` is the inverse of `step_6_paid_archive._build_archive_name`; the two MUST stay in lockstep. Renaming either side without updating both breaks Step 7's candidate scan silently (everything looks "unmatched"), and a wrong `MonthName` would corrupt the Summary filename.
- Step 7's move-then-rollback ordering. Summary writes BEFORE moves, but if any move fails the Summary is deleted AND successful moves are rolled back. Reverting to "log and continue" would re-introduce the duplicate-page Summary-(1) bug that the Codex review caught.
- The Step 7 audit ledger schema. `_state/monthly_aggregations.csv` is append-only and the column order is part of the contract (operator may open it in Excel, scripts may grep it). Adding columns at the end is fine; reordering or renaming existing ones isn't.
