# Strataco Invoice Automation — Trust, Flags & Safety Nets
## A decision worksheet for the owners

> **Audience note:** `docs/` otherwise holds office-staff training material
> (`client_training_brief.md`). This file is a different audience again: a one-time
> **owner decision worksheet**, not staff training. It is developer-adjacent in that
> every decision here maps to an open entry in `To-Speak-About.txt`.

> **Status (2026-05-14): meeting complete.** All decisions have been recorded below
> and transferred to `To-Speak-About.txt`. Code changes implementing Decisions 1, 4,
> and 5 are live in v0.16.0 (all 269 tests passing).

---

## Context — why this document exists

The invoice automation makes a judgement call on every email that arrives: *can I
figure out which strata plan this invoice belongs to, confidently enough to file it
without a human looking?* When the answer is yes, the invoice flows through untouched.
When the answer is "not sure," the system **flags** the email and waits for a person.

Right now those judgement calls are tuned **strict** — when in doubt, flag. That was
the safe choice for launch. But "strict" has a cost: every flag is work for the front
desk, and some flags can't actually be cleared by the normal recovery step, which
creates loops. The owners need to decide, scenario by scenario, **how strict each
policy should be** — where to trust the system more, and where to keep the safety net
tight.

**Scale so far:** this isn't theoretical. Across its first six live runs
(2026-05-12 through 2026-05-14, the latest on v0.15.0), the system has handled roughly
**359 invoice files** end-to-end — the very first run alone cleared a 224-item inbox
backlog, and the runs since have added the rest. Every scenario below is drawn from
that real traffic, not invented.

This worksheet lays out every place the system trusts, every place it flags, and every
place a too-strict rule can trap an email in a loop. Each item ends with a blank
**Decision** line. The goal of tomorrow's meeting is to fill those in.

A note on the document's origin: every open question below is also already logged in
the project's running policy file (`To-Speak-About.txt`). This worksheet is the
plain-language version of that file — clearing this worksheet *is* clearing that file.

---

## How the system thinks (the plain version)

### The trust ladder — how a plan number gets identified

When an invoice email arrives, the system looks for the strata plan number in this
order, and stops at the first place it finds one:

1. **The email subject line** — e.g. "FW: BCS 2707 Invoice 26376"
2. **The email body** — the first few lines of the message text
3. **The text inside the invoice PDF itself**
4. **The PDF's filename** — vendors often put the plan number in the file name
5. **Earlier emails in the same conversation** — used when a reply has a plan number
   in the subject but no attachment of its own

Even after the subject gives an answer, the system still reads the PDF and
**cross-checks** it. If the subject and the PDF point at the *same* plan, great. If
they disagree, that's a flag.

### "Flagging" means asking for a human

A flagged email gets a red flag in Outlook and stays in the inbox. Nothing is filed,
nothing is lost. It also shows up in the **daily summary email** the front desk
receives each morning. Flagging is the system saying *"I could be wrong here — look at
this before it goes anywhere."*

### The strictness dial

Every decision below sits on a dial between two poles:

- **Stricter** = flag more, trust less. Safer (an invoice never gets silently
  mis-filed), but more daily work for the front desk, and more chance of loops.
- **Looser** = trust more, flag less. Faster and less work, but a wrong guess gets
  filed silently and may not be caught until a manager or the accountant notices.

The system was launched all the way toward **strict**. The owners' job is to decide
which dials to ease off, and by how much.

### Two kinds of flagged email — this is the key idea

Not all flags are equal. This distinction matters more than any single rule:

- **Type 1 — the front desk can fix it.** The system just needed the plan number
  spelled out. The front desk replies to the email (to themselves) with the corrected
  subject, and next morning it files correctly. The flag did its job; the loop closes.

- **Type 2 — the front desk *cannot* fix it by replying.** The email itself is not
  something the system can ever auto-file, no matter what subject you give it. A reply
  just gets flagged again the next morning — **the email is now in a loop.** Examples:
  a cover letter mixed in with real invoices, a plan that genuinely isn't on our list,
  two legitimately-different related plans in one email, a legitimate re-bill the
  system insists is a duplicate.

  For Type 2, the only way to break the loop is a **human decision to take the email
  out of the inbox** — the front desk handles the invoices another way and moves the
  email to a "Handled" folder so it stops coming back.

