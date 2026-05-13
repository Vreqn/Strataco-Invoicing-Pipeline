# Duplicate Detection — Workflow / SOP

This is the operator-and-developer reference for the cross-step duplicate-detection layer that landed in 0.9.0. The pipeline keeps a persistent invoice ledger so a vendor who re-sends the same invoice — same day or six months later — gets caught before they waste manager or accountant attention.

## What gets checked

Every PDF that enters the pipeline is fingerprinted on **two layers**:

- **Layer A — SHA-256 of the PDF bytes.** Catches verbatim resends (vendor pressed Send twice; accounting system auto-resent). Deterministic, never false-positive.
- **Layer B — `(plan_norm, invoice_number, amount_cents, sender_domain)`.** Catches "vendor regenerated the same invoice with different bytes" — different PDF metadata, same logical invoice. Invoice number is extracted from labels like `Invoice #`, `Invoice No.`, `INV:`. Amount is extracted from `Total Due`, `Amount Due`, `Balance Due`, or `Grand Total`. `sender_domain` is the lowercased domain of the email's `From:` address, captured at intake — it's the deterministic 4th factor that prevents two different vendors who happen to use the same invoice number for the same property from being falsely merged. No LLM, no maintained vendor list.

A duplicate is declared when **EITHER** layer matches an existing ledger row whose `current_stage` is not `overridden`. Layer B fires only when ALL four fields are non-blank — rows inserted by non-email entry points (Steps 3/5/6: manual drops, retries) have a blank `sender_domain` and are deliberately excluded from Layer B. Those rows still get Layer A coverage.

## Where the check fires

| Step | Hook point | What happens on a hit |
|------|-----------|----------------------|
| 1 — intake | inside `_route_pdf`, before stamping | PDF not written to manager folder; email moves to `Inbox/duplicate_emails`; `dup_count` increments |
| 3 — pdf_sort | after `pdf_path.read_bytes()`, before stamping | PDF renamed `Processed-<ts>-DUPLICATE-<name>.pdf` in `_Unmatched/` so the next-run glob skips it; `dup_count` increments |
| 5 — to_ap | after `pdf_path.read_bytes()` in `_transfer_phase` | PDF renamed `Processed - DUPLICATE - <name>.pdf` in manager Approved/; AP transfer + Paid stamp skipped; `dup_count` increments |
| 6 — paid_archive | after archive write succeeds in `_archive_one` | Row's `current_stage` updated to `archived`; `archive_path` populated for the daily summary email |

Step 1 is the primary catch (~95% of duplicates land here). Steps 3 and 5 are safety nets for orphan PDFs the operator manually drops into `_Unmatched/` or a manager's `Approved/` without going through email.

## What the operator sees

### `Inbox/duplicate_emails`

A new Outlook subfolder, mirroring the existing `Inbox/processed_emails` pattern. Create it once in OWA before the first run:

> In OWA → Right-click `Inbox` → Create new subfolder → name it `duplicate_emails`.

Routing rules per email:

- **Every attachment was a duplicate** → email lands in `Inbox/duplicate_emails`
- **Mix of fresh routes + duplicates** → email lands in `Inbox/processed_emails` (the email had real work in it; the duplicate is just a side effect)
- **All fresh routes** → email lands in `Inbox/processed_emails` (unchanged)
- **Genuine failure (download error, write error, unmatched in pdf_text_fallback)** → email stays in Inbox for the operator's reply-to-self recovery (unchanged)

### Daily duplicate section (inside the combined "Invoices summary" email)

Step 6 at 07:00 sends one daily email with subject `Invoices summary — N processed, M unmatched, K duplicate — YYYY-MM-DD`. It is **always sent** — even on zero-everything days — so a silent inbox is a real signal that the cron didn't fire.

The body has three sections: `Processed`, `Unmatched`, and `Duplicates`. Empty sections render as `None today.`

Each entry in the `Duplicates` section lists: plan, the original archive path (or current pipeline location if not archived yet), the extracted invoice number and amount, when the fingerprint was first seen, and the total times it has been seen.

