"""Configuration loading for MANTRA.

Accepts JSON natively and YAML when PyYAML is installed. Secrets are never
stored in config files: the LLM section names an environment variable that
holds the API key at runtime.
"""

from __future__ import annotations

import copy
import json
import os

from mantra.core.exceptions import ConfigError

# Reasoning effort levels, ordered from fastest to most thorough. None omits the field.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

DEFAULTS = {
    "system_prompt": None,  # falls back to the loop default
    "max_steps": 30,
    # Message budget is defined only under context.
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        # Reasoning effort. Null omits the field for non-reasoning endpoints.
        "reasoning_effort": None,
    },
    "sandbox": {"provider": "local"},
    "tools": [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "run_command",
        "search_code",
        "find_file",
        "git_diff",
        "git_reset",
        "web_fetch",
    ],
    "evaluator": {"type": "command", "test_cmd": "python -m pytest tests/ -q"},
    "logging": {"type": "jsonl", "path": "logs/mantra-run.jsonl"},
    # Interactive defaults use the strictest approval mode.
    "approvals": "default",  # default | auto | yolo | plan
    "context": {"max_messages": 200, "max_chars": 240000},
    "auto_compact_tokens": 60000,  # summarise the history past this size; 0 disables
    "verbose": False,  # echo truncated tool output as it arrives
    # Use native terminal scrollback and mouse selection in the console.
    # Set false to opt into the fixed framed layout.
    "native_scrollback": True,
    "skills": {
        # Attach a matching skill to a plain prompt without being asked.
        # Off still leaves /skills fully usable by hand.
        "auto": True,
        # A bundle runs several agent turns, which is too much to start on
        # a guess, so a matching bundle is only ever offered as a hint.
        "auto_bundle": False,
    },
}


_MAX_CONFIG_BYTES = 1_000_000


def load_config(path: str) -> dict:
    """Load a config file and merge it deeply over the defaults."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    try:
        size = os.path.getsize(path)
        if size > _MAX_CONFIG_BYTES:
            raise ConfigError(f"config file too large ({size} bytes, limit {_MAX_CONFIG_BYTES})")
    except OSError:
        pass
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigError(f"config file too large (exceeds {_MAX_CONFIG_BYTES} bytes)")
    if path.lower().endswith((".yaml", ".yml")):
        data = _load_yaml(raw)
    else:
        data = _load_json(raw)
    return merge_defaults(data)


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return out


def merge_defaults(data: dict) -> dict:
    # Deep, not shallow: a shallow copy leaves the nested sections - llm,
    # skills, tools - shared with DEFAULTS, so a session setting its model
    # would silently rewrite the module defaults and every config loaded
    # afterwards in this process would inherit the change.
    merged = copy.deepcopy(DEFAULTS)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    # Presence alone says nothing: these keys are always present because
    # the defaults supply them. A section explicitly set to null, or a
    # tool list that is a string, used to sail through and then fail
    # with an attribute error deep inside assembly.
    required = ("llm", "sandbox", "evaluator")
    wrong_type = [
        key for key in required if not isinstance(merged.get(key), dict)
    ]
    if wrong_type:
        raise ConfigError(
            f"config sections must be objects: {', '.join(wrong_type)}"
        )
    tools = merged.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ConfigError("config must list at least one tool")
    _sentinel = object()
    bad_tool = next((t for t in tools if not isinstance(t, str) or not t.strip()), _sentinel)
    if bad_tool is not _sentinel:
        raise ConfigError(f"config tools must be non-empty names, got {bad_tool!r}")
    steps = merged.get("max_steps", 30)
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
        raise ConfigError(f"config max_steps must be a positive integer, got {steps!r}")
    mode = merged.get("approvals", "default")
    if mode not in ("default", "auto", "yolo", "plan"):
        raise ConfigError(
            f"config approvals must be one of default/auto/yolo/plan, got '{mode}'"
        )
    effort = merged.get("llm", {}).get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ConfigError(
            f"config llm.reasoning_effort must be one of "
            f"{'/'.join(REASONING_EFFORTS)} or null, got '{effort}'"
        )
    ctx = merged.get("context")
    if ctx is not None and not isinstance(ctx, dict):
        raise ConfigError("config context must be an object")
    if isinstance(ctx, dict):
        mm = ctx.get("max_messages")
        if mm is not None and (not isinstance(mm, int) or isinstance(mm, bool) or mm < 4):
            raise ConfigError(f"config context.max_messages must be an integer >=4, got {mm!r}")
        mc = ctx.get("max_chars")
        if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool) or mc < 2000):
            raise ConfigError(f"config context.max_chars must be an integer >=2000, got {mc!r}")
    return merged


def _load_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {exc}") from exc


def _load_yaml(raw: str) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "YAML config requires PyYAML; use a JSON config file instead"
        ) from exc
    try:
        return yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML config: {exc}") from exc
