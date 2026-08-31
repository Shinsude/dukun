"""ConsoleCompleter, main entry point, and REPL loop.

Extracted from console.py to reduce the monolithic file size.
Uses lazy imports from ``mantra.console`` to avoid circular dependencies.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from mantra.line_editor import Completion, LineEditor
from mantra.core.models import is_reasoning_model
import mantra.core.skills as skills
import mantra.core.workflows as workflows
from mantra.core.settings import (
    active as get_active,
    endpoints as known_endpoints,
)
from mantra.config import REASONING_EFFORTS, load_config
from mantra.core.approvals import MODES
from mantra.ui.rendering import Style
from mantra.ui.session import (
    ConsoleSession,
    _derive_key_env,
    _derive_name,
    _mask_line,
    _needs_first_run,
)
import mantra.compact as compact


MAX_INDEX_ENTRIES = 4000

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode",
}

_SAFE_HOME = os.path.expanduser("~")

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


class ConsoleCompleter:
    """Suggests slash commands after ``/`` and workspace paths after ``@``."""

    def __init__(self, session: ConsoleSession) -> None:
        self.session = session
        self._entries: list[str] = []
        self._indexed = False
        self._cache_root = ""
        self._cache_time = 0.0

    def begin(self) -> None:
        """Re-index the workspace once per prompt, not once per keystroke."""
        root = os.path.abspath(self.session.sandbox.root)
        now = time.monotonic()
        if self._indexed and root == self._cache_root and (now - self._cache_time) < 0.5:
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
        if not self._indexed:
            self.begin()
        cursor = max(0, min(cursor, len(buffer)))
        start = cursor
        while start > 0 and not buffer[start - 1].isspace():
            start -= 1
        token = buffer[start:cursor]
        if token.startswith("@"):
            return self._complete_path(start, cursor, token[1:])
        if token.startswith("/") and buffer[:start].lstrip("\ufeff\u200b\u00a0").strip() == "":
            return self._complete_command(cursor, token, start)
        stripped = buffer.lstrip()
        if stripped.startswith("/model "):
            return self._complete_model(cursor, token)
        if stripped.startswith("/connect"):
            c = self._complete_connect(buffer, start, cursor, token)
            if c:
                return c
        if stripped.startswith("/skills"):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if stripped.startswith("/workflow"):
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

    def _complete_command(self, cursor: int, token: str, start: int = 0):
        from mantra.console import SLASH_COMMANDS
        matches = [name for name, _ in SLASH_COMMANDS if name.startswith(token)]
        if not matches:
            return None
        labels = []
        for name in matches:
            description = next((d for n, d in SLASH_COMMANDS if n == name), "")
            labels.append(f"{name}  {description}")
        return Completion(items=matches, start=start, end=cursor, labels=labels)

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


def _infer_workspace() -> str:
    """Current directory becomes the workspace, like a real agent CLI."""
    cwd = os.getcwd()
    if cwd == PROJECT_ROOT:
        return os.path.join(PROJECT_ROOT, "workspace")
    protected = {
        os.path.dirname(_SAFE_HOME.rstrip("\\/")) or _SAFE_HOME,
        _SAFE_HOME,
        os.path.splitdrive(cwd)[0] + "\\",
    }
    normalized = cwd.rstrip("\\/")
    if any(normalized.lower() == p.lower().rstrip("\\/") for p in protected):
        return os.path.join(PROJECT_ROOT, "workspace")
    return cwd


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
        from mantra.console import _connect
        try:
            session._print(style.dim("no endpoint configured yet - let's connect one."))
            _connect(session, [])
            session._print("")
        except (KeyboardInterrupt, EOFError):
            session._print(style.dim("  setup skipped — run /connect to add an endpoint later"))
        except Exception:
            pass

    if interactive:
        session._compact = True
        session._warn_if_key_missing()
        layout = compact.CompactLayout()
        layout.enter()
        session.layout = layout
        layout.setup(0, session, style)
        layout.show_splash()
    else:
        session.banner()
    try:
        repl(session, style)
    finally:
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
                with session._pause_spinner():
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
        fixed_bottom = session.layout is not None and session.layout.active

        try:
            if fixed_bottom and session.layout is not None:
                session.layout.check_resize()
                editor.fixed_row = session.layout.prompt_row

            if fixed_bottom:
                line = reader(session.prompt_text(), skip_newline=True).strip("\ufeff\u200b\u00a0 \t\r\n")
            elif session.frame is not None:
                session.frame.row("")
                line = reader(session.prompt_text()).strip("\ufeff\u200b\u00a0 \t\r\n")
            else:
                line = reader(f"\n{session.prompt_text()}").strip("\ufeff\u200b\u00a0 \t\r\n")
        except (KeyboardInterrupt, EOFError):
            return
        if not line:
            continue
        session._prompt_sent_at = time.time()
        try:
            if fixed_bottom:
                session.layout.move_to_content()
            ts = time.strftime("%H:%M", time.localtime(session._prompt_sent_at))
            display = _mask_line(line)
            session._print(f"{session.style.on_grey(' ' + ts + ' ')} {session.style.bold(display)}")
            from mantra.console import dispatch
            if not dispatch(session, line):
                session.handle(line)
        except SystemExit:
            return