> **This is exactly the call you and your dad already made** for the
> "two invoices + one cover letter" email: rather than fighting the system to accept
> it, the front desk moves that email out of the inbox manually. That decision is
> **correct and safe** — and worth knowing, it does *not* break the "no manual drops"
> rule. That rule is about never hand-dropping *PDF files* into the manager work
> folders (that strips the date stamp the system applies). Moving the *email* itself
> touches no invoice file at all — it just stops the loop. Part 3 of this worksheet
> takes your cover-letter decision and asks: which *other* scenarios deserve the same
> treatment, and should we make "move the email to Handled" an official, written
> front-desk step with clear rules for when to use it?

---

## PART 1 — What the system does WITHOUT asking (silent trust)

These are the cases where the system files the invoice with no flag and no human
involvement. Most are obviously fine. A few are **trust-loose points** worth a
conscious "yes, we're comfortable with that" from the owners.

### 1.1 — Subject and PDF agree → filed silently
The subject says BCS 2707, the PDF text confirms BCS 2707. Filed to that plan's
manager. **No concern — this is the happy path.**

### 1.2 — Subject matches, but the PDF is a scan with no readable text → trusts the subject *(trust-loose point)*
Many invoices are scanned images with no machine-readable text. The system can't
cross-check those, so it **trusts whatever the subject said** and files it.
- **Comfortable with this?** It means: if a vendor scans an invoice *and* the front
  desk (or vendor) typed the wrong plan in the subject, it files wrong silently.
- **Decision (2026-05-14):** Comfortable as-is — current behaviour is correct.

### 1.3 — Subject matches, PDF has text but never prints a plan number → trusts the subject *(trust-loose point)*
Some invoices simply don't print the strata plan number anywhere. Same as above — the
system trusts the subject because the PDF gave it nothing to contradict.
- **Decision (2026-05-14):** Comfortable as-is — current behaviour is correct.

### 1.4 — One email, multiple invoices for clearly different buildings → auto-split silently
If an email has two invoices and they point at plainly different plans (e.g. BCS 2707
and BCS 2800 — different buildings, no chance of confusion), the system files each one
to its own plan automatically.
- **Mostly fine** — the risk only appears when the two plans are *related* (see
  Decision 3). Worth knowing it happens silently.
- **Decision (2026-05-14):** No concern. When non-invoice extras appear alongside
  invoices in the same email, the system forwards the full email to ALL involved
  managers (v0.16.0). Each invoice routes to its own plan's manager.

### 1.5 — Plan number found only in the PDF's filename → trusted as a fallback *(trust-loose point)*
If nothing else gives a plan number, the system will trust the plan number in the
*filename* the vendor used.
- **Comfortable with this?** Filenames are vendor-controlled and can be sloppy.
- **Decision (2026-05-14):** Comfortable as-is — current behaviour is correct.

### 1.6 — A duplicate invoice → silently skipped, email still cleared from the inbox
If the same invoice has already been processed, the system doesn't file it again. It
notes it in the daily summary's duplicates section and moves the email out of the
inbox. **No concern for true duplicates** — but see Decision 7 for *legitimate*
re-bills the system mistakes for duplicates.

### 1.7 — Front-desk rescue stamps and files *everything* in the email → *(trust-loose point)*
When the front desk replies-to-self to rescue an email, the system files **every PDF
in it** — including cover letters, account statements, and other non-invoices. The
manager then has to spot and delete those during their review.
- This is the *opposite* problem from strictness — here the system is too trusting.
- **Decision (2026-05-14):** Manager cleanup is acceptable — not frequent enough to
  warrant automatic filtering.

---

## PART 2 — Decision worksheet: when the system flags

Each item below is a real flag the system raises today. For each one, decide whether
to keep it strict, ease it off, or change how it behaves.

### Decision 1 — Subject says one plan, the PDF says a *different* plan
- **What happens now:** Flagged and held. The system refuses to guess which one is
  right.
- **Why:** The most dangerous silent failure is a vendor typing the wrong plan in the
  subject. Flagging surfaces it immediately.
- **Stricter pole:** Always flag (current).
- **Looser pole:** For specific vendors with a proven track record of accurate PDFs,
  trust the PDF over the subject and file without flagging.
- **The question for you:** Keep flagging every clash? Or build a "trusted vendor"
  list over time, once we have months of data showing which vendors are reliable?
- **Decision (2026-05-14):** Trust the PDF. When the PDF text unambiguously identifies
  a directly-managed plan, route to that plan without flagging. Implemented as
  `PDF_OVERRIDE` in v0.16.0. Per-vendor trusted-sender list deferred — revisit after
  several months of operational data.

