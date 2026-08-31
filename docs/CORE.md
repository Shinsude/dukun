# Core Domain

## Orchestrator

Orchestrator is the sole stateful core component. Runs the provision, conversation, and tool-dispatch loop. It is constructed via dependency injection and interacts with all collaborators through abstract interfaces. The loop supports optional context reuse for multi-turn conversations, external abort signalling that is propagated to the sandbox and checked between steps and inside the streaming callback, and per-tool approval through a pluggable policy. It accumulates token usage and cache metrics including prompt and completion counts and cached token counts, handles deduplication of tool-call identifiers within a turn with guaranteed uniqueness, validates that language-model responses have the expected shape, and ensures that an evaluation is always produced even after errors, timeouts, or aborts, with cleanup attempted in all cases.

## Context Management

The context manager owns the message list sent to the language model. It retains the system prompt and initial task, enforces limits on message count and total characters with validation that limits are integers meeting minima, and drops the oldest complete turn first to avoid orphaned tool results, with fallbacks that remove the oldest non-tool message and then the oldest tool message. It provides operations to seed the conversation, append messages with truncation of oversized single messages, replace the body with a summary while preserving the system prompt, and recompute size after external edits. When still over budget after dropping turns, it truncates the largest message and recurses once. Token estimation is based on character count divided by a constant.

## Knowledge Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state with handling for non-repository directories and for status-probe failures. Repository-specific instructions are discovered by searching the workspace root for well-known filenames in a defined preference order. Each source is capped to bound the prompt size, with durable memory retaining the tail of the file to preserve recent entries and truncating single-line oversize content to the cap rather than leaving it oversized. The final assembled prompt is capped to prevent context blow-up. Appending durable memory uses a per-process lock and an inter-process lock file with stale-lock breaking, re-reads the file after acquiring the lock to avoid lost updates, prunes oldest lines while over the cap, and writes atomically via a temporary file with restricted permissions.

## Approvals

The approval module classifies each tool invocation into safe, mutating, or destructive. File writes and edits are mutating, while certain shell commands are classified as destructive via pattern matching that covers removal, formatting, process control, privilege escalation, and destructive version-control operations, and safe commands are recognized via a separate pattern set. Four modes are defined that differ in which categories are auto-allowed and which require prompting, with a plan mode that refuses all mutations. The policy remembers affirmative answers that were marked to persist for the remainder of the session on a per-tool-key basis, with distinct key generation for commands, file paths, and other tools, and it suppresses exceptions from the prompting callback.

## Events and Exceptions

The event bus provides synchronous fan-out to subscribed handlers and suppresses handler exceptions to isolate observers from the run. The exceptions module defines a hierarchy with distinct types for harness, configuration, tool, sandbox, language-model, evaluation, and abort conditions, allowing the orchestrator to distinguish operator interruption from other failures.
