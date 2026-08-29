"""Logging / observability contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Logger(ABC):
    """Receives structured events from the harness.

    Implementations must never raise: observability failures must not kill
    an agent run. Wrap sinks defensively.
    """

    @abstractmethod
    def log(self, event: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release resources. Optional and idempotent."""
