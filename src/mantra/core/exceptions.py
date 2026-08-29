"""MANTRA exception hierarchy."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness failures."""


class ConfigError(HarnessError):
    """Configuration is missing, malformed, or references unknown components."""


class ToolError(HarnessError):
    """A tool execution failed in a way the agent cannot recover from."""


class SandboxError(HarnessError):
    """The sandbox could not be provisioned or a fatal exec failure occurred."""


class LLMError(HarnessError):
    """The LLM API call failed after retries."""


class EvaluationError(HarnessError):
    """The evaluator could not produce a verdict."""


class AbortError(HarnessError):
    """The operator interrupted the run (Ctrl+C) and it should stop cleanly."""
