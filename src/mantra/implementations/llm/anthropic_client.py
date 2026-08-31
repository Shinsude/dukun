"""Anthropic Messages client: stdlib only, buffered/streaming, retries.

Translates MANTRA's OpenAI-style message history into Anthropic's
``/v1/messages`` shape and back again, so the AgentLoop needs no
knowledge of the provider.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from mantra.core.exceptions import LLMError
from mantra.core.keys import resolve as resolve_key
from mantra.interfaces.llm_client import LLMClient, LLMResponse, ToolCall

IncompleteRead = http.client.IncompleteRead

_MAX_RESPONSE_BYTES = 5_000_000
_MAX_CONTENT_PARTS_BYTES = 2_000_000
_DEFAULT_VERSION = "2023-06-01"

DeltaCallback = Callable[[str], None]


def translate_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Split out the system prompt and normalise roles for /messages.

    Returns ``(system, anon_messages)``. Consecutive same-role messages
    are merged, and tool results are embedded as ``tool_result`` blocks so
    Anthropic sees which assistant tool call they answer.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def append(block: dict[str, Any]) -> None:
        if out and out[-1]["role"] == block["role"]:
            out[-1]["content"].extend(block["content"])
        else:
            out.append(block)

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        if role == "system":
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                system_parts.append(text)
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                arguments = _parse_arguments(function.get("arguments"))
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"call_{len(out)}",
                        "name": function.get("name") or "",
                        "input": arguments,
                    }
                )
            if blocks:
                append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            text = str(message.get("content") or "")
            append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id") or "",
                            "content": text,
                        }
                    ],
                }
            )
            continue
        if role == "user":
            text = message.get("content")
            if not isinstance(text, str):
                text = str(text) if text is not None else ""
            if text.strip():
                append({"role": "user", "content": [{"type": "text", "text": text}]})
    return "\n\n".join(system_parts), out


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function schema -> Anthropic tool definitions."""
    if not tools:
        return []
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def parse_stream(lines, on_delta: DeltaCallback | None = None) -> LLMResponse:
    """Parse an Anthropic SSE stream into one response.

    Tracks text deltas for the UI and tool_use blocks by index, applying
    the input_json_delta fragments on content_block_stop.
    """
    index_parts: dict[int, str] = {}
    tool_uses: dict[int, dict[str, Any]] = {}
    content_bytes = 0
    malformed = 0

    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            malformed += 1
            if malformed > 20:
                raise LLMError("stream contained too many malformed chunks")
            continue
        malformed = 0
        etype = event.get("type", "")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = int(event.get("index", 0))
                tool_uses[idx] = {
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                }
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            idx = int(event.get("index", 0))
            if dtype == "text_delta":
                piece = delta.get("text") or ""
                if piece:
                    content_bytes += len(piece)
                    if content_bytes > _MAX_CONTENT_PARTS_BYTES:
                        raise LLMError("stream content exceeds cap")
                    if on_delta is not None:
                        try:
                            on_delta(piece)
                        except Exception:
                            pass
            elif dtype == "input_json_delta":
                slot = tool_uses.get(idx)
                if slot is not None:
                    slot.setdefault("_json", "")
                    slot["_json"] += delta.get("partial_json") or ""
        elif etype == "message_stop":
            break

    tool_calls = []
    for idx, slot in sorted(tool_uses.items()):
        raw_json = slot.pop("_json", "")
        if raw_json.strip():
            try:
                arguments = json.loads(raw_json)
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                arguments = {}
        else:
            arguments = slot.get("input") or {}
        tool_calls.append(
            ToolCall(id=slot.get("id") or f"call_{idx}", name=slot.get("name") or "", arguments=arguments)
        )
    return LLMResponse(
        content="".join(index_parts.values()) or None,
        tool_calls=tool_calls,
        usage=None,
    )


class AnthropicClient(LLMClient):
    """Anthropic ``/v1/messages`` client with tool use and streaming."""

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        api_key_env: str = "ANTHROPIC_API_KEY",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 3,
        stream: bool = True,
        include_usage: bool = True,
        api_version: str = _DEFAULT_VERSION,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.stream = stream
        self.include_usage = include_usage
        self.api_version = api_version
        self.last_usage: dict | None = None
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        try:
            has_key = bool(resolve_key(self.api_key_env))
        except OSError as exc:
            raise LLMError(f"could not read key for '{self.api_key_env}': {exc}") from exc
        if not has_key:
            raise LLMError(
                f"no API key available for '{self.api_key_env}'. Set that "
                "environment variable and reopen the terminal, or store the "
                "key once with: /connect"
            )
        system, body_messages = translate_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": body_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if system:
            payload["system"] = system
        provider_tools = translate_tools(tools)
        if provider_tools:
            payload["tools"] = provider_tools
        use_stream = self.stream and on_delta is not None
        if use_stream:
            payload["stream"] = True

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            body = json.dumps(payload).encode("utf-8")
            try:
                if use_stream:
                    response = self._request_stream(body, on_delta)
                else:
                    response = self._request(body)
                if response.usage:
                    self.last_usage = response.usage
                return response
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode(errors="replace")[:300]
                except OSError:
                    pass
                if exc.code in (401, 403):
                    raise LLMError(
                        f"the server rejected the key from '{self.api_key_env}' "
                        f"(HTTP {exc.code}) at {self.base_url}: {detail}"
                    ) from exc
                if exc.code == 400:
                    raise LLMError(
                        f"the server rejected the request (HTTP 400) at "
                        f"{self.base_url}: {detail or 'no detail given'}"
                    ) from exc
                last_error = f"HTTP {exc.code}: {detail}"
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
            except (urllib.error.URLError, TimeoutError, OSError, IncompleteRead) as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    def _headers(self, stream: bool = False) -> dict[str, str]:
        api_key = resolve_key(self.api_key_env) or ""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MANTRA/1.0 (https://opencode.ai)",
            "anthropic-version": self.api_version,
        }
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    def _request(self, body: bytes) -> LLMResponse:
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            try:
                raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
            except TypeError:
                raw_bytes = response.read()
            if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                raise LLMError("LLM response exceeds size cap")
            raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.base_url}/messages did not return JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("LLM response not a JSON object")

        text = ""
        tool_calls = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text += block.get("text") or ""
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id") or f"call_{len(tool_calls)}",
                        name=block.get("name") or "",
                        arguments=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
        usage = data.get("usage")
        return LLMResponse(content=text or None, tool_calls=tool_calls, usage=_normalise_usage(usage))

    def _request_stream(self, body: bytes, on_delta: DeltaCallback) -> LLMResponse:
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            def _line_iter():
                if hasattr(response, "readline"):
                    try:
                        while True:
                            raw = response.readline()
                            if not raw:
                                break
                            if isinstance(raw, bytes):
                                yield raw.decode("utf-8", errors="replace")
                            else:
                                yield str(raw)
                    except Exception:
                        pass
                    return
                for raw in response:  # type: ignore[attr-defined]
                    if isinstance(raw, bytes):
                        text = raw.decode("utf-8", errors="replace")
                    else:
                        text = str(raw)
                    for line in text.splitlines():
                        yield line + "\n"

            return parse_stream(_line_iter(), on_delta)


def _normalise_usage(usage: Any) -> dict[str, Any] | None:
    """Map Anthropic usage onto MANTRA's prompt/completion fields."""
    if not isinstance(usage, dict):
        return None
    out = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
    }
    cached = usage.get("cache_read_input_tokens")
    if cached is not None:
        out["cached_tokens"] = cached
    return out