"""System-prompt assembly: base prompt + registry + memory + repo instructions.

Adopted from the workflow-layer harness: durable knowledge is injected into
every session so fixed failure classes stay fixed and project state survives
restarts - with a hard cap so it can never grow into bloat.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from typing import Any

_memory_lock = threading.Lock()

# How long to wait for another process to finish writing memory, and how
# old a lock has to be before it is assumed abandoned. The wait is short
# because it lands on the interactive thread once per turn; the stale
# window has to be comfortably longer than a write takes.
_LOCK_WAIT_SECONDS = 0.5
_LOCK_STALE_SECONDS = 10.0

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
    """Collect host and workspace facts so the model uses correct commands for the current platform.
    """
    lines = [
        f"- date: {time.strftime('%Y-%m-%d')}",
        f"- os: {platform.system()} {platform.release()}",
        f"- shell: {_shell_name()}",
        f"- python: {platform.python_version()}",
        f"- workspace: {workspace}",
    ]
    repo_state, flag = _git_checked(workspace, "rev-parse", "--is-inside-work-tree")
    if repo_state == "ok" and flag == "true":
        branch_state, branch = _git_checked(workspace, "rev-parse", "--abbrev-ref", "HEAD")
        # A freshly initialised repo has no HEAD until the first commit.
        branch = (branch or "(no commits yet)") if branch_state == "ok" else "unknown"
        dirty_state, dirty = _git_checked(workspace, "status", "--porcelain")
        if dirty_state == "ok":
            state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} modified"
        else:
            # "clean" here would be a guess dressed up as a fact.
            state = "state unknown (status probe failed)"
        lines.append(f"- git: branch {branch} ({state})")
    elif repo_state == "error":
        lines.append("- git: state unknown (probe failed)")
    else:
        lines.append("- git: not a repository")
    return "\n".join(lines)


def _shell_name() -> str:
    if os.name == "nt":
        return "cmd.exe via subprocess (PowerShell available; no Unix coreutils)"
    return os.environ.get("SHELL", "/bin/sh")


def _git(workspace: str, *args: str) -> str:
    """Output of one git call, or an empty string if it did not succeed.

    Convenience wrapper for callers that genuinely do not care why a
    probe failed. Anything that has to tell "clean" from "unknown" must
    use :func:`_git_checked` instead.
    """
    ok, output = _git_checked(workspace, *args)
    return output if ok else ""


def _git_checked(workspace: str, *args: str) -> tuple[str, str]:
    """How the call went, and what it printed.

    The state is one of three values, because two different failures
    look identical if only the output is examined:

    ``"ok"``     the command ran and exited zero
    ``"no"``     the command ran and refused - for a boolean probe such as
                 "is this a repository" that is a definitive answer, not
                 a failure
    ``"error"``  the command could not be run at all, or timed out, and
                 nothing is known

    Collapsing ``no`` into ``error`` reported an ordinary non-repository
    directory as an unknown state, and collapsing ``error`` into ``ok``
    reported a timed-out status probe as a clean working tree.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "error", ""
    if completed.returncode != 0:
        return "no", ""
    return "ok", completed.stdout.strip()


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

    result = "\n\n".join(sections)
    # Total cap to prevent context blow-up (base + caps could exceed 20k)
    TOTAL_CAP = 20000
    if len(result) > TOTAL_CAP:
        result = result[:TOTAL_CAP] + "\n... [truncated]"
    return result


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
    Uses a per process lock and an atomic file replacement to reduce races.
    """
    if not memory_path or not text.strip():
        return False
    entry = text.rstrip() + "\n"
    # Per process lock to serialize concurrent appends within this process
    with _memory_lock:
        existing = _read_file(memory_path)
        combined = (existing + "\n" + entry).lstrip("\n") if existing else entry
        while len(combined) > cap:
            lines = combined.split("\n")
            combined = "\n".join(lines[1:])
            if "\n" not in combined and len(combined) > cap:
                combined = combined[-cap:]
                break
        parent = os.path.dirname(memory_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
            except OSError:
                pass
        # File lock for inter process coordination via lock file (best effort)
        lock_path = memory_path + ".lock"
        lock_acquired = False
        lock_handle = None
        try:
            # A process that died holding the lock leaves the file behind
            # forever, after which every append waits out the full
            # deadline and then writes unprotected anyway. Anything older
            # than the stale window belonged to a process that is gone.
            _break_stale_lock(lock_path)
            # Try to acquire file lock with timeout. The wait is short:
            # this runs on the interactive thread, once per turn.
            start = time.monotonic()
            while time.monotonic() - start < _LOCK_WAIT_SECONDS:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    lock_handle = fd
                    lock_acquired = True
                    break
                except FileExistsError:
                    time.sleep(0.02)
                except OSError:
                    break
            # Re-read after acquiring lock to avoid lost update
            if lock_acquired:
                fresh = _read_file(memory_path)
                if fresh != existing:
                    combined = (fresh + "\n" + entry).lstrip("\n") if fresh else entry
                    while len(combined) > cap:
                        lines = combined.split("\n")
                        combined = "\n".join(lines[1:])
                        if "\n" not in combined and len(combined) > cap:
                            combined = combined[-cap:]
                            break
            tmp_path = memory_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(combined)
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                os.replace(tmp_path, memory_path)
                return True
            except OSError:
                # Fallback to direct write
                try:
                    with open(memory_path, "w", encoding="utf-8", newline="\n") as handle:
                        handle.write(combined)
                    try:
                        os.chmod(memory_path, 0o600)
                    except OSError:
                        pass
                    return True
                except OSError:
                    return False
        finally:
            if lock_acquired and lock_handle is not None:
                try:
                    os.close(lock_handle)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
    return False


def _break_stale_lock(lock_path: str) -> bool:
    """Remove a lock whose holder is no longer around to remove it."""
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False
    if age < _LOCK_STALE_SECONDS:
        return False
    try:
        os.remove(lock_path)
        return True
    except OSError:
        return False


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""
