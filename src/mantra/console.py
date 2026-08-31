"""Interactive console: ANSI, spinner, markdown, streaming. Stdlib only.

Thin facade: dispatch, _menu, SLASH_COMMANDS, HELP_TEXT and the public
API stay here.  Bulk implementation lives in ``mantra.ui.*``.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from mantra.config import REASONING_EFFORTS
from mantra.core.agent_loop import AgentLoop, RunResult, DEFAULT_SYSTEM_PROMPT  # noqa: F401
from mantra.core.approvals import MODES
from mantra.core.keys import mask, store as store_key, stored_keys
from mantra.core.menu import Option, choose
from mantra.core.models import fetch_models
import mantra.core.sessions as sessions
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
from mantra.line_editor import Completion, LineEditor

# ---------------------------------------------------------------------------
# Re-exports — public API that callers import from mantra.console
# ---------------------------------------------------------------------------
from mantra.ui.rendering import (  # noqa: F401
    Style,
    StreamingRenderer,
    Spinner,
    render_markdown,
)
from mantra.ui.session import (  # noqa: F401
    ConsoleSession,
    _apply_model,
    _choose_model,
    _connect_choose_endpoint,
    _connect_new,
    _derive_key_env,
    _derive_name,
    _effort_options,
    _endpoint_options,
    _is_auth_failure,
    _mask_line,
    _model_options,
    _needs_first_run,
    _pick_effort,
    _read_choice,
    _read_multiline,
    _read_one,
    _read_secret,
    _replace_key,
    _rescue_catalogue,
    _short,
    _short_endpoint,
    _transcript,
    _try_fetch,
    _type_a_model,
    provider_needs_key,
    TYPE_A_MODEL,
    RE_ENTER_KEY,
    SWITCH_ENDPOINT,
    NEW_ENDPOINT,
    MENTION_RE,
    MAX_ATTACH_CHARS,
    MAX_TOTAL_ATTACH_CHARS,
    MAX_GLOB_HITS,
    MAX_LISTING_ENTRIES,
    MAX_INDEX_ENTRIES,
)
from mantra.ui.session import _ask_secret  # noqa: F401
from mantra.ui.session import _is_safe_session_path  # noqa: F401
from mantra.ui.session import _format_elapsed  # noqa: F401
from mantra.ui.session import _skills  # noqa: F401
from mantra.ui.session import _skills_auto  # noqa: F401
from mantra.ui.session import _skills_bundles  # noqa: F401
from mantra.ui.session import _skills_find  # noqa: F401
from mantra.ui.session import _skills_launch  # noqa: F401
from mantra.ui.session import _skills_list  # noqa: F401
from mantra.ui.session import _skills_show  # noqa: F401
from mantra.ui.session import _skills_use  # noqa: F401
from mantra.ui.session import _goal  # noqa: F401
from mantra.ui.session import _workflow  # noqa: F401
from mantra.ui.session import _workflow_create  # noqa: F401
from mantra.ui.session import _workflow_launch  # noqa: F401
from mantra.ui.session import _workflow_remove  # noqa: F401
from mantra.ui.session import _workflow_show  # noqa: F401
from mantra.ui.app import (  # noqa: F401
    ConsoleCompleter,
    main,
    repl,
    _infer_workspace,
)


# ---------------------------------------------------------------------------
# HELP_TEXT
# ---------------------------------------------------------------------------
HELP_TEXT = """Commands:
  /connect              add or switch endpoint — just a URL and key, then
                        pick from the models it serves. Nothing else to
                        configure. /connect list, /connect remove <name>
                        /connect <url> [key] [model]  (all inline)
  /connect key [name]   replace the stored key. Always asks, so a key that
                        was mistyped can be put right
  /model                pick a model from a menu (filters as you type).
                        Direct: /model <name>  or  /model <name> <effort>
                        effort: off|minimal|low|medium|high|xhigh · /model help for details
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


