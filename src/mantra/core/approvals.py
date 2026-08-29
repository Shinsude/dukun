"""Tool-call approval policy.

A daily-driver agent needs a gate between "the model asked" and "the host
did it". This module classifies what a tool is about to do; the front end
supplies the actual prompt through an ``ask`` callback, so the policy stays
testable and the TUI owns all terminal I/O.

Modes:
    default  auto-allow read-only work, ask about anything that mutates
    auto     auto-allow mutations too, still ask about destructive commands
    yolo     allow everything (trusted throwaway work only)
    plan     refuse every mutation without asking (read-only exploration)
"""

from __future__ import annotations

import re
from typing import Any, Callable

AskCallback = Callable[[str], str]  # returns "y" | "n" | "a"
NoteCallback = Callable[[str], None]

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "run_command", "git_reset"})

MODES = ("default", "auto", "yolo", "plan")

# Patterns that can destroy work outside the model's ability to restore it.
_DESTRUCTIVE = (
    r"rm\s+(-[rRfF]+\s+)*[^\s]*\s*(-[rRfF]+)",  # rm -rf / rm -fr
    r"\brm\s+-[rR]",
    r"\brmdir\b",
    r"\bformat\s+[a-zA-Z]{1,2}:",  # format C: - not "--format=..." flags
    r"\bmkfs\b",
    r"\bdd\b\s+if=",
    r"del\s+/[sfqSFQ]",
    r"\bRemove-Item\b[^\n]*-Recurse",
    r"\brm\s+.*\*",
    r"git\s+push[^\n]*--force",
    r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[fdx]",
    r"git\s+checkout\s+--\s",
    r"git\s+restore\s+--?\w*\s*\.",
    r"\bshutdown\b",
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\btaskkill\b",
    r"\bStop-Process\b",
    r"curl[^\n|]*\|\s*(ba|z|d)?sh",
    r"wget[^\n|]*\|\s*(ba|z|d)?sh",
    r"\biex\b",
    r"Invoke-Expression",
    r"Set-ExecutionPolicy",
    r"\bchmod\s+777\b",
    r"\bsudo\b",
    r"\breg\s+delete\b",
    r"\bnet\s+user\b",
    r"\bdiskpart\b",
    r"\bcertutil\b",
    r"\bsc\s+delete\b",
    r">\s*/dev/sd",
    r"\btruncate\b",
    r"\battrib\s+",
)

# Read-only inspection: safe to run unattended in every mode.
_SAFE_COMMANDS = (
    r"^\s*(ls|dir|cat|type|echo|head|tail|wc|find|rg|grep|where|which|pwd|cd)\b",
    r"^\s*git\s+(status|diff|log|show|branch|rev-parse|ls-files|remote)\b",
    r"^\s*python\s+-m\s+(pytest|unittest|mypy|ruff|flake8)\b",
    r"^\s*python\s+--version\s*$",
    r"^\s*(pytest|py\.test|node|npm|git|go|cargo|dotnet)\s+--version\s*$",
    r"^\s*(pytest|py\.test)\b",
    r"^\s*(npm|pnpm|yarn)\s+(test|run\s+test|run\s+lint)\b",
    r"^\s*(go|cargo)\s+test\b",
    r"^\s*dotnet\s+test\b",
)

_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE), re.IGNORECASE)
_SAFE_RE = re.compile("|".join(_SAFE_COMMANDS), re.IGNORECASE)


def classify_command(command: str) -> str:
    """Return ``destructive``, ``safe``, or ``mutating`` for a shell command."""
    if not command or not command.strip():
        return "safe"
    if _DESTRUCTIVE_RE.search(command):
        return "destructive"
    if _SAFE_RE.match(command):
        return "safe"
    return "mutating"


def classify(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Return ``(risk, human detail)`` for one tool call."""
    if tool not in MUTATING_TOOLS:
        return "safe", tool

    if tool in ("write_file", "edit_file"):
        path = str(arguments.get("path", "?"))
        verb = "overwrite" if tool == "write_file" else "edit"
        return "mutating", f"{verb} {path}"

    if tool == "git_reset":
        return "destructive", "discard all uncommitted changes (git checkout -- .)"

    command = str(arguments.get("command", "")).strip()
    preview = command if len(command) <= 160 else command[:157] + "..."
    return classify_command(command), preview or "(empty command)"


class ApprovalPolicy:
    """Decides whether a tool call may execute."""

    def __init__(
        self,
        mode: str = "default",
        ask: AskCallback | None = None,
        note: NoteCallback | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown approval mode '{mode}' (known: {list(MODES)})")
        self.mode = mode
        self._ask = ask or (lambda prompt: "n")
        self._note = note or (lambda message: None)
        self.session_allowed: set[str] = set()

    def check(self, tool: str, arguments: dict[str, Any]) -> bool:
        risk, detail = classify(tool, arguments)

        if self.mode == "plan" and tool in MUTATING_TOOLS:
            self._note(f"plan mode: refused {tool} ({detail})")
            return False

        if risk == "safe":
            return True

        if self.mode == "yolo":
            return True

        if self.mode == "auto" and risk == "mutating":
            return True

        key = self._key(tool, arguments)
        if key in self.session_allowed:
            return True

        return self._confirm(tool, detail, risk, key)

    def allow_for_session(self, tool: str, arguments: dict[str, Any]) -> None:
        self.session_allowed.add(self._key(tool, arguments))

    def reset_session(self) -> None:
        self.session_allowed.clear()

    def _confirm(self, tool: str, detail: str, risk: str, key: str) -> bool:
        tag = "DESTRUCTIVE " if risk == "destructive" else ""
        answer = self._ask(f"{tag}{tool}: {detail}")
        if answer == "a":
            self.session_allowed.add(key)
            return True
        return answer == "y"

    @staticmethod
    def _key(tool: str, arguments: dict[str, Any]) -> str:
        if tool == "run_command":
            command = str(arguments.get("command", "")).strip()
            return f"run_command::{' '.join(command.split()).lower()}"
        if tool in ("write_file", "edit_file"):
            return f"{tool}::{str(arguments.get('path', '')).lower()}"
        return tool
