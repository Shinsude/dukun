"""Command execution and git tools."""

from __future__ import annotations

from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Execute a shell command in the sandbox workspace and return "
        "stdout, stderr, and the exit code."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "number",
                "description": "Seconds before the command is killed (default 120)",
            },
        },
        "required": ["command"],
    }

    def execute(self, sandbox: Sandbox, command: str, timeout: float = 120.0) -> str:
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            return f"ERROR: timeout must be a number, got {timeout!r}"
        if not 0 < timeout_f <= 600:
            return "ERROR: timeout must be between 0 and 600 seconds"
        if not isinstance(command, str) or not command.strip():
            return "ERROR: command must be a non-empty string"
        if len(command) > 10000:
            return "ERROR: command too long"
        result = sandbox.exec(command, timeout=timeout_f)
        parts = [f"exit_code: {result.exit_code}"]
        if result.timed_out:
            parts.append("(command timed out)")
        if result.stdout:
            parts.append(f"stdout:\n{result.stdout[:20000]}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr[:10000]}")
        return "\n".join(parts)


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show the current uncommitted diff of the repository."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, sandbox: Sandbox) -> str:
        result = sandbox.exec("git diff", timeout=30)
        if result.exit_code != 0:
            return f"ERROR: git diff failed: {result.stderr[:500] or 'unknown'}"
        return result.stdout or "(no changes)"


class GitResetTool(Tool):
    name = "git_reset"
    description = (
        "Discard all uncommitted changes (git checkout -- .). "
        "Use to start over after bad edits."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, sandbox: Sandbox) -> str:
        result = sandbox.exec("git checkout -- . && git reset", timeout=30)
        if result.exit_code != 0:
            return f"ERROR: {result.stderr}"
        return "OK: working tree reset"
