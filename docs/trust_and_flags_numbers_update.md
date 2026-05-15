# Trust & Flags Worksheet — Numbers Amendment (2026-05-14)

Paste this alongside `trust_and_flags_worksheet.md`. It corrects the volume figures
to reflect every Step 1 run on record, not just the first one.

## What changed

The worksheet originally cited a single batch — "~8 flagged out of ~224." That 224 was
just the **first live run** (a one-time inbox-backlog flush). Five more runs have
happened since, including the latest on v0.15.0.

## Every Step 1 run on record

| Run | Inbox fetched | Processed | Flagged |
|---|---:|---:|---:|
| 2026-05-12 22:17 | 226 | 224 | 17 |
| 2026-05-12 23:03 | 72 | 0 | — |
| 2026-05-13 00:03 | 72 | 0 | — |
| 2026-05-13 21:22 | 142 | 91 | 49 |
| 2026-05-13 22:24 | 69 | 0 | 56 |
| 2026-05-14 00:15 (v0.15.0) | 69 | 44 | 34 |

## Cumulative figure

**~359 invoice files handled end-to-end** = 224 + 91 + 44, across six runs.
v0.15.0 contributed the most recent 44.

## Counting caveats (important when quoting these)

- **Do not sum the "fetched" column.** Each run re-scans the entire inbox, so flagged
  emails that haven't cleared get re-counted every run. Summing fetched counts
  massively double-counts.
- **"Processed" is attachment-level, not unique emails.** The v0.15.0 run's `44` =
  30 invoices filed + 14 duplicate-skips, drawn from 69 emails in the inbox.
- The first run's `224` was logged under the pre-v0.15.0 schema (review-needed emails
  counted as `errors`), so its breakdown isn't directly comparable to later runs.

## Latest run detail (2026-05-14, v0.15.0)

- 69 emails fetched from the inbox
- 34 flagged for review
- 30 invoices filed across manager queues
- 14 duplicate PDFs skipped
- 1 error (a Graph API 404 moving one email to `processed_emails` — not a routing miss)
- 11 plain-text emails with no PDF or attachment at all (newsletters, scam warnings,
  survey spam, "overdue invoice" nags with nothing attached) — auto-skipped, no flag
- ~2 of the 34 flags were the Decision 6 "real invoice + boilerplate" shape
