# Architecture Guide

## High-Level Design

The system is organized into four layers. The core layer holds the orchestration and state management and depends only on abstract contracts. The interface layer declares those contracts. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and user-facing entry points.

The central orchestrator is the only stateful component in the core. It is constructed via dependency injection with fully built collaborators and never imports concrete implementations. This separation allows new language model clients, sandboxes, tools, evaluators, or loggers to be added by implementing an interface and registering the implementation, without changing the core.

## Module Interactions

A task enters through either the interactive console or the headless runner. The console builds a long-lived session that reuses the same sandbox directory, context manager, and approval policy across turns. The headless runner builds a fresh sandbox per invocation.

The orchestrator drives the loop: it seeds or extends the conversation history, requests a response from the language model, and if the response contains tool invocations, checks each against the approval policy and dispatches it to the appropriate tool. Tool execution is delegated to the sandbox, and the resulting observation is appended to the history. Usage metadata is accumulated for accounting. The loop terminates on a final answer, step limit, abort, or error, after which the evaluator inspects the sandbox and the logger records the outcome. An event bus fans out lifecycle events to optional observers.

Conversation history is owned by a dedicated manager that enforces limits on message count and total characters. The system prompt and initial task are pinned, and the oldest complete turn is dropped first, ensuring that tool results are never orphaned. System prompt assembly merges the base instruction, environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions, each subject to a size cap. Both entry points now assemble the full set; the headless runner previously supplied only the failure registry, which gave a graded run a weaker standing prompt than an interactive one on the same workspace.

Component assembly is performed by a registry that maps short names from configuration sections to concrete classes. The registry validates unknown names at startup and shares a single edit ledger across file tools to enforce read-before-edit within a session.

## Key Decisions and Trade-offs

Zero third-party dependencies are favoured for core operation. Networking, process execution, and terminal handling rely on the standard library, with an optional parsing library required only for one configuration format. This reduces installation drift at the cost of reimplementing some utilities.

Isolation is offered at two levels. A host-directory sandbox executes directly in a workspace folder for speed during trusted development. A container-based sandbox manages a disposable container via the container runtime command-line interface for stronger isolation. Neither is presented as a complete security boundary.

Streaming is optional and callback-driven. When a delta handler is supplied, content fragments are delivered incrementally and tool-call deltas are accumulated by index before being assembled into complete invocations. A single-file structured log provides observability without requiring external services.

Approval is modelled as a separate policy with four modes that are evaluated per tool call. Read-only operations are always allowed. The prompt for confirmation is supplied by the front end, keeping the policy testable and free of terminal input handling.
