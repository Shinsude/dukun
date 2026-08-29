"""Tool contract: one capability the agent may invoke."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mantra.interfaces.sandbox import Sandbox


class Tool(ABC):
    """A single agent-facing tool.

    ``parameters`` is a JSON Schema object describing the arguments; it is
    passed verbatim to the LLM as part of the function-calling spec.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, sandbox: Sandbox, **kwargs: Any) -> str:
        """Run the tool against the sandbox and return an observation string."""
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """Function-calling schema fragment for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
