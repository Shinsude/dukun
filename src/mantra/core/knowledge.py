"""System-prompt assembly: base prompt + registry + memory + repo instructions.

Adopted from the workflow-layer harness: durable knowledge is injected into
every session so fixed failure classes stay fixed and project state survives
restarts - with a hard cap so it can never grow into bloat.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from typing import Any

MEMORY_CAP_CHARS = 8000
INSTRUCTIONS_CAP_CHARS = 4000
INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md", ".mantra-instructions.md")


def find_instructions_file(workspace: str) -> str | None:
    """First instruction file present at the workspace root, if any."""
    for name in INSTRUCTION_FILENAMES:
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def render_environment(workspace: str) -> str:
    """Facts about the host and checkout, so the model stops guessing.

    A model that knows it is on Windows with PowerShell available writes
    working commands instead of Unix ones it hallucinated. Cheap to build
    once per session, and it removes a whole class of failed turns.
    """
    lines = [
        f"- date: {time.strftime('%Y-%m-%d')}",
        f"- os: {platform.system()} {platform.release()}",
        f"- shell: {_shell_name()}",
        f"- python: {platform.python_version()}",
        f"- workspace: {workspace}",
    ]
    if _git(workspace, "rev-parse", "--is-inside-work-tree") == "true":
        # A freshly initialised repo has no HEAD until the first commit.
        branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD") or "(no commits yet)"
        dirty = _git(workspace, "status", "--porcelain")
        state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} modified"
        lines.append(f"- git: branch {branch} ({state})")
    else:
        lines.append("- git: not a repository")
    return "\n".join(lines)


def _shell_name() -> str:
    if os.name == "nt":
        return "cmd.exe via subprocess (PowerShell available; no Unix coreutils)"
    return os.environ.get("SHELL", "/bin/sh")


def _git(workspace: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def assemble_system_prompt(
    base_prompt: str,
    known_failures_path: str | None = None,
    memory_path: str | None = None,
    instructions_path: str | None = None,
    environment: str | None = None,
) -> str:
    """Append registry, memory, and repo-instruction sections when they exist."""
    sections = [base_prompt]

    if environment:
        sections.append("## Environment (facts, do not guess)\n\n" + environment.strip())

    kf_text = _read_capped(known_failures_path, MEMORY_CAP_CHARS)
    if kf_text:
        sections.append(
            "## Known-failure classes (never repeat these)\n\n" + kf_text.strip()
        )

    mem_text = _read_tail(memory_path, MEMORY_CAP_CHARS)
    if mem_text:
        sections.append(
            "## Workspace memory (durable notes from earlier sessions)\n\n"
            + mem_text.strip()
        )

    instr_text = _read_capped(instructions_path, INSTRUCTIONS_CAP_CHARS)
    if instr_text:
        sections.append(
            "## Project instructions (from the workspace itself; follow these)\n\n"
            + instr_text.strip()
        )

    return "\n\n".join(sections)


def _read_capped(path: str | None, cap: int) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(cap)
    except OSError:
        return ""


def _read_tail(path: str | None, cap: int) -> str:
    """Newest entries are appended last, so keep the tail of the file."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return ""
    if len(content) > cap:
        # Cut at a line boundary so no half-entry is shown.
        content = content[-cap:]
        content = content.split("\n", 1)[-1]
    return content


def append_memory(memory_path: str | None, text: str, cap: int = MEMORY_CAP_CHARS) -> bool:
    """Append one dated entry, pruning oldest lines beyond the cap.

    Returns True when the write succeeded; missing parent dirs are created.
    """
    if not memory_path or not text.strip():
        return False
    entry = text.rstrip() + "\n"
    existing = _read_file(memory_path)
    combined = (existing + "\n" + entry).lstrip("\n") if existing else entry
    while len(combined) > cap:
        lines = combined.split("\n")
        combined = "\n".join(lines[1:])
        if "\n" not in combined:
            break
    parent = os.path.dirname(memory_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(memory_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(combined)
        return True
    except OSError:
        return False


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""
