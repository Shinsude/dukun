# Interfaces

## Language Model Client

The language model contract accepts a list of messages, an optional list of tool schemas, and an optional callback for streamed content fragments. It returns a normalized response that contains either a final answer or a collection of tool invocations, each with an identifier, name, and arguments, plus usage metadata including prompt and completion token counts and cached token counts.

## Sandbox

The sandbox contract defines lifecycle and file operations. Provisioning prepares the workspace, optionally fetching a repository and checking out a commit, and running a setup command. Execution runs a shell command with a timeout and returns exit status, standard output, standard error, and a timeout flag, with support for external abort. File operations read and write paths relative to the workspace and reject escapes via resolved path checks. Cleanup is idempotent.

## Tool

The tool contract defines a single capability. It exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string. The schema is passed to the language model as part of the function-calling specification.

## Evaluator

The evaluator contract examines the sandbox after the orchestrator finishes. It returns a verdict indicating pass or fail, a descriptive detail string, and optional metrics. One implementation runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support. The other always passes for interactive sessions without automated grading.

## Logger and Event Bus

The logger contract receives structured events with a name and payload and never propagates input/output errors to the caller. Implementations append one record per line with thread-safe access. The event bus contract allows subscription of handlers and fans out events synchronously while suppressing handler exceptions.
