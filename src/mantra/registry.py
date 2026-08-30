"""Component registry and configuration-driven assembly.

Maps short names from the config file to concrete classes. Third parties
can extend MANTRA by registering additional classes here (or by adding
entries in their own bootstrap code) without touching the core.
"""

from __future__ import annotations

import inspect

from mantra.core.exceptions import ConfigError
from mantra.implementations.evaluators.command_evaluator import CommandEvaluator
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.llm.mock_client import ScriptedLLMClient
from mantra.implementations.llm.openai_client import OpenAICompatClient
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.docker_sandbox import DockerSandbox
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.implementations.tools.command_tool import (
    GitDiffTool,
    GitResetTool,
    RunCommandTool,
)
from mantra.implementations.tools.file_tools import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from mantra.implementations.tools.search_tools import FindFileTool, SearchCodeTool
from mantra.implementations.tools.web_tools import WebFetchTool
from mantra.interfaces.evaluator import Evaluator
from mantra.interfaces.llm_client import LLMClient
from mantra.interfaces.logger import Logger
from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

LLM_REGISTRY: dict[str, type[LLMClient]] = {
    "openai": OpenAICompatClient,
    "scripted": ScriptedLLMClient,
}

SANDBOX_REGISTRY: dict[str, type[Sandbox]] = {
    "local": LocalSandbox,
    "docker": DockerSandbox,
}

EVALUATOR_REGISTRY: dict[str, type[Evaluator]] = {
    "command": CommandEvaluator,
    "none": NullEvaluator,
}

LOGGER_REGISTRY: dict[str, type[Logger]] = {
    "jsonl": JsonlLogger,
}

TOOL_REGISTRY: dict[str, type[Tool]] = {
    tool.name: tool
    for tool in (
        ReadFileTool,
        WriteFileTool,
        EditFileTool,
        ListDirTool,
        RunCommandTool,
        SearchCodeTool,
        FindFileTool,
        GitDiffTool,
        GitResetTool,
        WebFetchTool,
    )
}
# Spelled without the underscore because that is how people say it
# aloud, and a wrong name here is a confusing "unknown tool" at startup.
TOOL_REGISTRY["webfetch"] = WebFetchTool


def build_llm(config: dict) -> LLMClient:
    kind = config.get("provider")
    cls = LLM_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown llm provider '{kind}' (known: {sorted(LLM_REGISTRY)})")
    return _construct(cls, config)


def build_sandbox(config: dict) -> Sandbox:
    kind = config.get("provider", "local")
    cls = SANDBOX_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown sandbox provider '{kind}' (known: {sorted(SANDBOX_REGISTRY)})")
    return _construct(cls, config)


def build_evaluator(config: dict) -> Evaluator:
    kind = config.get("type", "command")
    cls = EVALUATOR_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown evaluator '{kind}' (known: {sorted(EVALUATOR_REGISTRY)})")
    return _construct(cls, config)


def build_logger(config: dict) -> Logger:
    kind = config.get("type", "jsonl")
    cls = LOGGER_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown logger '{kind}' (known: {sorted(LOGGER_REGISTRY)})")
    return _construct(cls, config)


def build_tools(names: list[str]) -> list[Tool]:
    """Instantiate tools by registry name; fail loudly on unknown names.

    File tools share one EditLedger instance so read-before-edit tracking
    works across the whole tool set within a session.
    """
    from mantra.implementations.tools.edit_ledger import EditLedger

    ledger = EditLedger()
    tools = []
    for name in names:
        cls = TOOL_REGISTRY.get(name)
        if cls is None:
            raise ConfigError(
                f"unknown tool '{name}' (known: {sorted(TOOL_REGISTRY)})"
            )
        tool = cls()
        if hasattr(tool, "ledger"):
            tool.ledger = ledger
        tools.append(tool)
    return tools


def _construct(cls, config: dict):
    """Build a component from its config section.

    Only keys matching constructor parameters are forwarded, so sections may
    carry documentation or future fields safely.
    """
    params = _constructor_params(cls)
    kwargs = {}
    for key, value in config.items():
        if key in ("provider", "type"):
            continue
        if key in params:
            kwargs[key] = value
    missing_required = {
        name for name, param in params.items()
        if param.default is param.empty and name not in kwargs and not name.startswith("_")
    }
    if missing_required:
        raise ConfigError(
            f"{cls.__name__} requires parameters not present in config: "
            f"{sorted(missing_required)}"
        )
    return cls(**kwargs)


def _constructor_params(cls) -> dict[str, inspect.Parameter]:
    return {
        name: param
        for name, param in inspect.signature(cls.__init__).parameters.items()
        if name != "self"
    }