### Decision 2 — The PDF mentions a plan number that isn't on our managed list ⚠️ *loop trap*
- **What happens now:** Flagged and held — **and there is no recovery step.** Replying
  to self with that same plan number can't help, because the plan genuinely isn't in
  the system. The email loops until someone intervenes manually.
- **Why it's a policy question:** The plan has no manager assigned, so there's nowhere
  to file it even if we wanted to.
- **The question for you — pick a front-desk path:**
  - **(a)** Front desk contacts the vendor to confirm the plan; if it's really ours,
    the plan gets added to the master list; if not, the invoice is rejected.
  - **(b)** Treat "plan not on our list" as an automatic signal to add it to the
    master list (assumes vendors are rarely wrong).
  - **(c)** Define a written manual-handling path for "real invoice, plan not on our
    list" — including moving the email to Handled to break the loop.
- **Decision (2026-05-14):** Front desk discretion. Contact the vendor to clarify, or
  if the invoice isn't ours, delete the email and move it to the Handled folder to
  break the loop. No code change needed.

### Decision 3 — One email, two invoices for closely-*related* plans (e.g. BCS 2707A and BCS 2707B)
- **What happens now:** Flagged. Related plans that share a base number are easy to mix
  up, so the system won't auto-split them the way it does for clearly-different
  buildings.
- **Stricter pole:** Keep flagging; front desk asks the vendor to resend as separate
  emails (current).
- **Looser pole:** If each PDF clearly and independently identifies its own variant,
  auto-file each one — trust the vendor's per-invoice labelling.
