"""Sync event bus for hooks and observability."""

from __future__ import annotations

from typing import Any, Callable

EventHandler = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Fan-out events; swallow handler errors to isolate observers."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            try:
                handler(event, payload)
            except Exception:
                pass
