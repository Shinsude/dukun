# Core Domain

## Orchestrator

The orchestrator is the only stateful component in the core. It owns the run loop that provisions the sandbox, builds the bounded conversation, requests language model responses, and dispatches tool calls. It is constructed via dependency injection and interacts with all collaborators through abstract interfaces. The loop supports optional context reuse, external abort signalling, and per-tool approval. It accumulates token usage and cache metrics and ensures that an evaluation is always produced even after errors.

## Context Management

The context manager owns the message list sent to the language model. It retains the system prompt and initial task, enforces limits on message count and total characters, and drops the oldest complete turn first to avoid orphaned tool results. It provides operations to seed the conversation, append messages, replace the body with a summary, and recompute size after external edits. Token estimation is based on character count divided by a constant.

## Knowledge Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location, and version control branch and dirty state. Each source is capped to bound the prompt size, with durable memory retaining the tail of the file to preserve recent entries.

## Approvals

The approval module classifies each tool invocation into safe, mutating, or destructive. File writes and edits are mutating, while certain shell commands are classified as destructive via pattern matching. Four modes are defined that differ in which categories are auto-allowed and which require prompting. The policy remembers affirmative answers for the remainder of the session on a per-tool-key basis.

## Events and Exceptions

The event bus provides synchronous fan-out to subscribed handlers and suppresses handler exceptions to isolate observers from the run. The exceptions module defines a hierarchy with distinct types for configuration, tool, sandbox, language model, evaluation, and abort conditions.
