"""Ledger: enforce read-before-edit via content hash."""

from __future__ import annotations

import hashlib


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EditLedger:
    """Per-session path -> hash of last seen content."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def remember(self, path: str, content: str) -> None:
        """Record content hash for path."""
        self._seen[self._key(path)] = content_hash(content)

    def has_seen(self, path: str) -> bool:
        return self._key(path) in self._seen

    def is_current(self, path: str, content: str) -> bool:
        return self._seen.get(self._key(path)) == content_hash(content)

    def forget_all(self) -> None:
        self._seen.clear()

    @staticmethod
    def _key(path: str) -> str:
        """Normalize path to one key per file; preserve .env distinction."""
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
