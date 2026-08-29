"""Append-only JSONL event logger."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from mantra.interfaces.logger import Logger


class JsonlLogger(Logger):
    """Writes one JSON object per line; never raises into the caller."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: str, payload: dict[str, Any]) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **payload}
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass  # observability must never break a run

    def close(self) -> None:
        pass
