"""LLM client contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized model reply: either tool calls or a final answer."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None  # {"prompt_tokens": n, "completion_tokens": n}

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


class LLMClient(ABC):
    """Chat client with optional function calling."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Any = None,
    ) -> LLMResponse:
        """Send the conversation and tool schemas, return a normalized response.

        ``on_delta(content_fragment)`` is invoked per streamed fragment when
        the client supports streaming; implementations may ignore it.
        """
        raise NotImplementedError
