"""Discovering the models an endpoint actually serves.

The point of asking for only a base URL and a key is that MANTRA can then
read the catalogue itself, rather than making the user type a model name
they have to look up somewhere else. That is a plain GET on ``/models``,
which everything speaking the OpenAI protocol implements.

The list is a convenience, never a gate: if discovery fails the user can
still type a model name by hand, which matters for endpoints that hide
their catalogue or front it with a proxy.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from mantra.core.exceptions import LLMError
from mantra.core.keys import resolve as resolve_key

DEFAULT_TIMEOUT = 20.0

# Substrings that mark a model as one that thinks before it answers.
# This is a hint used to offer an effort choice, not a gate: a wrong
# guess costs the user one extra prompt, nothing more.
_REASONING_HINTS = (
    "o1", "o3", "o4", "gpt-5", "reasoning", "thinking", "-think",
    "deepseek-r1", "r1-", "qwq", "magistral", "kimi-k2-thinking",
)
_REASONING_RE = re.compile("|".join(re.escape(h) for h in _REASONING_HINTS), re.IGNORECASE)

# Entries a gateway advertises that chat completions cannot use. They
# are dropped rather than shown: an embedding or a text-to-speech model
# in a chat picker is not a neutral extra row, it is a wrong answer the
# operator has to recognise and skip, and on a large account they are
# most of the list.
# "instruct" used to be filtered outright, which silently hid perfectly
# ordinary chat models whose names happen to contain it. Only the legacy
# completion families are noise; everything else stays in the catalogue.
_NOISE_RE = re.compile(
    r"("
    r"embedding|whisper|transcri\w*|speech|audio|realtime|"
    r"tts|dall-e|image|sora|video|moderation|"
    r"rerank|re-rank|similarity|babbage|davinci|"
    r"turbo-instruct|instruct-?\d+\.?\d*$|"
    r"computer-use|codex-mini"
    r")",
    re.IGNORECASE,
)

# Date-stamped snapshots: ``gpt-4o-2024-11-20``, ``gpt-4o-20241120``,
# ``claude-3-5-sonnet-20241022``. Real and selectable, but they are the
# frozen form of a model that is also listed under its plain name, so
# they are ranked to the bottom instead of crowding out the name the
# operator was looking for.
_SNAPSHOT_RE = re.compile(r"-(20\d{2})-?(\d{2})?-?(\d{2})?$")


def is_reasoning_model(model_id: str) -> bool:
    """Whether to offer a thinking-effort choice for this model."""
    return bool(model_id and _REASONING_RE.search(model_id))


def _looks_like_a_model(model_id: str) -> bool:
    return bool(model_id) and not _NOISE_RE.search(model_id)


def _rank(model_id: str) -> tuple[int, str]:
    """Sort key: live names first, dated snapshots last, then A-Z.

    Alphabetical alone puts ``gpt-4o-2024-05-13`` above ``gpt-4o`` and
    scatters a family across the list. Only the snapshot flag is used
    to reorder - families still group together, which is what makes a
    long catalogue scannable.
    """
    return (1 if _SNAPSHOT_RE.search(model_id) else 0, model_id.lower())


def rank_models(model_ids: list[str]) -> list[str]:
    """Order a catalogue for a human to read."""
    return sorted(model_ids, key=_rank)


def fetch_models(
    base_url: str,
    api_key_env: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """Model ids advertised at ``{base_url}/models``, sorted and de-noised.

    Raises LLMError with a plain-English cause, because the two common
    failures - a wrong key and an endpoint without a catalogue - need
    different advice.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise LLMError("no base URL to ask for models")

    api_key = resolve_key(api_key_env)
    headers = {"Accept": "application/json", "User-Agent": "MANTRA/1.0 (https://opencode.ai)"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(f"{base}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:200]
        except OSError:
            pass
        if exc.code in (401, 403):
            raise LLMError(
                f"the endpoint refused the key (HTTP {exc.code}). Check the "
                f"key, or re-enter it with: /connect"
            ) from exc
        if exc.code == 404:
            raise LLMError(
                f"{base} has no /models listing (HTTP 404). Type the model "
                "name by hand: /model <name>"
            ) from exc
        raise LLMError(f"could not list models (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"could not reach {base}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{base}/models did not return JSON") from exc

    return _extract_ids(payload)


def _extract_ids(payload: Any) -> list[str]:
    """Pull model ids out of the handful of shapes endpoints return."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []

    ids: list[str] = []
    for entry in data:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = entry.get("id") or entry.get("name") or entry.get("model") or ""
        else:
            continue
        candidate = str(candidate).strip()
        if candidate and candidate not in ids and _looks_like_a_model(candidate):
            ids.append(candidate)
    return rank_models(ids)
