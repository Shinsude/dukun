"""Evaluator that always passes.

For interactive console sessions where there is no hidden test command:
the run's success is judged by the operator, not by an automated grader.
"""

from __future__ import annotations

from mantra.interfaces.evaluator import EvaluationResult, Evaluator
from mantra.interfaces.sandbox import Sandbox


class NullEvaluator(Evaluator):
    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        return EvaluationResult(
            passed=True, detail="interactive run (no automatic grading)"
        )