Recipient is `notify_email()` (resolves to `NOTIFY_OVERRIDE_EMAIL` during the shadow phase, `NOTIFY_DEFAULT_EMAIL` post-cutover).

## The ledger file

`_state/invoice_fingerprints.csv` — one row per unique fingerprint, forever. Schema:

```
first_seen_date, sha256, plan_norm, invoice_number, amount_cents,
sender_domain, archive_path, current_stage, last_seen_date, dup_count,
last_dup_date
```

`current_stage` ∈ `{intake, manager_queue, ap_queue, archived, quarantined, overridden}`. The file is upsert-in-place (not append-only), so the same `sha256` will only ever appear once. Atomic full-file rewrites under `portalocker.Lock` make a crash mid-write safe — the previous good copy stays in place.

CSVs written before `sender_domain` existed remain readable: the parser falls back to an empty `sender_domain` for missing columns. Legacy rows therefore keep Layer A (sha256) coverage but no longer contribute to Layer B matching — which is the right behaviour because we don't know which vendor they came from.

Open the file in Excel any time. The pipeline tolerates this exactly the same way `_state/monthly_aggregations.csv` does.

**Size budget (at 45–60 invoices/day):** ~2 MB/year, ~22 MB at year 10. Excel-openable indefinitely. No archival rotation needed.

## Override workflow

When a vendor legitimately re-bills (corrected invoice, credit-and-rebill), you'll see the new invoice flagged as a duplicate. To let it through:

```
python tools/dup_override.py <sha256_prefix> --reason "vendor re-billed for credit applied"
```

- The sha256 can be a prefix (12 chars is usually enough). If multiple rows match the prefix, the script lists them and exits non-zero so you can disambiguate.
- The reason is **required**. It's logged to `logs/dup_override_<date>.log` for audit.
- The override is **one-shot**: the next time that fingerprint flows through Step 1/3/5, the override is consumed and the row goes back to a normal lifecycle stage. To override again, re-run.

You can find the sha256 prefix in the `Duplicates` section of the daily Invoices summary email, or in the step log line `duplicate skipped: <name> (sha=<short>...)`.

## Rescue / diagnostic — `tools/dup_reconcile.py`

```
python tools/dup_reconcile.py             # report to stdout
python tools/dup_reconcile.py --tsv out.tsv  # also write detailed TSV
```

Walks every pipeline folder (manager, AP, archives, `_Unmatched/`), hashes every PDF, and reports:

- **Orphans**: PDFs on disk with no ledger row. Usually means a Step N ledger-write failure after the file write succeeded.
- **Stale archived rows**: ledger says archived at path X but the file is missing.
- **Multi-arrival fingerprints**: `dup_count > 0` informational summary.

Read-only — does NOT mutate the ledger or move files.

## Known limitation: cross-process check-then-write TOCTOU

Steps 1, 3, 5, 6 each load the ledger once at startup and consult it via in-memory lookups before writing a PDF to disk. The mutation (`upsert` etc.) is transactional under the lock, but the **check that precedes the mutation** is not. In the narrow window between "Process A read the ledger and concluded the fingerprint was new" and "Process A's upsert acquired the lock", Process B can insert a row for the same fingerprint.

Result: both A and B write their PDF to disk (duplicate in the manager folder), then their upserts serialize and one ends up overwriting the other's row. Operator notices two copies in the manager folder and handles manually.

Realistic exposure: zero under normal Task Scheduler timing (Steps 1/3/5/6 are well-separated at 06:00, 06:20, 06:40, 07:00). The race requires a hand-run overlapping the cron AND both runs picking up the same fingerprint (would require, e.g., the same PDF appearing in both an email attachment AND `_Unmatched/` simultaneously). Documenting as a known limitation rather than fixing because a complete fix would require restructuring the PDF-write path to reserve the ledger entry before writing.

## Edge cases

### Same-day double arrival

