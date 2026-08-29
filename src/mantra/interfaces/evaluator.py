"""Evaluator contract: decide whether an agent run succeeded."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from mantra.interfaces.sandbox import Sandbox


@dataclass
class EvaluationResult:
    """Verdict plus evidence from an evaluation pass."""

    passed: bool
    detail: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


class Evaluator(ABC):
    """Scores the final sandbox state after the agent loop finishes."""

    @abstractmethod
    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        raise NotImplementedError
