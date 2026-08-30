"""MANTRA TUI - a Grok-style terminal interface for the coding agent.

Pure standard library: ANSI escape codes (enabled for Windows consoles via
the VT workaround), a braille spinner while the model works, dim tool-trace
lines, markdown-lite rendering of replies, token-level streaming, and a
bordered container that each turn is written into (see
``container.py``).

Run:
    python console.py                          examples/config.json + ./workspace
    python console.py --workspace D:\\myrepo    operate on any repository
    python console.py --once "do something"    single message, then exit
    python console.py --plain                  no ANSI styling (redirects, logs)
    python console.py --approve yolo           skip every confirmation

Commands:
  /help /workspace /memory /diff /undo /tools /model /connect /approve
  /cost /compact /clear /reset /save /load /resume /goal /skills /workflow /paste /steps /verbose /exit
Anything else you type is sent to the agent as a task.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import urlparse

from mantra.config import REASONING_EFFORTS, load_config
from mantra.core.agent_loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, RunResult
from mantra.core.approvals import MODES, ApprovalPolicy
from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.exceptions import AbortError, HarnessError
from mantra.core.keys import has_stored, mask, store as store_key, stored_keys
from mantra.core.menu import Option, choose, raw_mode
from mantra.core.models import fetch_models, is_reasoning_model
import mantra.core.sessions as sessions
import mantra.core.skills as skills
import mantra.core.workflows as workflows
from mantra.core.settings import (
    active as get_active,
    add_endpoint,
    endpoint_name_for_url,
    endpoints as known_endpoints,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    set_skills_prefs,
    settings_path,
    skills_prefs,
    validate_endpoint,
)
from mantra.core.knowledge import (
    append_memory,
    assemble_system_prompt,
    find_instructions_file,
    render_environment,
)
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.line_editor import Completion, LineEditor
import mantra.compact as compact
from mantra.registry import build_llm, build_tools

# Local helpers (container/dashboard removed — compact is sole TUI)
_WRITE_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")
def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KNOWN_FAILURES_PATH = os.path.join(PROJECT_ROOT, "knowledge", "known-failures.md")

HELP_TEXT = """Commands:
  /connect              add or switch endpoint. Give a URL and a key, then
                        pick from the models it serves. Nothing else to
                        configure. /connect list, /connect remove <name>
  /connect key [name]   replace the stored key. Always asks, so a key that
                        was mistyped can be put right
  /model                pick a model from a menu, then an effort for it:
                        off|minimal|low|medium|high|xhigh
  /help                 show this help
  /workspace            print the workspace path and its contents
  /memory               show the durable memory file for this workspace
  /diff                 show uncommitted changes in the workspace
  /undo                 discard uncommitted changes (asks for confirmation)
  /tools                list the tools this agent can call
  /approve              pick an approval mode: default|auto|yolo|plan
  /cost                 token usage for this session
  /dashboard            show the status overlay (Ctrl+G)
  /compact              summarise the conversation to free context
  /clear                drop the conversation, keep the system prompt
  /reset                start a fresh conversation (files stay on disk)
  /save [path]          save the session to JSON
  /load <path>          restore a saved session
  /resume [name]        pick up a saved session. No name opens a menu;
                        /resume list shows them all. Sessions are saved
                        automatically as you work, so this works even
                        after closing the window
  /goal <text>          set the standing goal for this session. It is
                        sent with every turn, so the agent keeps aiming
                        at it. /goal note <text>, /goal done
  /skills [name]        skills found in your skills directories.
                        /skills find <text> · /skills use <name> ·
                        /skills bundles · /skills launch <bundle> ·
                        /skills auto [on|off] attaches a skill that
                        fits what you typed, for that turn only
  /workflow             run a saved sequence of steps.
                        /workflow create <name> (one step per line, . to
                        finish) · /workflow show [name] ·
                        /workflow launch <name> · /workflow remove <name>
  /paste                read a multi-line message until a line with only "."
  /steps [n]            show or set the per-message step limit
  /verbose              toggle echoing tool output
  /exit                 leave the console (also: Ctrl+C)
  /                     same as /help