Both arrivals are processed in the same Step 1 run. The first hits an empty ledger and gets routed normally; its row is added in-memory + on-disk. The second arrival hits the freshly-added row and gets caught as a duplicate. The `consumed_prior_ids` machinery is orthogonal — it prevents conversation-link from re-routing the same prior twice, not regular duplicate detection.

### Override consume — same-bytes re-arrival vs different-bytes regen

An override row (created via `dup_override.py`) is one-shot. Two distinct re-arrival cases:

- **Layer A (same bytes re-arrive):** `_check_dup_status` sees `current_stage=overridden`, treats as "route normally". The subsequent `upsert` overwrites the override row in place with `current_stage=manager_queue` — override consumed.

- **Layer B (regenerated bytes, same semantic key):** `_check_dup_status` surfaces the override row separately. After a successful route, the caller invokes `consume_override_and_insert(old_sha, new_row)` which atomically marks the old row `superseded` and inserts the new row. The old sha is retained as audit history; a future re-arrival of the OLD bytes hits a `superseded` row and IS blocked as a duplicate (those bytes were already processed before the override fired).

Double-consume protection: `consume_override_and_insert` checks the old row's stage inside the locked transaction and raises `ValueError` if it's no longer `overridden`. The callers catch this and fall back to a plain `upsert`, so a race between two regen-arrivals doesn't blow up.

### Duplicate arrives weeks after archival

Ledger lookup returns the `archived` row. The duplicate-summary email references the `archive_path` so the operator sees exactly where the original lives.

### Conversation-link "you already sent this"

If the operator replies-to-self for a PDF that was already archived weeks ago, Step 1 fetches the prior, the duplicate check fires inside `_route_pdf`, nothing re-routes, `dup_count` increments, and both the reply and the prior move to `duplicate_emails`. The system has told the operator: "your reply was unnecessary; the original already went through."

### Step 1 all-or-nothing rule

`_process_pdf_text_fallback` (the no-subject-no-body-match path) has a Pass 1 / Pass 2 architecture. Pass 1 classifies every attachment without mutating the ledger. Pass 2 commits only if every PDF is auto-handleable — either matched-and-new (will route) or matched-and-duplicate (will skip). A "matched and duplicate" PDF counts as auto-handleable.

If ANY attachment is unmatched / non-PDF / download-failed, Pass 2 short-circuits: nothing routes, nothing saves, **`dup_count` does not increment** for any attachment. We haven't committed to processing the email yet, so we don't dirty the ledger.

### Ledger missing (first-run)

`dup_ledger.load()` returns an empty ledger; the pipeline proceeds. Subsequent upserts auto-create the file with the correct header.

### Ledger corrupted

`dup_ledger.load()` raises `ValueError` on the first malformed row. Every step that consults the ledger halts the day with a clear error in `daily_summary.csv`. The corrupted file must be fixed before the next run — silent-treat-as-empty would let every already-archived invoice re-enter the pipeline.

Repair: open `_state/invoice_fingerprints.csv` in Excel, find the bad row (matches the error message), delete it or fix the malformed column, save. Re-run the step.

## Implementation references (for developers)

- `tools/_lib/dup_fingerprint.py` — hash + Layer B extractors. Best-effort; returns `""` / `None` on garbage input rather than raising.
- `tools/_lib/dup_ledger.py` — `Ledger.upsert()`, `increment_dup_count()`, `update_stage()`, `find_by_hash()`, `find_by_semantic_key()`. Mirrors `aggregation_ledger.py`'s locking pattern.
- `tools/_lib/paths.py:invoice_fingerprints_csv()` — file location.
- `steps/step_1_intake.py::_route_pdf` and `_check_dup_status` — primary catch.
- `steps/step_1_intake.py::_email_destination` — `processed_emails` vs `duplicate_emails` decision.
- `steps/step_6_paid_archive.py::_build_combined_summary_email` — daily summary formatter (processed + unmatched + duplicates in one email).
- `tests/test_dup_fingerprint.py`, `tests/test_dup_ledger.py`, `tests/test_step6_summary_email.py` — unit tests.
