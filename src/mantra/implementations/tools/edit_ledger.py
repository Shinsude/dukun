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
        """One entry per real file.

        Separators are normalised so ``a\\b.py`` and ``a/b.py`` agree,
        and a single leading ``./`` is dropped. Stripping the characters
        "." and "/" wholesale collided ``.env`` with ``env`` and
        ``.hidden/x`` with ``hidden/x``, so a read of one satisfied the
        edit guard for the other - the exact failure the guard exists to
        prevent.

        Uses posix normpath to collapse ``a/./b``, ``a//b`` etc while
        preserving ``.env`` distinction. Trailing slash stripped.
        """
        import posixpath

        normalized = path.replace("\\", "/")
        # posixpath.normpath collapses redundant separators and ./ but
        # also strips trailing slash — re-add for directory markers handled above
        normalized = posixpath.normpath(normalized)
        # normpath turns "" into ".", restore empty
        if normalized == ".":
            normalized = ""
        # Remove leading ./ that normpath may leave as "./a"
        if normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized
