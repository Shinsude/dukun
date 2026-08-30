"""Conversation history with bounded, turn-aware growth.

Keeps the system prompt and the first task message pinned, then drops the
oldest complete turn first when the budget is exceeded, so the model never
loses its instructions and never sees orphaned tool results.

A "turn" is one assistant message plus every tool result that answers it.
Dropping anything less leaves a ``tool`` message whose ``tool_call_id`` has
no matching call, which chat-completions servers reject outright.
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count good enough for budgeting, not for billing."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(str(content))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(str(function.get("arguments") or "")) + 32
    return max(1, total // CHARS_PER_TOKEN)


class ContextManager:
    """Owns the message list sent to the LLM."""

    def __init__(self, max_messages: int = 200, max_chars: int = 240_000) -> None:
        if max_messages < 4:
            raise ValueError("max_messages must be at least 4")
        self.max_messages = max_messages
        self.max_chars = max_chars
        self.messages: list[dict[str, Any]] = []
        self._chars = 0

    def seed(self, system_prompt: str, user_task: str) -> None:
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self._recount()

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self._chars += _message_size(message)
        self._truncate()

    def replace_body(self, messages: list[dict[str, Any]]) -> None:
        """Keep the pinned system prompt, replace everything after it.

        Used by compaction: the summary (plus any recent tail the caller
        keeps) becomes the new body.
        """
        system = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else {"role": "system", "content": ""}
        )
        self.messages = [system] + list(messages)
        self._recount()

    def resync(self) -> None:
        """Recompute cached sizes after messages were edited in place."""
        self._recount()

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.messages)

    @property
    def chars(self) -> int:
        return self._chars

    def _truncate(self) -> None:
        while self._over_budget() and self._drop_oldest_turn():
            pass

    def _over_budget(self) -> bool:
        return len(self.messages) > self.max_messages or self._chars > self.max_chars

    def _drop_oldest_turn(self) -> bool:
        """Remove the oldest assistant turn and the tool results it produced."""
        for index in range(2, len(self.messages)):
            if self.messages[index].get("role") != "assistant":
                continue
            end = index + 1
            while end < len(self.messages) and self.messages[end].get("role") == "tool":
                end += 1
            del self.messages[index:end]
            self._recount()
            return True
        # No assistant turn left to drop: fall back to oldest non-tool message to avoid orphaning tool results.
        for index in range(2, len(self.messages)):
            if self.messages[index].get("role") != "tool":
                del self.messages[index]
                self._recount()
                return True
        # Only tool messages remain — remove the oldest tool as last resort
        if len(self.messages) > 2:
            del self.messages[2]
            self._recount()
            return True
        return False

    def _recount(self) -> None:
        self._chars = sum(_message_size(m) for m in self.messages)


def _message_size(message: dict[str, Any]) -> int:
    content = message.get("content")
    size = len(content) if isinstance(content, str) else len(str(content or ""))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        size += len(str(function.get("arguments") or "")) + 32
    return size
