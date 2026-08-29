# MANTRA

A modular, standalone harness for LLM coding agents with a Grok-style
terminal interface. MANTRA hands a coding task to an LLM, lets it explore and
edit a repository through tools inside a sandboxed environment, and grades
the result by running a test command.

Zero third-party dependencies: the LLM client uses the standard library (with
SSE streaming), the local sandbox uses `subprocess`, the Docker sandbox
drives the `docker` CLI, and the TUI is plain ANSI. PyYAML is optional (only
for YAML configs).

## Quick start

```
cd MANTRA
python console.py                         Grok-style TUI (recommended)
python console.py --workspace D:\myrepo   operate on an existing repository
python console.py --approve yolo          skip every confirmation
python console.py --model gpt-4o          override the configured model
python console.py --endpoint https://llm.internal/v1
                                          override the endpoint for this run
python console.py --reasoning high        thinking effort for reasoning models
python main.py --config examples/config.json --task examples/task.live.json
```

The API key is read from an environment variable - never store it in files:

```powershell
[Environment]::SetEnvironmentVariable('OPENAI_API_KEY', '<your key>', 'User')
```

## Choosing a model and endpoint

MANTRA speaks the OpenAI chat-completions protocol, so any server that
implements it works. There are no built-in providers to learn: you give
MANTRA a base URL and a key, and it fetches the model list itself.

```
/connect                add or switch endpoint - both open a menu
/model                  pick a model from a menu
/model gpt-4o           skip the menu and name it directly
```

Every command with children opens a menu you drive with the arrow keys,
the mouse, or by typing to filter:

```
  models at https://llm.internal/v1
   › llama-3.1-70b
     qwen2.5-coder-32b
     deepseek-r1-distill          thinks
     + type a model name          not listed above
     up/down or click · enter selects · esc cancels
```

Type to narrow the list (`qwe` jumps to `qwen2.5-coder-32b`), click a row
to take it, or press `Enter` on the highlighted one. `Esc` leaves things
exactly as they were.

The catalogue is cleaned up before it reaches you: entries that cannot
serve a chat - embeddings, whisper, TTS, image generation, realtime audio,
moderation - are left out, and dated snapshots (`gpt-4o-2024-11-20`) sink
below the name you were looking for rather than sorting above it. When the
endpoint serves something that is not listed, `+ type a model name` takes
it without a trip to a config file.

Local endpoints (Ollama, LM Studio) need no key and are not nagged about
one.

### Bring your own key and endpoint

Two things are needed: a base URL and an API key. Give MANTRA those and
it fetches the catalogue itself, so no model name has to be typed from
memory or looked up in a file:

```
/connect
  endpoint url (https://...)> https://llm.internal/v1
  api key for llm (hidden)> ****

  saved 'llm' → https://llm.internal/v1
  key stored (sk-t…1234)

  models at https://llm.internal/v1                  ← menu
   › llama-3.1-70b
     qwen2.5-coder-32b
     deepseek-r1-distill          thinks
     + type a model name          not listed above
```

A second menu asks for the effort to use with that model, right after the
first. That is the whole setup.

`/connect` also accepts both values directly, for scripts:

```
/connect https://llm.internal/v1 sk-...
```

Once an endpoint is saved, its name switches to it - no URL to retype:

```
/connect llm            switch to a saved endpoint
/connect list           show them, and where the file is
/connect remove llm     forget one
/connect key [name]     replace the stored key
/connect keys           list stored keys, masked
```

`/connect key` **always** asks, even when a key is already stored. That
matters because a key mistyped on the way in is otherwise permanent:
discovery comes back 401, `/connect` is re-run, and the prompt is skipped
because something is in the store. If the endpoint refuses a key, the
model picker offers to re-enter it on the spot rather than telling you to
edit a file.

If nothing is configured, `mantra` walks you through this on first launch
instead of dropping you at a prompt that cannot reach anything.

### The settings file

Everything MANTRA knows about your endpoints lives in one file,
`~/.mantra/config.json`. It is plain JSON, written to be read and edited
by hand - `/connect` is a convenience, never a gate:

