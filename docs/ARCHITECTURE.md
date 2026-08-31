# Architecture Guide

## High-Level Design

Four layers isolate concerns and allow extension without core changes. Core holds orchestration and depends only on interfaces. The interface layer declares those contracts for language-model clients, sandboxes, tools, evaluators, loggers, and the event bus. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and user-facing entry points for interactive and headless operation.

The central orchestrator is the only stateful component in the core. It is constructed via dependency injection with fully built collaborators and never imports concrete implementations. This separation allows new clients, sandboxes, tools, evaluators, or loggers to be added by implementing an interface and registering the implementation, without changing the core.

## Module Interactions

A task enters through either the interactive console or the headless runner. The console builds a long-lived session that reuses the same sandbox directory, context manager, and approval policy across turns. The headless runner builds a fresh sandbox per invocation.

Orchestrator seeds history, requests a response, and dispatches approved tool calls. Tool execution is delegated to the sandbox, and the resulting observation is appended to the history. Usage metadata is accumulated for accounting. The loop terminates on a final answer, step limit, abort, or error, after which the evaluator inspects the sandbox and the logger records the outcome. An event bus fans out lifecycle events to optional observers.

Conversation history is owned by a dedicated manager that enforces limits on message count and total characters. The initial system prompt and task are pinned, and the oldest complete turn is dropped first, ensuring that tool results are never orphaned. When the character budget is exceeded, the manager truncates the largest message and, if still over budget, continues dropping turns. Oversized single messages are truncated before insertion.

System prompt assembly merges the base instruction, environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state. Each source is capped to bound the prompt size, with durable memory retaining the tail of the file to preserve recent entries. Single-line entries that exceed the cap are truncated to the cap rather than left oversized.

Component assembly is performed by a registry that maps short names from configuration sections to concrete classes. The registry validates unknown names at startup, validates that required constructor parameters are present, and shares a single edit ledger across file tools to enforce read-before-edit within a session. Duplicate tool names that resolve to the same implementation are deduplicated.

## Key Decisions and Trade-Offs

Zero third-party dependencies are favored for core operation. Networking, process execution, and terminal handling rely on the standard library, with an optional parsing library required only for the alternative configuration format. This reduces installation drift at the cost of reimplementing some utilities.

Isolation is offered at two levels. A host-directory sandbox executes directly in a workspace folder for speed during trusted development. A container-based sandbox manages a disposable container via the container runtime interface for stronger isolation. Neither is presented as a complete security boundary, and both enforce path confinement via resolved-path checks at every file and directory operation. The host sandbox also blocks traversal and home-directory expansion in commands and checks each component of a newly created parent chain for symbolic links.

Streaming is optional and callback-driven. When a delta handler is supplied, content fragments are delivered incrementally and tool-call deltas are accumulated by index before being assembled into complete invocations. The parser tolerates occasional malformed chunks but raises after a threshold of consecutive malformed chunks. Total streamed content and individual tool arguments are capped, and a single-line buffer without line breaks is bounded to prevent unbounded memory growth.

Approval is modeled as a separate policy with four modes that are evaluated per tool call. Read-only operations are always allowed. The prompt for confirmation is supplied by the front end, keeping the policy testable and free of terminal input handling. Affirmative answers are remembered for the remainder of the session on a per-tool-key basis.

Persistence is file-based and atomic. Settings, credentials, workflows, and session transcripts are written via temporary files with restricted permissions and then moved into place, with quarantine handling for corrupted files.

