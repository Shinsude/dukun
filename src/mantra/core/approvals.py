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
    # The non-POSIX recursive delete is spelled "rd" far more often than
    # "rmdir" in practice, and the short form was the one that slipped
    # through to auto-approval.
    r"\brd\b\s+/[sSqQ]",
    r"\berase\b\s+/[fFqQ]",
    r"\bkill\b",
    r"\bpkill\b",
    r"\bkillall\b",
    r"\bchown\b",
    r"docker\s+rm\b",
    r"git\s+stash\s+drop\b",
    r"git\s+branch\s+-D\b",
    r"git\s+push\b[^\n]*--delete\b",
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
    r"\bchmod\b[^\n]*777",
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

# Side effects that make an otherwise read-only command write. Matching
# the leading verb alone classified "find . -delete" and "echo x > file"
# as safe, so the command ran unconfirmed in every mode. A quote-wrapped
# ">" is over-caught here; erring towards asking is the right direction.
_WRITE_EFFECT_RE = re.compile(
    r"(?<![\d&])>{1,2}\s*(?!&\d)\S"  # output redirection, not 2>&1
    r"|\btee\b"
    r"|\s-(delete|exec|ok|fdelete)\b",
    re.IGNORECASE,
)

_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE), re.IGNORECASE)
_SAFE_RE = re.compile("|".join(_SAFE_COMMANDS), re.IGNORECASE)

# Statement separators. Every statement has to be safe for the whole to
# be safe, so these are applied unconditionally rather than only when a
# separator happens to be present.
_STATEMENT_RE = re.compile(r"\s*(?:;|\|\||\&\&|\||\n)\s*")


def _statement_is_safe(statement: str) -> bool:
    """Safe means an allowlisted verb *and* nothing that writes."""
    if not _SAFE_RE.match(statement):
        return False
    return not _WRITE_EFFECT_RE.search(statement)


def classify_command(command: str) -> str:
    """Return ``destructive``, ``safe``, or ``mutating`` for a shell command."""
    if not command or not command.strip():
        return "safe"
    if _DESTRUCTIVE_RE.search(command):
        return "destructive"
    for part in _STATEMENT_RE.split(command):
        if not part.strip():
            continue
        if not _statement_is_safe(part):
            return "mutating"
    return "safe"


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
            # Preserve case for correctness on case sensitive systems;
            # only normalize whitespace.
            return f"run_command::{' '.join(command.split())}"
        if tool in ("write_file", "edit_file"):
            # Preserve case; normalize separators to forward slash.
            raw = str(arguments.get("path", "")).strip()
            normalized = raw.replace("\\", "/")
            return f"{tool}::{normalized}"
        return tool
