"""Orchestrator with injected deps; hooks for context, abort, approval."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.exceptions import AbortError, LLMError, SandboxError, ToolError
from mantra.interfaces.evaluator import EvaluationResult, Evaluator
from mantra.interfaces.llm_client import LLMClient
from mantra.interfaces.logger import Logger
from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

DEFAULT_SYSTEM_PROMPT = (
    "You are a senior engineer and universal solver. Deliver correct, minimal, verified results for any task — "
    "coding, analysis, research, writing. Never hallucinate APIs, facts, or syntax; if unsure, say \"I don't know.\" "
    "Be direct, no fluff. Use Environment for workspace context — answer without tools when possible. "
    "For complex work: explore, plan, act, verify. Batch tools in one turn (list_dir + read_file), "
    "never repeat the same call, read before edit, confirm with tests before finishing."
)


@dataclass
class RunResult:
    """Result of one run: verdict, steps, timing, metrics."""

    task_id: str
    passed: bool
    evaluation_detail: str
    steps_used: int
    stopped_reason: str  # "final" | "max_steps" | "error" | "aborted"
    final_message: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


class AgentLoop:
    """Run one task: provision, loop, evaluate, cleanup."""

    def __init__(
        self,
        llm: LLMClient,
        sandbox: Sandbox,
        tools: list[Tool],
        evaluator: Evaluator,
        logger: Logger,
        events: EventBus | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 30,
        on_delta: Any = None,
        context: ContextManager | None = None,
        abort: threading.Event | None = None,
        approver: Any = None,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.tools = {t.name: t for t in tools}
        self.evaluator = evaluator
        self.logger = logger
        self.events = events or EventBus()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.on_delta = on_delta
        self.context = context
        self.abort = abort
        self.approver = approver

    @property
    def aborted(self) -> bool:
        return bool(self.abort and self.abort.is_set())

    def run(self, task: dict[str, Any]) -> RunResult:
        task_id = str(task.get("task_id", "unnamed"))
        started = time.monotonic()
        context = self.context or ContextManager()
        self.context = context
        self._seed_context(context, task)
        # Share abort signal so sandbox exec can be interrupted.
        try:
            setattr(self.sandbox, "abort", self.abort)
        except Exception:
            pass

        tool_schemas = [t.schema() for t in self.tools.values()]

        self._emit("run_start", {"task_id": task_id})
        stopped_reason = "max_steps"
        final_message: str | None = None
        steps = 0
        aborted = False
        metrics: dict[str, float] = {"tool_errors": 0, "denied": 0}
        recent_calls: dict[str, int] = {}

        try:
            self.sandbox.setup(task)
            while steps < self.max_steps:
                if self.aborted:
                    stopped_reason = "aborted"
                    break
                steps += 1

                response = self.llm.chat(
                    context.messages, tools=tool_schemas, on_delta=self.on_delta
                )
                # Response must be LLMResponse-like.
                if response is None or not hasattr(response, "is_final"):
                    raise LLMError(f"LLM returned invalid response: {type(response).__name__}")
                self._absorb_usage(response, metrics)

                if response.is_final:
                    stopped_reason = "final"
                    final_message = response.content
                    # Append final answer to history for next-turn visibility.
                    content = response.content if isinstance(response.content, str) else (str(response.content) if response.content is not None else "")
                    context.append({"role": "assistant", "content": content})
                    break

                # Deduplicate tool call IDs per turn (LLM may repeat them).
                seen_ids: set[str] = set()
                dedup_calls = []
                for c in response.tool_calls or []:
                    cid = getattr(c, "id", "") or ""
                    original = cid
                    counter = 0
                    while cid in seen_ids:
                        counter += 1
                        cid = f"{original}_{counter}" if original else f"call_{counter}"
                    if cid != getattr(c, "id", ""):
                        try:
                            c.id = cid  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    seen_ids.add(cid)
                    dedup_calls.append(c)
                # Serialize tool arguments safely.
                tool_calls_payload = []
                for call in dedup_calls:
                    try:
                        args_json = json.dumps(call.arguments)
                    except (TypeError, ValueError) as exc:
                        raise LLMError(f"tool arguments not serializable for '{call.name}': {exc}") from exc
                    tool_calls_payload.append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": args_json,
                            },
                        }
                    )
                # Record assistant turn once per reply.
                content = response.content if isinstance(response.content, str) else (str(response.content) if response.content else "")
                context.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls_payload,
                    }
                )

                for idx, call in enumerate(dedup_calls):
                    if self.aborted:
                        stopped_reason = "aborted"
                        # Add aborted observations to avoid orphaned tool entries.
                        for remaining in dedup_calls[idx:]:
                            context.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": remaining.id,
                                    "name": remaining.name,
                                    "content": "ERROR: interrupted by operator",
                                }
                            )
                        break
                    # Block repeated identical tool calls (canonical key).
                    try:
                        key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
                    except Exception:
                        # Fallback: sorted items for stable key.
                        try:
                            if isinstance(call.arguments, dict):
                                parts = [f"{k}={v}" for k, v in sorted(call.arguments.items(), key=lambda x: str(x[0]))]
                                key = f"{call.name}:" + ",".join(parts)
                            else:
                                key = f"{call.name}:{call.arguments}"
                        except Exception:
                            key = f"{call.name}:{type(call.arguments).__name__}"
                    cnt = recent_calls.get(key, 0) + 1
                    recent_calls[key] = cnt
                    if cnt >= 2:
                        observation = f"ERROR: you already called {call.name} {call.arguments} — result is already in history above. Do not repeat. Use it or try a different file (e.g. README.md, pyproject.toml)."
                        metrics["tool_errors"] += 1
                    else:
                        observation = self._dispatch_tool(task_id, steps, call, metrics)
                    context.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": observation,
                        }
                    )
                if stopped_reason == "aborted":
                    break
            else:
                stopped_reason = "max_steps"
        except AbortError:
            aborted = True
            stopped_reason = "aborted"
            final_message = "(interrupted)"
        except (LLMError, SandboxError, ToolError) as exc:
            stopped_reason = "error"
            final_message = f"mantra error: {exc}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": str(exc)})
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - ensure RunResult even on unexpected crash
            stopped_reason = "error"
            final_message = f"mantra error: {exc}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": str(exc)})
            except Exception:
                pass
        finally:
            if aborted or stopped_reason == "aborted":
                evaluation = EvaluationResult(
                    passed=False, detail="interrupted before completion"
                )
            else:
                try:
                    evaluation = self.evaluator.evaluate(self.sandbox, task)
                except Exception as exc:  # noqa: BLE001 - verdict must exist
                    evaluation = EvaluationResult(
                        passed=False, detail=f"evaluator crashed: {exc}"
                    )
            try:
                self.sandbox.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not mask results
                pass

        elapsed = time.monotonic() - started
        result = RunResult(
            task_id=task_id,
            passed=evaluation.passed and stopped_reason not in ("error", "aborted"),
            evaluation_detail=evaluation.detail,
            steps_used=steps,
            stopped_reason=stopped_reason,
            final_message=final_message,
            metrics=dict(metrics),
            elapsed_seconds=elapsed,
        )
        self._emit("run_end", _result_payload(result))
        self.logger.log("run_result", _result_payload(result))
        return result

    def _dispatch_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Approve and execute one tool; failures become observations."""
        try:
            if self.approver is not None and not self.approver.check(call.name, call.arguments):
                metrics["denied"] += 1
                self._emit(
                    "tool_denied",
                    {"task_id": task_id, "step": step, "tool": call.name},
                )
                return (
                    f"ERROR: the operator denied '{call.name}'. Do not retry it; "
                    "explain what you would have done and ask, or use another approach."
                )
        except Exception as exc:  # noqa: BLE001 - approver must not crash run
            metrics["tool_errors"] += 1
            return f"ERROR: approval check failed for '{call.name}': {exc}"
        return self._execute_tool(task_id, step, call, metrics)

    def _execute_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Execute tool; convert failures to observations."""
        tool = self.tools.get(call.name)
        if tool is None:
            metrics["tool_errors"] += 1
            return f"ERROR: unknown tool '{call.name}'"
        self._emit(
            "tool_call",
            {"task_id": task_id, "step": step, "tool": call.name, "args": call.arguments},
        )
        started = time.monotonic()
        try:
            observation = tool.execute(self.sandbox, **call.arguments)
        except AbortError:
            raise
        except TypeError as exc:
            observation = f"ERROR: bad arguments for '{call.name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface to the agent
            observation = f"ERROR: tool '{call.name}' failed: {exc}"
        if str(observation).startswith("ERROR"):
            metrics["tool_errors"] += 1
        # Include edit result so UI can show diffs.
        result_payload: dict = {
            "task_id": task_id,
            "step": step,
            "tool": call.name,
            "seconds": round(time.monotonic() - started, 3),
            "ok": not str(observation).startswith("ERROR"),
        }
        if call.name in ("edit_file", "write_file"):
            result_payload["result"] = observation
        self._emit("tool_result", result_payload)
        return observation

    def _seed_context(self, context: ContextManager, task: dict[str, Any]) -> None:
        """Seed first turn or append to existing history."""
        rendered = self._render_task(task)
        if not context.messages:
            context.seed(self.system_prompt, rendered)
        else:
            context.append({"role": "user", "content": rendered})

    def _absorb_usage(self, response, metrics: dict[str, float]) -> None:
        usage = getattr(response, "usage", None)
        if not isinstance(usage, dict):
            # Some gateways return usage as object with attributes
            try:
                usage = dict(usage)  # type: ignore[arg-type]
            except Exception:
                metrics["usage_unknown"] = metrics.get("usage_unknown", 0) + 1
                return
            if not isinstance(usage, dict):
                metrics["usage_unknown"] = metrics.get("usage_unknown", 0) + 1
                return
        def _to_int(v: Any) -> int | None:
            if isinstance(v, (int, float)):
                iv = int(v)
                return iv if iv >= 0 else None
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                try:
                    iv = int(v.strip())
                    return iv if iv >= 0 else None
                except ValueError:
                    return None
            return None
        prompt = _to_int(usage.get("prompt_tokens"))
        completion = _to_int(usage.get("completion_tokens"))
        if prompt is not None:
            metrics["tokens_in"] = metrics.get("tokens_in", 0) + prompt
        if completion is not None:
            metrics["tokens_out"] = metrics.get("tokens_out", 0) + completion
        # Prompt caching: providers report cached_tokens inside the usage
        # object.  The field lives at different nesting depths depending on
        # the provider (OpenAI uses prompt_tokens_details.cached_tokens,
        # some others flatten it), so we check both levels.
        cached = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _to_int(details.get("cached_tokens", 0))
        elif "cached_tokens" in usage:
            cached = _to_int(usage.get("cached_tokens", 0))
        if cached is not None:
            metrics["cache_hit"] = metrics.get("cache_hit", 0) + cached

    def _render_task(self, task: dict[str, Any]) -> str:
        raw_statement = task.get("problem_statement")
        statement = str(raw_statement).strip() if raw_statement is not None else ""
        parts = [statement] if statement else []
        repo = task.get("repo_url")
        if repo:
            repo_str = str(repo).strip()
            if repo_str:
                parts.append(f"Repository: {repo_str} @ {task.get('base_commit', 'HEAD')}")
        return "\n\n".join(parts)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self.events.emit(event, payload)
        except Exception:
            pass
        try:
            self.logger.log(event, payload)
        except Exception:
            pass


def _result_payload(result: RunResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "passed": result.passed,
        "stopped_reason": result.stopped_reason,
        "steps_used": result.steps_used,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "evaluation_detail": result.evaluation_detail,
        "metrics": result.metrics,
    }
