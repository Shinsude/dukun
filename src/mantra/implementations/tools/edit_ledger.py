"""Read-before-edit ledger.

Adopted from the workflow-layer harness (read-safe/anchor-edit pattern):
an agent may only edit a file it has read during this session, and an edit
is rejected when the file changed on disk since that read (stale old_string).
This kills the two most common self-inflicted agent failures: blind edits
and edits against outdated content.
"""

from __future__ import annotations

import hashlib


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EditLedger:
    """Per-session map of path -> hash of the content last seen via tools."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def remember(self, path: str, content: str) -> None:
        """Record the exact content the agent just read or wrote."""
        self._seen[self._key(path)] = content_hash(content)

    def has_seen(self, path: str) -> bool:
        return self._key(path) in self._seen

    def is_current(self, path: str, content: str) -> bool:
        return self._seen.get(self._key(path)) == content_hash(content)

    def forget_all(self) -> None:
        self._seen.clear()

    @staticmethod
    def _key(path: str) -> str:
        # Normalize separators so "a\\b.py" and "a/b.py" are one entry.
        return path.replace("\\", "/").lstrip("./")
