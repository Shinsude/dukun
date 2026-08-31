"""ConsoleSession and associated helper functions.

Extracted from console.py to reduce the monolithic file size.
The session class owns workspace, sandbox, context, tools, LLM client,
approvals, and event bus for one interactive REPL session.
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any

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
    add_endpoint,
    active as get_active,
    endpoint_name_for_url,
    endpoints as known_endpoints,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    settings_path,
    validate_endpoint,
)
from mantra.core.knowledge import (
    append_memory,
    assemble_system_prompt,
    find_instructions_file,
    render_environment,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KNOWN_FAILURES_PATH = os.path.join(PROJECT_ROOT, "knowledge", "known-failures.md")
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.core.keys import resolve as resolve_key
from mantra.registry import build_llm, build_tools
from mantra.ui.rendering import (
    Style,
    Spinner,
    StreamingRenderer,
    render_markdown,
)
from mantra.core.settings import skills_prefs, set_skills_prefs


MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_][\w.:/\\\-*]*)")
MAX_ATTACH_CHARS = 20_000
MAX_TOTAL_ATTACH_CHARS = 60_000
MAX_GLOB_HITS = 20
MAX_LISTING_ENTRIES = 100
MAX_INDEX_ENTRIES = 4000


def _short(count: int) -> str:
    """1234 -> 1.2k. Token counts only ever need two significant figures."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time for display."""
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
    """Host and path, without the scheme."""
    url = (base_url or "").strip()
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix):]
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


def _is_safe_session_path(path: str, workspace: str) -> bool:
    """Check if a session file path is inside allowed directories."""
    try:
        real = os.path.realpath(os.path.abspath(path))
        allowed = [
            os.path.realpath(workspace),
            os.path.realpath(os.path.join(workspace, ".mantra")),
            os.path.realpath(sessions.sessions_dir()),
            os.path.realpath(tempfile.gettempdir()),
            os.path.realpath(os.path.expanduser("~/.mantra")),
        ]
        try:
            allowed.append(os.path.realpath(os.path.join(PROJECT_ROOT, "workspace")))
        except Exception:
            pass
        for base in allowed:
            if real == base or real.startswith(base + os.sep):
                return True
        return False
    except Exception:
        return False


def _mask_line(line: str) -> str:
    """Mask secrets in a line before echoing."""
    stripped = line.strip()
    if stripped.lower().startswith("/connect"):
        parts = stripped.split()
        if len(parts) >= 3 and parts[1] not in ("list", "remove", "key", "keys", "show", "forget", "delete"):
            parts[-1] = "***"
            return " ".join(parts)
        if len(parts) >= 3 and parts[1] in ("key",):
            parts[-1] = "***"
            return " ".join(parts)
    return line


def _read_choice(session: ConsoleSession, prompt_text: str) -> str:
    """Read a line from the operator; empty when there is no terminal."""
    from mantra.line_editor import LineEditor
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


def _read_multiline(session: ConsoleSession) -> str:
    """Read several lines, ended by a lone dot."""
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


# ---------------------------------------------------------------------------
# Skills, goal, workflow helpers (called by dispatch)
# ---------------------------------------------------------------------------

def _skills(session: ConsoleSession, argument: str) -> None:
    """/skills: discover, read, attach and run skill bundles."""
    from mantra.console import _menu
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
        _skills_show(session, argument)


def _skills_list(session: ConsoleSession) -> None:
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


def _skills_show(session: ConsoleSession, name: str) -> None:
    from mantra.console import _menu
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
    for line in body.split("\n"):
        session._print("  " + line.rstrip())
    session._print("")
    session._print(session.style.dim(f"  /skills use {found.name} to attach it"))


def _skills_use(session: ConsoleSession, name: str) -> None:
    from mantra.console import _menu
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


def _skills_bundles(session: ConsoleSession) -> None:
    bundles = skills.load_bundles()
    if not bundles:
        session._print(session.style.dim("  no bundles found (no BUNDLES.md in any skills root)"))
        return
    session._print(session.style.bold(f"  bundles ({len(bundles)})"))
    for name, steps in sorted(bundles.items()):
        session._print(f"  {name:<16} {session.style.dim(' > '.join(steps))}")
    session._print("")
    session._print(session.style.dim("  /skills launch <bundle> to run one in order"))


