"""Code search tools.

The content search walks the workspace in pure Python so it works in any
sandbox with a file view and needs no external grep binary.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mantra", ".pytest_cache", "dist", "build"}
_MAX_RESULTS = 50
_MAX_FILE_BYTES = 500_000
_SKIP_EXT = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Search for a literal substring across text files in the workspace. "
        "Returns matching lines prefixed with 'path:line'."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Literal text to find"}
        },
        "required": ["query"],
    }

    def execute(self, sandbox: Sandbox, query: str) -> str:
        if "\x00" in query or "\n" in query or "\r" in query:
            return "ERROR: query contains invalid characters"
        root = getattr(sandbox, "root", None)
        if root is None:
            # Fall back to shell grep for sandboxes without a file view.
            # Use shlex.quote for safe shell quoting and reject obvious meta.
            if len(query) > 500:
                return "ERROR: query too long"
            # Still escape single quotes for the inner single-quoted string
            escaped = query.replace("'", "'\\''")
            # Validate that the escaped query does not contain shell meta outside quotes
            # The grep pattern is inside single quotes, so only ' needed escaping.
            result = sandbox.exec(f"grep -rn '{escaped}' . --exclude-dir=.git")
            return result.stdout[:20000] if result.exit_code == 0 else "(no matches)"

        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in _SKIP_EXT:
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(full, root)
                hits.extend(self._scan_file(full, rel, query))
                if len(hits) >= _MAX_RESULTS:
                    break
            if len(hits) >= _MAX_RESULTS:
                break
        if not hits:
            return "(no matches)"
        return "\n".join(hits)

    @staticmethod
    def _scan_file(full: str, rel: str, query: str) -> list[str]:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        out = []
        for lineno, line in enumerate(lines, start=1):
            if query in line:
                out.append(f"{rel}:{lineno}: {line.rstrip()[:300]}")
                if len(out) >= _MAX_RESULTS:
                    break
        return out


class FindFileTool(Tool):
    name = "find_file"
    description = "Find files whose name contains the given substring."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }

    def execute(self, sandbox: Sandbox, pattern: str) -> str:
        if "\x00" in pattern or "\n" in pattern or "\r" in pattern:
            return "ERROR: pattern contains invalid characters"
        if _SHELL_META_RE.search(pattern) or '"' in pattern or "'" in pattern or "*" in pattern or "?" in pattern:
            return "ERROR: pattern contains unsupported characters"
        if len(pattern) > 200:
            return "ERROR: pattern too long"
        root = getattr(sandbox, "root", None)
        if root is None:
            quoted = shlex.quote(f"*{pattern}*")
            result = sandbox.exec(
                f"find . -name {quoted} -not -path './.git/*'"
            )
            return result.stdout[:20000] if result.exit_code == 0 else "(no matches)"

        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if pattern in filename:
                    matches.append(os.path.relpath(os.path.join(dirpath, filename), root))
                    if len(matches) >= _MAX_RESULTS:
                        return "\n".join(matches)
        return "\n".join(matches) or "(no matches)"