Reference files with @ in any message:
  explain @src/app.py
  why is @tests/test_smoke.py failing?
  review @src/*.py
  what's in @docs/

@path attaches a file's contents, a directory listing, or every file a glob
matches. Paths are relative to the workspace and cannot escape it.

Anything else you type is sent to the agent as a task.
Ctrl+C once stops the current run; twice leaves the console."""


# ---------------------------------------------------------------- ANSI styling

class Style:
    """ANSI wrappers; disabled entirely under --plain."""

    def __init__(self, enabled: bool = True) -> None:
        if enabled and os.name == "nt":
            os.system("")  # enable VT processing on Windows consoles
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    # Basic colors
    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def red(self, t): return self._wrap("31", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def blue(self, t): return self._wrap("34", t)
    def magenta(self, t): return self._wrap("35", t)
    def cyan(self, t): return self._wrap("36", t)
    # Bright neon variants (80s theme)
    def bright_red(self, t): return self._wrap("91", t)
    def bright_green(self, t): return self._wrap("92", t)
    def bright_yellow(self, t): return self._wrap("93", t)
    def bright_blue(self, t): return self._wrap("94", t)
    def bright_magenta(self, t): return self._wrap("95", t)
    def bright_cyan(self, t): return self._wrap("96", t)
    def bright_white(self, t): return self._wrap("97", t)
    # Neon combos
    def neon_title(self, t): return self._wrap("1;95", t)  # bold magenta
    def neon_label(self, t): return self._wrap("93", t)    # bright yellow
    def neon_value(self, t): return self._wrap("97", t)    # bright white
    def neon_accent(self, t): return self._wrap("92", t)   # bright green
    def neon_border(self, t): return self._wrap("96", t)   # bright cyan
    # Gothic palette
    def grey(self, t): return self._wrap("37", t)         # grey
    def strike(self, t): return self._wrap("9", t)        # strikethrough
    # Background helpers
    def bg_grey(self, t): return self._wrap("47", t)      # light grey background
    def on_grey(self, t): return self._wrap("97;100", t)   # white on dark grey
    def on_grey_light(self, t): return self._wrap("97;47", t)  # white on light grey




# ANSI escape sequence pattern for sanitisation.
import re as _re_mod
_ANSI_SANITIZE_RE = _re_mod.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _sanitize_output(text: str) -> str:
    """Strip raw ANSI escape sequences from model output.

    Prevents cursor movement, color changes, or other terminal
    manipulation from untrusted model-generated text.
    """
    return _ANSI_SANITIZE_RE.sub("", text)


class StreamingRenderer:
    """Apply inline markdown formatting to streamed text fragments.

    Buffers incoming text until a newline arrives, then processes the
    complete line through the full markdown pipeline.  Code fences are
    tracked across pieces so content inside them stays literal.
    """

    def __init__(self, style: Style) -> None:
        self.style = style
        self._in_code_fence = False
        self._buf: str = ""

    def reset(self) -> None:
        """Reset state for a new response."""
        self._in_code_fence = False
        self._buf = ""

    def render_piece(self, piece: str) -> str:
        """Render a text fragment with inline markdown."""
        self._buf += _sanitize_output(piece)
        out = ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            out += _render_md_line(line, self.style, self) + "\n"
        return out

    def flush(self) -> str:
        """Flush any remaining buffered text (called at end of stream)."""
        if self._buf:
            leftover = self._buf
            self._buf = ""
            return _render_md_line(leftover, self.style, self)
        return ""


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    """Braille spinner on its own thread; pauses cleanly for real output."""

    def __init__(self, style: Style, label: str = "Chanting", frame: Any = None, layout: Any = None) -> None:
        self.style = style
        self.label = label
        # Optional callable that dresses the line in the container's
        # sides, so a spinner inside a frame stays inside the frame.
        self._frame = frame
        # Optional TerminalLayout for bottom-fixed prompt mode.
        self._layout = layout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._paused = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self.started_at = time.monotonic()

    def _render(self, frame_char: str) -> str:
        elapsed = int(time.monotonic() - self.started_at)
        if elapsed < 60:
            elapsed_str = f"{elapsed}s"
        else:
            elapsed_str = f"{elapsed // 60}m{elapsed % 60:02d}s"
        pulse = int(time.monotonic() * 4) % 6
        if pulse < 3:
            label_styled = self.style.dim(self.label)
        else:
            label_styled = self.style.grey(self.label)
        body = f"{self.style.cyan(frame_char)} {label_styled} {self.style.bright_yellow(elapsed_str)}"
        if self._layout is not None and self._layout.active:
            return body
        return "\r" + (self._frame(body) if self._frame else body + " ")

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            with self._lock:
                if not self._paused:
                    with _WRITE_LOCK:
                        if self._layout is not None and self._layout.active:
                            self._layout.draw_border_status(self._render(SPINNER_FRAMES[i % len(SPINNER_FRAMES)]))
                        else:
                            sys.stdout.write(self._render(SPINNER_FRAMES[i % len(SPINNER_FRAMES)]))
                            sys.stdout.flush()
            i += 1

    def start(self):
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def stop(self, clear: bool = True):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        if clear:
            self.clear_line()

    def clear_line(self):
        with self._lock:
            with _WRITE_LOCK:
                if self._layout is not None and self._layout.active:
                    self._layout.draw_border_status("")
                else:
                    sys.stdout.write("\r\033[K")
                sys.stdout.flush()

    @contextmanager
    def paused(self):
        """Suppress all spinner drawing while real output prints."""
        with self._lock:
            was = self._paused
            self._paused = True
            with _WRITE_LOCK:
                if self._layout is not None and self._layout.active:
                    self._layout.draw_border_status("")
                else:
                    sys.stdout.write("\r\033[K")
                sys.stdout.flush()
        try:
            yield
        finally:
            with self._lock:
                self._paused = was


# ------------------------------------------------------------- markdown-lite

def _render_md_line(line: str, style: Style, ctx: Any = None) -> str:
    """Render a single markdown line to ANSI-styled text.

    *ctx* is an optional StreamingRenderer used only for code-fence
    state tracking during streaming.
    """
    in_fence = getattr(ctx, "_in_code_fence", False)
    stripped = line.strip()

    # ── code fences ──────────────────────────────────────────────
    if in_fence:
        if stripped.startswith("```"):
            if ctx is not None:
                ctx._in_code_fence = False
            return style.dim(chr(0x2504) * 4)
        return style.grey(line)
    if stripped.startswith("```"):
        if ctx is not None:
            ctx._in_code_fence = True
        return style.grey(chr(0x2504) * 4)

    # ── headings ─────────────────────────────────────────────────
    if stripped.startswith("#"):
        level = len(stripped) - len(stripped.lstrip("#"))
        heading = stripped.lstrip("# ").rstrip()
        if level == 1:
            return style.bold(style.bright_white(heading)) + chr(10) + style.grey(chr(0x2500) * 40)
        if level == 2:
            return style.bold(heading) + chr(10) + style.grey(chr(0x2500) * 40)
        return style.bold(heading)

    # ── horizontal rule ──────────────────────────────────────────
    if stripped in ("---", "***", "___") and len(stripped) >= 3:
        return style.grey(chr(0x2500) * 40)

    # ── blockquote ───────────────────────────────────────────────
    if stripped.startswith(">"):
        quote = stripped[1:].lstrip()
        return style.grey("│ ") + style.dim(quote)

    # ── unordered list ───────────────────────────────────────────
    m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
    if m_list:
        indent, rest = m_list.group(1), m_list.group(2)
        return indent + style.grey("• ") + _inline_md(rest, style)

    # ── ordered list ─────────────────────────────────────────────
    m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
    if m_ord:
        indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
        return indent + style.bright_cyan(num + ". ") + _inline_md(rest, style)

    # ── normal paragraph ─────────────────────────────────────────
    return _inline_md(line, style)


def render_markdown(text: str, style: Style) -> str:
    """Full markdown to ANSI: neon theme.

    Headings, code fences, lists, blockquotes, horizontal rules,
    inline code, bold, italic, and links.
    """
    out_lines = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        # Code fence toggle.
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(style.grey(chr(0x2504) * 4) if in_fence else style.dim(chr(0x2504) * 4))
            continue
        if in_fence:
            out_lines.append(style.grey(line))
            continue
        # Headings.
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("# ").rstrip()
            if level == 1:
                out_lines.append(style.bold(style.bright_white(heading)))
            else:
                out_lines.append(style.bold(heading))
            if level <= 2:
                out_lines.append(style.grey(chr(0x2500) * 40))
            continue
        # Horizontal rule.
        if stripped in ("---", "***", "___") and len(stripped) >= 3:
            out_lines.append(style.grey(chr(0x2500) * 40))
            continue
        # Blockquote.
        if stripped.startswith(">"):
            quote = stripped[1:].lstrip()
            out_lines.append(style.grey("│ ") + style.dim(quote))
            continue
        # Unordered list.
        m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if m_list:
            indent, rest = m_list.group(1), m_list.group(2)
            out_lines.append(indent + style.grey("• ") + _inline_md(rest, style))
            continue
        # Ordered list.
        m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m_ord:
            indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
            out_lines.append(indent + style.bright_cyan(num + ". ") + _inline_md(rest, style))
            continue
        # Normal paragraph.
        out_lines.append(_inline_md(line, style))
    return "\n".join(out_lines)


def _inline_md(line: str, style: Style) -> str:
    """Inline markdown: code, bold, italic, strikethrough, links."""
    import re as _re
    # Order matters: process code spans first so their contents stay literal.
    parts = line.split("`")
    for i in range(0, len(parts)):
        if i % 2 == 1:
            parts[i] = style.grey(parts[i])
        else:
            segment = parts[i]
            # Links: [text](url) -> styled text
            segment = _re.sub(
                r'\[([^\]]+)\]\([^)]+\)',
                lambda m: style.bright_cyan(m.group(1)),
                segment,
            )
            # Bold: **text**
            segment = _re.sub(r'\*\*(.+?)\*\*', lambda m: style.bright_white(m.group(1)), segment)
            # Italic: *text*
            segment = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: style.bright_magenta(m.group(1)), segment)
            # Strikethrough: ~~text~~
            segment = _re.sub(r'~~(.+?)~~', lambda m: style.strike(m.group(1)), segment)
            parts[i] = segment
    return "`".join(parts)


# @name mentions: a bare word, path, or glob. The lookbehind keeps
# "user@example.com" and "a@b" from being read as file references.
MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_][\w.:/\\\-*]*)")
MAX_ATTACH_CHARS = 20_000
MAX_TOTAL_ATTACH_CHARS = 60_000
MAX_GLOB_HITS = 20
MAX_LISTING_ENTRIES = 100


def _short(count: int) -> str:
    """1234 -> 1.2k. Token counts only ever need two significant figures."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time for display.

    < 60s: '1.6s'
    >= 60s: '1m23s'
    >= 3600s: '1h05m'
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"done in {m}m{s:02d}s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"done in {h}h{m:02d}m"


def _short_endpoint(base_url: str) -> str:
    """Host and path, without the scheme - what a header has room for.

    ``https://api.openai.com/v1`` is too long to sit next to a model
    name, and the ``https://`` is the part that carries no information:
    every endpoint has one.
    """
    url = (base_url or "").strip()
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix) :]
            break
    return url.rstrip("/").removesuffix("/v1")


def _transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten history to plain text for the summariser."""
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            names = ", ".join(
                (call.get("function") or {}).get("name", "?")
                for call in message["tool_calls"]
            )
            lines.append(f"assistant: [called {names}]")
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            body = content if len(content) <= 300 else content[:300] + " ..."
            lines.append(f"result of {message.get('name', 'tool')}: {body}")
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ------------------------------------------------------------------- session

class ConsoleSession:
    """One REPL session over one persistent local workspace, Grok-style."""

    def __init__(
        self,
        config: dict,
        workspace: str,
        style: Style,
        llm: Any = None,
        ask: Any = None,
    ) -> None:
        self.config = config
        self.style = style
        self.workspace = workspace
        self.sandbox = LocalSandbox(workspace)
        self.sandbox.setup({})
        self._isolate_git(workspace)
        self.memory_path = os.path.join(workspace, ".mantra", "memory.md")
        self.instructions_path = find_instructions_file(workspace)

        ctx_cfg = config.get("context") or {}
        self.context = ContextManager(
            max_messages=int(ctx_cfg.get("max_messages", 200)),
            max_chars=int(ctx_cfg.get("max_chars", 240_000)),
        )
        self.system_prompt = assemble_system_prompt(
            config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            known_failures_path=KNOWN_FAILURES_PATH,
            memory_path=self.memory_path,
            instructions_path=self.instructions_path,
            environment=render_environment(workspace),
        )
        self.tools = build_tools(config["tools"])
        self.llm = llm if llm is not None else build_llm(config["llm"])
        self.approvals = ApprovalPolicy(
            mode=config.get("approvals", "auto"),
            ask=ask or self._ask,
            note=self._note,
        )
        self.totals = {"tokens_in": 0, "tokens_out": 0, "turns": 0, "tool_errors": 0, "cache_hit": 0}
        self.reported_changes: set[str] = set()
        # Model ids discovered from the endpoint, so `/model <tab>` can
        # complete from what is actually served rather than a guess.
        self.known_models: list[str] = []
        # Last few tool calls, newest last, for the /dashboard overlay.
        self.recent_tools: list[str] = []
        # Per-turn cache metrics for trend analysis.
        self.turn_history: list[dict] = []  # [{turn, tokens_in, tokens_out, cache_hit, cache_rate}]

        log_path = config["logging"].get("path", "logs/mantra-console.jsonl")
        if not os.path.isabs(log_path):
            log_path = os.path.join(PROJECT_ROOT, log_path)
        self.logger = JsonlLogger(log_path)
        self.bus = EventBus()
        self.bus.subscribe(self._on_event)

        self.message_count = 0
        self.verbose = bool(config.get("verbose", False))
        self.max_steps = int(config.get("max_steps", 30))
        # Set the first time autosave writes, so a session that never got
        # anywhere leaves no file behind. Adopted by /resume so picking a
        # session up continues it instead of forking it.
        self.session_name = ""
        # The standing objective, if the operator set one. Injected into
        # every turn's system prompt, so an agent working across many
        # turns keeps aiming at the same thing instead of drifting to
        # whatever the last message asked for.
        self.goal = ""
        # Free-form notes the operator attached to the goal with
        # /goal note <text>: constraints found along the way, decisions
        # made. Shown with the goal so they are not re-litigated.
        self.goal_notes: list[str] = []
        # Skills attached with /skills use <name>. Their procedures ride
        # along in the system prompt so the agent follows them rather
        # than improvising, which is the whole point of a skill existing.
        self.active_skills: list[str] = []
        # Skills the router attached on its own for the turn in flight.
        # Detached the moment that turn ends, because routing reads one
        # request - leaving its guess attached would mean every later
        # turn inherits a procedure nobody asked for, and would stop the
        # router ever looking again.
        self.auto_attached: list[str] = []
        # True while a bundle is running its steps. A bundle step is a
        # turn like any other, but it is one the router must keep its
        # hands off: the step already knows which skill it wants.
        self.in_bundle = False

        self._spinner: Spinner | None = None
        self._streamed_this_run = False
        self._stream_header_done = False
        self._turn_started: float = 0.0
        # Live token counter: approximate tokens received during streaming.
        self._stream_tokens: int = 0
        self._last_counter_update: float = 0.0
        # Timestamp of when the last prompt was sent (Enter pressed).
        self._prompt_sent_at: float = 0.0
        # Streaming markdown renderer for inline formatting during token streaming.
        self._stream_renderer = StreamingRenderer(self.style)
        # Compact layout only — no Frame, no dashboard
        self.frame = None  # kept for compat, always None in compact
        self.layout: compact.CompactLayout | None = None
        self._compact = True  # compact is now sole TUI
        self._splash_visible = True
        self._abort = threading.Event()
        self._prev_sigint = None

    # ---- frame compat (no-ops — compact has no Frame) --------------

    def open_frame(self) -> bool:
        return False

    def close_frame(self, label: str = "") -> None:
        pass

    def _sign_off(self) -> str:
        totals = self.totals
        if not totals["turns"]:
            return "bye"
        return (
            f"bye · {totals['turns']} turns · "
            f"{_short(totals['tokens_in'])} in / {_short(totals['tokens_out'])} out"
        )

    def prompt_text(self, body: str = "") -> str:
        """Gold prompt — fixed at bottom when layout active."""
        if not body:
            body = self.style.bright_yellow("\u2502 MANTRA > ") if self.style.enabled else "\u2502 MANTRA > "
        if self.layout is not None and self.layout.active:
            return self.layout.prompt_text(body)
        return body

    def close_prompt(self, column: int) -> None:
        pass

    def _frame_title(self) -> str:
        return ""
    def refresh_title(self) -> None:
        return

    def _isolate_git(self, workspace: str) -> None:
        """Give the workspace its own git repo if it does not have one."""
        if os.path.isdir(os.path.join(workspace, ".git")):
            return
        try:
            subprocess.run(
                ["git", "init"], cwd=workspace,
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- output helpers (spinner-aware) ---------------------------------

    def _print(self, text: str = "") -> None:
        if self.layout is not None and self.layout.active:
            with self._pause_spinner():
                self.layout.write(text + "\n")
            return
        if self._spinner:
            with self._spinner.paused():
                try:
                    sys.stdout.write(text + "\n")
                except UnicodeEncodeError:
                    sys.stdout.write((text + "\n").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
                sys.stdout.flush()
        else:
            try:
                sys.stdout.write(text + "\n")
            except UnicodeEncodeError:
                sys.stdout.write((text + "\n").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
            sys.stdout.flush()

    @contextmanager
    def _pause_spinner(self):
        """Hold the spinner while the frame draws, if there is one."""
        if self._spinner:
            with self._spinner.paused():
                yield
        else:
            yield

    def _format_diff(self, diff_text: str, max_lines: int = 60) -> str:
        """Colour a unified diff for terminal display."""
        if not diff_text:
            return ""
        lines = diff_text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append(self.style.dim(f"... ({len(diff_text.splitlines()) - max_lines} more lines)"))
        out = []
        for line in lines:
            if line.startswith("+"):
                out.append(self.style.green(line))
            elif line.startswith("-"):
                out.append(self.style.red(line))
            elif line.startswith("@@"):
                out.append(self.style.dim(line))
            else:
                out.append(self.style.dim(line))
        return "\n".join(out)

    def _note(self, text: str) -> None:
        self._print(f"  {self.style.dim(text)}")

    def fresh_line(self) -> None:
        """End the row the operator is on and start a clean one.

        The prompt and the secret reader own their own line, so a frame
        row is left open underneath the caret when they finish. Anything
        the app draws next - the dashboard, a menu, a new reply - has to
        start below that line, not beside it. Starting on the open row
        would glue a ``│`` onto the prompt and push every later row a
        column past the border.
        """
        out = sys.stdout
        out.write("\n")
        out.flush()


    def _on_event(self, name: str, payload: dict) -> None:
        if name == "tool_call":
            step = payload.get("step")
            tool = payload.get("tool")
            args = payload.get("args") or {}
            # Show file path for file-operation tools so the operator can
            # follow along without waiting for the full reply.
            detail = ""
            if tool in ("write_file", "edit_file", "read_file", "list_dir"):
                path = args.get("path") or args.get("directory") or ""
                if path:
                    detail = f" {self.style.dim(path)}"
            elif tool == "run_command":
                cmd = args.get("command") or ""
                if cmd:
                    # Truncate long commands.
                    detail = f" {self.style.dim(cmd[:60])}{'…' if len(cmd) > 60 else ''}"
            self._print(f"  {self.style.dim(f'· step {step}')} {self.style.yellow(tool)}{detail}")
        elif name == "tool_denied":
            self._print(f"  {self.style.red('✗ denied')} {self.style.dim(payload.get('tool',''))}")
        elif name == "run_error":
            self._print(f"  {self.style.red('!! ' + str(payload.get('error')))}")
        elif name == "tool_result":
            tool = payload.get("tool")
            result = payload.get("result")
            ok = payload.get("ok")
            seconds = payload.get("seconds")
            # Show result for file-edit tools
            if tool in ("edit_file", "write_file"):
                if isinstance(result, str) and result.strip():
                    # Show coloured diff
                    self._print(self._format_diff(result.strip()))
                elif self.verbose:
                    detail = self.style.dim("ok" if ok else "failed")
                    self._print(f"    {detail} {seconds}s")
            elif self.verbose:
                detail = self.style.dim("ok" if ok else "failed")
                self._print(f"    {detail} {seconds}s")

    def _on_delta(self, piece: str) -> None:
        """Streamed content fragment from the LLM client."""
        if self._abort.is_set():
            raise AbortError("interrupted by operator")
        if self._spinner:
            # Streamed tokens accumulate on ONE terminal line, so no spinner
            # tick can ever be safe after the first fragment - a frame drawn
            # between fragments overwrites the line start and the next
            # fragment's clear erases everything before it. Kill the spinner
            # for the rest of the run on the first token.
            self._spinner.stop(clear=True)
            self._spinner = None
        # Approximate token count: ~4 chars per token.
        self._stream_tokens += max(1, len(piece) // 4)
        rendered = self._stream_renderer.render_piece(piece)
        if self.layout is not None and self.layout.active:
            if not self._stream_header_done:
                self.layout.write(f"{self.style.bold('agent')} ")
                self._stream_header_done = True
            self.layout.write(rendered)
            self._update_live_counter()
        else:
            if not self._stream_header_done:
                sys.stdout.write(f"{self.style.bold('agent')} ")
                self._stream_header_done = True
            sys.stdout.write(rendered)
            sys.stdout.flush()
        self._streamed_this_run = True

    def _update_live_counter(self) -> None:
        """Update the live token counter in the bottom prompt row."""
        if self.layout is None or not self.layout.active:
            return
        # Throttle: only update every 100ms to avoid flicker.
        now = time.monotonic()
        if now - self._last_counter_update < 0.1:
            return
        self._last_counter_update = now
        # Use short form (1.2k) and caps for visual consistency with top bar; prompt stays gold, counter is bright cyan for visibility
        tok_str = _short(self._stream_tokens)
        counter = self.style.bright_cyan(f" {tok_str} TOK ↓ ")
        if self.style.enabled:
            body = self.style.bright_yellow("\u2502 MANTRA >") + counter
        else:
            body = "\u2502 MANTRA >" + counter
        self.layout.draw_prompt(body=body)

    # ---- approvals -------------------------------------------------------

    def _ask(self, prompt: str) -> str:
        """Terminal prompt for the approval policy. Returns y / n / a."""
        # Retire the spinner for the rest of the run: it would otherwise keep
        # redrawing its frame on top of the prompt while we wait for an answer.
        if self._spinner:
            self._spinner.stop(clear=True)
            self._spinner = None
        self._print("")
        self._print(f"  {self.style.yellow('allow?')} {prompt}")
        self._print(self.style.dim("  [y]es   [n]o   [a]lways for this session"))
        try:
            if self.frame is not None:
                # Leave the caret on an open row so the answer is typed
                # inside the frame rather than beside it.
                self.frame.prompt("  allow> ")
                answer = input().strip().lower()
            else:
                answer = input("  allow> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "n"
        finally:
            if self.frame is not None:
                # input() consumed the newline, so the row it was typed on
                # is already gone and cannot be closed - only forgotten.
                self.frame.abandon_row()
        if answer in ("a", "always"):
            return "a"
        if answer in ("y", "yes"):
            return "y"
        return "n"

    # ---- @ mentions -------------------------------------------------------

    def expand_mentions(self, text: str) -> tuple[str, list[str]]:
        """Turn ``@path`` tokens into real context the model can see.

        Keeps the operator's wording intact and appends an "Attached
        context" block, which is what every mainstream agent CLI does and
        what the model already understands. Unknown references are left
        alone and reported rather than silently dropped.
        """
        tokens = MENTION_RE.findall(text)
        if not tokens:
            return text, []

        # Normalize: a root given with forward slashes compares unequal to
        # normpath output on Windows, which made every mention "no match".
        root = os.path.abspath(self.sandbox.root)
        blocks: list[str] = []
        attached: list[str] = []
        total = 0
        seen: set[str] = set()

        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            paths = self._resolve_mention(token, root)
            if not paths:
                self._note(f"no match for @{token}")
                continue
            for rel in paths:
                if total >= MAX_TOTAL_ATTACH_CHARS:
                    self._note("attachment budget reached - remaining mentions skipped")
                    break
                full = os.path.join(root, rel)
                block = (
                    self._render_listing(rel, full)
                    if os.path.isdir(full)
                    else self._render_file(rel, full)
                )
                if not block:
                    continue
                blocks.append(block)
                attached.append(rel)
                total += len(block)

        if not blocks:
            return text, []
        return text + "\n\nAttached context:\n\n" + "\n\n".join(blocks), attached

    def _resolve_mention(self, token: str, root: str) -> list[str]:
        """Resolve one mention to workspace-relative paths. Escapes refused."""
        root = os.path.realpath(os.path.abspath(root))
        candidate = token.replace("/", os.sep).replace("\\", os.sep)
        if "*" in token:
            hits = sorted(glob.glob(candidate, root_dir=root, recursive=True))
            valid: list[str] = []
            for hit in hits:
                full_hit = os.path.realpath(os.path.join(root, hit))
                if not (full_hit == root or full_hit.startswith(root + os.sep)):
                    continue
                if os.path.isfile(os.path.join(root, hit)):
                    valid.append(hit)
                if len(valid) >= MAX_GLOB_HITS:
                    break
            return valid
        full = os.path.realpath(os.path.join(root, candidate))
        # Never read outside the workspace, however the path was written.
        if full != root and not full.startswith(root + os.sep):
            return []
        return [os.path.relpath(full, root)] if os.path.exists(full) else []

    @staticmethod
    def _render_file(rel: str, full: str) -> str:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(MAX_ATTACH_CHARS + 1)
        except OSError:
            return ""
        truncated = len(content) > MAX_ATTACH_CHARS
        body = content[:MAX_ATTACH_CHARS].rstrip()
        if truncated:
            body += "\n... [truncated]"
        return f"--- @{rel} ---\n{body}"

    @staticmethod
    def _render_listing(rel: str, full: str) -> str:
        try:
            entries = sorted(os.listdir(full))[:MAX_LISTING_ENTRIES]
        except OSError:
            return ""
        lines = [f"--- @{rel} (directory, {len(entries)} entries) ---"]
        for entry in entries:
            kind = "dir " if os.path.isdir(os.path.join(full, entry)) else "file"
            lines.append(f"{kind} {entry}")
        return "\n".join(lines)

    # ---- interrupt handling ----------------------------------------------

    def _install_sigint(self) -> None:
        def handler(signum, frame):
            if self._abort.is_set():
                raise KeyboardInterrupt
            self._abort.set()
            self._print(self.style.dim("  (stopping after this step - ctrl+c again to quit)"))

        self._prev_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            self._prev_sigint = None  # not on the main thread

    def _restore_sigint(self) -> None:
        if self._prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (ValueError, OSError):
                pass

    # ---- message handling ------------------------------------------------

    def _effective_system_prompt(self) -> str:
        """The base prompt plus whatever the session is aiming at.

        Rebuilt per turn rather than frozen at startup, because the goal
        is set and cleared while the session is running. Appended rather
        than spliced into the base so the standing instructions stay
        intact when the goal changes.
        """
        prompt = self.system_prompt
        for name in self.active_skills:
            skill = skills.get(name)
            if skill is None or not skill.body.strip():
                continue
            prompt += (
                "\n\n## Skill in force: " + skill.name
                + "\nFollow this procedure where it applies to the current task.\n\n"
                + skill.body.strip()
            )
            if skill.resources:
                prompt += "\n\nBundled with this skill: " + ", ".join(skill.resources)
        if not self.goal:
            return prompt
        lines = [
            "",
            "## Standing goal",
            "The operator set this goal for the session. It outlives any",
            "single message: work toward it on every turn, and treat the",
            "current request as a step within it rather than a replacement.",
            "",
            f"Goal: {self.goal}",
        ]
        if self.goal_notes:
            lines.append("")
            lines.append("Notes recorded while working toward it:")
            for note in self.goal_notes:
                lines.append(f"- {note}")
        lines.append("")
        lines.append(
            "When the goal is fully met, say so plainly in your final "
            "message and start it with GOAL COMPLETE so the operator can "
            "clear it without checking by hand. Do not claim it is "
            "complete until it actually is."
        )
        return prompt + "\n" + "\n".join(lines)

    def set_goal(self, text: str) -> None:
        self.goal = text.strip()
        if not self.goal:
            return
        self._print(self.style.dim(f"  goal set: {self.goal}"))
        self._print(self.style.dim("  /goal to check it · /goal done to clear it"))

    def show_goal(self) -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set - /goal <what you want done>"))
            return
        self._print(f"  {self.style.bold('goal')} {self.goal}")
        for note in self.goal_notes:
            self._print(self.style.dim(f"    · {note}"))
        if not self.goal_notes:
            self._print(self.style.dim("    (no notes - /goal note <text> to add one)"))

    def clear_goal(self, reason: str = "") -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set"))
            return
        finished = self.goal
        self.goal = ""
        self.goal_notes = []
        self._print(self.style.dim(f"  goal cleared: {finished}"))
        if reason:
            self._print(self.style.dim(f"  {reason}"))

    def add_goal_note(self, text: str) -> None:
        if not self.goal:
            self._print(self.style.dim("  set a goal first: /goal <what you want done>"))
            return
        self.goal_notes.append(text.strip())
        self._print(self.style.dim(f"  noted ({len(self.goal_notes)} on this goal)"))

    def _check_goal_completion(self, result: "RunResult | None") -> None:
        """Notice an agent that declared the goal met.

        The agent cannot clear the goal itself - only report - so a wrong
        claim costs nothing but a line the operator can ignore.
        """
        if not self.goal or result is None or not result.final_message:
            return
        if "GOAL COMPLETE" not in result.final_message.upper():
            return
        self._print(
            self.style.dim("  the agent reports the goal is met - /goal done to clear it")
        )

    def auto_route(self, text: str) -> str | None:
        """Attach the skill this request is asking for, without being asked.

        Returns the name of a bundle that fits the request, or None. The
        skill is attached for this turn only; the bundle is handed back
        rather than launched, because a bundle is several turns and the
        caller has to decide whether to spend them.

        Declines when a skill is already attached: that means the
        operator chose one, or a bundle step chose one, and either way a
        deliberate choice outranks a guess.
        """
        # config.json sets the baseline; the preferences the operator
        # switched with /skills auto are stored beside their endpoints and
        # win over it, because a choice made out loud outranks a file.
        prefs = dict(self.config.get("skills") or {})
        prefs.update(skills_prefs())
        if not prefs.get("auto", True) or self.in_bundle or self.active_skills:
            return None
        if not skills.list_skills():
            # Nothing to route to, so do not pay for a scan of a directory
            # the operator has not populated on every single turn.
            return None
        found, bundle = skills.recommend(text)
        if found is not None:
            key = found.name.lower()
            self.active_skills.append(key)
            self.auto_attached.append(key)
            summary = " ".join(str(found.description).split())
            if len(summary) > 56:
                summary = summary[:53].rstrip() + "..."
            self._note(f"skill auto-attached: {found.name} — {summary}")
        if bundle is None:
            return None
        if prefs.get("auto_bundle", False):
            # The bundle attaches its own skill per step, so the single
            # skill routed a moment ago would only crowd it out.
            self._detach_auto()
            return bundle
        # Left as a hint rather than launched: a bundle is several turns
        # of work and starting that on a guess is not a favour.
        self._note(f"bundle '{bundle}' covers this end to end - /skills launch {bundle}")
        return None

    def _detach_auto(self) -> None:
        """Drop whatever the router attached, once the turn is over."""
        if not self.auto_attached:
            return
        self.active_skills = [s for s in self.active_skills if s not in self.auto_attached]
        self.auto_attached = []

    def handle(self, text: str) -> RunResult | None:
        # Startup card disappears on first real work (Grok-style)
        # But skip if viewport has content from a resumed session.
        has_resumed_content = (
            self.layout is not None
            and self.layout.active
            and len(self.layout.lines) > 0
        )
        if getattr(self, "_splash_visible", False) and self.layout is not None and not has_resumed_content:
            try:
                if getattr(self.layout, "_splash_visible", False):
                    self.layout.hide_splash()
            except Exception:
                pass
            self._splash_visible = False
        self.message_count += 1
        self._abort.clear()
        text, attached = self.expand_mentions(text)
        if attached:
            shown = attached[:8]
            extra = "" if len(attached) <= 8 else f" (+{len(attached) - 8} more)"
            self._note("attached: " + ", ".join(shown) + extra)
        # Routed before the turn runs, so the chosen procedure is in the
        # system prompt the agent actually reads rather than in the next
        # one, which may never come.
        bundle = self.auto_route(text)
        if bundle:
            return _skills_launch(self, bundle)
        task = {"task_id": f"console-{self.message_count}", "problem_statement": text}

        self._auto_compact()

        loop = AgentLoop(
            llm=self.llm,
            sandbox=self.sandbox,
            tools=self.tools,
            evaluator=NullEvaluator(),
            logger=self.logger,
            events=self.bus,
            system_prompt=self._effective_system_prompt(),
            max_steps=self.max_steps,
            on_delta=self._on_delta,
            context=self.context,
            abort=self._abort,
            approver=self.approvals,
        )
        self._streamed_this_run = False
        self._stream_header_done = False
        self._stream_tokens = 0
        self._last_counter_update = 0.0
        self._stream_renderer.reset()

        self._turn_started = time.monotonic()
        self._spinner = Spinner(
            self.style,
            frame=self.frame.frame if self.frame else None,
            layout=self.layout,
        ).start() if sys.stdout.isatty() else None
        result = None
        self._install_sigint()
        self._start_dashboard_refresh()
        try:
            result = loop.run(task)
        except (KeyboardInterrupt, AbortError):
            self._abort.set()
            self._print(self.style.red("  interrupted"))
        except HarnessError as exc:
            self._print(self.style.red(f"  !! {exc}"))
        else:
            # Flush any remaining buffered text from the streaming renderer.
            if self._streamed_this_run:
                tail = self._stream_renderer.flush()
                if tail:
                    if self.layout is not None and self.layout.active:
                        self.layout.write(tail)
                    elif self.frame is not None:
                        self.frame.write(tail)
                        self.frame.flush()
                    else:
                        sys.stdout.write(tail)
                        sys.stdout.flush()
                # Streamed in full, so the reply is already on screen - do
                # not print it again. A second copy would sit below the
                # first, and because the streaming path emits raw text
                # while render_markdown would strip its marks, the two
                # would disagree with each other line for line.
                if self.frame is None:
                    if self.layout is not None and self.layout.active:
                        self.layout.write("\n")
                    else:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
            elif result is not None and result.final_message:
                body = render_markdown(result.final_message, self.style)
                if self.frame is not None:
                    # Delivered whole, so it can be word-wrapped properly
                    # instead of streamed a word at a time.
                    self.frame.row(body)
                else:
                    self._print(f"{self.style.bold('agent')} {body}")
            if result is not None:
                self._record_usage(result)
                self._record_memory(task, result)
                self._report_changes()
                self._check_goal_completion(result)
                # After the turn is fully reported, so a session saved
                # mid-turn cannot be missing the assistant's last answer.
                self.autosave()
        finally:
            self._stop_dashboard_refresh()
            self._restore_sigint()
            if self._spinner:
                # Never clear after a streamed reply: the reply's last line is
                # still on screen and clear_line would erase it.
                self._spinner.stop(clear=not self._streamed_this_run)
                self._spinner = None
            # Last, so the skill the router chose is in force for the whole
            # turn - including the divider row - and not a moment less.
            self._detach_auto()
            self._end_turn(result)
        return result

    def _end_turn(self, result: "RunResult | None") -> None:
        """Mark where a turn stopped.

        Inside the frame this is a divider row rather than a bottom
        border: the session goes on, so the box does not close. The
        verdict and the usage line ride on it, which is also the
        narrowest place in the frame - hence the short form.
        """
        # Clear the live token counter so the next prompt is clean.
        self._stream_tokens = 0
        if self.frame is not None:
            if result is None:
                self.frame.divider("no result")
            else:
                self.frame.divider(self._footer(result))
            return
        if result is not None:
            self._print(f"  {self.style.dim(self._usage_line(result))}")

    def _footer(self, result: "RunResult | None") -> str:
        """One compact line for the bottom border."""
        if result is None:
            return "no result"
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        usage = (
            f"{_short(tin)} in / {_short(tout)} out"
            if (tin or tout)
            else f"~{_short(self.context.tokens)} in context"
        )
        steps = result.steps_used
        steps_label = f"{steps} step" if steps == 1 else f"{steps} steps"
        cache_bit = f" · {_short(cache)} cached" if cache else ""
        return f"{result.stopped_reason} · {steps_label} · {usage}{cache_bit}"

    # ---- auto-refresh dashboard ------------------------------------------

    def _start_dashboard_refresh(self) -> None:
        pass
    def _stop_dashboard_refresh(self) -> None:
        pass
    def _refresh_dashboard_in_place(self) -> None:
        pass

    def _record_usage(self, result: RunResult) -> None:
        self.totals["turns"] += 1
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        self.totals["tokens_in"] += tin
        self.totals["tokens_out"] += tout
        self.totals["tool_errors"] += int(result.metrics.get("tool_errors", 0))
        self.totals["cache_hit"] += cache
        # Record per-turn metrics for trend analysis.
        rate = (cache * 100 // tin) if tin > 0 else 0
        self.turn_history.append({
            "turn": self.totals["turns"],
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_hit": cache,
            "cache_rate": rate,
        })
        # Wire CACHE instantly — top bar shows hit rate without waiting for next chrome redraw
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def _usage_line(self, result: RunResult) -> str:
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        steps = result.steps_used
        elapsed = time.monotonic() - self._turn_started
        elapsed_str = _format_elapsed(elapsed)
        cache_bit = f" · {_short(cache)} CACHED" if cache else ""
        if not tin and not tout:
            return f"{elapsed_str} · {steps} STEP{cache_bit} · CTX {_short(self.context.tokens)}"
        return (
            f"{elapsed_str} · {steps} STEP · "
            f"I/O {_short(tin)} / {_short(tout)}{cache_bit} · CTX {_short(self.context.tokens)}"
        )

    def _record_memory(self, task: dict, result: RunResult) -> None:
        final = (result.final_message or "").strip().replace("\n", " ")[:300]
        append_memory(
            self.memory_path,
            f"- {time.strftime('%Y-%m-%d %H:%M')} | {task['task_id']} | "
            f"{result.stopped_reason}: {final}",
        )

    def _report_changes(self) -> None:
        """Announce files the agent touched, newest first, once each."""
        changed = getattr(self.sandbox, "changed", set()) or set()
        fresh = sorted(changed - self.reported_changes)
        if not fresh:
            return
        self.reported_changes.update(fresh)
        shown = fresh[:8]
        more = "" if len(fresh) <= 8 else f" (+{len(fresh) - 8} more)"
        self._print(f"  {self.style.dim('changed: ' + ', '.join(shown) + more)}")

    # ---- context management ----------------------------------------------

    def _auto_compact(self) -> None:
        limit = int(self.config.get("auto_compact_tokens", 0) or 0)
        if limit and self.context.tokens > limit:
            self._print(self.style.dim(f"  (context ~{self.context.tokens} tokens, compacting)"))
            self.compact()

    def compact(self) -> bool:
        """Summarise the conversation, keeping the system prompt and summary."""
        if len(self.context.messages) <= 3:
            return False
        transcript = _transcript(self.context.messages)
        request = (
            "Summarise this coding session so work can continue without the "
            "original transcript. Cover: the goal, every file created or "
            "modified and why, commands that were run and their outcome, any "
            "error encountered and how it was resolved, and the exact state "
            "left off at. Be dense and concrete; no preamble.\n\n"
            f"{transcript}"
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": request}], tools=None, on_delta=None
            )
        except HarnessError as exc:
            self._print(self.style.red(f"  compaction failed: {exc}"))
            return False
        summary = (response.content or "").strip()
        if not summary:
            return False
        before = self.context.tokens
        self.context.replace_body(
            [
                {
                    "role": "user",
                    "content": "Earlier in this session (compressed summary):\n" + summary,
                }
            ]
        )
        self._print(
            self.style.dim(f"  compacted: ~{before} -> ~{self.context.tokens} tokens")
        )
        return True

    # ---- session persistence ---------------------------------------------

    def save_session(self, path: str) -> bool:
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": self.workspace,
            "model": self.config.get("llm", {}).get("model", "?"),
            "totals": self.totals,
            "messages": self.context.messages,
        }
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._print(self.style.red(f"  save failed: {exc}"))
            return False
        self._print(self.style.dim(f"  saved {len(self.context.messages)} messages to {path}"))
        return True

    def load_session(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._print(self.style.red(f"  load failed: {exc}"))
            return False
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.red("  load failed: no messages in file"))
            return False
        self.context.messages = list(messages)
        self.context.resync()
        totals = payload.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: int(v) for k, v in totals.items() if k in self.totals})
        self._print(
            self.style.dim(f"  restored {len(messages)} messages (~{self.context.tokens} tokens)")
        )
        return True

    # ---- resumable sessions ----------------------------------------------

    def autosave(self) -> None:
        """Keep the session resumable without being asked.

        Called after every turn. It is silent on purpose: a "saved"
        line after each reply would be noise, and the only time the
        operator learns the file exists is when /resume lists it.

        Nothing is written until there is a real conversation - saving
        after "hello" would fill the store with sessions not worth
        resuming and push the real ones out of the listing.
        """
        if len(self.context.messages) < 2:
            return
        if not self.session_name:
            self.session_name = sessions.derive_name(self.workspace, self.model_name())
        sessions.save(
            self.session_name,
            {
                "workspace": self.workspace,
                "model": self.model_name(),
                "summary": self._session_summary(),
                "totals": self.totals,
                "goal": self.goal,
                "goal_notes": self.goal_notes,
                "messages": self.context.messages,
            },
        )

    def model_name(self) -> str:
        return str(self.config.get("llm", {}).get("model", "") or "")

    def _session_summary(self) -> str:
        """The first thing the operator said - the only useful label."""
        for message in self.context.messages:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return re.sub(r"\s+", " ", content).strip()[:70]
        return ""

    def resume_session(self, name: str) -> bool:
        """Restore a saved session by name."""
        data = sessions.load(name)
        if data is None:
            known = sessions.list_sessions()
            if not known:
                self._print(self.style.dim("  no saved sessions yet"))
                return False
            self._print(self.style.red(f"  no session named '{name}'"))
            self._print(self.style.dim("  /resume lists them"))
            return False
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.red(f"  session '{name}' has no conversation"))
            return False
        self.context.messages = list(messages)
        self.context.resync()
        totals = data.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: int(v) for k, v in totals.items() if k in self.totals})
        self.message_count = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        # The goal travels with the conversation: resuming a session to
        # finish something and finding the objective gone defeats the
        # point of resuming it.
        self.goal = str(data.get("goal") or "")
        notes = data.get("goal_notes")
        self.goal_notes = [str(n) for n in notes] if isinstance(notes, list) else []
        # Adopt the name, so the next autosave continues this session
        # rather than starting a second file beside it.
        self.session_name = name
        self._print(
            self.style.dim(
                f"  resumed '{name}' - {len(messages)} messages "
                f"(~{self.context.tokens} tokens)"
            )
        )
        # Replay conversation history in the viewport.
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                ts = ""
                self._print(f"{self.style.on_grey(ts)} {self.style.bold(content)}")
            elif role == "assistant" and isinstance(content, str):
                # Show a condensed version of assistant replies.
                preview = content.strip()
                if len(preview) > 120:
                    preview = preview[:120] + "..."
                self._print(f"{self.style.green('agent')} {self.style.dim(preview)}")
        summary = data.get("summary") or ""
        if summary:
            self._print(self.style.dim(f"  started with: {summary}"))
        return True

    def show_sessions(self) -> None:
        """List what can be resumed."""
        known = sessions.list_sessions()
        if not known:
            self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return
        self._print(self.style.bold("  saved sessions"))
        for item in known:
            when = item["saved_at"] or "unknown time"
            turns = item["turns"]
            label = f"{turns} turn" if turns == 1 else f"{turns} turns"
            head = f"  {item['name']}"
            if item["name"] == self.session_name:
                head += self.style.dim(" (current)")
            self._print(head)
            self._print(self.style.dim(f"      {when} · {label} · {item['model'] or '?'}"))
            if item["summary"]:
                self._print(self.style.dim(f"      {item['summary']}"))
        self._print("")
        self._print(self.style.dim("  /resume <name> to pick one up"))

    def pick_session(self) -> bool:
        """Resume from a menu. False when nothing was chosen."""
        known = sessions.list_sessions()
        if not known:
            self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return False
        options = []
        for item in known:
            summary = item["summary"] or "no summary"
            options.append(
                Option(
                    item["name"],
                    item["name"],
                    f"{item['saved_at']} · {item['turns']} turns · {summary}",
                )
            )
        chosen = _menu(
            self,
            "Resume a session",
            options,
            hint="↑↓ move · Enter resume · Esc cancel",
        )
        if not chosen:
            return False
        return self.resume_session(chosen)

    # ---- inspection commands ---------------------------------------------

    def _git(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout if completed.returncode == 0 else ""

    def _git_ok(self, *args: str) -> bool:
        """Run git and report success; stdout alone cannot distinguish it."""
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def show_workspace(self) -> None:
        root = self.sandbox.root
        self._print(f"workspace: {root}")
        entries = sorted(os.listdir(root))[:50] if os.path.isdir(root) else []
        for entry in entries:
            full = os.path.join(root, entry)
            self._print(("> " if os.path.isdir(full) else "") + entry)
        if not entries:
            self._print("(empty)")

    def show_diff(self) -> None:
        stat = self._git("diff", "--stat")
        status = self._git("status", "--short")
        if not stat and not status:
            self._print("(no uncommitted changes)")
            return
        if status:
            self._print(status.rstrip())
        if stat:
            self._print(stat.rstrip())
        diff = self._git("diff")
        if diff:
            lines = diff.splitlines()
            cap = 400
            self._print("\n".join(lines[:cap]))
            if len(lines) > cap:
                self._print(f"... ({len(lines) - cap} more lines)")

    def undo_changes(self) -> None:
        status = self._git("status", "--porcelain")
        if not status:
            self._print("(nothing to undo - working tree is clean)")
            return
        self._print(f"{len(status.strip().splitlines())} file(s) would be reverted:")
        self._print(status.rstrip())
        try:
            answer = input('type "yes" to discard all uncommitted changes: ').strip()
        except (KeyboardInterrupt, EOFError):
            self._print("cancelled")
            return
        if answer.lower() != "yes":
            self._print("cancelled")
            return
        self._print("reverted" if self._git_ok("checkout", "--", ".") else "revert failed")

    def show_cost(self, compact: bool = False, as_json: bool = False) -> None:
        t = self.totals
        tokens_in = t['tokens_in']
        tokens_out = t['tokens_out']
        cache_hit = t['cache_hit']
        context_tokens = self.context.tokens
        context_chars = self.context.chars

        # Derived metrics.
        cache_rate = (cache_hit * 100 // tokens_in) if tokens_in > 0 else 0
        cache_saved = cache_hit // 2  # ~50% discount

        if as_json:
            import json
            payload = {
                "turns": t['turns'],
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_hit": cache_hit,
                "cache_rate": cache_rate,
                "cache_saved": cache_saved,
                "tool_errors": t['tool_errors'],
                "context_tokens": context_tokens,
                "context_chars": context_chars,
            }
            if self.turn_history:
                payload["turn_history"] = self.turn_history[-10:]
            self._print(json.dumps(payload, indent=2))
            return

        self._print(f"turns        {t['turns']}")
        self._print(f"tokens in    {tokens_in}")
        self._print(f"tokens out   {tokens_out}")
        # Cache analytics: show hit rate and estimated savings.
        if tokens_in > 0 and cache_hit > 0:
            self._print(f"cache hit    {cache_hit} ({cache_rate}% of prompt)")
            self._print(f"cache saved  {cache_saved} tokens (~50% discount)")
        else:
            self._print(f"cache hit    {cache_hit}")
        self._print(f"tool errors  {t['tool_errors']}")
        self._print(f"context      ~{context_tokens} tokens ({context_chars} chars)")
        # Per-turn cache trend (last 5 turns) — skipped in compact mode.
        if not compact and self.turn_history:
            recent = self.turn_history[-5:]
            self._print("")
            self._print("  turn  in      out     cached  rate")
            self._print("  " + "─" * 40)
            for entry in recent:
                tin = entry['tokens_in']
                tout = entry['tokens_out']
                ch = entry['cache_hit']
                cr = entry['cache_rate']
                self._print(
                    f"  {entry['turn']:<6}{tin:<8}{tout:<8}{ch:<8}{cr}%"
                )

    def show_dashboard(self) -> bool:
        """In compact mode, dashboard is the startup card (once)."""
        if not sys.stdout.isatty():
            return False
        try:
            if self.layout is not None and self.layout.active:
                self.layout.show_splash()
                return True
            return False
        except Exception:
            return False

    def show_dashboard_counted(self) -> int:
        """Compact: just the startup card height."""
        try:
            return len(compact.render_card(self, enabled=getattr(self.style, "enabled", True)))
        except Exception:
            return 5

    def set_model(self, name: str, quiet: bool = False) -> None:
        self.config.setdefault("llm", {})["model"] = name
        try:
            self.llm = build_llm(self.config["llm"])
        except HarnessError as exc:
            self._print(self.style.red(f"  could not switch model: {exc}"))
            return
        if not quiet:
            self._print(self.style.dim(f"  model is now {name}"))
        self.refresh_title()
        self._warn_if_key_missing()
        # Wire top info instantly so MODEL shows new name without waiting for next turn
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def set_reasoning(self, level: str, quiet: bool = False) -> None:
        """Set the thinking budget for the current model.

        Not every endpoint understands the field; the client sheds it on a
        400 and the reply simply arrives without the extra thinking.
        """
        wanted = level.strip().lower()
        if wanted in ("off", "none", ""):
            wanted = None
        elif wanted not in REASONING_EFFORTS:
            self._print(
                self.style.yellow(f"  reasoning must be one of {', '.join(REASONING_EFFORTS)} or off")
            )
            return
        llm = self.config.setdefault("llm", {})
        llm["reasoning_effort"] = wanted
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.red(f"  could not set reasoning: {exc}"))
            return
        if not quiet:
            self._print(
                self.style.dim(f"  reasoning is now {wanted}" if wanted else "  reasoning off")
            )
        self.refresh_title()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def show_reasoning(self) -> None:
        effort = self.config.get("llm", {}).get("reasoning_effort")
        current = effort or "off"
        options = " ".join(
            f"[{e}]" if e == effort else e for e in REASONING_EFFORTS
        )
        self._print(f"  reasoning  {current}   {self.style.dim(options + '  off')}")
        self._print(
            self.style.dim(
                "  higher means more thorough and slower; ignored by models "
                "that do not reason"
            )
        )

    @property
    def endpoint_name(self) -> str:
        """Which saved endpoint the current base URL belongs to, if any."""
        llm = self.config.get("llm", {})
        return endpoint_name_for_url(llm.get("base_url", "")) or ""

    def use_endpoint(self, name: str, model: str | None = None) -> bool:
        """Point the agent at a saved endpoint. True on success."""
        entry = known_endpoints().get(name.lower())
        if entry is None:
            self._print(self.style.yellow(f"  no endpoint named '{name}'"))
            self._print(self.style.dim("  add one with /connect, or list them: /connect"))
            return False
        llm = self.config.setdefault("llm", {})
        llm["base_url"] = entry["base_url"]
        llm["api_key_env"] = entry.get("api_key_env") or ""
        # A model name rarely survives a move between endpoints, so take
        # the first one this endpoint offers unless one was asked for.
        llm["model"] = model or (entry.get("models") or [""])[0] or llm.get("model", "")
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.red(f"  could not switch endpoint: {exc}"))
            return False
        set_active(endpoint=name.lower(), model=llm.get("model", ""))
        self._print(self.style.dim(f"  endpoint is now {entry['base_url']}"))
        self.refresh_title()
        self._warn_if_key_missing()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass
        return True

    def _warn_if_key_missing(self) -> None:
        """Say so up front when the key variable is unset.

        Silence here turns into a confusing 401 three steps into a task.
        """
        llm = self.config.get("llm", {})
        key_env = llm.get("api_key_env") or ""
        if not provider_needs_key(llm.get("base_url", ""), key_env):
            return
        if os.environ.get(key_env) or has_stored(key_env):
            return
        self._print(self.style.yellow(f"  warning: no key for ${key_env}"))
        self._print(
            self.style.dim(
                f"  store one with /connect, or edit {settings_path()}"
            )
        )

    def show_endpoints(self) -> None:
        """List what the user has configured, and where the file is."""
        llm = self.config.get("llm", {})
        current = (llm.get("base_url") or "").rstrip("/")
        known = known_endpoints()
        if not known:
            self._print(self.style.dim("  no endpoints yet - add one with /connect"))
            # Name the file even here: an empty list is exactly when
            # somebody is most likely to want to type one in by hand.
            self._print(self.style.dim(f"  or add one to {settings_path()}"))
            return
        self._print(self.style.bold("  endpoints"))
        for name in sorted(known):
            entry = known[name]
            marker = "*" if entry["base_url"] == current else " "
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry["base_url"], key_env):
                key_state = "no key needed"
            elif os.environ.get(key_env):
                key_state = "key in env"
            elif has_stored(key_env):
                key_state = f"stored {mask(stored_keys().get(key_env))}"
            else:
                key_state = "no key"
            count = len(entry.get("models") or [])
            model_bit = f"{count} model" + ("" if count == 1 else "s")
            tail = " · ".join(p for p in (key_state, model_bit) if p)
            self._print(
                f"  {marker} {name:<12} {entry['base_url']:<38}"
                f" {self.style.dim(tail)}"
            )
        self._print(self.style.dim("  * = current. add or switch: /connect"))
        self._print(self.style.dim(f"  or edit by hand: {settings_path()}"))

    def banner(self) -> None:
        s = self.style
        art = [
            "╔══════════════════════════════╗",
            "║  M A N T R A                 ║",
            "║  coding agent harness        ║",
            "╚══════════════════════════════╝",
        ]
        for line in art:
            self._print(s.cyan(line))
        llm_cfg = self.config.get("llm", {})
        self._print(f"model      {s.bold(llm_cfg.get('model', '?'))}")
        self._print(f"endpoint   {s.dim(llm_cfg.get('base_url', '?'))}")
        self._print(f"workspace  {self.sandbox.root}")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch:
            dirty = self._git("status", "--porcelain")
            state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} uncommitted"
            self._print(f"git        {branch} ({state})")
        self._print(f"approvals  {self.approvals.mode}  {s.dim('/'.join(MODES))}")
        self._print(f"tools      {len(self.tools)} loaded, step limit {self.max_steps}")
        if self.instructions_path:
            self._print(s.dim(f"instructions loaded from {os.path.basename(self.instructions_path)}"))
        if os.path.isfile(KNOWN_FAILURES_PATH):
            # Skip the "## KF-N" template line: only numbered entries count.
            count = sum(
                1 for ln in open(KNOWN_FAILURES_PATH, encoding="utf-8", errors="replace")
                if ln.startswith("## KF-") and ln[6:7].isdigit()
            )
            self._print(s.dim(f"known-failure registry: {count} classes"))
        self._print(s.dim("type /help for commands"))


# There are no built-in endpoints. Everything MANTRA knows about lives
# in the user's own settings file, which is hand-editable and is
# written by /connect. See core/settings.py for the shape.

# Endpoints reached over localhost that accept any key or none at all.
KEYLESS_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


def provider_needs_key(base_url: str, api_key_env: str) -> bool:
    """False for local endpoints, which simply do not check a key."""
    if not api_key_env:
        return False
    lowered = (base_url or "").lower()
    return not any(host in lowered for host in KEYLESS_HOSTS)


SLASH_COMMANDS = [
    ("/connect", "add or switch endpoint - URL and key, models auto-fetch"),
    ("/model", "pick a model (and its reasoning effort) from a menu"),
    # After the two a newcomer needs: useful, but not day one.
    ("/connect key", "replace the stored key for an endpoint"),
    ("/help", "show the command list"),
    ("/workspace", "print the workspace path and contents"),
    ("/memory", "show the durable memory file"),
    ("/diff", "show uncommitted changes"),
    ("/undo", "discard uncommitted changes"),
    ("/tools", "list the tools the agent can call"),
    ("/approve", "show or switch approval mode"),
    ("/cost", "token usage for this session"),
    ("/dashboard", "show the status overlay (Ctrl+G)"),
    ("/compact", "summarise the conversation"),
    ("/clear", "drop the conversation"),
    ("/reset", "start a fresh conversation"),
    ("/save", "save the session to JSON"),
    ("/load", "restore a saved session"),
    ("/resume", "pick up a saved session where it left off"),
    ("/goal", "set or check the standing goal for this session"),
    ("/workflow", "run a saved sequence of steps"),
    ("/skills", "find, read, attach and run skill bundles"),
    ("/paste", "multi-line input"),
    ("/steps", "show or set the step limit"),
    ("/verbose", "toggle tool output echo"),
    ("/exit", "leave the console"),
]

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode",
}
MAX_INDEX_ENTRIES = 4000


class ConsoleCompleter:
    """Suggests slash commands after ``/`` and workspace paths after ``@``."""

    def __init__(self, session: "ConsoleSession") -> None:
        self.session = session
        self._entries: list[str] = []
        self._indexed = False
        self._cache_root = ""
        self._cache_time = 0.0

    def begin(self) -> None:
        """Re-index the workspace once per prompt, not once per keystroke."""
        root = os.path.abspath(self.session.sandbox.root)
        now = time.monotonic()
        if self._indexed and root == self._cache_root and (now - self._cache_time) < 1.5:
            return
        entries: list[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                relative_dir = os.path.relpath(dirpath, root)
                base = "" if relative_dir == "." else relative_dir.replace(os.sep, "/")
                for name in sorted(dirnames):
                    entries.append(f"{base}/{name}/" if base else f"{name}/")
                for name in sorted(filenames):
                    entries.append(f"{base}/{name}" if base else name)
                if len(entries) >= MAX_INDEX_ENTRIES:
                    break
        except OSError:
            entries = []
        self._entries = entries[:MAX_INDEX_ENTRIES]
        self._indexed = True
        self._cache_root = root
        self._cache_time = time.monotonic()

    def complete(self, buffer: str, cursor: int):
        # The editor primes this once per prompt, but a caller using the
        # completer directly should not get silence for forgetting to.
        if not self._indexed:
            self.begin()
        # A cursor past the end must not index off the string; clamp
        # rather than trusting every caller to pass a sane position.
        cursor = max(0, min(cursor, len(buffer)))
        start = cursor
        while start > 0 and not buffer[start - 1].isspace():
            start -= 1
        token = buffer[start:cursor]
        if token.startswith("@"):
            return self._complete_path(start, cursor, token[1:])
        if buffer.startswith("/") and start == 0:
            return self._complete_command(cursor, token)
        # "/model gpt-" completes from the endpoint's own catalogue once
        # we have fetched it, which beats guessing at names.
        if buffer.startswith("/model "):
            return self._complete_model(cursor, token)
        if buffer.startswith("/connect"):
            c = self._complete_connect(buffer, start, cursor, token)
            if c:
                return c
        if buffer.startswith("/skills"):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if buffer.startswith("/workflow"):
            c = self._complete_workflow(buffer, start, cursor, token)
            if c:
                return c
        return None

    def _complete_model(self, cursor: int, token: str):
        known = getattr(self.session, "known_models", None) or []
        lowered = token.lower()
        matches = [m for m in known if m.lower().startswith(lowered)]
        if not matches:
            return None
        matches = matches[:50]
        labels = [m + ("   reasons" if is_reasoning_model(m) else "") for m in matches]
        return Completion(
            items=matches, start=cursor - len(token), end=cursor, labels=labels
        )

    def _complete_command(self, cursor: int, token: str):
        matches = [name for name, _ in SLASH_COMMANDS if name.startswith(token)]
        if not matches:
            return None
        labels = []
        for name in matches:
            description = next((d for n, d in SLASH_COMMANDS if n == name), "")
            labels.append(f"{name}  {description}")
        return Completion(items=matches, start=0, end=cursor, labels=labels)

    def _complete_path(self, start: int, cursor: int, query: str):
        if not self._entries:
            return None
        needle = query.lower().replace("\\", "/")
        prefix = [e for e in self._entries if e.lower().startswith(needle)]
        inside = [e for e in self._entries if needle and needle in e.lower()]
        ordered = prefix + [e for e in inside if e not in prefix]
        matches = ordered[:50]
        if not matches:
            return None
        return Completion(
            items=["@" + m for m in matches], start=start, end=cursor, labels=matches
        )

    def _complete_connect(self, buffer: str, start: int, cursor: int, token: str):
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] != "/connect":
            return None
        if len(parts) == 1:
            subcommands = ["list", "remove", "key", "keys", "show"]
            endpoints = sorted(known_endpoints().keys())
            candidates = subcommands + endpoints
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches and lowered:
                matches = [c for c in candidates if lowered in c.lower()]
            if not matches:
                return None
            labels = []
            for m in matches[:50]:
                if m in endpoints:
                    ep = known_endpoints()[m]
                    labels.append(f"{m}  {ep.get('base_url','')}")
                else:
                    labels.append(m)
            return Completion(items=matches[:50], start=start, end=cursor, labels=labels[:50])
        elif len(parts) == 2:
            sub = parts[1].lower()
            if sub in ("remove", "forget", "delete", "rm", "key", "keys", "show", "list"):
                endpoints = sorted(known_endpoints().keys())
                lowered = token.lower()
                matches = [e for e in endpoints if e.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [e for e in endpoints if lowered in e.lower()]
                if not matches:
                    return None
                labels = [f"{m}  {known_endpoints()[m].get('base_url','')}" for m in matches[:50]]
                return Completion(items=matches[:50], start=start, end=cursor, labels=labels)
        return None

    def _complete_skills(self, buffer: str, start: int, cursor: int, token: str):
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] != "/skills":
            return None
        subcommands = ["list", "show", "use", "apply", "find", "search", "route", "bundles", "bundle", "launch", "run", "auto", "clear", "off", "drop", "on"]
        skill_names = [s.name for s in skills.list_skills()]
        bundle_names = list(skills.load_bundles().keys())
        if len(parts) == 1:
            candidates = subcommands + skill_names
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches and lowered:
                matches = [c for c in candidates if lowered in c.lower()]
            if not matches:
                return None
            return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        elif len(parts) == 2:
            sub = parts[1].lower()
            if sub in ("show", "use", "apply", "on"):
                lowered = token.lower()
                matches = [n for n in skill_names if n.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [n for n in skill_names if lowered in n.lower()]
                if not matches:
                    return None
                labels = []
                for n in matches[:50]:
                    sk = skills.get(n)
                    desc = sk.description if sk and sk.description else ""
                    labels.append(f"{n}  {desc[:40]}" if desc else n)
                return Completion(items=matches[:50], start=start, end=cursor, labels=labels)
            elif sub in ("find", "search", "route"):
                lowered = token.lower()
                if not lowered:
                    matches = skill_names[:50]
                else:
                    matches = []
                    for sk in skills.list_skills():
                        if lowered in sk.name.lower() or lowered in (sk.description or "").lower():
                            matches.append(sk.name)
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
            elif sub in ("launch", "run", "bundle", "bundles"):
                lowered = token.lower()
                matches = [b for b in bundle_names if b.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [b for b in bundle_names if lowered in b.lower()]
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
            elif sub == "auto":
                candidates = ["on", "off", "bundle"]
                lowered = token.lower()
                matches = [c for c in candidates if c.lower().startswith(lowered)]
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        elif len(parts) == 3 and parts[1].lower() == "auto" and parts[2].lower() == "bundle":
            candidates = ["on", "off"]
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches:
                return None
            return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        return None

    def _complete_workflow(self, buffer: str, start: int, cursor: int, token: str):
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] != "/workflow":
            return None
        subcommands = ["list", "show", "create", "launch", "run", "start", "remove", "delete", "rm"]
        workflow_names = [w["name"] for w in workflows.list_workflows()]
        if len(parts) == 1:
            candidates = subcommands + workflow_names
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches and lowered:
                matches = [c for c in candidates if lowered in c.lower()]
            if not matches:
                return None
            return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        elif len(parts) == 2:
            sub = parts[1].lower()
            if sub in ("show", "launch", "run", "start", "remove", "delete", "rm"):
                lowered = token.lower()
                matches = [n for n in workflow_names if n.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [n for n in workflow_names if lowered in n.lower()]
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        return None


_SAFE_HOME = os.path.expanduser("~")


def _infer_workspace() -> str:
    """Current directory becomes the workspace, like a real agent CLI.

    Guards: skip MANTRA itself (uses its own sandbox) and refuse to turn
    filesystem roots or the user home into a workspace - git-isolating
    ``C:\\Users\\<name>`` would be destructive nonsense.
    """
    cwd = os.getcwd()
    if cwd == PROJECT_ROOT:
        return os.path.join(PROJECT_ROOT, "workspace")
    protected = {
        os.path.dirname(_SAFE_HOME.rstrip("\\/")) or _SAFE_HOME,
        _SAFE_HOME,
        os.path.splitdrive(cwd)[0] + "\\",  # drive root, e.g. C:\
    }
    normalized = cwd.rstrip("\\/")
    if any(normalized.lower() == p.lower().rstrip("\\/") for p in protected):
        return os.path.join(PROJECT_ROOT, "workspace")
    return cwd


# ------------------------------------------------------------------ commands

def _read_multiline(session: "ConsoleSession") -> str:
    """Read several lines, ended by a lone dot.

    Each line goes through the editor so that, inside the frame, they
    are drawn as frame rows rather than spilling out past the border.
    """
    session._print("  (paste your message; finish with a line containing only .)")
    lines = []
    try:
        while True:
            line = _read_choice(session, "")
            if line.strip() == ".":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        pass
    return "\n".join(lines).strip()


def _read_choice(session: "ConsoleSession", prompt_text: str) -> str:
    """Read a line from the operator; empty when there is no terminal.

    Goes through the line editor rather than ``input()`` so that, inside
    the frame, the row the answer is typed on is closed properly instead
    of being abandoned with its right border missing.

    Every caller must tolerate an empty answer, because a piped run has
    nobody to answer and must not block or eat the next line.
    """
    if not sys.stdin.isatty():
        return ""
    try:
        editor = LineEditor(
            session.style,
            completer=None,
            hint="",
            on_submit=session.close_prompt,
        )
        return editor.read(session.prompt_text(prompt_text)).strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def _skills(session: "ConsoleSession", argument: str) -> None:
    """/skills: discover, read, attach and run skill bundles."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip()

    if head == "":
        known = skills.list_skills()
        if known:
            choice = _menu(session, "Skills", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in sorted(known, key=lambda s: s.name)])
            if choice:
                _skills_show(session, choice)
                return
        _skills_list(session)
    elif head == "list":
        _skills_list(session)
    elif head == "show":
        _skills_show(session, rest)
    elif head in ("use", "apply"):
        _skills_use(session, rest)
    elif head in ("bundles", "bundle"):
        _skills_bundles(session)
    elif head in ("launch", "run"):
        _skills_launch(session, rest)
    elif head in ("find", "search", "route"):
        _skills_find(session, rest)
    elif head == "auto":
        _skills_auto(session, rest)
    elif head in ("clear", "off", "drop"):
        if session.active_skills:
            session._print(session.style.dim("  skills detached: " + ", ".join(session.active_skills)))
            session.active_skills = []
        else:
            session._print(session.style.dim("  no skills attached"))
    elif head == "on":
        _skills_use(session, rest)
    else:
        # A bare name is the common case, so it shows rather than
        # demanding `show` first.
        _skills_show(session, argument)


def _skills_list(session: "ConsoleSession") -> None:
    known = skills.list_skills()
    if not known:
        session._print(session.style.dim("  no skills found"))
        session._print(session.style.dim(f"  looked in: {', '.join(str(r) for r in skills.roots())}"))
        session._print(session.style.dim(f"  set {skills._OVERRIDE_ENV} to point at a skills directory"))
        return
    index = skills.routing_table()
    session._print(session.style.bold(f"  skills ({len(known)})"))
    for skill in known:
        entry = index.get(skill.name.lower(), {})
        function = entry.get("function") or skill.description
        function = " ".join(str(function).split())
        if len(function) > 68:
            function = function[:65].rstrip() + "..."
        mark = "*" if skill.name.lower() in session.active_skills else " "
        session._print(f" {mark} {skill.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  /skills show <name> · /skills use <name> · /skills find <text>"))


def _skills_show(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = skills.list_skills()
        if not known:
            _skills_list(session)
            return
        choice = _menu(session, "Show skill", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in sorted(known, key=lambda s: s.name)])
        if not choice:
            _skills_list(session)
            return
        name = choice
    found = skills.get(name)
    if found is None:
        cands = skills.find(name, limit=8)
        if cands:
            choice = _menu(session, f"Skill '{name}' not found", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in cands])
            if choice:
                found = skills.get(choice)
            else:
                session._print(session.style.red(f"  no skill named '{name}'"))
                return
            if found is None:
                session._print(session.style.red(f"  no skill named '{name}'"))
                return
        else:
            session._print(session.style.red(f"  no skill named '{name}'"))
            return
    session._print(session.style.bold(f"  {found.name}"))
    if found.description:
        session._print(session.style.dim(f"  {found.description}"))
    meta = []
    if found.version:
        meta.append(f"v{found.version}")
    if found.resources:
        meta.append("bundles " + ", ".join(found.resources))
    if meta:
        session._print(session.style.dim("  " + " · ".join(meta)))
    body = found.body.strip()
    if not body:
        session._print(session.style.dim("  (empty)"))
        return
    session._print("")
    # Indented so the procedure reads as a block inside the frame
    # rather than as more console output.
    for line in body.split("\n"):
        session._print("  " + line.rstrip())
    session._print("")
    session._print(session.style.dim(f"  /skills use {found.name} to attach it"))


def _skills_use(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = skills.list_skills()
        if not known:
            session._print(session.style.dim("  no skills found"))
            return
        choice = _menu(session, "Use skill", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in sorted(known, key=lambda s: s.name)])
        if not choice:
            session._print(session.style.dim("  usage: /skills use <name>"))
            return
        name = choice
    found = skills.get(name)
    if found is None:
        cands = skills.find(name, limit=8)
        if cands:
            choice = _menu(session, f"Skill '{name}' not found", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in cands])
            if choice:
                found = skills.get(choice)
                if found:
                    name = choice
                else:
                    session._print(session.style.red(f"  no skill named '{name}'"))
                    return
            else:
                session._print(session.style.red(f"  no skill named '{name}'"))
                return
        else:
            session._print(session.style.red(f"  no skill named '{name}'"))
            return
    key = found.name.lower()
    if key in session.active_skills:
        session._print(session.style.dim(f"  '{found.name}' is already attached"))
        return
    session.active_skills.append(key)
    session._print(session.style.dim(f"  attached '{found.name}' - it now rides along with every turn"))
    session._print(session.style.dim("  /skills clear to detach"))


def _skills_bundles(session: "ConsoleSession") -> None:
    bundles = skills.load_bundles()
    if not bundles:
        session._print(session.style.dim("  no bundles found (no BUNDLES.md in any skills root)"))
        return
    session._print(session.style.bold(f"  bundles ({len(bundles)})"))
    for name, steps in sorted(bundles.items()):
        session._print(f"  {name:<16} {session.style.dim(' → '.join(steps))}")
    session._print("")
    session._print(session.style.dim("  /skills launch <bundle> to run one in order"))


def _skills_launch(session: "ConsoleSession", name: str) -> RunResult | None:
    """Run a bundle as ordered steps, attaching each skill in turn."""
    if not name:
        bundles = skills.load_bundles()
        if not bundles:
            session._print(session.style.dim("  no bundles found"))
            return None
        choice = _menu(session, "Launch bundle", [Option(value=n, label=n, hint=" → ".join(v[:2])) for n, v in sorted(bundles.items())])
        if not choice:
            session._print(session.style.dim("  usage: /skills launch <bundle>"))
            return None
        name = choice
    steps = skills.get_bundle(name)
    if steps is None:
        bundles = skills.load_bundles()
        if bundles:
            choice = _menu(session, f"Bundle '{name}' not found", [Option(value=n, label=n, hint=" → ".join(v[:2])) for n, v in sorted(bundles.items())])
            if choice:
                return _skills_launch(session, choice)
        session._print(session.style.red(f"  no bundle named '{name}'"))
        return None
    known = skills.load_all()
    missing = [s for s in steps if s.lower() not in known]
    if missing:
        session._print(session.style.yellow(f"  bundle names skills that are not installed: {', '.join(missing)}"))
        return None
    count = len(steps)
    label = f"{count} step" if count == 1 else f"{count} steps"
    session._print(session.style.bold(f"  launching bundle '{name}' ({label})"))
    previous = list(session.active_skills)
    last: RunResult | None = None
    was_in_bundle = session.in_bundle
    # Steps are turns, and the router would otherwise re-read each step's
    # boilerplate and attach something of its own over the top.
    session.in_bundle = True
    try:
        for position, step in enumerate(steps, 1):
            skill = known[step.lower()]
            session.active_skills = [skill.name.lower()]
            session._print("")
            session._print(
                session.style.dim(f"  step {position} of {count}: {skill.name} — {skill.description}")
            )
            try:
                result = session.handle(
                    f"Apply the {skill.name} skill to the current work."
                )
            except KeyboardInterrupt:
                session._print(session.style.yellow("  bundle stopped"))
                return last
            if result is None:
                session._print(session.style.yellow("  bundle stopped: the step did not complete"))
                return last
            last = result
    finally:
        session.in_bundle = was_in_bundle
        # Whatever the previous attachment was, put it back - a bundle
        # borrowing the slot must not silently drop what was there.
        session.active_skills = previous
    session._print("")
    session._print(session.style.dim(f"  bundle '{name}' finished"))
    return last


def _skills_auto(session: "ConsoleSession", argument: str) -> None:
    """/skills auto [on|off|bundle on|off]: routing without being asked."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip().lower()
    stored = skills_prefs()
    auto = bool(stored.get("auto", (session.config.get("skills") or {}).get("auto", True)))
    bundles = bool(
        stored.get("auto_bundle", (session.config.get("skills") or {}).get("auto_bundle", False))
    )

    if not head:
        session._print(session.style.bold("  skill auto-routing"))
        session._print(f"  {'auto':<14} {'on' if auto else 'off'}")
        session._print(f"  {'auto bundle':<14} {'on' if bundles else 'off'}")
        session._print("")
        session._print(
            session.style.dim(
                "  on - a matching skill attaches itself to a plain prompt, for that turn only"
            )
        )
        session._print(
            session.style.dim("  off - skills are used only when you name them with /skills use")
        )
        session._print("")
        session._print(session.style.dim("  /skills auto on|off · /skills auto bundle on|off"))
        return

    if head == "bundle":
        if rest not in ("on", "off"):
            session._print(session.style.dim("  usage: /skills auto bundle on|off"))
            return
        bundles = rest == "on"
        set_skills_prefs(auto_bundle=bundles)
        session._print(session.style.dim(f"  bundle auto-launch {'on' if bundles else 'off'}"))
        if bundles:
            session._print(
                session.style.yellow(
                    "  a bundle runs several turns - it will start on its own when one fits"
                )
            )
        return

    if head not in ("on", "off"):
        session._print(session.style.dim("  usage: /skills auto [on|off|bundle on|off]"))
        return
    auto = head == "on"
    set_skills_prefs(auto=auto)
    # Turning routing off is also the way to say "stop guessing at this
    # conversation", so an attachment already in force goes with it.
    if not auto:
        session._detach_auto()
    session._print(session.style.dim(f"  skill auto-routing {'on' if auto else 'off'}"))


def _skills_find(session: "ConsoleSession", query: str) -> None:
    if not query:
        known = skills.list_skills()
        if not known:
            session._print(session.style.dim("  no skills found"))
            return
        choice = _menu(session, "Find skill", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in sorted(known, key=lambda s: s.name)])
        if choice:
            _skills_show(session, choice)
        else:
            session._print(session.style.dim("  usage: /skills find <what you want to do>"))
        return
    hits = skills.find(query)
    if not hits:
        known = skills.list_skills()
        if known:
            choice = _menu(session, f"No match for '{query}'", [Option(value=s.name, label=s.name, hint=(s.description or "")[:50]) for s in sorted(known, key=lambda s: s.name)])
            if choice:
                _skills_show(session, choice)
                return
        session._print(session.style.dim(f"  nothing matches '{query}'"))
        return
    # Show selectable menu for the hits; if cancelled or non-interactive, fall back to text list
    choice = _menu(session, f"Skills for: {query}", [Option(value=s.name, label=s.name, hint=(skills.routing_table().get(s.name.lower(), {}).get("function") or s.description or "")[:50]) for s in hits])
    if choice:
        _skills_show(session, choice)
        return
    session._print(session.style.bold(f"  skills for: {query}"))
    index = skills.routing_table()
    for found in hits:
        function = index.get(found.name.lower(), {}).get("function") or found.description
        function = " ".join(str(function).split())
        if len(function) > 60:
            function = function[:57].rstrip() + "..."
        session._print(f"  {found.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  /skills show <name> to read one"))


def _goal(session: "ConsoleSession", argument: str) -> None:
    """/goal: the standing objective the whole session is working toward.

    Subcommands are checked before the free-text form, because
    ``/goal note`` is a lot more likely to be meant as a subcommand than
    as a goal whose entire text is the word "note".
    """
    argument = argument.strip()
    if not argument:
        session.show_goal()
        return
    head, _, rest = argument.partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("done", "clear", "drop"):
        session.clear_goal(rest or "cleared by the operator")
    elif head in ("note", "add"):
        if not rest:
            session._print(session.style.dim("  usage: /goal note <text>"))
        else:
            session.add_goal_note(rest)
    elif head in ("show", "check", "status"):
        session.show_goal()
    else:
        # Not a subcommand, so the whole line is the goal.
        session.set_goal(argument)


def _workflow(session: "ConsoleSession", argument: str) -> None:
    """/workflow create | show | launch | remove."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip()

    if head in ("", "list", "show"):
        _workflow_show(session, rest)
    elif head == "create":
        _workflow_create(session, rest)
    elif head in ("launch", "run", "start"):
        _workflow_launch(session, rest)
    elif head in ("remove", "delete", "rm"):
        _workflow_remove(session, rest)
    else:
        session._print(session.style.dim("  usage: /workflow create|show|launch|remove <name>"))


def _workflow_show(session: "ConsoleSession", name: str) -> None:
    if name:
        found = workflows.get(name)
        if found is None:
            known = workflows.list_workflows()
            if known:
                choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
                if choice:
                    _workflow_show(session, choice)
                    return
            session._print(session.style.red(f"  no workflow named '{workflows.slug(name)}'"))
            return
        steps = found["steps"]
        session._print(f"  {session.style.bold(found['name'])}")
        for index, step in enumerate(steps, 1):
            session._print(f"    {index}. {step}")
        session._print(session.style.dim(f"  /workflow launch {found['name']}"))
        return

    known = workflows.list_workflows()
    if not known:
        session._print(session.style.dim("  no workflows yet"))
        session._print(session.style.dim("  /workflow create <name>, then type one step per line"))
        return
    choice = _menu(session, "Show workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
    if choice:
        _workflow_show(session, choice)
        return
    session._print(session.style.bold("  workflows"))
    for item in known:
        count = len(item["steps"])
        label = f"{count} step" if count == 1 else f"{count} steps"
        session._print(f"  {item['name']}  {session.style.dim(label)}")
    session._print("")
    session._print(session.style.dim("  /workflow show <name> · /workflow launch <name>"))


def _workflow_create(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /workflow create <name>"))
        return
    session._print(
        session.style.dim(f"  steps for '{workflows.slug(name)}', one per line, . to finish:")
    )
    raw = _read_multiline(session)
    steps = [line.strip() for line in raw.split("\n") if line.strip()]
    ok, message = workflows.create(name, steps)
    colour = session.style.dim if ok else session.style.red
    session._print(f"  {colour(message)}")


def _workflow_launch(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to launch"))
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        choice = _menu(session, "Launch workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        name = choice
    found = workflows.get(name)
    if found is None:
        known = workflows.list_workflows()
        if known:
            choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_launch(session, choice)
                return
        session._print(session.style.red(f"  no workflow named '{workflows.slug(name)}'"))
        return
    steps = found["steps"]
    label = f"{len(steps)} step" if len(steps) == 1 else f"{len(steps)} steps"
    session._print(session.style.bold(f"  launching '{found['name']}' ({label})"))
    for index, step in enumerate(steps, 1):
        session._print("")
        session._print(session.style.dim(f"  step {index} of {len(steps)}: {step}"))
        try:
            result = session.handle(step)
        except KeyboardInterrupt:
            session._print(session.style.yellow("  workflow stopped"))
            return
        if result is None:
            session._print(session.style.yellow("  workflow stopped: the step did not complete"))
            return
    session._print("")
    session._print(session.style.dim(f"  workflow '{found['name']}' finished"))


def _workflow_remove(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to remove"))
            return
        choice = _menu(session, "Remove workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow remove <name>"))
            return
        name = choice
    if workflows.delete(name):
        session._print(session.style.dim(f"  removed '{workflows.slug(name)}'"))
    else:
        known = workflows.list_workflows()
        if known:
            choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_remove(session, choice)
                return
        session._print(session.style.red(f"  no workflow named '{workflows.slug(name)}'"))


def _menu(
    session: "ConsoleSession",
    title: str,
    options: list[Any],
    hint: str = "",
    allow_filter: bool = True,
    cursor: int = 0,
) -> str | None:
    """Open a selectable menu and return the chosen value.

    Returns None when cancelled, when nothing matched, or when there is
    no terminal to draw on. Callers treat None as "no change".
    """
    if not options:
        return None
    chosen = choose(
        session.style,
        title,
        options,
        hint=hint or "up/down or click · enter selects · esc cancels",
        allow_filter=allow_filter,
        cursor=cursor,
        # Menus are drawn as frame rows when the session is framed, so
        # a list never hangs off the side of the box.
        frame=session.frame,
    )
    # The menu returns "" for an explicit cancel and None when there is
    # no terminal; both mean "leave things as they are".
    return chosen or None


def _model_options(session: "ConsoleSession", models: list[str]) -> list[Option]:
    """Model rows, marking the ones that think before answering."""
    current = session.config.get("llm", {}).get("model", "")
    options = []
    for name in models:
        hint = "thinks" if is_reasoning_model(name) else ""
        if name == current:
            hint = (hint + " · current").strip(" ·")
        options.append(Option(value=name, hint=hint))
    return options


def _effort_options(current: str | None) -> list[Option]:
    """Thinking levels, with the one in force marked.

    "off" leads because it is the safe answer for a model that does not
    reason, and it is what most models should be left on.
    """
    levels = ("off", *REASONING_EFFORTS)
    options = []
    for level in levels:
        hints = []
        if level == current:
            hints.append("current")
        if level == "off":
            hints.append("send no effort field")
        elif level == "high":
            hints.append("most thorough, slowest")
        options.append(Option(value=level, hint=" · ".join(hints)))
    return options


def _pick_effort(session: "ConsoleSession", model: str) -> str | None:
    """Offer a thinking level for the model just chosen.

    Always offered, for every model. Effort is a property of the model
    in the operator's head, so gating the question on whether the name
    looks like a reasoning model just made it disappear for most of
    them - and the guess was wrong often enough to be worse than asking.
    """
    current = session.config.get("llm", {}).get("reasoning_effort") or "off"
    options = _effort_options(current)
    index = next((i for i, o in enumerate(options) if o.value == current), 0)
    title = f"reasoning effort for {model}"
    if not is_reasoning_model(model):
        title += session.style.dim(" (this model may ignore it)")
    chosen = _menu(session, title, options, allow_filter=False, cursor=index)
    return chosen or None


def _apply_model(session: "ConsoleSession", model: str, effort: str | None = None) -> None:
    """Set a model and settle reasoning as a property of that model.

    "off" is the default rather than an inherited level, so switching to
    a model that does not reason stops sending a field chosen for
    something else instead of leaving it behind.
    """
    session.set_model(model, quiet=True)
    session.set_reasoning(effort or "off", quiet=True)
    # One line for both, because model and effort are one choice here.
    effort_now = session.config["llm"].get("reasoning_effort") or "off"
    session._print(
        session.style.dim(
            f"  model is now {model} · reasoning {effort_now}"
        )
    )
    # Remember the pairing so the next /model menu opens on it.
    set_active(model=model, reasoning_effort=session.config["llm"].get("reasoning_effort"))
    # Refresh info bar.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()


def _try_fetch(session: "ConsoleSession") -> tuple[list[str], Exception | None]:
    """Ask the endpoint what it serves.

    Returns the models and the failure, because the two need different
    remedies: a rejected key is fixable on the spot, while a gateway
    with no catalogue just means typing the name.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    if not base_url:
        return [], None
    try:
        return fetch_models(base_url, llm.get("api_key_env")), None
    except HarnessError as exc:
        session._print(session.style.dim(f"  (could not list models: {exc})"))
        return [], exc


# Ways out of an empty catalogue. These are menu entries rather than
# printed advice because the operator is already at the point where
# something is wrong - being told to edit a file is not a fix.
TYPE_A_MODEL = "+ type a model name"
RE_ENTER_KEY = "+ re-enter the api key"
SWITCH_ENDPOINT = "+ connect a different endpoint"


def _is_auth_failure(error: Exception | None) -> bool:
    """True when the endpoint refused the credential.

    Detected from the message rather than a typed exception because the
    client raises the harness-level error, and the only thing that
    matters here is which remedy to offer.
    """
    return error is not None and "refused the key" in str(error)


def _rescue_catalogue(session: "ConsoleSession", error: Exception | None) -> bool:
    """Offer a way forward when no model list can be had.

    An empty catalogue used to end in "edit the settings file", which
    is the one answer that does not help: the common cause is a key
    that was mistyped, and that is fixable right here.
    """
    s = session.style
    llm = session.config.get("llm", {})
    key_env = llm.get("api_key_env") or ""
    options: list[Option] = []
    # Offer the key only when it could plausibly be the cause. Offering
    # it for an unreachable host sends someone off to re-paste a key
    # that was never the problem; not offering it for an unknown cause
    # leaves the most common fix off the menu.
    if provider_needs_key(llm.get("base_url", ""), key_env) and (
        error is None or _is_auth_failure(error)
    ):
        options.append(Option(value=RE_ENTER_KEY, hint="most likely if the key was mistyped"))
    options.append(Option(value=TYPE_A_MODEL, hint="if the endpoint hides its list"))
    options.append(Option(value=SWITCH_ENDPOINT, hint=""))
    session._print(s.yellow("  no models to choose from yet"))
    choice = _menu(session, "how do you want to fix it?", options, allow_filter=False)
    if choice == RE_ENTER_KEY:
        if not _replace_key(session):
            return False
        return _choose_model(session)
    if choice == TYPE_A_MODEL:
        return _type_a_model(session)
    if choice == SWITCH_ENDPOINT:
        return _connect(session, [])
    session._print(s.dim(f"  or add models by hand: {settings_path()}"))
    return False


def _type_a_model(session: "ConsoleSession") -> bool:
    """Take a model name from the operator and adopt it."""
    name = _read_choice(session, "  model name> ").strip()
    if not name:
        session._print(session.style.dim("  cancelled"))
        return False
    _apply_model(session, name, _pick_effort(session, name))
    endpoint = session.endpoint_name
    if endpoint:
        saved = models_for(endpoint)
        if name not in saved:
            set_models(endpoint, [*saved, name])
    return True


def _choose_model(session: "ConsoleSession") -> bool:
    """Open the model menu; effort follows as part of the same choice.

    This is the single place model and reasoning are decided, because
    effort is a property of the chosen model rather than a separate
    setting the operator has to remember to keep in step.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    if not base_url:
        session._print(session.style.yellow("  no endpoint configured - /connect first"))
        return False

    name = session.endpoint_name
    models = models_for(name) if name else []
    # Prefer what the endpoint says right now; fall back to the models
    # recorded in the settings file when it cannot be reached.
    fetched, error = _try_fetch(session)
    if fetched:
        models = fetched
        session.known_models = fetched
        if name:
            set_models(name, fetched)
    elif not models:
        return _rescue_catalogue(session, error)
    else:
        session.known_models = models

    # Always offer typing a name: a catalogue is never complete, and
    # without it an endpoint serving one model nobody listed is a dead
    # end rather than one more keystroke.
    options = _model_options(session, models)
    options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
    chosen = _menu(session, f"models at {base_url}", options)
    if not chosen:
        return False
    if chosen == TYPE_A_MODEL:
        return _type_a_model(session)
    _apply_model(session, chosen, _pick_effort(session, chosen))
    return True


NEW_ENDPOINT = "+ add a new endpoint"


def _endpoint_options(session: "ConsoleSession") -> list[Option]:
    """Saved endpoints, newest first, with an entry to add another."""
    known = known_endpoints()
    current = session.endpoint_name
    options = []
    for name in sorted(known):
        entry = known[name]
        hint = "current" if name == current else entry.get("base_url", "")
        options.append(Option(value=name, hint=hint))
    options.append(Option(value=NEW_ENDPOINT, hint=""))
    return options


def _connect_choose_endpoint(session: "ConsoleSession") -> str | None:
    """Menu over saved endpoints. None means the operator cancelled."""
    known = known_endpoints()
    if not known:
        return NEW_ENDPOINT
    return _menu(session, "endpoints", _endpoint_options(session), allow_filter=False)


def _replace_key(session: "ConsoleSession", name: str = "") -> bool:
    """Store a key over whatever is already there.

    Always prompts, even when a key is stored - which is the whole
    point. A key that was mistyped on the way in used to be permanent:
    discovery would 401, and running /connect again skipped the prompt
    because something was already in the store.
    """
    s = session.style
    llm = session.config.get("llm", {})
    name = (name or session.endpoint_name or "").lower()
    entry = known_endpoints().get(name)
    if entry is not None:
        base_url = entry.get("base_url", "")
        key_env = entry.get("api_key_env") or _derive_key_env(name)
    else:
        base_url = llm.get("base_url", "")
        if not base_url:
            session._print(s.yellow("  no endpoint to set a key for - /connect first"))
            return False
        name = _derive_name(base_url)
        key_env = _derive_key_env(name)

    if not provider_needs_key(base_url, key_env):
        session._print(s.dim(f"  {base_url} does not take a key"))
        return False
    if os.environ.get(key_env):
        # The environment wins over the store, so a file key cannot
        # rescue a bad value that came from a variable. Say so rather
        # than accepting a key that will never be used.
        session._print(s.yellow(f"  ${key_env} is set in this shell and wins over stored keys"))
        session._print(s.dim(f"  clear it with: set {key_env}=   then re-run /connect key"))

    stored = stored_keys().get(key_env, "")
    if stored:
        session._print(s.dim(f"  {name} is using {mask(stored)}"))
    key = _ask_secret(session, f"  new api key for {name} (hidden, blank cancels)> ").strip()
    if not key:
        session._print(s.dim("  cancelled"))
        return False
    store_key(key_env, key)
    session._print(s.dim(f"  key stored ({mask(key)})"))
    return True


def _connect_new(session: "ConsoleSession", url: str = "", key: str = "") -> bool:
    """Walk through adding an endpoint: URL, key, then pick a model."""
    s = session.style
    if not url:
        url = _read_choice(session, "  endpoint url (https://...)> ").strip()
    if not url:
        session._print(s.dim("  cancelled"))
        return False
    if "://" not in url:
        # Tolerate a host typed without a scheme rather than failing.
        # Anything that already carries a scheme is left alone, so a
        # wrong one is reported instead of being prefixed into nonsense.
        url = "https://" + url
    url = url.rstrip("/")
    problem = validate_endpoint({"base_url": url})
    if problem:
        session._print(s.red(f"  {problem}"))
        return False
    if not urlparse(url).hostname:
        session._print(s.red("  that does not look like a hostname"))
        return False

    name = _derive_name(url)
    key_env = _derive_key_env(name)

    if key:
        # An explicit key always replaces the stored one. Otherwise a
        # rejected key could never be corrected by re-running the same
        # command, which is the first thing anyone would try.
        store_key(key_env, key)
        session._print(s.dim(f"  key stored ({mask(key)})"))
    elif provider_needs_key(url, key_env) and not (
        os.environ.get(key_env) or has_stored(key_env)
    ):
        # Only ask when it is genuinely missing: one already in the
        # environment or the store is left alone, and /connect key is
        # the way to replace it.
        key = _ask_secret(session, f"  api key for {name} (hidden)> ")
        if not key:
            session._print(s.yellow("  no key given - skipping the fetch"))
            session._print(s.dim("  store one later with /connect key, or add one to"))
            session._print(s.dim(f"  {settings_path()}"))
            return False
        store_key(key_env, key)
        session._print(s.dim(f"  key stored ({mask(key)})"))

    try:
        add_endpoint(name, url, key_env, note=f"added {time.strftime('%Y-%m-%d')}")
    except ValueError as exc:
        session._print(s.red(f"  {exc}"))
        return False
    session._print(s.dim(f"  saved '{name}' → {url}"))
    if not session.use_endpoint(name):
        return False
    picked = _choose_model(session)
    # After the model menu: the header now names both the new endpoint
    # and the model the operator just chose.
    session.refresh_title()
    return picked


def _connect(session: "ConsoleSession", args: list[str]) -> bool:
    """Add or switch endpoints, then pick a model from the menu.

    Two values are enough from the operator - a base URL and a key. The
    catalogue comes from the endpoint itself, so no model name has to be
    typed or remembered.
    """
    if len(args) >= 2:
        # Scripted form: /connect <url> <key>
        return _connect_new(session, args[0], args[1])
    if len(args) == 1:
        if args[0] in ("list", "show"):
            session.show_endpoints()
            return True
        if args[0] in ("remove", "forget", "delete"):
            eps = sorted(known_endpoints().keys())
            if not eps:
                session._print(session.style.dim("  no endpoints to remove"))
            else:
                choice = _menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url","")) for n in eps])
                if choice:
                    _connect_remove(session, choice)
            return True
        # A saved endpoint's own name means "switch to it". Saved names
        # never contain a dot or a slash, so an exact match cannot be a
        # host someone meant to add - and without this, `/connect groq`
        # would quietly invent https://groq and ask for a key.
        saved = known_endpoints().get(args[0].lower())
        if saved:
            return session.use_endpoint(args[0].lower())
        return _connect_new(session, args[0])

    choice = _connect_choose_endpoint(session)
    if not choice:
        return False
    if choice == NEW_ENDPOINT:
        return _connect_new(session)
    if not session.use_endpoint(choice):
        return False
    # Refresh info bar after endpoint change.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()
    return _choose_model(session)


def _connect_remove(session: "ConsoleSession", name: str) -> None:
    if remove_endpoint(name.lower()):
        session._print(session.style.dim(f"  removed '{name}'"))
    else:
        session._print(session.style.yellow(f"  no endpoint named '{name}'"))


def show_keys(session: "ConsoleSession") -> None:
    """List stored keys by masked value only - never the key itself."""
    known = stored_keys()
    if not known:
        session._print(session.style.dim("  no keys stored yet"))
        session._print(session.style.dim("  /connect stores one when you add an endpoint"))
        return
    session._print(session.style.bold("  stored keys"))
    for name in sorted(known):
        session._print(f"  {name:<24} {session.style.dim(mask(known[name]))}")


def _read_one() -> str:
    """One keystroke, without waiting for a line."""
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch()
    return sys.stdin.read(1)


def _read_secret(
    prompt_text: str,
    closer: Callable[[int], None] | None = None,
) -> str:
    """Read a key without echoing it, and own the newline it ends with.

    ``getpass`` is deliberately not used: it writes to a stream of its
    own choosing and decides for itself whether to end the line, which
    leaves the frame unable to close the row the key was typed on. That
    matters more than it sounds - this is the prompt someone reaches
    for when a key was rejected, so it has to look right.

    Returns "" when there is no terminal to read from, so a piped run
    answers nothing and moves on instead of blocking.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    chars: list[str] = []
    try:
        with raw_mode():
            while True:
                char = _read_one()
                if char in ("\r", "\n"):
                    break
                if char == "\x03":  # ctrl+c
                    raise KeyboardInterrupt
                if char == "\x04":  # ctrl+d on an empty answer cancels
                    if not chars:
                        break
                    continue
                if char in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                elif len(char) == 1 and char >= " ":
                    chars.append(char)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ""
    if closer is not None:
        closer(visible_len(prompt_text) + len(chars))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars).strip()


def _ask_secret(session: "ConsoleSession", label: str) -> str:
    """Read a key for ``session``, closing the frame row it was typed on.

    The label goes through :meth:`ConsoleSession.prompt_text` so the
    prompt carries the frame's left border, and the row is closed with
    the same column count the editor uses.
    """
    return _read_secret(session.prompt_text(label), closer=session.close_prompt)


def _derive_name(url: str) -> str:
    """A short handle for an endpoint, e.g. ``https://api.openai.com/v1``.

    Strips the ``api.`` and ``www.`` prefixes and the port, then keeps
    the first label: ``api.openai.com`` -> ``openai``. Used both as the
    saved provider name and as the seed for the env var name.
    """
    host = urlparse(url).hostname or ""
    host = host.lower()
    for prefix in ("api.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split(":")[0]
    label = host.split(".")[0] if host else ""
    return re.sub(r"[^a-z0-9]+", "", label) or "endpoint"


def _derive_key_env(name: str) -> str:
    """``openai`` -> ``OPENAI_API_KEY``."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_API_KEY"



def _needs_first_run(session: "ConsoleSession") -> bool:
    """True when no usable credential is configured yet.

    Used to walk a first-time operator through setup instead of greeting
    them with a dashboard that cannot reach anything.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    key_env = llm.get("api_key_env") or ""
    if not base_url:
        return True
    if provider_needs_key(base_url, key_env):
        return not (os.environ.get(key_env) or has_stored(key_env))
    return False


def dispatch(session: ConsoleSession, line: str) -> bool:
    """Run a slash command. Returns True when the line was a command."""
    if not line.startswith("/"):
        return False
    parts = line.split(None, 1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/exit", "/quit"):
        session._print("bye")
        raise SystemExit(0)
    if command in ("/help", "/"):
        session._print(HELP_TEXT)
    elif command == "/workspace":
        session.show_workspace()
    elif command == "/memory":
        mem = session.memory_path
        session._print(f"memory file: {mem}")
        session._print(open(mem, encoding="utf-8", errors="replace").read() if os.path.isfile(mem) else "(empty)")
    elif command == "/diff":
        session.show_diff()
    elif command == "/undo":
        session.undo_changes()
    elif command == "/tools":
        for tool in sorted(session.tools, key=lambda t: t.name):
            session._print(f"  {tool.name}")
    elif command == "/model":
        parts = argument.split()
        if not parts:
            # No argument: the menu. Effort is part of the same choice,
            # so /reasoning is not a separate stop any more.
            if not _choose_model(session):
                llm = session.config.get("llm", {})
                session._print(f"model      {llm.get('model', '?')}")
                session._print(f"endpoint   {llm.get('base_url', '?')}")
        elif len(parts) >= 2:
            # "/model gpt-5 high" sets both in one go.
            _apply_model(session, parts[0], parts[1])
        else:
            _apply_model(session, parts[0], _pick_effort(session, parts[0]))
    elif command in ("/reasoning", "/effort"):
        # Reasoning is a property of the model, so this is now the model
        # menu. Kept as an alias so muscle memory still lands somewhere.
        if argument:
            session.set_reasoning(argument)
        elif not _choose_model(session):
            session.show_reasoning()
    elif command in ("/connect", "/setup", "/login", "/endpoint", "/endpoints"):
        args = argument.split()
        if args and args[0] in ("remove", "forget", "delete"):
            if len(args) >= 2:
                _connect_remove(session, args[1])
            else:
                eps = sorted(known_endpoints().keys())
                if not eps:
                    session._print(session.style.dim("  no endpoints to remove"))
                else:
                    choice = _menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url","")) for n in eps])
                    if choice:
                        _connect_remove(session, choice)
        elif args and args[0] == "keys":
            show_keys(session)
        elif args and args[0] == "key":
            if len(args) >= 2:
                _replace_key(session, args[1])
            else:
                eps = sorted(known_endpoints().keys())
                if not eps:
                    session._print(session.style.dim("  no endpoints yet - add one with /connect"))
                else:
                    choice = _menu(session, "Replace key for", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url","")) for n in eps])
                    if choice:
                        _replace_key(session, choice)
                    else:
                        _replace_key(session, "")
        else:
            _connect(session, args)
    elif command == "/approve":
        if not argument:
            # Modes are a fixed list, so they get a menu too.
            chosen = _menu(
                session,
                "approval mode",
                [Option(value=m, hint="current" if m == session.approvals.mode else "")
                 for m in MODES],
                allow_filter=False,
            )
            if chosen and chosen in MODES:
                session.approvals.mode = chosen
                session.approvals.reset_session()
                session._print(f"approval mode is now {chosen}")
                if session.layout is not None and session.layout.active:
                    session.layout.draw_chrome()
            else:
                session._print(f"approval mode: {session.approvals.mode}  (choose: {'/'.join(MODES)})")
        elif argument in MODES:
            session.approvals.mode = argument
            session.approvals.reset_session()
            session._print(f"approval mode is now {argument}")
            if session.layout is not None and session.layout.active:
                session.layout.draw_chrome()
        else:
            session._print(f"unknown mode '{argument}'; choose one of {'/'.join(MODES)}")
    elif command == "/cost":
        arg = argument.strip().lower()
        session.show_cost(
            compact=arg in ("compact", "brief", "c", "--compact"),
            as_json=arg in ("json", "j", "--json"),
        )
    elif command in ("/dashboard", "/dash"):
        # Compact: just re-show startup card
        if not session.show_dashboard():
            session._print(session.style.dim("  (compact card renders on a tty)"))
    elif command == "/compact":
        if not session.compact():
            session._print("nothing to compact")
    elif command == "/clear":
        session.context.replace_body([])
        session.reported_changes.clear()
        if session.layout is not None and session.layout.active:
            session.layout.clear_content()
        session._print("conversation cleared (files kept)")
    elif command == "/reset":
        session.message_count = 0
        session.context.replace_body([])
        session.reported_changes.clear()
        if session.layout is not None and session.layout.active:
            session.layout.clear_content()
        session._print("conversation reset (files kept)")
    elif command == "/save":
        path = argument or os.path.join(session.workspace, ".mantra", "session.json")
        session.save_session(path)
    elif command == "/load":
        if not argument:
            session._print("usage: /load <path>")
        else:
            session.load_session(argument)
    elif command == "/resume":
        parts = argument.split() if argument else []
        if not parts:
            session.pick_session()
        elif parts[0] in ("list", "show"):
            session.show_sessions()
        else:
            session.resume_session(parts[0])
    elif command == "/goal":
        _goal(session, argument)
    elif command == "/workflow":
        _workflow(session, argument)
    elif command == "/skills":
        _skills(session, argument)
    elif command == "/paste":
        text = _read_multiline(session)
        if text:
            session.handle(text)
    elif command == "/steps":
        if argument:
            try:
                session.max_steps = max(1, int(argument))
                session._print(f"step limit is now {session.max_steps}")
            except ValueError:
                session._print("usage: /steps <number>")
        else:
            session._print(session.max_steps)
    elif command == "/verbose":
        session.verbose = not session.verbose
        session._print(f"verbose {'on' if session.verbose else 'off'}")
    else:
        session._print(f"unknown command '{command}' - /help for the list")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mantra-console", description="MANTRA interactive console")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "examples", "config.json"))
    parser.add_argument("--workspace", default=None, help="Persistent working folder (default: <project>/workspace)")
    parser.add_argument("--once", default=None, metavar="MSG", help="Handle one message non-interactively, then exit")
    parser.add_argument("--model", default=None, help="Override the configured model")
    parser.add_argument("--reasoning", default=None, metavar="LEVEL",
                        help=f"Thinking effort: {', '.join(REASONING_EFFORTS)}, or off")
    parser.add_argument("--endpoint", default=None, metavar="URL",
                        help="Override the endpoint base URL for this run")
    parser.add_argument("--approve", default=None, choices=list(MODES), help="Override the approval mode")
    parser.add_argument("--plain", action="store_true", help="Disable ANSI styling")
    parser.add_argument("--compact", action="store_true", help="Compact TUI (now default, flag kept for compat)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    # Apply the user's saved active pick so the startup check and header
    # reflect the chosen endpoint instead of the example default.
    try:
        act = get_active()
        ep_name = (act.get("endpoint") or "").strip()
        if ep_name and not args.endpoint:
            ep = known_endpoints().get(ep_name.lower())
            if ep and ep.get("base_url"):
                llm_cfg = config.setdefault("llm", {})
                llm_cfg["base_url"] = ep["base_url"]
                if ep.get("api_key_env"):
                    llm_cfg["api_key_env"] = ep["api_key_env"]
                if not args.model and act.get("model"):
                    llm_cfg["model"] = act["model"]
                if not args.reasoning and "reasoning_effort" in act:
                    llm_cfg["reasoning_effort"] = act["reasoning_effort"]
    except Exception:
        pass
    if args.endpoint:
        url = args.endpoint.rstrip("/")
        if not url.startswith(("http://", "https://")):
            parser.error("--endpoint must start with http:// or https://")
        llm = config.setdefault("llm", {})
        llm["base_url"] = url
        llm["api_key_env"] = llm.get("api_key_env") or _derive_key_env(_derive_name(url))
    if args.model:
        config.setdefault("llm", {})["model"] = args.model
    if args.reasoning:
        level = args.reasoning.strip().lower()
        config.setdefault("llm", {})["reasoning_effort"] = (
            None if level in ("off", "none") else level
        )
        if level not in ("off", "none") and level not in REASONING_EFFORTS:
            parser.error(
                f"unknown reasoning level '{args.reasoning}'; "
                f"choose from {', '.join(REASONING_EFFORTS)} or off"
            )
    if args.approve:
        config["approvals"] = args.approve

    style = Style(enabled=not args.plain)
    workspace = args.workspace or _infer_workspace()
    session = ConsoleSession(config, workspace, style)

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    needs_setup = interactive and _needs_first_run(session)

    if args.once is not None:
        session._warn_if_key_missing()
        session.handle(args.once)
        return 0

    if needs_setup:
        # Nothing usable is configured, so a dashboard would greet the
        # operator with an endpoint that cannot answer. Set up first -
        # and set up *before* the shell opens, because setup is what
        # decides the endpoint and model the header names. A header is
        # the one row an append-only terminal cannot go back and fix
        # once it has scrolled, so it has to be right the first time.
        session._print(style.dim("no endpoint configured yet - let's connect one."))
        _connect(session, [])
        session._print("")

    if interactive:
        session._compact = True
        session._warn_if_key_missing()
        layout = compact.CompactLayout()
        layout.enter()
        session.layout = layout
        layout.setup(0, session, style)
        layout.show_splash()
        # Prompt is owned exclusively by LineEditor — not drawn here to avoid duplication
    else:
        session.banner()
    try:
        repl(session, style)
    finally:
        # Always close: a shell left open would leave the operator
        # typing at a prompt with no bottom edge.
        if session.layout is not None and session.layout.active:
            session.layout.cleanup()
        session.close_frame()
    return 0


def repl(session: ConsoleSession, style: Style, reader: Any = None) -> None:
    """The read-eval-print loop. Split out from main so it can be tested."""
    fixed_bottom = session.layout is not None and session.layout.active

    if reader is None:
        is_compact = bool(getattr(session, "_compact", False))

        def _ctrl_g_handler() -> None:
            if fixed_bottom and session.layout is not None:
                session.layout.render_content()
                session.layout.draw_chrome()

        def _page_up() -> None:
            # Dismiss popup before scrolling to avoid mispositioning.
            editor._dismissed = True
            if session.layout is not None and session.layout.active:
                session.layout.scroll_up(3)

        def _page_down() -> None:
            editor._dismissed = True
            if session.layout is not None and session.layout.active:
                session.layout.scroll_down(3)

        editor = LineEditor(
            style,
            completer=ConsoleCompleter(session),
            no_popup=False,
            popup_above=fixed_bottom,
            on_ctrl_g=_ctrl_g_handler,
            on_submit=session.close_prompt if not fixed_bottom else None,
            on_page_up=_page_up if fixed_bottom else None,
            on_page_down=_page_down if fixed_bottom else None,
        )

        if fixed_bottom and session.layout is not None:
            editor.on_before_draw = lambda count=0: session.layout.restore_popup_rows(count)

            def _resize_prompt() -> str | None:
                if session.layout is None:
                    return None
                changed = session.layout.check_resize()
                if changed:
                    content_height = session.layout.content_bottom - session.layout.content_top + 1
                    editor.max_popup = max(1, min(8, content_height))
                    editor.fixed_row = session.layout.prompt_row
                    return session.prompt_text()
                return None

            editor.on_resize = _resize_prompt
            editor.fixed_row = session.layout.prompt_row

        reader = editor.read

    while True:
        # Recompute fixed_bottom each iteration (layout may activate/deactivate).
        fixed_bottom = session.layout is not None and session.layout.active

        try:
            if fixed_bottom and session.layout is not None:
                session.layout.check_resize()
                editor.fixed_row = session.layout.prompt_row

            if fixed_bottom:
                line = reader(session.prompt_text(), skip_newline=True).strip()
                # Prompt owned by LineEditor — layout does not redraw here
            elif session.frame is not None:
                session.frame.row("")
                line = reader(session.prompt_text()).strip()
            else:
                line = reader(f"\n{session.prompt_text()}").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if not line:
            continue
        session._prompt_sent_at = time.time()
        try:
            if fixed_bottom:
                session.layout.move_to_content()
            ts = time.strftime("%H:%M", time.localtime(session._prompt_sent_at))
            session._print(f"{session.style.on_grey(' ' + ts + ' ')} {session.style.bold(line)}")
            if not dispatch(session, line):
                session.handle(line)
            # The next reader() call draws the fixed prompt.
        except SystemExit:
            return


if __name__ == "__main__":
    sys.exit(main())