def _skills_launch(session: ConsoleSession, name: str) -> RunResult | None:
    """Run a bundle as ordered steps, attaching each skill in turn."""
    from mantra.console import _menu
    if not name:
        bundles = skills.load_bundles()
        if not bundles:
            session._print(session.style.dim("  no bundles found"))
            return None
        choice = _menu(session, "Launch bundle", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
        if not choice:
            session._print(session.style.dim("  usage: /skills launch <bundle>"))
            return None
        name = choice
    steps = skills.get_bundle(name)
    if steps is None:
        bundles = skills.load_bundles()
        if bundles:
            choice = _menu(session, f"Bundle '{name}' not found", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
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
        session.active_skills = previous
    session._print("")
    session._print(session.style.dim(f"  bundle '{name}' finished"))
    return last


def _skills_auto(session: ConsoleSession, argument: str) -> None:
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
    if not auto:
        session._detach_auto()
    session._print(session.style.dim(f"  skill auto-routing {'on' if auto else 'off'}"))


def _skills_find(session: ConsoleSession, query: str) -> None:
    from mantra.console import _menu
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


def _goal(session: ConsoleSession, argument: str) -> None:
    """/goal: the standing objective the whole session is working toward."""
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
        session.set_goal(argument)


def _workflow(session: ConsoleSession, argument: str) -> None:
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


def _workflow_show(session: ConsoleSession, name: str) -> None:
    from mantra.console import _menu
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


def _workflow_create(session: ConsoleSession, name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /workflow create <name>"))
        return
    session._print(
        session.style.dim(f"  steps for '{workflows.slug(name)}', one per line, . to finish:")
    )
    from mantra.console import _read_multiline
    raw = _read_multiline(session)
    steps = [line.strip() for line in raw.split("\n") if line.strip()]
    ok, message = workflows.create(name, steps)
    colour = session.style.dim if ok else session.style.red
    session._print(f"  {colour(message)}")


def _workflow_launch(session: ConsoleSession, name: str) -> None:
    from mantra.console import _menu
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


def _workflow_remove(session: ConsoleSession, name: str) -> None:
    from mantra.console import _menu
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


# ---------------------------------------------------------------------------
# Endpoint / model helpers (called by dispatch, stay in this module)
# ---------------------------------------------------------------------------

KEYLESS_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


def provider_needs_key(base_url: str, api_key_env: str) -> bool:
    """False for local endpoints, which simply do not check a key."""
    if not api_key_env:
        return False
    lowered = (base_url or "").lower()
    return not any(host in lowered for host in KEYLESS_HOSTS)


def _model_options(session: ConsoleSession, models: list[str]) -> list[Option]:
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
    """Thinking levels, with the one in force marked."""
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


def _pick_effort(session: ConsoleSession, model: str) -> str | None:
    from mantra.console import _menu
    current = session.config.get("llm", {}).get("reasoning_effort") or "off"
    options = _effort_options(current)
    index = next((i for i, o in enumerate(options) if o.value == current), 0)
    title = f"reasoning effort for {model}"
    if not is_reasoning_model(model):
        title += session.style.dim(" (this model may ignore it)")
    chosen = _menu(session, title, options, allow_filter=False, cursor=index)
    return chosen or None


def _apply_model(session: ConsoleSession, model: str, effort: str | None = None) -> None:
    """Set a model and settle reasoning as a property of that model."""
    session.set_model(model, quiet=True)
    session.set_reasoning(effort or "off", quiet=True)
    effort_now = session.config["llm"].get("reasoning_effort") or "off"
    session._print(
        session.style.dim(
            f"  model is now {model} · reasoning {effort_now}"
        )
    )
    set_active(model=model, reasoning_effort=session.config["llm"].get("reasoning_effort"))
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()


def _try_fetch(session: ConsoleSession) -> tuple[list[str], Exception | None]:
    """Ask the endpoint what it serves."""
    from mantra.console import fetch_models
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    if not base_url:
        return [], None
    try:
        return fetch_models(base_url, llm.get("api_key_env")), None
    except HarnessError as exc:
        session._print(session.style.dim(f"  (could not list models: {exc})"))
        return [], exc


TYPE_A_MODEL = "+ type a model name"
RE_ENTER_KEY = "+ re-enter the api key"
SWITCH_ENDPOINT = "+ connect a different endpoint"


def _is_auth_failure(error: Exception | None) -> bool:
    return error is not None and "refused the key" in str(error)


def _rescue_catalogue(session: ConsoleSession, error: Exception | None) -> bool:
    from mantra.console import _menu
    s = session.style
    llm = session.config.get("llm", {})
    key_env = llm.get("api_key_env") or ""
    options: list[Option] = []
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
        from mantra.console import _connect
        return _connect(session, [])
    session._print(s.dim(f"  or add models by hand: {settings_path()}"))
    return False


def _type_a_model(session: ConsoleSession) -> bool:
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


def _choose_model(session: ConsoleSession) -> bool:
    from mantra.console import _menu
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    all_by_model: dict[str, str] = {}
    for ep_name, entry in known_endpoints().items():
        for m in entry.get("models", []) or []:
            if m not in all_by_model:
                all_by_model[m] = ep_name
    name = session.endpoint_name
    fetched, error = _try_fetch(session)
    if fetched:
        session.known_models = fetched
        if name:
            set_models(name, fetched)
        for m in fetched:
            if m not in all_by_model:
                all_by_model[m] = name or "current"
    elif not all_by_model:
        if not base_url:
            session._print(session.style.yellow("  no endpoint configured - /connect first"))
            return False
        return _rescue_catalogue(session, error)
    else:
        session.known_models = list(all_by_model.keys())

    current = llm.get("model", "")
    options: list[Option] = []
    for m, provider in sorted(all_by_model.items(), key=lambda x: x[0].lower()):
        hint = provider
        if m == current:
            hint = (hint + " · current").strip(" ·") if hint else "current"
        if is_reasoning_model(m):
            hint = (hint + " · thinks").strip(" ·") if hint else "thinks"
        options.append(Option(value=m, hint=hint))
    options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
    title = "models — all providers" if len(known_endpoints()) > 1 else f"models at {base_url}" if base_url else "models"

    def _on_delete_model(model: str) -> None:
        provider = all_by_model.get(model)
        if not provider:
            return
        entry = known_endpoints().get(provider)
        if not entry:
            return
        models = [m for m in entry.get("models", []) if m != model]
        set_models(provider, models)
        session._print(session.style.dim(f"  removed model '{model}' from {provider}"))
        all_by_model.pop(model, None)

    chosen = _menu(session, title, options, allow_delete=True, on_delete=_on_delete_model)
    if not chosen:
        return False
    if chosen == TYPE_A_MODEL:
        return _type_a_model(session)
    provider = all_by_model.get(chosen)
    if provider and provider != name:
        session._print(session.style.dim(f"  switching to {provider} for {chosen}"))
        session.use_endpoint(provider)
    _apply_model(session, chosen, _pick_effort(session, chosen))
    return True


NEW_ENDPOINT = "+ add a new endpoint"


def _endpoint_options(session: ConsoleSession) -> list[Option]:
    known = known_endpoints()
    current = session.endpoint_name
    options = []
    for name in sorted(known):
        entry = known[name]
        hint = "current" if name == current else entry.get("base_url", "")
        options.append(Option(value=name, hint=hint))
    options.append(Option(value=NEW_ENDPOINT, hint=""))
    return options


def _connect_choose_endpoint(session: ConsoleSession) -> str | None:
    from mantra.console import _menu
    known = known_endpoints()
    if not known:
        return NEW_ENDPOINT

    def _on_delete(name: str) -> None:
        entry = known_endpoints().get(name.lower())
        key_env = entry.get("api_key_env") if entry else ""
        removed = remove_endpoint(name.lower())
        if removed:
            if key_env:
                still_used = any(
                    e.get("api_key_env") == key_env
                    for n, e in known_endpoints().items()
                    if n != name.lower()
                )
                if not still_used:
                    try:
                        from mantra.core.keys import remove as remove_key
                        remove_key(key_env)
                    except Exception:
                        pass
            session._print(session.style.dim(f"  removed '{name}' (+ key {key_env})"))

    return _menu(
        session,
        "endpoints",
        _endpoint_options(session),
        allow_filter=False,
        allow_delete=True,
        on_delete=_on_delete,
    )


def _replace_key(session: ConsoleSession, name: str = "") -> bool:
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
        session._print(s.yellow(f"  ${key_env} is set in this shell and wins over stored keys"))
        session._print(s.dim(f"  clear it with: set {key_env}=   then re-run /connect key"))

    stored = stored_keys().get(key_env, "")
    if stored:
        session._print(s.dim(f"  {name} is using {mask(stored)}"))
    try:
        from mantra.console import _read_secret
        key = _read_secret(session.prompt_text(f"  new api key for {name} (visible, blank cancels)> "), closer=session.close_prompt).strip()
        if not key:
            alt = _read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
            if alt:
                key = alt
    except Exception:
        key = _read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
    if not key:
        session._print(s.dim("  cancelled"))
        return False
    store_key(key_env, key)
    session._print(s.dim(f"  key stored ({mask(key)})"))
    return True


def _connect_new(session: ConsoleSession, url: str = "", key: str = "", model: str = "") -> bool:
    s = session.style
    if not url:
        url = _read_choice(session, "  endpoint url (e.g. https://api.openai.com/v1)> ").strip()
        if not url:
            session._print(s.dim("  tip: paste a full URL, or try /connect list to see saved ones"))
            session._print(s.dim("  examples: /connect https://api.openai.com/v1  ·  /connect https://api.meta.ai/v1"))
            return False
    if not url:
        session._print(s.dim("  cancelled"))
        return False
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")
    problem = validate_endpoint({"base_url": url})
    if problem:
        session._print(s.red(f"  {problem}"))
        return False
    from urllib.parse import urlparse
    if not urlparse(url).hostname:
        session._print(s.red("  that does not look like a hostname"))
        return False

    name = _derive_name(url)
    key_env = _derive_key_env(name)

    if key:
        store_key(key_env, key)
        session._print(s.dim(f"  key stored ({mask(key)})"))
    elif provider_needs_key(url, key_env):
        existing = os.environ.get(key_env) or stored_keys().get(key_env, "")
        if existing:
            session._print(s.dim(f"  current key {mask(existing)} ({key_env}) — press enter to keep, or paste new"))
        key = _read_choice(session, f"  api key for {name}> ").strip()
        if key:
            store_key(key_env, key)
            if os.environ.get(key_env) and os.environ.get(key_env) != key:
                session._print(s.yellow(f"  note: ${key_env} env still set and wins until you restart shell"))
            session._print(s.dim(f"  key stored ({mask(key)})"))
        elif not existing:
            session._print(s.yellow("  no key given - skipping the fetch"))
            session._print(s.dim("  store one later with /connect key, or add one to"))
            session._print(s.dim(f"  {settings_path()}"))
            return False

    try:
        add_endpoint(name, url, key_env, note=f"added {time.strftime('%Y-%m-%d')}")
    except ValueError as exc:
        session._print(s.red(f"  {exc}"))
        return False
    session._print(s.dim(f"  saved '{name}' > {url}"))
    if not session.use_endpoint(name):
        return False
    if model:
        _apply_model(session, model)
        session.refresh_title()
        return True
    session.refresh_title()
    session._print(s.dim("  fetching models…"))
    picked = _choose_model(session)
    if not picked:
        session._print(s.dim("  tip: /connect <url> <key> <model> to set in one go"))
    return picked


def _derive_name(url: str) -> str:
    from urllib.parse import urlparse
    import re as _re
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return "endpoint"
    for prefix in ("api.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    host = host.split(":")[0]
    label = host.split(".")[0] if host else ""
    base = _re.sub(r"[^a-z0-9]+", "", label) or "endpoint"
    path = parsed.path.strip("/").lower()
    if path:
        parts = [p for p in path.split("/") if p not in ("v1", "v2", "v3", "api")]
        if parts:
            suffix = _re.sub(r"[^a-z0-9]+", "", parts[-1])
            if suffix and suffix != base:
                return f"{base}-{suffix}"
    return base


def _derive_key_env(name: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_API_KEY"


def _read_one() -> str:
    if os.name == "nt":
        import msvcrt
        return msvcrt.getwch()
    return sys.stdin.read(1)


def _read_secret(
    prompt_text: str,
    closer: Any | None = None,
) -> str:
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
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x04":
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
        from mantra.core.menu import visible_len
        closer(visible_len(prompt_text) + len(chars))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars).strip()


def _ask_secret(session: ConsoleSession, label: str) -> str:
    return _read_secret(session.prompt_text(label), closer=session.close_prompt)


def _needs_first_run(session: ConsoleSession) -> bool:
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    key_env = llm.get("api_key_env") or ""
    if not base_url:
        return True
    if provider_needs_key(base_url, key_env):
        return not (os.environ.get(key_env) or has_stored(key_env))
    return False


# ---------------------------------------------------------------------------
# ConsoleSession
# ---------------------------------------------------------------------------

class ConsoleSession:
    """One REPL session over one persistent local workspace."""

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
        env = render_environment(workspace)
        try:
            entries = os.listdir(workspace)[:50]
            files = []
            for e in sorted(entries)[:35]:
                p = os.path.join(workspace, e)
                files.append(f"{e}/" if os.path.isdir(p) else e)
            env += f"\n- workspace files: {', '.join(files) or '(empty)'}"
            for hint in ("README.md", "package.json", "pyproject.toml", "AGENTS.md", "main.py", "index.html"):
                if hint in entries:
                    env += f"\n- has {hint}"
            for readme in ("README.md", "readme.md"):
                rp = os.path.join(workspace, readme)
                if os.path.isfile(rp):
                    try:
                        with open(rp, "r", encoding="utf-8", errors="replace") as f:
                            head = f.read(2500).strip().replace("\r", "")
                        if head:
                            snippet = head[:500].replace("\n", " ")
                            env += f"\n- README: {snippet[:400]}"
                        break
                    except Exception:
                        pass
        except Exception:
            pass
        self.system_prompt = assemble_system_prompt(
            config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            known_failures_path=KNOWN_FAILURES_PATH,
            memory_path=self.memory_path,
            instructions_path=self.instructions_path,
            environment=env,
        )
        self.tools = build_tools(config["tools"], plugins=config.get("plugins"))
        self.llm = llm if llm is not None else build_llm(config["llm"])
        self.approvals = ApprovalPolicy(
            mode=config.get("approvals", "auto"),
            ask=ask or self._ask,
            note=self._note,
        )
        self.totals = {"tokens_in": 0, "tokens_out": 0, "turns": 0, "tool_errors": 0, "cache_hit": 0}
        self.reported_changes: set[str] = set()
        self.known_models: list[str] = []
        self.recent_tools: list[str] = []
        self.turn_history: list[dict] = []

        log_path = config["logging"].get("path", "logs/mantra-console.jsonl")
        if not os.path.isabs(log_path):
            log_path = os.path.join(PROJECT_ROOT, log_path)
        self.logger = JsonlLogger(log_path)
        self.bus = EventBus()
        self.bus.subscribe(self._on_event)

        self.message_count = 0
        self.verbose = bool(config.get("verbose", False))
        self.max_steps = int(config.get("max_steps", 30))
        self.session_name = ""
        self.goal = ""
        self.goal_notes: list[str] = []
        self.active_skills: list[str] = []
        self.auto_attached: list[str] = []
        self.in_bundle = False

        self._spinner: Spinner | None = None
        self._streamed_this_run = False
        self._stream_header_done = False
        self._turn_started: float = 0.0
        self._stream_tokens: int = 0
        self._last_counter_update: float = 0.0
        self._prompt_sent_at: float = 0.0
        self._stream_renderer = StreamingRenderer(self.style)
        self.frame = None
        self.layout: Any | None = None
        self._compact = True
        self._splash_visible = True
        self._abort = threading.Event()
        self._prev_sigint = None

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
        if os.path.isdir(os.path.join(workspace, ".git")):
            return
        try:
            subprocess.run(
                ["git", "init"], cwd=workspace,
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

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
        if self._spinner:
            with self._spinner.paused():
                yield
        else:
            yield

    def _format_diff(self, diff_text: str, max_lines: int = 60) -> str:
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
        out = sys.stdout
        out.write("\n")
        out.flush()

    def _on_event(self, name: str, payload: dict) -> None:
        if name == "tool_call":
            step = payload.get("step")
            tool = payload.get("tool")
            args = payload.get("args") or {}
            detail = ""
            if tool in ("write_file", "edit_file", "read_file", "list_dir"):
                path = args.get("path") or args.get("directory") or ""
                if path:
                    detail = f" {self.style.dim('>')} {self.style.dim(path.upper())}"
            elif tool == "run_command":
                cmd = args.get("command") or ""
                if cmd:
                    detail = f" {self.style.dim('>')} {self.style.dim(cmd[:60].upper())}{'...' if len(cmd) > 60 else ''}"
            self._print(f"  {self.style.dim(f'* STEP {step}')} {self.style.bright_magenta(tool.upper())}{detail}")
        elif name == "tool_denied":
            self._print(f"  {self.style.red('✗ DENIED')} {self.style.dim(payload.get('tool','').upper())}")
        elif name == "run_error":
            self._print(f"  {self.style.red('!! ' + str(payload.get('error')))}")
        elif name == "tool_result":
            tool = payload.get("tool")
            result = payload.get("result")
            ok = payload.get("ok")
            seconds = payload.get("seconds")
            if tool in ("edit_file", "write_file"):
                if isinstance(result, str) and result.strip():
                    self._print(self._format_diff(result.strip()))
                elif self.verbose:
                    detail = self.style.dim("OK" if ok else "FAILED")
                    self._print(f"    {detail} {seconds}s")
            elif self.verbose:
                detail = self.style.dim("OK" if ok else "FAILED")
                self._print(f"    {detail} {seconds}s")

    def _on_delta(self, piece: str) -> None:
        if self._abort.is_set():
            raise AbortError("interrupted by operator")
        if self._spinner:
            self._spinner.stop(clear=True)
            self._spinner = None
        self._stream_tokens += max(1, len(piece) // 4)
        rendered = self._stream_renderer.render_piece(piece)
        if self.layout is not None and self.layout.active:
            if not self._stream_header_done:
                self.layout.write(f"{self.style.neon_title('ENCHANTER')} ")
                self._stream_header_done = True
            self.layout.write(rendered)
            self._update_live_counter()
        else:
            if not self._stream_header_done:
                sys.stdout.write(f"{self.style.neon_title('ENCHANTER')} ")
                self._stream_header_done = True
            sys.stdout.write(rendered)
            sys.stdout.flush()
        self._streamed_this_run = True

    def _update_live_counter(self) -> None:
        if self.layout is None or not self.layout.active:
            return
        now = time.monotonic()
        if now - self._last_counter_update < 0.1:
            return
        self._last_counter_update = now
        tok_str = _short(self._stream_tokens)
        counter = self.style.bright_cyan(f" {tok_str} TOK ↓ ")
        if self.style.enabled:
            body = self.style.bright_yellow("\u2502 MANTRA >") + counter
        else:
            body = "\u2502 MANTRA >" + counter
        self.layout.draw_prompt(body=body)

    def _ask(self, prompt: str) -> str:
        if self._spinner:
            self._spinner.stop(clear=True)
            self._spinner = None
        self._print("")
        self._print(f"  {self.style.yellow('allow?')} {prompt}")
        self._print(self.style.dim("  [y]es   [n]o   [a]lways for this session"))
        try:
            if self.frame is not None:
                self.frame.prompt("  allow> ")
                answer = input().strip().lower()
            else:
                answer = input("  allow> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "n"
        finally:
            if self.frame is not None:
                self.frame.abandon_row()
        if answer in ("a", "always"):
            return "a"
        if answer in ("y", "yes"):
            return "y"
        return "n"

    def expand_mentions(self, text: str) -> tuple[str, list[str]]:
        tokens = MENTION_RE.findall(text)
        if not tokens:
            return text, []
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
            body += "\n* [truncated]"
        return f"* @{rel.upper()} *\n{body}"

    @staticmethod
    def _render_listing(rel: str, full: str) -> str:
        try:
            entries = sorted(os.listdir(full))[:MAX_LISTING_ENTRIES]
        except OSError:
            return ""
        lines = [f"* @{rel.upper()} ({len(entries)} entries) *"]
        for entry in entries:
            kind = "DIR " if os.path.isdir(os.path.join(full, entry)) else "FILE"
            lines.append(f"{kind} {entry}")
        return "\n".join(lines)

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
            self._prev_sigint = None

    def _restore_sigint(self) -> None:
        if self._prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (ValueError, OSError):
                pass

    def _effective_system_prompt(self) -> str:
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

    def _check_goal_completion(self, result: RunResult | None) -> None:
        if not self.goal or result is None or not result.final_message:
            return
        if "GOAL COMPLETE" not in result.final_message.upper():
            return
        self._print(
            self.style.dim("  the agent reports the goal is met - /goal done to clear it")
        )

    def auto_route(self, text: str) -> str | None:
        from mantra.core.settings import skills_prefs as _sp
        prefs = dict(self.config.get("skills") or {})
        prefs.update(_sp())
        if not prefs.get("auto", True) or self.in_bundle or self.active_skills:
            return None
        if not skills.list_skills():
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
            self._detach_auto()
            return bundle
        self._note(f"bundle '{bundle}' covers this end to end - /skills launch {bundle}")
        return None

    def _detach_auto(self) -> None:
        if not self.auto_attached:
            return
        self.active_skills = [s for s in self.active_skills if s not in self.auto_attached]
        self.auto_attached = []

    def handle(self, text: str) -> RunResult | None:
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
        from mantra.ui.session import _skills_launch
        bundle = self.auto_route(text)
        if bundle:
            return _skills_launch(self, bundle)
        task = {"task_id": f"console-{self.message_count}", "problem_statement": text}

        self._auto_compact()

        from mantra.console import AgentLoop
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
                if self.frame is None:
                    if self.layout is not None and self.layout.active:
                        self.layout.write("\n")
                    else:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
            elif result is not None and result.final_message:
                body = render_markdown(result.final_message, self.style)
                if self.frame is not None:
                    self.frame.row(body)
                else:
                    self._print(f"{self.style.neon_title('ENCHANTER')} {body}")
            if result is not None:
                self._record_usage(result)
                self._record_memory(task, result)
                self._report_changes()
                self._check_goal_completion(result)
                self.autosave()
        finally:
            self._stop_dashboard_refresh()
            self._restore_sigint()
            if self._spinner:
                self._spinner.stop(clear=not self._streamed_this_run)
                self._spinner = None
            self._detach_auto()
            self._end_turn(result)
        return result

    def _end_turn(self, result: RunResult | None) -> None:
        self._stream_tokens = 0
        if self.frame is not None:
            if result is None:
                self.frame.divider("no result")
            else:
                self.frame.divider(self._footer(result))
            return
        if result is not None:
            self._print(f"  {self.style.dim(self._usage_line(result))}")

    def _footer(self, result: RunResult | None) -> str:
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
        rate = (cache * 100 // tin) if tin > 0 else 0
        self.turn_history.append({
            "turn": self.totals["turns"],
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_hit": cache,
            "cache_rate": rate,
        })
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
        changed = getattr(self.sandbox, "changed", set()) or set()
        fresh = sorted(changed - self.reported_changes)
        if not fresh:
            return
        self.reported_changes.update(fresh)
        shown = fresh[:8]
        more = "" if len(fresh) <= 8 else f" (+{len(fresh) - 8} more)"
        self._print(f"  {self.style.dim('changed: ' + ', '.join(shown) + more)}")

    def _auto_compact(self) -> None:
        limit = int(self.config.get("auto_compact_tokens", 0) or 0)
        if limit and self.context.tokens > limit:
            self._print(self.style.dim(f"  (context ~{self.context.tokens} tokens, compacting)"))
            self.compact()

    def compact(self) -> bool:
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

    def save_session(self, path: str) -> bool:
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.yellow(f"  refusing to save outside allowed dirs: {path}"))
            self._print(self.style.dim(f"  allowed: workspace, {sessions.sessions_dir()}, temp"))
            return False
        if len(path) > 500:
            self._print(self.style.red("  save failed: path too long"))
            return False
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": self.workspace,
            "model": self.config.get("llm", {}).get("model", "?"),
            "totals": self.totals,
            "messages": self.context.messages,
        }
        try:
            data = json.dumps(payload, ensure_ascii=False)
            if len(data) > 10_000_000:
                self._print(self.style.red("  save failed: session too large"))
                return False
        except Exception:
            pass
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            self._print(self.style.red(f"  save failed: {exc}"))
            return False
        self._print(self.style.dim(f"  saved {len(self.context.messages)} messages to {path}"))
        return True

    def load_session(self, path: str) -> bool:
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.yellow(f"  refusing to load outside allowed dirs: {path}"))
            return False
        try:
            try:
                if os.path.getsize(path) > 10_000_000:
                    self._print(self.style.red("  load failed: file too large"))
                    return False
            except OSError:
                pass
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._print(self.style.red(f"  load failed: {exc}"))
            return False
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.red("  load failed: no messages in file"))
            return False
        if len(messages) > 500:
            self._print(self.style.yellow(f"  warning: large session {len(messages)} messages, truncating"))
            messages = messages[-500:]
        self.context.messages = list(messages)
        self.context.resync()
        totals = payload.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: int(v) for k, v in totals.items() if k in self.totals})
        self._print(
            self.style.dim(f"  restored {len(messages)} messages (~{self.context.tokens} tokens)")
        )
        return True

    def autosave(self) -> None:
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
        for message in self.context.messages:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return re.sub(r"\s+", " ", content).strip()[:70]
        return ""

    def resume_session(self, name: str) -> bool:
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
        self.goal = str(data.get("goal") or "")
        notes = data.get("goal_notes")
        self.goal_notes = [str(n) for n in notes] if isinstance(notes, list) else []
        self.session_name = name
        self._print(
            self.style.dim(
                f"  resumed '{name}' - {len(messages)} messages "
                f"(~{self.context.tokens} tokens)"
            )
        )
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                self._print(f"{self.style.neon_label('you')} {content}")
            elif role == "assistant" and isinstance(content, str):
                self._print(f"{self.style.neon_title('ENCHANTER')}")
                self._print(content)
            elif role == "tool" and isinstance(content, str):
                self._print(f"{self.style.dim('tool')} {self.style.dim(content[:200])}")
        summary = data.get("summary") or ""
        if summary:
            self._print(self.style.dim(f"  started with: {summary}"))
        return True

    def show_sessions(self) -> None:
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
        from mantra.console import _menu
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
        try:
            entries = sorted(os.listdir(root))[:50] if os.path.isdir(root) else []
        except OSError as exc:
            self._print(self.style.red(f"  cannot list workspace: {exc}"))
            return
        for entry in entries:
            try:
                full = os.path.join(root, entry)
                self._print(("> " if os.path.isdir(full) else "") + entry)
            except OSError:
                self._print(entry)
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

        cache_rate = (cache_hit * 100 // tokens_in) if tokens_in > 0 else 0
        cache_saved = cache_hit // 2

        if as_json:
            import json as _json
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
            self._print(_json.dumps(payload, indent=2))
            return

        self._print(f"turns        {t['turns']}")
        self._print(f"tokens in    {tokens_in}")
        self._print(f"tokens out   {tokens_out}")
        if tokens_in > 0 and cache_hit > 0:
            self._print(f"cache hit    {cache_hit} ({cache_rate}% of prompt)")
            self._print(f"cache saved  {cache_saved} tokens (~50% discount)")
        else:
            self._print(f"cache hit    {cache_hit}")
        self._print(f"tool errors  {t['tool_errors']}")
        self._print(f"context      ~{context_tokens} tokens ({context_chars} chars)")
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
        try:
            from mantra.ui import compact
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
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def set_reasoning(self, level: str, quiet: bool = False) -> None:
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
        llm = self.config.get("llm", {})
        return endpoint_name_for_url(llm.get("base_url", "")) or ""

    def use_endpoint(self, name: str, model: str | None = None) -> bool:
        entry = known_endpoints().get(name.lower())
        if entry is None:
            self._print(self.style.yellow(f"  no endpoint named '{name}'"))
            self._print(self.style.dim("  add one with /connect, or list them: /connect"))
            return False
        llm = self.config.setdefault("llm", {})
        llm["base_url"] = entry["base_url"]
        llm["api_key_env"] = entry.get("api_key_env") or ""
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
        llm = self.config.get("llm", {})
        current = (llm.get("base_url") or "").rstrip("/")
        known = known_endpoints()
        if not known:
            self._print(self.style.dim("  no endpoints yet - add one with /connect"))
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
            count = sum(
                1 for ln in open(KNOWN_FAILURES_PATH, encoding="utf-8", errors="replace")
                if ln.startswith("## KF-") and ln[6:7].isdigit()
            )
            self._print(s.dim(f"known-failure registry: {count} classes"))
        self._print(s.dim("type /help for commands"))
