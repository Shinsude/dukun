"""Ledger: enforce read-before-edit via content hash."""

from __future__ import annotations

import hashlib

# Marker appended by sandboxes when a read is clipped at the size cap.
TRUNCATION_MARKER = "\n... [truncated]"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def was_clipped(content: str) -> bool:
    """Whether ``content`` came from a read that hit the sandbox size cap."""
    return isinstance(content, str) and content.endswith(TRUNCATION_MARKER)


class EditLedger:
    """Per-session path -> hash of last seen content.

    Tracks a ``truncated`` flag separately from the hash: when a read hit
    the sandbox size cap the hash covers only a prefix, so an edit on such
    a file must be refused on size grounds alone - not because of a
    legitimately embedded "[truncated]" marker in content.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._truncated: set[str] = set()

    def remember(self, path: str, content: str, truncated: bool = False) -> None:
        """Record content hash for path (and whether the read was clipped)."""
        key = self._key(path)
        self._seen[key] = content_hash(content)
        if truncated:
            self._truncated.add(key)
        else:
            self._truncated.discard(key)

    def has_seen(self, path: str) -> bool:
        return self._key(path) in self._seen

    def is_current(self, path: str, content: str) -> bool:
        return self._seen.get(self._key(path)) == content_hash(content)

    def was_truncated(self, path: str) -> bool:
        """Whether the last recorded read of this file was clipped by size."""
        return self._key(path) in self._truncated

    def forget_all(self) -> None:
        self._seen.clear()
        self._truncated.clear()

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
