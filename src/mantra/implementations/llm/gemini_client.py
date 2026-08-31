"""Google Gemini client: stdlib only, buffered/streaming, retries.

Translates MANTRA's OpenAI-style message history into Gemini's
``generateContent`` shape and back. Consecutive same-role turns are
merged because Gemini refuses to alternate twice.
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

DeltaCallback = Callable[[str], None]


def translate_body(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Split off the system prompt; normalise contents for generateContent."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        if role == "system":
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                system_parts.append(text)
            continue
        if role == "user":
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                append("user", [{"text": text}])
            continue
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                parts.append({"text": content})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                args = function.get("arguments")
                if isinstance(args, str) and args.strip():
                    try:
                        parsed = json.loads(args)
                        args = parsed if isinstance(parsed, dict) else {}
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                parts.append({"functionCall": {"name": function.get("name") or "", "args": args}})
            if parts:
                append("model", parts)
            continue
        if role == "tool":
            text = message.get("content")
            text = text if isinstance(text, str) else (str(text) if text is not None else "")
            result = {"result": text}
            append(
                "user",
                [
                    {
                        "functionResponse": {
                            "name": message.get("name") or "",
                            "response": result,
                        }
                    }
                ],
            )
            continue
    return "\n\n".join(system_parts), contents


def translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function schema -> Gemini functionDeclarations."""
    if not tools:
        return []
    declarations = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict) or not function.get("name"):
            continue
        declarations.append(
            {
                "name": function.get("name"),
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


def parse_stream(lines, on_delta: DeltaCallback | None = None) -> LLMResponse:
    """Parse a Gemini SSE stream.

    Text arrives per-part as it is produced; functionCall parts usually
    arrive whole. Arguments are accumulated per part index in case a
    gateway fragments them.
    """
    text_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    content_bytes = 0
    part_idx = 0
    malformed = 0

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
            malformed += 1
            if malformed > 20:
                raise LLMError("stream contained too many malformed chunks")
            continue
        malformed = 0
        candidates = chunk.get("candidates") or []
        if not candidates:
            continue
        content = (candidates[0] or {}).get("content") or {}
        for part in (content.get("parts") or []):
            if part is None:
                continue
            if isinstance(part.get("text"), str):
                piece = part["text"]
                if piece:
                    content_bytes += len(piece)
                    if content_bytes > _MAX_CONTENT_PARTS_BYTES:
                        raise LLMError("stream content exceeds cap")
                    text_parts.append(piece)
                    if on_delta is not None:
                        try:
                            on_delta(piece)
                        except Exception:
                            pass
            call = part.get("functionCall")
            if isinstance(call, dict):
                name = call.get("name") or ""
                args = call.get("args")
                if isinstance(args, str) and args.strip():
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                slot = calls.setdefault(part_idx, {"name": name, "_args": {}})
                slot["name"] = name or slot.get("name", "")
                if args:
                    slot["_args"].update(args)
            part_idx += 1
    text = "".join(text_parts)
    tool_calls = []
    for idx, slot in sorted(calls.items()):
        tool_calls.append(
            ToolCall(
                id=f"call_{idx}",
                name=slot.get("name") or "",
                arguments=slot.get("_args") or {},
            )
        )
    return LLMResponse(content=text or None, tool_calls=tool_calls, usage=None)


class GeminiClient(LLMClient):
    """Google Gemini ``generateContent`` client with tool use."""

    def __init__(
        self,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key_env: str = "GEMINI_API_KEY",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 3,
        stream: bool = True,
        include_usage: bool = True,
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
        self.last_usage: dict | None = None
        # Some gateways reject the systemInstruction field outright; once
        # flagged it is folded into the first user turn instead.
        self._system_supported = True
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
        system, contents = translate_body(messages, tools)
        payload: dict[str, Any] = {"contents": contents}
        if system and self._system_supported:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        provider_tools = translate_tools(tools)
        if provider_tools:
            payload["tools"] = provider_tools
        payload["generationConfig"] = {
            "temperature": self.temperature,
            "maxOutputTokens": self.max_tokens,
        }
        use_stream = self.stream and on_delta is not None

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            body = json.dumps(payload).encode("utf-8")
            try:
                if use_stream:
                    response = self._request(body + b"", on_delta, stream=True)
                else:
                    response = self._request(body, on_delta=None, stream=False)
                if response.usage:
                    self.last_usage = response.usage
                return response
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode(errors="replace")[:300]
                except OSError:
                    pass
                if exc.code == 400 and payload.get("systemInstruction") \
                        and ("system" in detail.lower() or "systeminstruction" in detail.lower()):
                    with self._lock:
                        self._system_supported = False
                    system_text = system
                    del payload["systemInstruction"]
                    if system_text and contents and contents[0]["role"] == "user":
                        contents[0]["parts"].insert(0, {"text": system_text})
                    last_error = detail or "systemInstruction rejected"
                    continue
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

    def _headers(self) -> dict[str, str]:
        api_key = resolve_key(self.api_key_env) or ""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MANTRA/1.0 (https://opencode.ai)",
        }
        if api_key:
            headers["x-goog-api-key"] = api_key
        return headers

    def _request(
        self,
        body: bytes,
        on_delta: DeltaCallback | None,
        stream: bool = False,
    ) -> LLMResponse:
        url = f"{self.base_url}/models/{self.model}:generateContent"
        if stream:
            url += "?alt=sse"
        request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if stream:
                return self._read_stream(response, on_delta)
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
            raise LLMError(f"{self.base_url} did not return JSON: {exc}") from exc
        return _parse_generate(data)

    def _read_stream(self, response, on_delta: DeltaCallback | None) -> LLMResponse:
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


def _parse_generate(data: dict[str, Any]) -> LLMResponse:
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini response returned no candidates")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = ""
    tool_calls = []
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            text += part["text"]
        call = part.get("functionCall")
        if isinstance(call, dict):
            args = call.get("args")
            if isinstance(args, str) and args.strip():
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                ToolCall(id=f"call_{idx}", name=call.get("name") or "", arguments=args)
            )
    usage = data.get("usageMetadata")
    return LLMResponse(content=text or None, tool_calls=tool_calls, usage=_normalise_usage(usage))


def _normalise_usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    out = {
        "prompt_tokens": usage.get("promptTokenCount"),
        "completion_tokens": usage.get("candidatesTokenCount"),
    }
    cached = usage.get("cachedContentTokenCount")
    if cached is not None:
        out["cached_tokens"] = cached
    return out