```json
{
  "active": {
    "endpoint": "llm",
    "model": "llama-3.1-70b",
    "reasoning_effort": null
  },
  "endpoints": {
    "llm": {
      "api_key_env": "LLM_API_KEY",
      "base_url": "https://llm.internal/v1",
      "models": ["llama-3.1-70b", "qwen2.5-coder-32b"],
      "note": "added 2026-08-29"
    }
  },
  "version": 1
}
```

Add an endpoint by writing it in, add a model to `models` that discovery
missed, change `active.model`, delete a stale entry - MANTRA picks the
file up on the next command, and a broken file is treated as empty rather
than crashing startup.

Keys are never written here, only the *name* of the variable that holds
one. Keys live in `~/.mantra/credentials.json` (mode `600` on POSIX) and
are never printed in full - only a masked form like `sk-p…9f2a`.

A key is looked up in this order, first match wins:

1. the environment variable named by `api_key_env`
2. the stored entry in `~/.mantra/credentials.json`

So a variable set for one session overrides a stored key without touching
the file, and `/connect list` shows which of the two is in use for each
endpoint. If you would rather not store a key on disk at all, set the
environment variable instead and skip the prompt - nothing else changes.

**For a server that speaks a different wire format** (not OpenAI
chat-completions), implement `LLMClient` from `interfaces/llm_client.py`
and register it in `LLM_REGISTRY` in `registry.py`; then
`"provider": "myclient"` in config selects it.

### Reasoning effort

Effort belongs to the model, so it is chosen when the model is chosen -
the menu asks for a model and then for an effort, in one flow. That is
why there is no separate `/reasoning` command to keep in step; the alias
still works and lands on the model menu:

```
/model o3-mini          menu asks for an effort, then sets both
/model gpt-5 high       set both at once, no menu
/reasoning high         set it directly
/reasoning off          stop sending the field entirely
```

The effort menu is offered for **every** model, not only for the ones
whose names look like reasoning models. Gating it on the name made the
choice disappear for most catalogues, and the guess was wrong often
enough to be worse than asking - a model that ignores the field is simply
left on `off`.

