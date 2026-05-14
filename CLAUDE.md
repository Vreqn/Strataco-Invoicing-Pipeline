# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Example: If you need to pull data from a website, don't attempt it directly. Read `workflows/scrape_website.md`, figure out the required inputs, then execute `tools/scrape_single_site.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` that do the actual work
- API calls, data transformations, file operations, database queries
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate

**1. Look for existing tools first**
Before building anything new, check `tools/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)
- Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## Policy vs bug — when to defer to `To-Speak-About.txt`

Not every code decision is a coding decision. Some are about **how the business wants its data interpreted, processed, named, or filed** — and those answers depend on the operator and the client, not on what the code currently does.

When you encounter a question that's about **policy, workflow, naming, or data interpretation** — not a bug — don't pick an answer and ship it. Add an entry to `To-Speak-About.txt` at the project root with:

1. **Issue** — what the question is, with file paths and line numbers.
2. **Why it's a policy question** — what about the answer depends on real-world preference rather than code correctness.
3. **Options** — concrete alternatives, each with downstream implications.
4. **Decision** — left blank; filled in after Krisztian discusses with his dad / client.

Examples that go in `To-Speak-About.txt`:
- "When a PDF mentions plan `BCS 2707` but the XLS only has `BCS 2707A` and `BCS 2707B`, which suffix should we route under?"
- "Should the archived filename use the original vendor name, the strata name, or the check number first?"
- "When a manager has zero invoices, should they still get a daily reminder email?"

Examples that **do not** go in `To-Speak-About.txt` (just fix them):
- "`os.replace` overwrites silently — switch to a collision-safe write."
- "URL path segments not encoded — wrap with `urllib.parse.quote`."
- "Subject regex assembles digits and suffix in the wrong order."

Rule of thumb: if two reasonable people who both understand the code might disagree on the right answer because their answer depends on operational preference, it's a `To-Speak-About.txt` entry. If a senior engineer would call it a bug after a 30-second look, just fix it.

## Audience rules — non-negotiable

Project text serves two distinct audiences. Confusing them is the most common drift mode in this codebase.

**The audiences:**
- **Front desk** = the person who organises the inbox each morning. Their universe is Outlook (Inbox + flagged emails) and the daily summary email Step 6 sends. Use the term **"front desk"** (drop the older catch-all "operator" when it refers to this role).
- **Manager** and **accountant** = office-staff roles with their own named work folders (`To_Approve`, `Approved`, `Approved_Invoices`, `Paid_Invoices`).
- **Developer / admin** = Krisztian. Reads logs, maintains `Strataplan_List.xlsx`, makes code changes. Not babysitting day-to-day.

**Where things belong:**
- `docs/client_training_brief.md` is office-staff-facing.
- `CLAUDE.md`, `ReleaseNotes.txt`, `HANDOFF.md`, `workflows/*.md`, `To-Speak-About.txt` are developer-facing.
- `README.md` is mostly developer-facing but contains a few office-staff-facing sections (e.g. the Recovery workflow). Treat those sections by the office-staff rules below.

**Universal rule — no manual drops (applies to EVERYONE, front desk and developer alike):**
Every file in `To_Approve` / `Approved` / `Approved_Invoices` / `Paid_Invoices` / `Strata_Plans/<plan>/` must come through Step 1. That's the only place the Received stamp gets applied. Manually dropping a PDF strips the stamp, which silently breaks Step 5's flatten and Step 6's archive-filename logic (the check number can't be read from a missing stamp). Recovery for every flagged or unmatched email is **get it back into the Inbox with a corrected subject and let Step 1 re-route** — for the front desk that's reply-to-self; for the developer that's the same recipe or a re-triggered Step 1 run after fixing whatever was wrong upstream. Never write a "drop the PDF into the folder by hand" recipe — for any audience.

**Office-staff-material rules (apply to anything written for front desk, managers, or accountants):**
- No log file paths, no `_Unmatched/`, no folder-spelunking outside named work folders, no code symbols, no line numbers. Plain cause-and-effect.
- Office staff escalate to the developer; they do not troubleshoot. If a recipe says "open the log and search for X," it belongs in developer-facing material, not here.

**Developer-discipline rule:**
No active daily monitoring is expected. The system surfaces problems via Inbox flags and the daily summary email. If you find yourself about to write a developer recipe involving periodic log-grepping as a maintenance ritual, redesign the surfacing instead — that's a sign something the front desk should see isn't visible yet.

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## Regression Testing

Before claiming any tool or workflow change is complete, you MUST run the relevant tests AND paste the actual command output into your reply. "I'll set up tests" is not enough. "Tests pass" without showing the output is not enough. Show the command, show the result. If you skip this step, you have not finished the task.

The test runner for this project is **pytest**. Use it for everything. Do not invent ad-hoc test scripts, do not use `unittest`, do not use `if __name__ == "__main__"` smoke tests. If pytest isn't installed in the environment yet, install it (`pip install pytest pytest-mock`) and note it in the workflow.

### Where Tests Live

```
tests/
├── conftest.py              # Shared pytest fixtures
├── fixtures/
│   ├── inputs/              # Sanitized real-world inputs (sample emails, PDFs, XLS rows)
│   └── golden/              # Known-correct outputs for each input
├── test_<tool_name>.py      # One test file per script in tools/
└── integration/             # End-to-end workflow tests
```

Every script in `tools/` should have a matching `test_<tool_name>.py`. If a tool has no tests yet, write them before changing the tool — otherwise you have no way to know what you broke.

### What Counts as a Regression Test

A regression test is concrete: a real input fixture, a known-good output, and an assertion comparing them. When a new tool works for the first time, the very next step is to capture that run's input and output as a fixture/golden pair. That pair becomes the permanent regression test for that tool.

For tools that hit external APIs (Microsoft Graph, Google Drive, web scraping), mock the calls. Use `pytest-mock` or `responses`. Live API calls in tests burn credits, fail in odd ways, and defeat the point. If you find a tool with un-mocked tests, fix that as part of touching the tool.

### When You MUST Run Tests

There are two test cadences. Use the right one for where you are in the work.

**Inner loop — while you're iterating on a single tool:**
Run `pytest tests/test_<tool_name>.py -v` and paste the output. This is fast and keeps you moving. Use it for every change attempt, every bug fix iteration, every "let me try that again."

**Done gate — before claiming the task is complete:**
Run the full suite with `pytest tests/ -v` and paste the output. No exceptions. The full suite catches the case where your fix to one tool quietly broke a different tool that imports from it, shares a fixture with it, or depends on the same data shape.

You must run the full suite before:

1. Telling me a tool change is complete
2. Telling me a workflow update is complete
3. Closing out any bug fix
4. Responding to "does this work?" or "is it ready?"
5. Moving on to a different tool or workflow in the same session

If the full suite reveals a failure in a tool you didn't think you touched, that's exactly why the gate exists. Fix it, rerun the full suite, paste the green output. Then you're done.

Per-file runs during iteration are not a substitute for the done gate. "I ran the test for the tool I changed and it passed" is not the same as "the system still works." Show both.

### Bug Fix Protocol

When you fix a bug, you add a test that catches it before you fix it. The sequence:

1. Capture the input that triggered the bug as a fixture in `tests/fixtures/inputs/`
2. Write a test asserting the correct behavior on that input
3. Run the test — it should fail (this proves the test actually catches the bug)
4. Fix the bug in the tool
5. Run the test — it should pass
6. Run the full suite — nothing else should have broken

Paste the output of steps 3, 5, and 6. If you skipped step 3, you don't know if your test is real.

### Updating Goldens

If a change is supposed to alter output (intentional behavior change, not a bug fix), update the golden file deliberately and explain why in the commit message. If the behavior change affects how the business interprets data (naming, routing, classification), add a `To-Speak-About.txt` entry before updating the golden — that's a policy decision, not a code one.

Never edit a golden just to make a failing test go quiet. That defeats the entire system.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/           # Temporary files (scraped data, intermediate exports). Regenerated as needed.
tools/          # Python scripts for deterministic execution
tests/          # pytest regression tests, one test_<tool>.py per script in tools/
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.

Always ask questions if you're unsure.

Treat the README as an operators manual rather than a history of changes. You can create a ReleaseNotes.txt file and put all the updates there.
