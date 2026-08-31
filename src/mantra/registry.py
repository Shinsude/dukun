"""Registry: map config names to classes; extend without core changes.

Third-party tools can be registered from plain ``.py`` files in plugin
directories. Listing a directory in the ``MANTRA_PLUGINS`` environment
variable (path-separated) or in the config ``plugins`` array makes every
``Tool`` subclass defined there available by name to ``build_tools``.
The code in a plugin runs in this process, so treat plugin directories
like any other trusted code you add to the package.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from typing import Iterable

from mantra.core.exceptions import ConfigError
from mantra.implementations.evaluators.command_evaluator import CommandEvaluator
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.llm.anthropic_client import AnthropicClient
from mantra.implementations.llm.gemini_client import GeminiClient
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
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
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
# Alias without underscore for usability.
TOOL_REGISTRY["webfetch"] = WebFetchTool

# Directories whose *.py files are loaded as tool plugins.
PLUGIN_ENV = "MANTRA_PLUGINS"


def plugin_dirs(config_plugins: Iterable[str] | None = None) -> list[str]:
    """Plugin directories from the environment and the config, in order."""
    dirs: list[str] = []
    env = os.environ.get(PLUGIN_ENV, "")
    if env.strip():
        for piece in env.split(os.pathsep):
            piece = piece.strip()
            if piece:
                dirs.append(piece)
    for entry in config_plugins or []:
        if isinstance(entry, str) and entry.strip():
            dirs.append(entry.strip())
    return dirs


def load_plugins(config_plugins: Iterable[str] | None = None) -> list[str]:
    """Register Tool subclasses found in plugin directories.

    Returns the names of tools that were added. Already-registered names
    raise ConfigError rather than silently shadowing a built-in.
    """
    added: list[str] = []
    for directory in plugin_dirs(config_plugins):
        added.extend(_load_plugin_dir(directory))
    return added


def _load_plugin_dir(directory: str) -> list[str]:
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        raise ConfigError(f"plugin directory not found: {directory}")
    added: list[str] = []
    files = sorted(
        name for name in os.listdir(directory)
        if name.endswith(".py") and not name.startswith("_")
    )
    if not files:
        return added
    for name in files:
        module_name = f"mantra_plugin_{name[:-3]}"
        full = os.path.join(directory, name)
        try:
            spec = importlib.util.spec_from_file_location(module_name, full)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"plugin '{name}' failed to load: {exc}") from exc
        candidates = list(vars(module).values())
        declared = getattr(module, "tools", None)
        if declared is not None:
            candidates = candidates + list(declared)
        for value in candidates:
            if not (isinstance(value, type) and issubclass(value, Tool)):
                continue
            if value is Tool or not value.name:
                continue
            if value.name in TOOL_REGISTRY:
                raise ConfigError(
                    f"plugin '{name}' defines tool '{value.name}', which already exists"
                )
            TOOL_REGISTRY[value.name] = value
            added.append(value.name)
    return added


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


def build_tools(names: list[str], plugins: Iterable[str] | None = None) -> list[Tool]:
    """Create tools by name; share one EditLedger; dedupe aliases.

    When ``plugins`` lists directories, their Tool subclasses become
    available first (also honouring the MANTRA_PLUGINS env var).
    """
    from mantra.implementations.tools.edit_ledger import EditLedger

    # Env var dirs load even when no config plugins are listed.
    load_plugins(plugins)

    ledger = EditLedger()
    seen_classes: set[type[Tool]] = set()
    tools = []
    for name in names:
        cls = TOOL_REGISTRY.get(name)
        if cls is None:
            raise ConfigError(
                f"unknown tool '{name}' (known: {sorted(TOOL_REGISTRY)})"
            )
        if cls in seen_classes:
            continue
        seen_classes.add(cls)
        tool = cls()
        if hasattr(tool, "ledger"):
            tool.ledger = ledger
        tools.append(tool)
    return tools


def _construct(cls, config: dict):
    """Build component; forward only matching params."""
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
