"""Exception hierarchy."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness failures."""


class ConfigError(HarnessError):
    """Bad config or unknown component."""


class ToolError(HarnessError):
    """Unrecoverable tool failure."""


class SandboxError(HarnessError):
    """Sandbox provisioning or fatal exec failure."""


class LLMError(HarnessError):
    """LLM call failed after retries."""


class EvaluationError(HarnessError):
    """Evaluator produced no verdict."""


class AbortError(HarnessError):
    """Operator interrupt; stop cleanly."""
