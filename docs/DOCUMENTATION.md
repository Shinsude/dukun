# MANTRA Documentation

> Modular harness that delegates tasks to a language model. Provides a sandboxed workspace, file and command tools, and automated grading via a test command. Available as an interactive console or a headless runner.

---

## Table of Contents

- [1. Overview](#1-overview)
- [2. Quick Start](#2-quick-start)
- [3. Architecture](#3-architecture)
  - [3.1 Four-Layer Design](#31-four-layer-design)
  - [3.2 Module Interactions](#32-module-interactions)
  - [3.3 Registry](#33-registry)
  - [3.4 Key Trade-Offs](#34-key-trade-offs)
- [4. Interfaces](#4-interfaces)
  - [4.1 Language-Model Client](#41-language-model-client)
  - [4.2 Sandbox](#42-sandbox)
  - [4.3 Tool](#43-tool)
  - [4.4 Evaluator](#44-evaluator)
  - [4.5 Logger and Event Bus](#45-logger-and-event-bus)
- [5. Implementations](#5-implementations)
  - [5.1 Language-Model Clients](#51-language-model-clients)
  - [5.2 Sandboxes](#52-sandboxes)
  - [5.3 Tools](#53-tools)
  - [5.4 Evaluators and Loggers](#54-evaluators-and-loggers)
- [6. Core Domain](#6-core-domain)
  - [6.1 Orchestrator](#61-orchestrator)
  - [6.2 Context Management](#62-context-management)
  - [6.3 Knowledge Assembly](#63-knowledge-assembly)
  - [6.4 Approvals](#64-approvals)
  - [6.5 Events and Exceptions](#65-events-and-exceptions)
- [7. Configuration](#7-configuration)
  - [7.1 Format and Validation](#71-format-and-validation)
  - [7.2 Defaults](#72-defaults)
  - [7.3 User-Wide Settings and Credentials](#73-user-wide-settings-and-credentials)
  - [7.4 Configuration Sections Reference](#74-configuration-sections-reference)
  - [7.5 Task Document](#75-task-document)
- [8. Console](#8-console)
  - [8.1 Session](#81-session)
  - [8.2 Line Editor](#82-line-editor)
  - [8.3 Layout and Viewport](#83-layout-and-viewport)
  - [8.4 File Mentions](#84-file-mentions)
  - [8.5 Goals, Skills, and Workflows](#85-goals-skills-and-workflows)
  - [8.6 Interactive Commands](#86-interactive-commands)
  - [8.7 Help Text](#87-help-text)
- [9. Plugin System](#9-plugin-system)
  - [9.1 Plugin Directories](#91-plugin-directories)
  - [9.2 Plugin Module Format](#92-plugin-module-format)
  - [9.3 Configuration](#93-configuration)
  - [9.4 Registration and Deduplication](#94-registration-and-deduplication)
- [10. Non-OpenAI Clients](#10-non-openai-clients)
  - [10.1 Anthropic Client](#101-anthropic-client)
  - [10.2 Gemini Client](#102-gemini-client)
  - [10.3 Provider Registry](#103-provider-registry)
- [11. Regex Search](#11-regex-search)
- [12. Edit Ledger](#12-edit-ledger)
  - [12.1 Read-Before-Edit Enforcement](#121-read-before-edit-enforcement)
  - [12.2 Truncation Detection](#122-truncation-detection)
- [13. Setup](#13-setup)
  - [13.1 Prerequisites](#131-prerequisites)
  - [13.2 Installation](#132-installation)
  - [13.3 Local Development](#133-local-development)
  - [13.4 Verification](#134-verification)
- [14. Deployment](#14-deployment)
  - [14.1 Build](#141-build)
  - [14.2 Testing](#142-testing)
  - [14.3 Environment Promotion](#143-environment-promotion)
  - [14.4 Operational Runbooks](#144-operational-runbooks)
- [15. Adoption Report](#15-adoption-report)
  - [15.1 What Was Adopted](#151-what-was-adopted)
  - [15.2 What Was Left Behind](#152-what-was-left-behind)
  - [15.3 Post-Remediation State](#153-post-remediation-state)
- [16. Safety Notes](#16-safety-notes)

---

## 1. Overview

MANTRA is a modular harness that delegates tasks to a language model. It provides:

- **Sandboxed workspace** with host-directory or container-based isolation
- **File and command tools** with read-before-edit enforcement
- **Automated grading** via a configurable test command
- **Interactive console** with persistent workspace and conversation history
- **Headless runner** for scripted or CI/CD execution

The interactive console maintains a persistent workspace across turns, retains conversation history within configurable bounds, and supports inline references to workspace files. The headless runner executes one task from start to evaluation and records a structured event log.

---

## 2. Quick Start

### Interactive Console (Recommended)

```bash
# From project root (no installation required)
python src/mantra/console.py

# Or via installed entry point
mantra

# With overrides
mantra --workspace /path/to/repo --model gpt-4o --plain
```

Upon launch, the console shows model, endpoint, workspace, and version-control status. If no endpoint is configured, it guides you through adding one by supplying a base address and a credential.

Type a natural-language request at the prompt. The system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace.

### Headless Runner

```bash
mantra-headless --config config.json --task task.json
```

Provisions the workspace, executes the orchestrator loop, grades the result, writes an event log, and exits with a status indicating whether automated grading passed.

---

## 3. Architecture

### 3.1 Four-Layer Design

| Layer | Responsibility | Dependencies |
|-------|---------------|--------------|
| **Core** | Orchestration, state management, history, knowledge assembly | Only interfaces |
| **Interface** | Abstract contracts (LLMClient, Sandbox, Tool, Evaluator, Logger, EventBus) | stdlib only |
| **Implementation** | Concrete components for each contract | Interfaces |
| **Outer** | Assembly, config loading, user-facing entry points | All layers |

The central orchestrator is the only stateful core component. It is constructed via dependency injection with fully built collaborators and never imports concrete implementations. New clients, sandboxes, tools, evaluators, or loggers can be added by implementing an interface and registering the implementation, without changing the core.

### 3.2 Module Interactions

```
User Input → ConsoleSession / Headless Runner
                    ↓
              Orchestrator (core/agent_loop.py)
                    ↓
    ┌───────────────┼───────────────┐
    ↓               ↓               ↓
LLM Client      Sandbox         Evaluator
(interface)   (host/container)  (shell/null)
                    ↓
              Tool Execution
            (file/command/search)
                    ↓
              EventBus + Logger
```

- **Console**: builds a long-lived session reusing the same sandbox, context, and approvals across turns
- **Headless**: builds a fresh sandbox per invocation
- Orchestrator seeds history, requests responses, dispatches approved tool calls, accumulates usage, and produces evaluation

### 3.3 Registry

The registry maps short names from configuration sections to concrete classes:

```python
LLM_REGISTRY = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
    "scripted": ScriptedClient,
}

TOOL_REGISTRY = {
    "read_file": ReadFileTool,
    "write_file": WriteFileTool,
    "edit_file": EditFileTool,
    "list_dir": ListDirTool,
    "run_command": RunCommandTool,
    "search_code": SearchCodeTool,
    "find_file": FindFileTool,
    "git_diff": GitDiffTool,
    "git_discard": GitDiscardTool,
    "webfetch": WebFetchTool,
}
```

Registry validates unknown names at startup, validates required constructor parameters, and shares a single edit ledger across file tools. Duplicate tool names that resolve to the same implementation are deduplicated.

### 3.4 Key Trade-Offs

- **Zero dependencies** for core operation (stdlib only); optional parsing library only for alternative config format
- **Two sandbox levels**: host-directory (fast, trusted) and container-based (isolated, disposable)
- **Streaming** is optional, callback-driven, with malformed-chunk tolerance
- **Approval** is a separate policy with four modes, testable without terminal I/O
- **Persistence** is file-based and atomic (temp file + restricted permissions + move)

---

## 4. Interfaces

### 4.1 Language-Model Client

```python
class LLMClient(ABC):
    @abstractmethod
    def chat(self, messages, tools=None, on_delta=None) -> LLMResponse:
        ...
```

Accepts a list of messages, optional tool schemas, and an optional streaming callback. Returns a normalized response containing either a final answer or tool invocations (each with id, name, arguments) plus usage metadata (prompt tokens, completion tokens, cached tokens).

### 4.2 Sandbox

Defines lifecycle and file operations:
- **Provision**: validates inputs, fetches repository, runs setup
- **Execute**: runs shell command with timeout and abort support; returns exit status, stdout, stderr, timeout flag
- **File ops**: read/write paths relative to workspace; rejects escapes via resolved-path checks; reads capped and truncated; writes validated for size and parent-chain confinement
- **Cleanup**: idempotent; distinguishes owned vs. given directories

### 4.3 Tool

```python
class Tool(ABC):
    name: str
    description: str

    @abstractmethod
    def execute(self, sandbox, **kwargs) -> str: ...

    def schema(self) -> dict: ...
```

Exposes name, description, and parameter schema. Execution against a sandbox returns an observation string appended to the conversation. Schema is passed to the LLM as part of function-calling specification.

### 4.4 Evaluator

Examines the sandbox after the orchestrator finishes. Returns:
- **verdict**: pass or fail
- **detail**: descriptive string
- **metrics**: optional dict

Two implementations: `ShellEvaluator` (runs a command, zero exit = pass) and `NullEvaluator` (always neutral for interactive sessions).

### 4.5 Logger and Event Bus

- **Logger**: receives structured events with name + payload; never propagates errors; append-only file with timestamps and thread-safe locking
- **EventBus**: synchronous fan-out to subscribed handlers; suppresses handler exceptions to isolate observers

---

## 5. Implementations

### 5.1 Language-Model Clients

#### OpenAI Client (`openai_client.py`)

Speaks the standard chat completions protocol over stdlib networking:
- Buffered and streaming modes
- Tool-call delta accumulation by index (handles name/argument fragments arriving separately or as parsed structures)
- SSE framing with terminating sentinel and usage objects
- Retry with backoff
- Per-field downgrades when server rejects optional parameters (usage inclusion, reasoning effort, token-limit naming)
- Chat → responses-API fallback when chat endpoint returns not-found
- Credentials resolved from configured lookup; bearer token header
- Response size and streamed content capped; malformed chunks tolerated up to threshold

#### Anthropic Client (`anthropic_client.py`)

Speaks the Anthropic Messages API:
- Streaming with `event: message_delta` and `event: content_block_delta` handling
- `max_tokens` required by the API (set to 262144 by default)
- Tool schemas translated from OpenAI format to Anthropic format (`input_schema` instead of `parameters`)
- Response parsing extracts text from `content` blocks and tool_use blocks

```python
# Registration in registry.py
LLM_REGISTRY["anthropic"] = AnthropicClient
```

#### Gemini Client (`gemini_client.py`)

Speaks the Google Gemini API (`generateContent`):
- Uses `systemInstruction` for system prompt (extracted from first message)
- Tool schemas translated to Gemini format (`functionDeclarations` with `parameters.properties` flattened)
- Streaming with `candidates[0].content.parts` parsing
- Supports both `GEMINI_API_KEY` and `GOOGLE_API_KEY` environment variables

```python
# Registration in registry.py
LLM_REGISTRY["gemini"] = GeminiClient
LLM_REGISTRY["google"] = GeminiClient  # alias
```

#### Scripted Client (`scripted.py`)

Replays a fixed script of responses for offline testing. Raises when the script is exhausted.

### 5.2 Sandboxes

#### Host-Directory Sandbox (`local_sandbox.py`)

Executes directly in a workspace directory:
- Creates directory if needed; uses temp directory when none supplied
- Validates repository addresses and commit identifiers
- Blocks traversal, home-directory, and environment-variable expansion in commands
- Path resolution rejects escapes via symlink resolution at read/write/create time
- Parent-chain confinement checks for symbolic links
- Atomic temporary-file replacement for writes
- Reads capped and truncated with marker; writes rejected when content exceeds allowed size

#### Container Sandbox (`container_sandbox.py`)

Manages a disposable container via the container runtime CLI:
- Resource limits and optional networking control
- File transfer via staging with owner-only permissions
- Path escape validation; repository address/commit safety checks
- Cleanup removes the container; respects abort signals

### 5.3 Tools

#### File Tools (`file_tools.py`)

| Tool | Description |
|------|-------------|
| `read_file` | Read with truncation and ledger recording |
| `write_file` | Write with parent directory creation and ledger recording |
| `edit_file` | Edit with read-before-edit and stale-content checks; rejects edits to truncated content |
| `list_dir` | Directory listing with safe quoting and metacharacter validation |

#### Command Tools (`command_tools.py`)

| Tool | Description |
|------|-------------|
| `run_command` | Execute shell command; returns exit status, timeout flag, truncated output |
| `git_diff` | Show version-control differences |
| `git_discard` | Discard changes with timeout handling |

#### Search Tools (`search_tools.py`)

| Tool | Description |
|------|-------------|
| `search_code` | Walk workspace, filter symlinks, skip non-text files, return matching lines (supports `regex` flag) |
| `find_file` | Walk workspace, return matching file paths |

Search tools skip ignored directories, filter symlinked directories pointing outside the workspace, skip files above size limit or with non-text extensions, and return results with limits on count and line length.

#### Web Tool (`web_tools.py`)

| Tool | Description |
|------|-------------|
| `webfetch` | Fetch URL with scheme/size/decompression limits, charset detection, visible-text extraction, private-host blocking, redirect validation |

### 5.4 Evaluators and Loggers

#### Evaluators

- **ShellEvaluator** (`shell_evaluator.py`): runs a command, zero exit without timeout = pass; per-task override support; tail truncation of output
- **NullEvaluator** (`null_evaluator.py`): always neutral for interactive sessions

#### Logger

- **JsonlLogger** (`jsonl_logger.py`): append-only structured records with timestamp, event name, and payload; thread-safe locking; temp-file writes with restricted permissions

---

## 6. Core Domain

### 6.1 Orchestrator (`agent_loop.py`)

The sole stateful core component:
- Runs the provision → conversation → tool-dispatch loop
- Supports context reuse for multi-turn conversations
- External abort signalling propagated to sandbox and checked between steps
- Per-tool approval via pluggable policy
- Accumulates token usage and cache metrics
- Deduplicates tool-call identifiers within a turn
- Validates LLM response shape
- Ensures evaluation is always produced (even after errors, timeouts, aborts)

### 6.2 Context Management (`context.py`)

Owns the message list sent to the LLM:
- Retains system prompt and initial task
- Enforces message count and character count limits (validated as integers meeting minima)
- Drops oldest complete turn first (avoids orphaned tool results)
- Fallbacks: remove oldest non-tool message, then oldest tool message
- Truncates largest message when still over budget
- Token estimation based on character count / constant

### 6.3 Knowledge Assembly (`knowledge.py`)

Assembles the system prompt from multiple sources:
- **Base instruction**: default system prompt
- **Environment facts**: date, OS, shell, interpreter version, workspace path, git branch + dirty state
- **Known-failure knowledge**: recurring incident classes from `knowledge/known-failures.md`
- **Durable memory**: tail of `.mantra/memory.md` (capped, oldest pruned, single-line truncation)
- **Repository instructions**: discovered by searching workspace root for well-known filenames in preference order

Each source is capped. Final assembled prompt is capped to prevent context blow-up.

Memory appending uses per-process lock + inter-process lock file with stale-lock breaking; atomic writes via temp file with restricted permissions.

### 6.4 Approvals (`approvals.py`)

Classifies each tool invocation:
- **Safe**: read-only operations (always allowed)
- **Mutating**: file writes and edits
- **Destructive**: shell commands matching removal, formatting, process control, privilege escalation, destructive VCS patterns

Four modes:
| Mode | Safe | Mutating | Destructive |
|------|------|----------|-------------|
| `auto` | ✓ | ✓ | prompt |
| `semi` | ✓ | prompt | prompt |
| `strict` | ✓ | prompt | deny |
| `plan` | ✓ | deny | deny |

Affirmative answers remembered per-tool-key for the session. Prompt supplied by the front end (testable without terminal I/O).

### 6.5 Events and Exceptions (`events.py`, `exceptions.py`)

- **EventBus**: synchronous fan-out; suppresses handler exceptions
- **Exceptions**: distinct types for HarnessError, ConfigError, ToolError, SandboxError, LLMError, EvalError, AbortError

---

## 7. Configuration

### 7.1 Format and Validation

Configuration is a structured document. Primary format is native (JSON-like); alternative format requires an optional parsing library.

The loader:
1. Caps raw file size before parsing
2. Merges deeply into a set of defaults
3. Validates required sections (must be objects, at least one tool listed)
4. Validates enumerated values (approval mode, reasoning effort)
5. Validates context limits (integers meeting minima)
6. Reports errors as configuration errors with descriptive messages

### 7.2 Defaults

```python
DEFAULTS = {
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
    },
    "sandbox": {"provider": "local"},
    "tools": [
        "read_file", "write_file", "edit_file", "list_dir",
        "run_command", "search_code", "find_file",
        "git_diff", "git_discard", "webfetch",
    ],
    "evaluator": {"command": "python -m pytest tests/ -q --tb=short"},
    "logger": {"sink": "jsonl"},
    "approvals": "auto",
    "plugins": [],
    "context": {"messages": 200, "chars": 2_000_000},
    "max_steps": 50,
    "auto_compact_tokens": 100_000,
    "verbose": False,
    "skills": {"auto_attach": True, "auto_launch": False},
}
```

### 7.3 User-Wide Settings and Credentials

User-wide settings are in a hand-editable document in the home directory:
- Endpoints (base address, credential lookup, known models, note)
- Active endpoint, model, reasoning effort
- Skill routing preferences

Credentials store:
- Restricted permissions, never written to main settings file
- Keyed by lookup name
- Resolved first from process environment, then from store
- Only masked forms displayed

Override locations via environment variables:
- `MANTRA_SETTINGS`: redirect settings file
- `MANTRA_CREDENTIALS`: redirect credentials store
- `MANTRA_SESSIONS`: redirect session transcript directory
- `MANTRA_WORKFLOWS`: redirect workflow definitions file

### 7.4 Configuration Sections Reference

| Section | Keys | Description |
|---------|------|-------------|
| `llm` | `provider`, `model`, `base_url`, `api_key_env`, `temperature`, `max_tokens`, `reasoning_effort`, `stream` | Language-model settings |
| `sandbox` | `provider` (+ provider-specific) | Isolation provider and limits |
| `tools` | List of tool names | Tools exposed to the LLM |
| `evaluator` | `command`, `timeout` | Grading strategy |
| `logger` | `sink`, `file` | Event log sink |
| `approvals` | One of: `auto`, `semi`, `strict`, `plan` | Default approval policy |
| `plugins` | List of directory paths or plugin names | External tool directories |
| `context` | `messages`, `chars` | History retention limits |
| `max_steps` | Integer | Maximum steps per task |
| `auto_compact_tokens` | Integer | Threshold for auto-summarization |
| `verbose` | Boolean | Verbose output |
| `skills` | `auto_attach`, `auto_launch` | Skill routing preferences |

### 7.5 Task Document

| Key | Required | Description |
|-----|----------|-------------|
| `problem` | ✓ | Problem statement |
| `repo` | | Repository address |
| `base_commit` | | Base commit for diff |
| `setup` | | Setup command |
| `setup_timeout` | | Setup timeout |
| `clone_timeout` | | Clone timeout |
| `test_command` | | Per-task evaluator override |

---

## 8. Console

### 8.1 Session (`ConsoleSession`)

Owns workspace, sandbox, context, prompt, tools, client, approvals, and event bus. Maintains:
- Token/turn/error totals
- Recent tool history
- Per-turn cache metrics with hit-rate trends
- Streaming renderer (buffers fragments until line boundaries)
- Terminal escape sequence sanitization
- Background spinner (paused when real output arrives)

### 8.2 Line Editor

- Single-key reading with completion for commands and workspace paths
- Navigation, deletion, and history handling
- Popup with filtering, arrow-key selection, and hint area
- Dismiss/re-invoke via dedicated key
- Fallback to plain line input when stdin/stdout is not a terminal

### 8.3 Layout and Viewport

Compact viewport reserves rows for:
- Top information bar
- Separator
- Content area (internal buffer + scroll support)
- Border row
- Fixed bottom prompt (model, reasoning effort, workspace, live token estimate)

Supports alternate screen enter/leave, clear/redraw chrome, resize handling. Startup card is a centered adaptive block showing product name, tagline, and version; disappears after first turn.

### 8.4 File Mentions

Sigil-prefixed tokens (e.g., `@file.py`, `@src/`) are resolved relative to the workspace:
- Rejected if outside workspace (path escape check)
- Expanded to content, listing, or glob matches
- Each file capped; total attached content capped
- Glob hit count capped; directory listings limited
- Truncation marker when caps exceeded

### 8.5 Goals, Skills, and Workflows

- **Goals**: standing objective injected into every turn; notes supported; check for agent-reported completion
- **Skills**: procedure text appended to prompt; automatic routing with confidence/margin thresholds
- **Bundles**: ordered steps launching skills in sequence
- **Workflows**: named sequences of prompts launched as ordered steps

### 8.6 Interactive Commands

| Command | Description |
|---------|-------------|
| `/help` | List all commands |
| `/workspace` | Show workspace location |
| `/memory` | Show durable memory file |
| `/diff` | Show uncommitted changes |
| `/undo` | Discard changes |
| `/tools` | List available tools |
| `/model [name] [effort]` | Pick or set model |
| `/connect [url] [key] [model]` | Add or switch endpoint |
| `/connect list` | Show saved endpoints |
| `/connect remove <name>` | Remove endpoint |
| `/connect key [name]` | Replace stored key |
| `/approve [mode]` | Set approval mode |
| `/cost [--compact] [--json]` | Show token usage |
| `/dashboard` | Show startup card |
| `/compact` | Summarize conversation |
| `/clear` | Clear conversation (keep files) |
| `/reset` | Reset conversation and counters |
| `/save [path]` | Save session |
| `/load <path>` | Load session |
| `/resume [list\|name]` | Resume session |
| `/goal [text]` | Set/show/clear goal |
| `/workflow [name]` | Launch workflow |
| `/skills [attach\|list]` | Manage skills |
| `/paste` | Paste multiline input |
| `/steps [N]` | Set step limit |
| `/verbose` | Toggle verbose output |
| `/exit` | Exit |

### 8.7 Help Text

File references: `@path` or `@pattern` — resolved relative to workspace, content capped, total capped, unknown references reported.

Slash commands listed above. The banner shows model, endpoint, workspace, version-control status, approval mode, tool count, and instruction file information.

---

## 9. Plugin System

The plugin system allows extending MANTRA with custom tools from external directories without modifying the core codebase.

### 9.1 Plugin Directories

Plugins are discovered from:

1. **Environment variable**: `MANTRA_PLUGINS` — colon-separated list of directories (Unix) or semicolon-separated (Windows)
2. **Configuration file**: `"plugins"` key in the config document — list of directory paths or names

```bash
# Unix
export MANTRA_PLUGINS="/path/to/plugins:/another/path"

# Windows
set MANTRA_PLUGINS=C:\path\to\plugins;C:\another\path
```

### 9.2 Plugin Module Format

Each plugin is a Python module or package in a plugin directory. Tools are discovered by:

1. Looking for a `tools` attribute (list or single `Tool` instance)
2. Falling back to scanning for `Tool` subclasses in the module

```python
# my_plugin.py
from mantra.interfaces.tool import Tool

class MyCustomTool(Tool):
    name = "my_tool"
    description = "Does something custom"
    parameters = {"type": "object", "properties": {"input": {"type": "string"}}}

    def execute(self, sandbox, **kwargs):
        return f"Result: {kwargs.get('input', '')}"

# Option A: explicit list
tools = [MyCustomTool()]

# Option B: auto-discover (Tool subclasses)
```

### 9.3 Configuration

```json
{
  "plugins": [
    "/path/to/my/plugins",
    "another-plugin"
  ]
}
```

The `"plugins"` key is:
- Defaulted to `[]` if not present
- Validated to be a list
- Each entry must be a non-empty string

### 9.4 Registration and Deduplication

- Plugin tools are merged with built-in tools
- Duplicate tool names (same name, same implementation) are deduplicated
- Duplicate names with different implementations raise `ConfigError`
- Plugin tools are always loaded (not filtered by `--tool` flags)

---

## 10. Non-OpenAI Clients

MANTRA supports multiple LLM providers via a pluggable client architecture.

### 10.1 Anthropic Client

Speaks the Anthropic Messages API.

**Configuration:**
```json
{
  "llm": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "api_key_env": "ANTHROPIC_API_KEY"
  }
}
```

**Key differences from OpenAI:**
- `max_tokens` is required (default: 262144)
- Tool schemas use `input_schema` instead of `parameters`
- Response content is an array of blocks (`text`, `tool_use`)
- Streaming uses `event: message_delta` and `event: content_block_delta`

**Translation layer:**
- `translate_messages()`: converts OpenAI message format to Anthropic format
- `translate_tools()`: converts OpenAI tool schemas to Anthropic format
- `parse_stream()`: handles Anthropic SSE events

### 10.2 Gemini Client

Speaks the Google Gemini API.

**Configuration:**
```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "api_key_env": "GEMINI_API_KEY"
  }
}
```

**Key differences:**
- Uses `systemInstruction` for system prompt (extracted from first message)
- Tool schemas use `functionDeclarations` with flattened `parameters.properties`
- Response uses `candidates[0].content.parts`
- Supports both `GEMINI_API_KEY` and `GOOGLE_API_KEY`

**Translation layer:**
- `translate_body()`: converts OpenAI messages to Gemini format
- `translate_tools()`: converts OpenAI tool schemas to Gemini format
- `parse_stream()`: handles Gemini SSE events

### 10.3 Provider Registry

```python
LLM_REGISTRY = {
    "openai": OpenAIClient,       # default
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "google": GeminiClient,       # alias for gemini
    "scripted": ScriptedClient,   # testing
}
```

Provider selection is automatic based on the `llm.provider` config key. The registry validates unknown provider names at startup.

---

## 11. Regex Search

The `search_code` tool supports regular expressions via the `regex` parameter.

**Usage:**
```json
{
  "pattern": "\\bdef\\s+\\w+\\(",
  "regex": true,
  "path": "src/",
  "glob": "*.py"
}
```

**Behavior:**
- When `regex=true`: compiles pattern with `re.compile(pattern)`, uses `re.search()` per line, grep fallback uses `-nE`/`-niE` flags
- When `regex=false` (default): uses substring `in` operator for literal matching, grep fallback uses `-nF`/`-niF`

**Internal changes:**
- `_scan_file(full_path, rel_path, query, real_root, pattern=None)` — optional compiled pattern parameter
- Pattern compiled once per search call (not per file)
- Grep flags dynamically switch between `-nF` (fixed string) and `-nE` (extended regex)

---

## 12. Edit Ledger

The edit ledger enforces read-before-edit invariants and detects truncated content.

### 12.1 Read-Before-Edit Enforcement

```python
class EditLedger:
    def remember(self, path: str, content: str, truncated: bool = False) -> None:
        """Record a content hash for the path."""
        self._seen[normalized_path] = content_hash

    def was_read(self, path: str) -> bool:
        """True if the path was previously read or written."""
        return normalized_path in self._seen

    def matches(self, path: str, expected: str) -> bool:
        """True if the current content hash matches the expected hash."""
```

- Uses normalized path keys (same file with different separators is recognized)
- Content hashed for staleness detection

### 12.2 Truncation Detection

```python
TRUNCATION_MARKER = "... [truncated]"

def was_clipped(content: str) -> bool:
    """True if content ends with the truncation marker."""
    return content.rstrip().endswith(TRUNCATION_MARKER)

def was_truncated(self, path: str) -> bool:
    """True if this file was truncated on last read."""
    return normalized_path in self._truncated
```

- `ReadFileTool` records the truncated flag when content exceeds `_MAX_READ_CHARS`
- `EditFileTool` checks `ledger.was_truncated()` and rejects edits to truncated files
- Prevents silent data loss from editing truncated content

---

## 13. Setup

### 13.1 Prerequisites

- Supported Python version (3.10+) as declared in `pyproject.toml`
- Network access to a language-model service (for live operation)
- Container runtime (optional, for container-based sandbox)
- Version-control tooling (for workspace initialization and change inspection)

### 13.2 Installation

```bash
# From project root
pip install -e .

# Or without installation
python src/mantra/console.py --help
python src/mantra/main.py --help
```

No runtime dependencies are required. An optional parsing library is needed only for the alternative configuration format.

### 13.3 Local Development

- Workspace directory holds the repository under test
- If directory lacks VCS initialization, an empty repository is initialized automatically
- Workspace is reused across turns in interactive mode
- Repository-specific instructions discovered by searching workspace root for well-known filenames
- Per-workspace memory stored in `.mantra/` directory with cap

### 13.4 Verification

```bash
# Full offline suite (no network required)
python -m pytest tests/ -q

# Verbose output
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_console_session.py -v
```

The offline suite covers: orchestration loop, context truncation, config validation, read-before-edit contract, memory capping, instruction discovery, streaming parser, model discovery, approval classification, session persistence, and file-tool safety.

---

## 14. Deployment

### 14.1 Build

Declared via `pyproject.toml` with setuptools backend. Package discovery limited to source directory. Entry points:
- `mantra` → `mantra.console:main`
- `mantra-headless` → `mantra.main:main`

### 14.2 Testing

Test configuration in `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

Live probes (`tests/probe_api.py`, `tests/probe-workspace.py`) exercise end-to-end paths with valid credentials and are not required for the offline suite. Container sandbox tests require a container runtime.

### 14.3 Environment Promotion

- Configuration documents alongside example files
- Environment-specific values via configuration (resolved at runtime)
- Relative log paths anchored to project root (same config works from any directory)
- Task documents may override evaluator test command per-task
- User-wide settings, credentials, and transcripts in user home with env-var overrides

### 14.4 Operational Runbooks

**Monitoring**: Tail the append-only JSONL event log; aggregate pass rates, step counts, tool error rates, token usage, and cache hit metrics.

**Backup**: Preserve workspace directory, user-wide settings, credentials store, session transcripts, and workflow definitions. Standard file copy while idle.

**Recovery**:
- Corrupted settings → treated as empty, defaults used, quarantined copy retained
- Corrupted session transcript → skipped, remaining listed
- Corrupted workflow file → returns empty collection
- Sandbox failure → orchestrator ensures evaluation and cleanup

---

## 15. Adoption Report

### 15.1 What Was Adopted

| Feature | Description |
|---------|-------------|
| Read-Before-Edit Ledger | In-memory per-session ledger recording content hashes; rejects stale edits; normalized path keys |
| Known-Failure Registry | Recurring incident classes in system prompt; regression tests lock fixes |
| Durable Workspace Memory | Per-workspace `.mantra/memory.md` with cap, pruning, locking, atomic writes |
| Session Persistence | Auto-saved transcripts with size caps; newest-first listing; corrupted files skipped |
| Approval Policy | Safe/mutating/destructive classification; four modes; per-tool-key persistence |
| Turn-Aware Context | Bounded history; drop oldest complete turn first; avoid orphaned tool results |
| Self-Verification | Orchestrator ensures evaluation always produced |
| Auto-Compaction | Summarizes conversation when token threshold exceeded |

### 15.2 What Was Left Behind

- Host-level orchestration and permission plumbing
- Multi-agent fan-out
- Integrity chains for deployment
- Research-oriented evaluation stacks
- Session-mining families depending on host log formats
- Document-generation skill libraries

These address needs that do not apply to a folder-based harness moved rather than deployed, and their cost exceeds their benefit for the current use case.

### 15.3 Post-Remediation State

Adopted features now include:
- Hardened path confinement at every file and command boundary
- Owner-only permissions for staged container files
- Bounded streaming and memory handling
- Validated configuration limits

The suite of adopted ideas remains small, test-locked, and free of host-specific dependencies.

---

## 16. Safety Notes

- **Host sandbox** runs in the workspace folder; validates inputs, blocks traversal and expansion, enforces path confinement, caps output size
- **Container sandbox** manages a disposable container with limits and owner-only staging
- **Neither is a complete security boundary**
- Credentials resolve from environment then restricted store; only masked forms shown
- File operations check each component of newly created parent chains for symbolic links
- Command execution blocks home-directory, environment-variable expansion, and traversal patterns
- All persistent writes are atomic via temp files with restricted permissions
