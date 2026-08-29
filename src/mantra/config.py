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

# Thinking budget for reasoning models. Ordered: each is more thorough
# and slower than the one before. None means "do not send the field".
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

DEFAULTS = {
    "system_prompt": None,  # falls back to the loop default
    "max_steps": 30,
    "max_messages": 200,
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        # Optional thinking budget for reasoning models. null sends
        # nothing at all, which is what non-reasoning endpoints expect.
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
    # Interactive-session behaviour (the headless CLI ignores these).
    "approvals": "auto",  # default | auto | yolo | plan
    "context": {"max_messages": 200, "max_chars": 240000},
    "auto_compact_tokens": 60000,  # summarise the history past this size; 0 disables
    "verbose": False,  # echo truncated tool output as it arrives
    "skills": {
        # Attach a matching skill to a plain prompt without being asked.
        # Off still leaves /skills fully usable by hand.
        "auto": True,
        # A bundle runs several agent turns, which is too much to start on
        # a guess, so a matching bundle is only ever offered as a hint.
        "auto_bundle": False,
    },
}


def load_config(path: str) -> dict:
    """Load a config file and merge it over the defaults (shallow per section)."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
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
    required = ("llm", "sandbox", "evaluator")
    missing = [key for key in required if key not in merged]
    if missing:
        raise ConfigError(f"config sections missing: {missing}")
    tools = merged.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ConfigError("config must list at least one tool")
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
