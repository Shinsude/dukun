"""File tools: read, write, edit, list with caps and ledger."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_MAX_READ_CHARS = 20000
_MAX_WRITE_CHARS = 1_000_000


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
    ledger = None  # injected by registry

    def execute(self, sandbox: Sandbox, path: str) -> str:
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        try:
            content = sandbox.read_file(path)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot read {path}: {exc}"
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
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        if not isinstance(content, str):
            content = str(content)
        if len(content) > _MAX_WRITE_CHARS:
            return f"ERROR: content too large ({len(content)} > {_MAX_WRITE_CHARS})"
        try:
            sandbox.write_file(path, content)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot write {path}: {exc}"
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
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        if old_string == "":
            return "ERROR: old_string must be non-empty"
        if len(old_string) > _MAX_READ_CHARS or len(new_string) > _MAX_WRITE_CHARS:
            return "ERROR: old_string or new_string too large"
        try:
            content = sandbox.read_file(path)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot read {path}: {exc}"
        if "[truncated]" in content:
            return f"ERROR: {path} is too large to edit with edit_file (content truncated); use write_file with complete content"
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
        else:
            # No ledger is wiring bug; fail closed.
            return "ERROR: edit ledger not configured"
        if old_string not in content:
            return f"ERROR: old_string not found in {path}"
        new_content = content.replace(old_string, new_string, 1)
        if len(new_content) > _MAX_WRITE_CHARS:
            return f"ERROR: result too large ({len(new_content)} > {_MAX_WRITE_CHARS})"
        try:
            sandbox.write_file(path, new_content)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot write {path}: {exc}"
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
        # Validate to reduce injection risk.
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        root = getattr(sandbox, "root", None)
        if root is None:
            # No direct view: use shell with safe quoting.
            if _SHELL_META_RE.search(path) or '"' in path or "'" in path:
                return "ERROR: path contains unsupported characters for shell listing"
            quoted = shlex.quote(path)
            last_error = "listing failed"
            for cmd in (
                f"ls -la {quoted}",
                f"ls -1 {quoted}",
                f"python -c \"import os,sys; p=sys.argv[1]; print(chr(10).join(sorted(os.listdir(p))))\" {quoted}",
            ):
                result = sandbox.exec(cmd)
                if result.exit_code == 0:
                    if result.stdout.strip():
                        return result.stdout
                    return "(empty directory)"
                last_error = result.stderr or "listing failed"
            return f"ERROR: {last_error}"

        # Direct view: resolve and check confinement.
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
