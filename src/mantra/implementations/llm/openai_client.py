"""OpenAI-compatible chat client using only the standard library.

Works with any server exposing ``POST /v1/chat/completions`` with function
calling: OpenAI, Azure OpenAI gateways, vLLM, Ollama, LM Studio, etc.
The API key is read from the environment variable named in the config
(``api_key_env``), never from configuration files.

Streaming: when ``on_delta`` is provided, the request uses SSE and each
content fragment is passed to the callback as it arrives (token-level
display). Tool-call deltas are accumulated silently and returned complete.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from mantra.core.exceptions import LLMError
from mantra.core.keys import resolve as resolve_key
from mantra.interfaces.llm_client import LLMClient, LLMResponse, ToolCall

DeltaCallback = Callable[[str], None]

# A connection dropped mid-stream surfaces as this rather than an OSError,
# so it has to be named explicitly or it escapes the retry loop entirely.
IncompleteRead = http.client.IncompleteRead


def parse_sse_stream(lines, on_delta: DeltaCallback | None = None) -> LLMResponse:
    """Reduce an OpenAI SSE chunk sequence into one LLMResponse.

    Pure function over an iterable of decoded lines so it can be unit-tested
    without a network. Handles content deltas, tool_call deltas keyed by
    index, and the terminating ``data: [DONE]`` sentinel.
    """
    content_parts: list[str] = []
    # index -> {"id":..., "name":..., "args": "..."} accumulating fragments
    tool_acc: dict[int, dict[str, str]] = {}
    usage: dict | None = None

    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue  # tolerate keep-alive comments / partial noise
        # The usage object rides in a final chunk that carries no choices,
        # so it has to be read before the choices guard below.
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            content_parts.append(piece)
            if on_delta is not None:
                on_delta(piece)
        for call in delta.get("tool_calls") or []:
            idx = call.get("index", 0)
            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
            if call.get("id"):
                slot["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] = slot["name"] + fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

    tool_calls = []
    for i, slot in sorted(tool_acc.items()):
        try:
            arguments = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError as exc:
            # A stream cut mid-argument leaves partial JSON. That is a
            # transport failure like any other, so it has to come out as
            # one instead of a ValueError that nothing upstream catches.
            raise LLMError(
                f"the response ended mid-tool-call ({slot['name'] or 'call ' + str(i)}): {exc}"
            ) from exc
        tool_calls.append(
            ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=arguments)
        )
    content = "".join(content_parts)
    return LLMResponse(content=content or None, tool_calls=tool_calls, usage=usage)


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 3,
        stream: bool = True,
        include_usage: bool = True,
        reasoning_effort: str | None = None,
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
        self.reasoning_effort = reasoning_effort
        # Not every server understands stream_options; downgrade on a 400.
        self._usage_supported = include_usage
        # Nor reasoning_effort - local servers tend to reject it outright.
        self._reasoning_supported = reasoning_effort is not None
        # Reasoning models ask for the completion budget under a different
        # name. Remembered, or every turn pays for the same 400 again.
        self._token_field = "max_tokens"
        self._token_budget = max_tokens
        self.last_usage: dict | None = None

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        if not resolve_key(self.api_key_env):
            raise LLMError(
                f"no API key available for '{self.api_key_env}'. Set that "
                "environment variable, open a new terminal so it loads, or "
                "store the key once with: /connect"
            )

        use_stream = self.stream and on_delta is not None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            self._token_field: self._token_budget,
        }
        if tools:
            payload["tools"] = tools
        if use_stream:
            payload["stream"] = True
            if self._usage_supported:
                payload["stream_options"] = {"include_usage": True}
        if self._reasoning_supported and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

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
                # Each downgrade has to be for the field the server
                # actually complained about. Dropping reasoning_effort
                # because max_tokens was rejected would quietly turn off
                # reasoning on precisely the models that need it.
                if exc.code == 400 and payload.get("stream_options") \
                        and self._blamed(detail, "stream_options"):
                    self._usage_supported = False
                    del payload["stream_options"]
                    continue
                if exc.code == 400 and payload.get("reasoning_effort") \
                        and self._blamed(detail, "reasoning_effort"):
                    # Older and local servers reject the field outright
                    # rather than ignoring it. Shed it and carry on.
                    self._reasoning_supported = False
                    del payload["reasoning_effort"]
                    continue
                if exc.code == 400 and self._token_field == "max_tokens" \
                        and "max_completion_tokens" in detail:
                    # Reasoning models refuse max_tokens and want the
                    # completion budget named differently.
                    self._token_field = "max_completion_tokens"
                    payload.pop("max_tokens", None)
                    payload["max_completion_tokens"] = self._token_budget
                    continue
                if exc.code in (401, 403):
                    # Auth failures never succeed on retry; fail fast with cause.
                    raise LLMError(
                        f"the server rejected the key from '{self.api_key_env}' "
                        f"(HTTP {exc.code}) at {self.base_url}: {detail}"
                    ) from exc
                if exc.code == 400:
                    # We asked for something the server does not
                    # understand. Sending it again unchanged cannot
                    # succeed, and each attempt costs a round trip.
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

    @staticmethod
    def _blamed(detail: str, field: str) -> bool:
        """Did the server complain about this field?

        Servers that return an empty error body get the benefit of the
        doubt, which preserves the old behaviour of shedding one field
        per retry until the request goes through.
        """
        return not detail or field in detail

    def _headers(self) -> dict[str, str]:
        # Environment first, stored credential second.
        api_key = resolve_key(self.api_key_env) or ""
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request(self, body: bytes) -> LLMResponse:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"{self.base_url}/chat/completions did not return JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data.get("choices"):
            # An empty choices array is what a gateway returns when it
            # accepts the request and then has nothing to say. Indexing
            # it raised an IndexError that nothing upstream caught.
            raise LLMError(
                f"{self.base_url}/chat/completions returned no choices"
            )

        message = (data["choices"][0] or {}).get("message") or {}
        raw_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=call.get("id", ""),
                name=call["function"]["name"],
                arguments=json.loads(call["function"].get("arguments") or "{}"),
            )
            for call in raw_calls
        ]
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=data.get("usage") or None,
        )

    def _request_stream(self, body: bytes, on_delta: DeltaCallback) -> LLMResponse:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            lines = (raw.decode("utf-8", errors="replace") for raw in response)
            return parse_sse_stream(lines, on_delta)