# ---------------------------------------------------------------------------
# SLASH_COMMANDS — tab-completion and /help reference
# ---------------------------------------------------------------------------
SLASH_COMMANDS = [
    ("/connect", "add or switch endpoint — /connect <url> [key] [model], list, remove, key"),
    ("/model", "pick a model — /model <name> [effort], or menu"),
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


# ---------------------------------------------------------------------------
# _menu — must live here because tests mock ``console._menu``
# ---------------------------------------------------------------------------
def _menu(
    session: ConsoleSession,
    title: str,
    options: list[Any],
    hint: str = "",
    allow_filter: bool = True,
    cursor: int = 0,
    allow_delete: bool = False,
    on_delete: Any = None,
) -> str | None:
    """Open a selectable menu and return the chosen value.

    Returns None when cancelled, when nothing matched, or when there is
    no terminal to draw on. Callers treat None as "no change".
    """
    if not options:
        return None
    if allow_delete:
        hint = (hint + " · d deletes") if hint else "up/down · enter selects · esc cancels · d deletes"
    else:
        hint = hint or "up/down or click · enter selects · esc cancels"
    chosen = choose(
        session.style,
        title,
        options,
        hint=hint,
        allow_filter=allow_filter,
        cursor=cursor,
        frame=session.frame,
        allow_delete=allow_delete,
        on_delete=on_delete,
    )
    return chosen or None


# ---------------------------------------------------------------------------
# _connect / _connect_remove / show_keys — used by dispatch
# ---------------------------------------------------------------------------
def _connect(session: ConsoleSession, args: list[str]) -> bool:
    """Add or switch endpoints, then pick a model from the menu."""
    if args and args[0].lower() in ("help", "-h", "--help", "?", "h"):
        s = session.style
        session._print(s.bold("  /connect — add or switch endpoint"))
        session._print(s.dim("  usage:"))
        session._print("    /connect                         — pick from saved or add new")
        session._print("    /connect <url>                   — add endpoint, then pick model")
        session._print("    /connect <url> <key>             — add with key, then pick model")
        session._print("    /connect <url> <key> <model>     — add and set model directly")
        session._print("    /connect <name>                  — switch to saved endpoint")
        session._print("    /connect list                    — show saved endpoints")
        session._print("    /connect remove <name>           — delete endpoint")
        session._print("    /connect key [name]              — replace stored key")
        session._print(s.dim("  examples:"))
        session._print("    /connect https://api.openai.com/v1 sk-...")
        session._print("    /connect https://api.meta.ai/v1")
        session._print("    /connect groq")
        return True
    if args and args[0].lower() in ("remove", "forget", "delete", "rm"):
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
        return True
    if args and args[0].lower() in ("key", "keys"):
        if len(args) >= 3:
            store_key(_derive_key_env(args[1].lower()), args[2])
            session._print(session.style.dim(f"  key stored for {args[1].lower()} ({args[2][:4]}…{args[2][-4:]})"))
            return True
        if len(args) == 2:
            return _replace_key(session, args[1])
        return _replace_key(session, "")
    if len(args) >= 3:
        return _connect_new(session, args[0], args[1], args[2])
    if len(args) >= 2:
        return _connect_new(session, args[0], args[1])
    if len(args) == 1:
        if args[0].lower() in ("list", "show"):
            session.show_endpoints()
            return True
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
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()
    return _choose_model(session)


def _connect_remove(session: ConsoleSession, name: str) -> None:
    if remove_endpoint(name.lower()):
        session._print(session.style.dim(f"  removed '{name}'"))
    else:
        session._print(session.style.yellow(f"  no endpoint named '{name}'"))


def show_keys(session: ConsoleSession) -> None:
    """List stored keys by masked value only - never the key itself."""
    known = stored_keys()
    if not known:
        session._print(session.style.dim("  no keys stored yet"))
        session._print(session.style.dim("  /connect stores one when you add an endpoint"))
        return
    session._print(session.style.bold("  stored keys"))
    for name in sorted(known):
        session._print(f"  {name:<24} {session.style.dim(mask(known[name]))}")


# ---------------------------------------------------------------------------
# dispatch — the slash-command router
# ---------------------------------------------------------------------------
def dispatch(session: ConsoleSession, line: str) -> bool:
    """Run a slash command. Returns True when the line was a command."""
    stripped = line.lstrip("\ufeff\u200b\u00a0 \t\r\n")
    if not stripped.startswith("/"):
        return False
    parts = stripped.split(None, 1)
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
        if not os.path.isfile(mem):
            session._print("(empty)")
        else:
            try:
                if os.path.getsize(mem) > 20000:
                    with open(mem, encoding="utf-8", errors="replace") as h:
                        data = h.read(8000) + "\n... [truncated]"
                else:
                    with open(mem, encoding="utf-8", errors="replace") as h:
                        data = h.read()
                session._print(data or "(empty)")
            except OSError as exc:
                session._print(session.style.red(f"  cannot read memory: {exc}"))
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
            if not _choose_model(session):
                llm = session.config.get("llm", {})
                session._print(f"model      {llm.get('model', '?')}")
                session._print(f"endpoint   {llm.get('base_url', '?')}")
        elif parts[0].lower() in ("help", "-h", "--help", "?", "h"):
            s = session.style
            session._print(s.bold("  /model — pick a model"))
            session._print(s.dim("  usage:"))
            session._print("    /model                       — pick from endpoint catalogue")
            session._print("    /model <name>                — switch to model directly")
            session._print("    /model <name> <effort>       — switch and set reasoning")
            session._print("    /model help                  — show this help")
            session._print(s.dim("  effort: off | minimal | low | medium | high | xhigh"))
            session._print(s.dim("  examples:"))
            session._print("    /model gpt-4o")
            session._print("    /model gpt-5 high")
            session._print("    /model meta-llama-3")
        elif len(parts) >= 2:
            _apply_model(session, parts[0], parts[1])
        else:
            _apply_model(session, parts[0], _pick_effort(session, parts[0]))
    elif command in ("/reasoning", "/effort"):
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
        if not session.show_dashboard():
            session._print(session.style.dim("  (compact card renders on a tty)"))
    elif command == "/compact":
        if not session.compact():
            session._print("nothing to compact")
    elif command == "/clear":
        session.context.replace_body([])
        session.reported_changes.clear()
        session.goal = ""
        session.goal_notes = []
        session.active_skills = []
        session.auto_attached = []
        if session.layout is not None and session.layout.active:
            session.layout.clear_content()
        session._print("conversation cleared (files kept)")
    elif command == "/reset":
        session.message_count = 0
        session.context.replace_body([])
        session.reported_changes.clear()
        session.goal = ""
        session.goal_notes = []
        session.active_skills = []
        session.auto_attached = []
        session.approvals.reset_session()
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
                val = int(argument)
                if val < 1:
                    val = 1
                elif val > 100:
                    session._print(session.style.yellow("  step limit capped at 100"))
                    val = 100
                session.max_steps = val
                session._print(f"step limit is now {session.max_steps}")
            except ValueError:
                session._print("usage: /steps <number> (1-100)")
        else:
            session._print(session.max_steps)
    elif command == "/verbose":
        session.verbose = not session.verbose
        session._print(f"verbose {'on' if session.verbose else 'off'}")
    else:
        session._print(f"unknown command '{command}' - /help for the list")
    return True


if __name__ == "__main__":
    sys.exit(main())
