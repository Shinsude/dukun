"""Evaluator: always passes for interactive sessions."""

from __future__ import annotations

from mantra.interfaces.evaluator import EvaluationResult, Evaluator
from mantra.interfaces.sandbox import Sandbox


class NullEvaluator(Evaluator):
    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        return EvaluationResult(
            passed=True, detail="interactive run (no automatic grading)"
        )