Levels are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`. The
default is `off`, which sends nothing at all - the right behaviour for
models that do not reason. The menu opens on whichever level is already
in force, so re-picking a model never silently changes it.
`--reasoning high` does the same at launch, and `llm.reasoning_effort` in
config makes it permanent.

Two server quirks are handled automatically, once per session: endpoints
that reject `reasoning_effort` outright (mostly local ones) have it
dropped and the turn continues; endpoints that demand
`max_completion_tokens` instead of `max_tokens` are retried with the
right name. Each downgrade only fires for the field the server actually
complained about, so a complaint about one field never silently disables
another.

You can also edit `examples/config.json` directly; the `llm` block is
`provider`, `model`, `base_url`, `api_key_env`, `temperature`, `max_tokens`.

The default config targets OpenAI. `examples/config.meta.json` keeps the
`api.meta.ai` setup if you have a key for it:

```
python console.py --config examples/config.meta.json
```

If the key variable for the current endpoint is unset, MANTRA says so at
startup instead of failing with an opaque 401 three steps later. Launch
from anywhere with `MANTRA\mantra.cmd`.

## The TUI

`python console.py` opens a Grok-style terminal interface:

- **One frame for the whole session.** Typing `mantra` puts you inside
  it: a header on top, the conversation in the middle, the prompt at the
  bottom. It is the application window, not a box around one reply
- A startup panel inside that frame — model, endpoint, reasoning,
  approvals, workspace, git branch, token usage, tools, recent activity
- `Ctrl+G` (or `/dashboard`) brings that panel back at any time, above a
  prompt that stays live
- Token-level streaming of agent replies as they arrive (SSE) — straight
  into the frame, wrapped on word boundaries as the tokens land
- A spinner while the model works; dim tool-trace lines (`· step 2 write_file`)
- Markdown-lite rendering: headings, bold, code blocks in color
- `--plain` disables styling for redirects and log capture

### The session frame

The frame is opened when MANTRA starts and closed when you leave, so
everything - the startup panel, tool traces, replies, command output and
the prompt itself - happens inside the same border. Turns are separated
by a divider row rather than by closing and reopening the box:

```
╭─ MANTRA · api.openai.com · gpt-4o ───────────────────────────────╮
│           ╔══════════════════════════════════════════╗           │
│           ║  M A N T R A                             ║           │
│           ║  coding agent harness · Spells Matter    ║           │
│           ╚══════════════════════════════════════════╝           │
│                                                                  │
│ mantra> fix the failing test                                     │
│ · step 1 write_file                                              │
│ · step 2 run_command                                             │
│                                                                  │
│ Both are done. I wrote a.py and ran the tests — everything       │
│ passes now, and the failing fixture is gone.                     │
│                                                                  │
│ changed: a.py                                                    │
├─ final · 3 steps · 1.2k in / 340 out ────────────────────────────┤
│                                                                  │
│ mantra> _                                                        │
╰─ bye · 3 turns · 4.1k in / 902 out ──────────────────────────────╯
```

The divider is the answer to "where did that output end and the next turn
begin", and it carries how the turn ended, so the verdict costs no extra
row. The top border names the endpoint and model, so you can always see
what you are aiming at.

Two consequences of a terminal being an append-only surface:

First-run setup runs *before* the shell opens, because setup is what
decides which endpoint and model the header has to name — and a header is
the one row a terminal cannot go back and repaint once it has scrolled
away. Getting it right the first time is the only way to get it right.

The startup panel grows to fit the frame rather than sitting at a fixed
60 columns: on a wide terminal a small panel reads as a stray box
floating in empty space on the left. It stops growing at 100 columns,
past which a panel starts to look like a ruler.

Streaming drives the design. Rows are emitted as they complete, so text
renders inside the frame as it arrives rather than being buffered until
the end — which is why the row still being written has no right border;
padding a row that is still growing would mean redrawing it, and
redrawing is what makes a screen smear. Streamed text is held back by at
most one word, so a line breaks on a real word boundary once that word
has finished arriving.

The input row is the one exception: it is closed the moment you press
Enter, which is the first point at which it is complete. Menus and the
dashboard are drawn as frame rows too, so nothing hangs off the edge.

Colours are never cut in half and never charged against the width: a
styled row is measured in printed columns, not characters.

The frame is skipped where it cannot help — a pipe, a script, `--once`,
or a window narrower than 44 columns. Output there is exactly the plain
text it has always been, so automation is unaffected.

### Completion while you type

The prompt is a real line editor, not `input()`, so suggestions appear the
moment there is something to complete:

| Key | What it does |
|-----|--------------|
| `/` at the start | lists slash commands, narrowing as you type |
| `@` anywhere | lists workspace files and folders |
| Tab | accept the highlighted suggestion; if the popup is closed, reopen it |
| Up / Down | move through suggestions |
| Esc | close the popup for the rest of the line (Tab brings it back) |
| Ctrl+G | show the status panel without leaving the prompt |

Left/Right/Home/End/Delete edit normally. Popup rows are erased on every
repaint, so nothing smears down the screen. When stdin is not a terminal
(pipes, scripts, `--once`), the editor steps aside and plain line input is
used, so automation is unaffected.

### Menus

Any command with children - `/model`, `/connect`, `/approve`, and the
effort prompt that follows a model choice - opens a menu instead of
asking you to remember and type a name:

| Input | What it does |
|-------|--------------|
| Up / Down | move the highlight |
| Type | filter the list (e.g. `qwe` finds `qwen2.5-coder-32b`) |
| Space | page forward through a long list |
| Click | select the row under the pointer |
| Enter | take the highlighted row |
| Esc | cancel, leaving everything untouched |
| Backspace | edit the filter |

Mouse reporting is switched on only while a menu is open, so selecting
and copying text in the scrollback keeps working everywhere else. On a
pipe there is nobody to move a cursor, so menus decline to open rather
than blocking - the typed forms (`/model gpt-4o`, `/approve yolo`) do the
same job.

Type a request, watch tool calls stream, then type follow-ups. The
conversation carries across messages, so "do the same for the tests" works
without re-explaining. The workspace folder persists across messages - point
`--workspace` at any real repo to operate there. If that repo contains an
`AGENTS.md`, `CLAUDE.md`, or `.mantra-instructions.md`, its rules are loaded
into the agent automatically.

Built-in durable knowledge, injected into every session:

- `knowledge/known-failures.md` - failure classes the agent must not repeat
- `<workspace>/.mantra/memory.md` - per-project memory, auto-appended after
  each task, hard-capped so it can never bloat

The system prompt also carries an environment block (OS, shell, Python
version, workspace, git branch and dirty state) so the model stops guessing
whether `ls` or `dir` exists.

### Commands

| Command | What it does |
|---------|--------------|
| `/help` | show the command list |
| `/workspace` | print the workspace path and its contents |
| `/memory` | show the durable memory file for this workspace |
| `/diff` | show uncommitted changes |
| `/undo` | discard uncommitted changes (asks for confirmation) |
| `/tools` | list the tools the agent can call |
| `/connect [url] [key]` | add or switch endpoint; with no arguments, a menu of saved ones |
| `/connect list` | list saved endpoints and the settings file path |
| `/connect remove <name>` | forget an endpoint |
| `/connect key [name]` | replace the stored key; always prompts |
| `/connect keys` | list stored keys, masked |
| `/model [name] [effort]` | menu of the endpoint's models, or set one directly |
| `/approve [mode]` | menu of approval modes, or set one directly |
| `/cost` | token usage for the session |
| `/dashboard` | show the bordered status overlay (also `Ctrl+G`) |
| `/compact` | summarise the conversation to free context |
| `/clear` | drop the conversation, keep the system prompt |
| `/reset` | start a fresh conversation (files stay on disk) |
| `/save [path]` | save the session to JSON |
| `/load <path>` | restore a saved session |
| `/paste` | read a multi-line message until a line with only `.` |
| `/steps [n]` | show or set the per-message step limit |
| `/verbose` | toggle echoing tool output |
| `/exit` | leave the console (also: Ctrl+C) |

`--once "message"` runs one request non-interactively (used by
`tests/probe-console.ps1`). Ctrl+C once stops the current run; twice leaves
the console.

### @ references

Reference files inline and their contents get attached to the message:

```
explain @src/app.py
why is @tests/test_smoke.py failing?
review @src/*.py
what's in @docs?
```

A mention resolves to a file's contents, a directory listing, or every file
a glob matches. Paths are relative to the workspace, cannot escape it, and
each file is capped at 20k characters (60k total per message, older mentions
beyond that are skipped). Unknown references are reported, not silently
dropped. Your wording is preserved — the context is appended after it, so
the model still sees exactly what you typed.

### Approvals

Mutating actions go through a policy in `core/approvals.py`. Reads are always
free; the question is only what needs asking.

| Mode | File writes & edits | Ordinary commands | Destructive commands |
|------|--------------------|-------------------|----------------------|
| `default` | ask | ask | ask |
| `auto` (default) | allow | allow | ask |
| `yolo` | allow | allow | allow |
| `plan` | refuse | refuse | refuse |

Destructive means `rm -rf`, `git reset --hard`, `git push --force`,
`git checkout -- .`, `del /s`, `Remove-Item -Recurse`, piping a download into
a shell, and similar. Answering `a` at a prompt allows that exact action for
the rest of the session. Set the mode in the config or with `--approve`.

Tool safety beyond approvals: `edit_file` refuses to touch files the agent
has not read this session and rejects edits when the file changed since that
read. The sandbox also refuses paths that escape the workspace.

## Headless CLI

```
python main.py --config examples/config.json --task <task.json>
```

Exit code 0 means the evaluator passed. A full JSONL event log is written to
the path configured under `logging`.

### Task format (`task.json`)

```json
{
  "task_id": "example-123",
  "repo_url": "https://github.com/owner/repo.git",
  "base_commit": "a1b2c3d",
  "problem_statement": "Fix the bug in ...",
  "setup_cmd": "pip install -r requirements.txt",
  "test_cmd": "python -m pytest tests/ -q"
}
```

Only `problem_statement` is required; everything else is optional per task.
A task-level `test_cmd` overrides the config's evaluator command.

### Config sections (`examples/config.json`)

| Section    | Keys | Notes |
|------------|------|-------|
| `llm`      | `provider` (`openai` \| `scripted`), `model`, `base_url`, `api_key_env`, `temperature`, `max_tokens`, `reasoning_effort`, `stream` | Key comes from the env var named by `api_key_env`, else `~/.mantra/credentials.json` |
| `approvals`| `default` \| `auto` \| `yolo` \| `plan` | Read-only tools never prompt; `yolo` prompts for nothing |
| `sandbox`  | `provider` (`local` \| `docker`) plus image/mem_limit for docker | `docker` needs the docker CLI and daemon |
| `tools`    | List of tool names from the registry | Unknown names fail loudly at startup |
| `evaluator`| `type: command`, `test_cmd`, `timeout` | Per-task `test_cmd` overrides this |
| `logging`  | `type: jsonl`, `path` | Structured events for every step |
| `approvals`| `default` \| `auto` \| `yolo` \| `plan` | Interactive only; see the table above |
| `context`  | `max_messages`, `max_chars` | History budget before old turns are dropped |
| top level  | `max_steps`, `system_prompt`, `auto_compact_tokens`, `verbose` | Loop bounds and session behaviour |

`auto_compact_tokens` summarises the conversation once it passes that many
estimated tokens (0 disables it). The headless CLI ignores the interactive
keys.

The `scripted` LLM provider exists for offline testing only; it replays a
fixed response list and cannot be driven from config files.

## Architecture

```
MANTRA/
+-- core/                  Orchestrator (depends only on interfaces)
|   +-- agent_loop.py      The run loop: setup -> chat/tool loop -> evaluate -> cleanup
|   +-- context.py         Bounded conversation history (turn-aware truncation)
|   +-- knowledge.py       System-prompt assembly: registry, memory, repo instructions
|   +-- approvals.py       What needs asking before it runs (default/auto/yolo/plan)
|   +-- events.py          Event bus for hooks/observers
|   +-- exceptions.py      Error hierarchy
+-- interfaces/            Abstract contracts (the only thing core imports)
+-- implementations/       Swappable concrete components
|   +-- llm/               openai_client.py (streaming, any /v1/chat/completions server)
|   +-- sandbox/           local_sandbox.py (host dir), docker_sandbox.py (container)
|   +-- tools/             file ops (ledger-guarded), run_command, search, git helpers
|   +-- evaluators/        command_evaluator.py (pass = test_cmd exit 0), null_evaluator
|   +-- loggers/           jsonl_logger.py (append-only event log)
+-- knowledge/             known-failures.md registry (injected into prompts)
+-- console.py             Grok-style ANSI TUI (streaming, spinner, markdown-lite)
+-- registry.py            Name -> class registries; builds components from config
+-- config.py              JSON/YAML config with defaults merged in
+-- main.py                Headless CLI entrypoint
+-- examples/              Sample config and task files
+-- tests/                 Offline suite + live probes
+-- docs/ADOPTION.md       What was adopted from the HARNESSY workflow layer, and why
```

Every pluggable role is an abstract interface in `interfaces/`. To add a
component, implement the interface, register it in `registry.py`, and name it
in your config. The core never changes.

## Safety notes

The approval policy and the read-before-edit ledger reduce accidents; they
are convenience layers, not security boundaries. A model that is allowed to
run commands can always find a way to do something you did not intend -
`yolo` mode especially. Use `/diff` before `/undo`, and keep real work in a
repository with a clean baseline.

The `local` sandbox runs commands directly on the host with no isolation:
use it only for trusted development and testing. For untrusted tasks use
`"sandbox": {"provider": "docker"}`, which runs each task in a disposable
container with CPU/memory limits. Neither mode is a complete security
boundary; treat both as convenience layers.

## Tests

```
cd MANTRA
python -m unittest discover -s tests -v     # offline suite
python tests/probe-console.ps1              # live end-to-end probe
```

The offline suite covers the pass path, failure path, unknown-tool
resilience, max-step termination, context truncation, config validation,
the edit ledger contract, memory capping, workspace instruction loading, and
the SSE stream parser.

`tests/test_console_session.py` covers the interactive layer: workspace
persistence across turns, conversation continuity between messages,
turn-aware truncation never orphaning a tool result, approval classification
and the four modes, abort handling, denied tool calls, and the REPL
forwarding ordinary text to the agent. Live paths are exercised manually or
via probe scripts.