- **The question for you:** Keep flagging? Auto-file when each PDF is self-clear? Or a
  middle ground (auto-file only when the PDF's text *and* filename agree per invoice)?
- **Decision (2026-05-14):** Route each PDF to its own plan's manager. Distinct-plan
  AUTO_SPLIT already handles this with forwarding to all involved managers (v0.16.0).
  Suffix-variant cases with different managers (e.g. 2707A and 2707B) still flag for
  now — to be addressed as a future enhancement once the pipeline is stable.

### Decision 4 — The PDF says "BCS 2707" but our list only has "BCS 2707A" and "BCS 2707B"
- **What happens now:** The system files it to the right *manager* (both variants
  share one), but **labels the saved file with the first variant's name** — so the
  filename says something the invoice didn't.
- **The question for you — what should the filename say?**
  - **(a)** Label it with exactly what the PDF said ("BCS 2707") — honest, even if
    incomplete.
  - **(b)** Keep current behaviour — label with the first variant ("BCS 2707A") —
    convenient but technically misrepresents the invoice.
  - **(c)** Refuse to guess — flag it for the front desk to resolve.
- **Decision (2026-05-14):** Flag for front-desk correction (option c). When the PDF
  names only a base plan and the list has only variants, the system flags and holds so
  the front desk can confirm the correct variant. Implemented as `AMBIGUOUS` via
  `is_base_fallback` in v0.16.0.

### Decision 5 — Does "BCS 2707A" also count as a mention of plain "BCS 2707"?
- **What happens now:** Yes — a PDF saying "2707A" is counted as a mention of *both*
  "2707A" and "2707." When both exist on our list, this creates avoidable ambiguous
  flags.
- **Stricter/cleaner pole:** "2707A" counts only for "2707A." Removes a class of false
  flags; small risk if a vendor *meant* the base plan but typed a suffix.
- **Looser pole:** Leave it as-is and just document the trap.
- **Decision (2026-05-14):** Strict — "2707A" only counts for "2707A." Implemented in
  v0.16.0, removing a class of avoidable flags.

### Decision 6 — A real invoice arrives with vendor boilerplate attached (cover letter, fuel-surcharge sheet) ⚠️ *loop trap — your example*
- **What happens now:** The boilerplate PDF has no clear plan number, so **the whole
  email is flagged** — even though the real invoice in the same email is perfectly
  clear. How often this bites varies by batch: the first full live run (2026-05-12)
  flagged roughly **8 emails** this way out of a large backlog; the most recent run
  (2026-05-14) pulled **69 emails** from the inbox and only **about 2** hit this exact
  shape.
- **Why it loops:** Replying-to-self doesn't help — the boilerplate still has no plan,
  so it flags again. This is the exact case where you and your dad decided the front
  desk should move the email out of the inbox manually.
- **The question for you:**
  - **(a)** Keep strict — accept ~8 flags per batch as front-desk work, cleared by
    moving the email to Handled (your current decision).
  - **(b)** Auto-rescue — file the real invoice, set the boilerplate aside, don't flag
    the whole email.
  - **(c)** Maintain a list of known boilerplate filenames the system learns to ignore.
  - **(d)** Combination of (b) and (c).
- **Decision (2026-05-14):** Already resolved by v0.18.0. The real invoice is filed;
  the boilerplate (NO_PLAN sibling) is forwarded to the plan manager. Front desk inbox
  stays clean.

### Decision 7 — A vendor legitimately re-bills (credit + corrected invoice) ⚠️ *loop trap*
- **What happens now:** The system sees the same invoice number / amount / vendor and
  treats it as a **duplicate** — it won't file it. Today, **only a developer can
  override this** (via a command-line tool). The front desk has no way to say "no,
  this one is legitimate."
- **Why it loops:** Until someone overrides it, a legitimate re-bill keeps getting
  caught.
- **The question for you — how should the front desk approve a legitimate re-bill?**
  - **(a)** Reply to the daily summary email with an "allow this one" instruction.
  - **(b)** Reply-to-self on the flagged email with a keyword in the subject.
  - **(c)** Drag the email into a dedicated "release" folder.
  - **(d)** Tiered — simple gesture for routine cases, written reason required for
    high-value ones.
  - Plus: should a reason always be recorded, optionally, or not at all?
- **Decision (2026-05-14):** Already resolved by the 4-field fingerprint (plan +
  invoice # + amount + sender domain, v0.9.2). A re-bill with a corrected amount
  differs on the amount field and is not flagged as a duplicate. A front-desk override
  gesture for the rare edge case where all four fields match remains open for a future
  sprint.

### Decision 8 — An invoice arrives *after* the monthly summary was already sent
- **What happens now:** The system writes a second summary file marked "(1)" rather
  than touching the original.
- **Stricter/safest pole:** Keep the additive "(1)" file — nothing is overwritten,
  fully reversible, front desk decides what to do (current).
- **Looser/cleaner pole:** Re-merge into a single updated summary — cleanest result,
  but risky if the original was already sent to someone outside the office.
- **Middle:** Refuse to re-merge automatically; require a deliberate action.
- **Decision (2026-05-14):** Keep the additive "(1)" file. Nothing is overwritten;
  front desk decides whether to send both summaries to the board or discard one.

### Decision 9 *(maintenance — lower priority for owners)* — Retire the unused safety-net steps?
- **What happens now:** Two early "safety-net" stages of the pipeline now do nothing in
  normal operation (a newer stage absorbed their job). They cost nothing to leave
  running but add maintenance surface.
- **The question:** Keep them indefinitely as belt-and-braces, or retire them after a
  couple of months of confirmed no-op logs? This is really a developer call —
  included only for visibility.
- **Decision (2026-05-14):** Keep them running for now. Revisit after the full
  pipeline is stable and we have several months of all-zero logs.

---

## PART 3 — Feedback-loop traps and the loop-breaker

This part takes the Type 1 / Type 2 idea from the top and makes it concrete.

### The healthy loop (Type 1) — leave it alone
Most flagged emails are Type 1: the system just needed the plan number. Front desk
replies-to-self with the right subject, it files next morning. The flag worked. No
change needed.

### The trap (Type 2) — emails that can never auto-file
These are flagged emails where **replying-to-self will never work**, because the
problem isn't a missing subject — it's the input itself. Left alone, each one comes
back flagged every single morning:

| Type 2 scenario | Why reply-to-self can't fix it | Worksheet item |
|---|---|---|
| Real invoice(s) + cover letter / boilerplate | The boilerplate still has no plan number | Decision 6 |
| Plan genuinely not on our managed list | There's nowhere to file it | Decision 2 |
| Two legitimately-different related plans in one email | Vendor really did combine two properties | Decision 3 |
| Legitimate re-bill flagged as a duplicate | The duplicate rule keeps catching it | Decision 7 |
| Reply-to-self, but the original email never actually had an attachment (vendor sent a link, or an inline image) | There's no PDF in the conversation to find | — |
| A partial failure (some invoices filed, one errored out) | Needs a developer to reconcile | — |

### The loop-breaker — moving the email out of the inbox
For every Type 2 case, the resolution is the same shape: the front desk handles the
invoices through some other path (asks the vendor to resend cleanly, escalates to the
developer, etc.) **and then moves the email out of the inbox into a "Handled" folder**
so it stops being re-processed every morning.

This is safe. The email is not a pipeline file — moving it strips no stamp and breaks
nothing downstream. It is the correct and intended loop-breaker.

### The recommendation for the owners
Right now "move the email to Handled" is a decision you and your dad reached verbally
for *one* scenario (the cover letter). The recommendation is to make it an **official,
written front-desk step** with a clear rule for *when* it applies:

> *"If an email is flagged and you've confirmed it's something the system can never
> file on its own — a cover letter mixed in, a plan we don't manage, a vendor
> combining two buildings — handle the invoice the right way, then move the email to
> the Handled folder. Do not keep replying to it."*

The decisions in Part 2 (especially 2, 6, and 7) determine *how short that list of
Type 2 scenarios is.* The looser you set those dials, the fewer emails ever become
Type 2 traps in the first place. That's the real trade-off to weigh tomorrow: every
strict setting is safer per-invoice, but it adds another email that someone has to
recognise as a trap and manually move.

---

## PART 4 — Summary of decisions to make

| # | Decision | Strict (current) | Looser option | Decision (2026-05-14) |
|---|---|---|---|---|
| 1.2 | Trust subject when PDF is an unreadable scan | — | (it already trusts) | Current behaviour correct |
| 1.3 | Trust subject when PDF prints no plan number | — | (it already trusts) | Current behaviour correct |
| 1.5 | Trust plan number from the filename | — | (it already trusts) | Current behaviour correct |
| 1.7 | Rescue stamps every PDF incl. cover letters | manager cleans up | auto-filter non-invoices | Manager cleanup acceptable |
| 1 | Subject vs. PDF disagree | always flag | trusted-vendor list | Trust PDF: `PDF_OVERRIDE` (v0.16.0) |
| 2 | Plan not on our managed list ⚠️ | flag, no recovery | define a clear path | Front desk discretion; move to Handled |
| 3 | Two related plans in one email | always flag | auto-split when clear | AUTO_SPLIT + all managers (v0.16.0); suffix variants still flag (future) |
| 4 | PDF says base plan, list has only variants | label by guess | label honestly / flag | Flag: `AMBIGUOUS` via `is_base_fallback` (v0.16.0) |
| 5 | "2707A" also counts as "2707" | yes (causes flags) | strict: 2707A only | Strict: 2707A only (v0.16.0) |
| 6 | Real invoice + boilerplate ⚠️ | flag whole email | auto-rescue / ignore-list | v0.18.0 forward protocol resolves this |
| 7 | Legitimate re-bill seen as duplicate ⚠️ | developer-only override | front-desk gesture | 4-field fingerprint handles re-bills (v0.9.2); edge-case gesture open |
| 8 | Late invoice after monthly summary | additive "(1)" file | re-merge | Additive "(1)" file — keep as-is |
| 9 | Retire unused safety-net steps | keep | retire after proving idle | Keep; revisit after pipeline stable |
| — | Make "move email to Handled" an official step | verbal only | written front-desk rule | Official front-desk step — to be added to client training |

⚠️ = also a feedback-loop trap (Part 3)

---

## How to use this after the meeting

> **Status (2026-05-14): steps 2 and 3 below are complete.** Decisions are recorded
> above and in `To-Speak-About.txt`. Code changes (v0.16.0) are live.

1. **Take this file into Claude** and ask it to turn it into the visual one-page
   sheet for the owners — a clean grid of "what flags / what doesn't / the dial" with
   the loop-trap items marked. The structure here (Parts 1–4, the summary table) is
   already laid out to drop straight into a visual.
2. **In the meeting,** fill in every `Decision:` line and the last column of the
   Part 4 table. *(Done 2026-05-14.)*
3. **After the meeting,** the filled-in decisions get transferred into the project's
   `To-Speak-About.txt` file — each decision below maps to an open entry there, and
   filling those `Decision:` fields is what unblocks the developer to actually make
   the changes. *(Done 2026-05-14.)*

### Accuracy check on the existing policy file
All nine open questions in this worksheet are already correctly logged in
`To-Speak-About.txt` with their options spelled out and their `Decision:` fields
blank — nothing there is stale or wrong. This worksheet doesn't add new open
questions; it just translates the existing ones into owner-friendly language and
adds the unifying "Type 1 vs. Type 2 loop" framing, which the policy file currently
describes only case-by-case.
