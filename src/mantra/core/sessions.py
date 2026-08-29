"""Saved conversations, so a session can be picked up where it left off.

One JSON file per session under ``~/.mantra/sessions/``. The store is
deliberately dumb - it writes what the console hands it and never
interprets the contents - because the interesting decisions (what counts
as a session, what to name it, when to save) belong to the caller.

Each file is::

    {
      "version": 1,
      "name": "k-chat-20260829-0812",
      "saved_at": "2026-08-29 08:12:44",
      "workspace": "C:\\Users\\arif-\\K-CHAT",
      "model": "mistral-medium-latest",
      "summary": "the first user message, for the picker",
      "totals": {"tokens_in": 0, ...},
      "messages": [...]
    }

``MANTRA_SESSIONS`` redirects the directory for tests.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_VERSION = 1
_OVERRIDE_ENV = "MANTRA_SESSIONS"

# Below this a session is noise - a turn or two of "hello" - and listing
# it would bury the ones worth resuming.
_MIN_MESSAGES = 2


def sessions_dir() -> Path:
    """Where saved sessions live. Created on demand."""
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        target = Path(override)
    else:
        target = Path.home() / ".mantra" / "sessions"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - read-only home
        pass
    return target


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def derive_name(workspace: str = "", model: str = "") -> str:
    """A name for a session the operator never bothered to name.

    ``C:\\Users\\arif-\\K-CHAT`` -> ``k-chat-20260829-0812``. The stamp
    keeps two sessions from the same directory from colliding, and it
    sorts usefully because it reads year-month-day.
    """
    base = _slug(Path(workspace or "").name) if workspace else ""
    if not base and model:
        base = _slug(model)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{base}-{stamp}" if base else stamp


def _path(name: str) -> Path:
    return sessions_dir() / f"{name}.json"


def save(name: str, payload: dict[str, Any]) -> str | None:
    """Write a session. Returns the path, or None when it could not be written."""
    record = {
        "version": _VERSION,
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **payload,
    }
    target = _path(name)
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
    except OSError:
        return None
    return str(target)


def load(name: str) -> dict[str, Any] | None:
    """Read a session by name. None when missing or unreadable."""
    try:
        with open(_path(name), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None
    return data


def delete(name: str) -> bool:
    try:
        _path(name).unlink()
    except OSError:
        return False
    return True


def _summarise(messages: list[Any]) -> str:
    """The first thing the operator said, which is the only useful label."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                # Multimodal content blocks: take the first piece of text.
                content = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            text = re.sub(r"\s+", " ", str(content or "")).strip()
            if text:
                return text[:70]
    return ""


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """Saved sessions, newest first, with enough metadata for a picker.

    Corrupt files are skipped rather than raised: one bad JSON file in
    the directory should not make every session unlistable.
    """
    directory = sessions_dir()
    try:
        files = list(directory.glob("*.json"))
    except OSError:  # pragma: no cover - unreadable directory
        return []

    found: list[dict[str, Any]] = []
    for file in files:
        try:
            with open(file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        try:
            stamp = file.stat().st_mtime
        except OSError:  # pragma: no cover - race with deletion
            stamp = 0.0
        found.append(
            {
                "name": data.get("name") or file.stem,
                "saved_at": data.get("saved_at") or "",
                "mtime": stamp,
                "workspace": data.get("workspace") or "",
                "model": data.get("model") or "",
                "turns": sum(1 for m in messages if isinstance(m, dict)
                             and m.get("role") == "user"),
                "messages": len(messages),
                "summary": data.get("summary") or _summarise(messages),
                "path": str(file),
            }
        )

    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found[:limit]


def latest() -> dict[str, Any] | None:
    """The most recently saved session, or None when there are none."""
    found = list_sessions(limit=1)
    return found[0] if found else None
