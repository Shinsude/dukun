"""File operation tools."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_MAX_READ_CHARS = 20000


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file relative to the workspace root. "
        "Content is truncated after 20000 characters."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative file path"}},
        "required": ["path"],
    }
    ledger = None  # EditLedger, injected by the registry

    def execute(self, sandbox: Sandbox, path: str) -> str:
        content = sandbox.read_file(path)
        if self.ledger is not None:
            self.ledger.remember(path, content)
        if len(content) > _MAX_READ_CHARS:
            content = content[:_MAX_READ_CHARS] + "\n... [truncated]"
        return content


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file with the given content."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "content": {"type": "string", "description": "Full new file content"},
        },
        "required": ["path", "content"],
    }

    ledger = None  # EditLedger, injected by the registry

    def execute(self, sandbox: Sandbox, content: str, path: str) -> str:
        sandbox.write_file(path, content)
        if self.ledger is not None:
            self.ledger.remember(path, content)
        return f"OK: wrote {len(content)} chars to {path}"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace the first exact occurrence of old_string with new_string "
        "in an existing file. The file must have been read this session; "
        "the edit is rejected if the file changed since that read."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    ledger = None  # EditLedger, injected by the registry

    def execute(
        self, sandbox: Sandbox, path: str, old_string: str, new_string: str
    ) -> str:
        content = sandbox.read_file(path)
        if self.ledger is not None:
            if not self.ledger.has_seen(path):
                return (
                    f"ERROR: read {path} with read_file before editing "
                    "(no recorded read this session)"
                )
            if not self.ledger.is_current(path, content):
                return (
                    f"ERROR: {path} changed on disk since your last read - "
                    "read it again, then retry the edit"
                )
        if old_string not in content:
            return f"ERROR: old_string not found in {path}"
        new_content = content.replace(old_string, new_string, 1)
        sandbox.write_file(path, new_content)
        if self.ledger is not None:
            self.ledger.remember(path, new_content)
        return f"OK: edited {path}"


class ListDirTool(Tool):
    name = "list_dir"
    description = "List immediate children of a directory ('.' for the workspace root)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def execute(self, sandbox: Sandbox, path: str) -> str:
        # Basic validation to reduce injection risk for shell fallbacks
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        root = getattr(sandbox, "root", None)
        if root is None:
            # Sandboxes without a direct file view list via shell.
            # Validate against shell metacharacters and use safe quoting.
            if _SHELL_META_RE.search(path) or '"' in path or "'" in path:
                return "ERROR: path contains unsupported characters for shell listing"
            quoted = shlex.quote(path)
            for cmd in (
                f"ls -la {quoted}",
                f"ls -1 {quoted}",
                f"python -c \"import os,sys; p=sys.argv[1]; print(chr(10).join(sorted(os.listdir(p))))\" {quoted}",
            ):
                result = sandbox.exec(cmd)
                if result.exit_code == 0 and result.stdout.strip():
                    return result.stdout
            return result.stdout if result.exit_code == 0 else f"ERROR: {result.stderr or 'listing failed'}"

        # Direct file view: resolve and ensure confinement
        base = root if path in (".", "") else os.path.join(root, path)
        try:
            real_base = os.path.realpath(base)
            real_root = os.path.realpath(root)
            if not (real_base == real_root or real_base.startswith(real_root + os.sep)):
                return f"ERROR: path escapes workspace: {path}"
            entries = sorted(os.listdir(real_base))
        except OSError as exc:
            return f"ERROR: {exc}"
        lines = []
        for entry in entries:
            full = os.path.join(real_base, entry)
            marker = "/" if os.path.isdir(full) else ""
            lines.append(entry + marker)
        return "\n".join(lines) or "(empty directory)"
