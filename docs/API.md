# API Reference

## Entry Points

Two entry points are exposed. The interactive console accepts optional flags to select the workspace location, override the configured model, endpoint, reasoning effort, and approval mode, and to disable styling or request compact rendering. It also supports a single-message non-interactive flag that handles one prompt and exits. The headless runner requires paths to a configuration document and a task document and returns a process exit status indicating whether the evaluator passed.

## Interactive Commands

Commands are invoked with a leading slash. The help command lists all commands. Workspace inspection commands display the workspace location and contents, the durable memory file, and uncommitted changes, with a confirmation step before discarding changes. Tool, model, endpoint, and approval commands list or select the available options, presenting a menu when no argument is supplied and applying a direct assignment when an argument is supplied. Endpoint commands also support listing, removing, and replacing stored credentials, with always-prompted replacement to allow correction of a mistyped secret. Cost, context, and status commands display token usage and conversation size. History management commands summarise, clear, or reset the conversation while preserving the system prompt and files. Session persistence commands save the current conversation to a file, load from a file, or resume from automatically saved transcripts, with the most recent transcript listed first. Goal commands set, show, and clear a standing objective that is injected into every turn. Skill and workflow commands discover, display, attach, and launch procedural bundles. Pasting, step limit, and verbosity commands control input and execution bounds. The exit command terminates the session.

File references are written with a sigil followed by a path or pattern. A reference is resolved relative to the workspace, rejected if it would escape the workspace, and expanded to a file content block, a directory listing, or the set of files matching a glob. Each file is capped, the total attached content is capped, and unknown references are reported.

## Configuration Sections

The configuration document is divided into sections. The language model section identifies the provider name, model name, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort, and streaming preference. The sandbox section selects the isolation provider and its resource limits. The tools section lists the names of tools exposed to the model. The evaluator section selects the grading strategy, test command, and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects the default policy. The context section limits retained message count and character count. Top-level keys control the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity, and skill routing preferences.

The task document requires a problem statement. Optional keys provide a repository address, a base commit, a setup command, and a task-specific test command that overrides the evaluator configuration.

## Abstract Interfaces

The language model interface accepts a message list, optional tool schemas, and an optional streaming callback. It returns a normalized response that is either a final answer or a list of tool invocations, each with an identifier, name, and arguments, plus usage metadata. The streaming callback, when supplied, receives content fragments as they arrive.

The sandbox interface provisions the environment for a task, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Execution results include exit status, standard output, standard error, and a timeout flag. Cleanup is idempotent and safe to call multiple times.

The tool interface exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation.

The evaluator interface examines the sandbox after the loop and returns a pass flag, descriptive detail, and optional metrics.

The logger interface receives structured events and never propagates failures to the caller. The event bus interface fans out events to subscribed handlers and suppresses handler exceptions.

## Registry

The registry maps short names to concrete classes for language model clients, sandboxes, tools, evaluators, and loggers. Construction forwards only those configuration keys that match constructor parameters and validates that required parameters are present, reporting unknown names or missing parameters as configuration errors.
