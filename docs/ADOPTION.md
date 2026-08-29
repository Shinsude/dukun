# HARNESSY adoption report - what MANTRA should take, and what to leave

Source: full scan of `C:\Users\arif-\HARNESSY` (workflow-layer repo: 60+
PowerShell scripts, 47 skills, 112 test files, hook/orchestrator stack,
`.grok` state directory). Filter applied: adopt only what a solo daily-driver
coding agent needs; everything must earn its bytes.

## Adopt now (three small features, outsized power)

### 1. Read-before-edit ledger (from scripts/read-safe.ps1 + anchor-edit.ps1)

HARNESSY requires a recorded fresh read (SHA-256) before any file edit and
rejects edits whose anchor no longer matches ("stale old_string", incident
class C15). This is their most battle-tested guard - it exists because models
edit files they never read, or edit against outdated content.

Port as roughly 50 lines inside MANTRA's EditFileTool: keep a per-run dict of
path -> sha256(content) written by read_file; edit_file recomputes the hash
first and fails with "file changed since last read - re-read it" on mismatch.
No subprocess, no ledger directory (in-memory per run).

### 2. Known-failure registry (from .grok/known-failures.md)

Their highest-leverage process artifact: every recurring incident becomes an
entry (symptom / fix / probe / date) that is re-read before every task so a
fixed bug class cannot silently return in sibling code. Entries there follow
the KF-N format with pinned regression tests.

Port as a plain markdown list at knowledge/known-failures.md, appended to the
system prompt automatically by console and main. When a bug class is fixed,
one line gets added. Cost: one file plus ~10 lines of loader.

### 3. Durable workspace memory, capped (from .grok/memory.md + recall-digest.md)

HARNESSY keeps project state in memory.md and auto-loads a distilled
recall-digest.md each session. Powerful - but their memory.md has grown to
117 KB, which is the bloat failure mode to avoid copying.

Port as MANTRA/knowledge/memory.md with a hard cap (about 8 KB): newest
entries first, prune rule "keep only what changes a future decision". The
console loads the tail into the system prompt; after each completed task the
agent appends a two-line summary (what changed / where it left off).

## Adopt next (two, when needed)

### 4. Enforced self-verify before finishing (from .grok/verify.cmd concept)

HARNESSY never declares work done without running its aggregate gate. MANTRA
already grades tasks via test_cmd; extend AgentLoop so that when the model
says "done" but the task defines test_cmd, the harness runs it once and feeds
a failure back as an observation instead of ending the run. Roughly 15 lines
in the loop; turns evaluator from judge into coach.

### 5. Session digest (replaces their monitor/analytics/grok-analyzer family)

They run several large scripts over a unified event log. MANTRA already writes
logs/*.jsonl; a single ~80-line summarizer (tasks, pass rate, steps per task,
tool error rate, slowest tools) delivers most of the insight. Add only when
you actually want to inspect agent behavior trends.

## Leave behind (bloat for this use case)

- Orchestrator + hook stack (orchestrator.ps1, hooks/, pre-tool-use evaluators):
  host-level permission plumbing; MANTRA's sandbox boundary plus your judgment
  cover the same need without a second enforcement layer.
- Swarm/subagent skills (swarm, subagent-dev, panel): multi-agent fan-out is
  power without a consumer yet.
- Vault integrity chain (vault-*.ps1), deploy-parity, backup-config: designed
  for a repo deployed to a live installation; MANTRA is a folder you move.
- Evaluation stack (eval-fixtures, trajectory-grade, judge-validate,
  cost-per-change, blast-gate): research tooling for tuning the harness itself;
  revisit if MANTRA becomes a product.
- Mining family (mine-sessions, mine-chat-history, session-watch, metrics):
  depends on Grok host log formats.
- 40+ document/output skills (slides, excel, copy, humanize, design...):
  prompt libraries for the host LLM, not harness features.
- ide/mcp subtree: a separate Node client-facing add-on.
- Statusline, cron/queue runners, headless runner, skill linting pipeline:
  host lifecycle features.

## From the Grok host side (not portable, use via habits)

Todo tracking, subagents, browser verification, and plan mode are host
capabilities, not files to copy. The transferable ideas are behavioral and are
covered by items 1-4 above: verify claims against evidence, fail loudly on
missing input, re-read before editing, and record durable lessons.

## Suggested order

1. Items 1+2+3 together (one small change set: tools hardening + knowledge dir).
2. Item 4 once you notice the agent declaring done with failing tests.
3. Item 5 when logs get interesting.

Everything else: deliberately not adopted. Revisit this list if MANTRA grows
beyond a personal daily driver.
