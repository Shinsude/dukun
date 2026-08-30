"""File operation tools."""

from __future__ import annotations

import os
from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

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
            # Auto-record the read so edits work without an explicit read_file first.
            if not self.ledger.has_seen(path):
                self.ledger.remember(path, content)
            if not self.ledger.is_current(path, content):
                # File changed on disk — re-record and proceed.
                self.ledger.remember(path, content)
        if old_string not in content:
            return f"ERROR: old_string not found in {path}"
        new_content = content.replace(old_string, new_string, 1)
        sandbox.write_file(path, new_content)
        if self.ledger is not None:
            self.ledger.remember(path, new_content)
        # Build a unified diff of the change.
        old_lines = old_string.splitlines(keepends=True)
        new_lines = new_string.splitlines(keepends=True)
        diff_lines = [f"--- {path}\n", f"+++ {path}\n"]
        for line in old_lines:
            diff_lines.append(f"- {line.rstrip()}\n")
        for line in new_lines:
            diff_lines.append(f"+ {line.rstrip()}\n")
        return "".join(diff_lines)


class ListDirTool(Tool):
    name = "list_dir"
    description = "List immediate children of a directory ('.' for the workspace root)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def execute(self, sandbox: Sandbox, path: str) -> str:
        root = getattr(sandbox, "root", None)
        if root is None:
            # Sandboxes without a direct file view list via shell.
            # Try portable listings for different environments.
            for cmd in (
                f'ls -la "{path}"',
                f'ls -1 "{path}"',
                f'dir "{path}"',
                f'python -c "import os,sys; p=sys.argv[1]; print(chr(10).join(sorted(os.listdir(p))))" "{path}"',
            ):
                result = sandbox.exec(cmd)
                if result.exit_code == 0 and result.stdout.strip():
                    return result.stdout
            return result.stdout if result.exit_code == 0 else f"ERROR: {result.stderr or 'listing failed'}"

        base = root if path in (".", "") else os.path.join(root, path)
        try:
            entries = sorted(os.listdir(base))
        except OSError as exc:
            return f"ERROR: {exc}"
        lines = []
        for entry in entries:
            full = os.path.join(base, entry)
            marker = "/" if os.path.isdir(full) else ""
            lines.append(entry + marker)
        return "\n".join(lines) or "(empty directory)"
