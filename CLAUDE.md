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

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/           # Temporary files (scraped data, intermediate exports). Regenerated as needed.
tools/          # Python scripts for deterministic execution
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.

Always Ask questions if you're unsure

Treat the README as an operators manual rather then a history of changes. You can create a ReleaseNotes.txt file and put all the updates there. 
