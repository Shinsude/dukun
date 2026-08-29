"""The agent loop: the only stateful orchestrator in the harness.

Dependencies arrive fully constructed (dependency injection); the loop
knows interfaces, never concrete implementations.

Three hooks let a front end turn one-shot grading into an interactive tool
without the core learning anything about terminals:

    context   reuse one :class:`ContextManager` so the model remembers
              earlier turns instead of starting fresh every message
    abort     a :class:`threading.Event` checked between steps and inside
              the streaming callback, so Ctrl+C stops the run cleanly
    approver  consulted before every mutating tool call
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
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
    "You are a coding agent working inside a repository checkout. "
    "Use the provided tools to explore the code, make changes, and verify "
    "your work by running commands. When you believe the task is complete, "
    "reply with a final summary instead of a tool call."
)


@dataclass
class RunResult:
    """Everything worth keeping from one agent run."""

    task_id: str
    passed: bool
    evaluation_detail: str
    steps_used: int
    stopped_reason: str  # "final" | "max_steps" | "error" | "aborted"
    final_message: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


class AgentLoop:
    """Runs one task: provision sandbox -> loop -> evaluate -> cleanup."""

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
        # Propagate abort to sandbox so exec can be interrupted mid-command.
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
        provisioned = False
        metrics: dict[str, float] = {"tool_errors": 0, "denied": 0}

        try:
            self.sandbox.setup(task)
            provisioned = True
            while steps < self.max_steps:
                if self.aborted:
                    stopped_reason = "aborted"
                    break
                steps += 1

                response = self.llm.chat(
                    context.messages, tools=tool_schemas, on_delta=self.on_delta
                )
                self._absorb_usage(response, metrics)

                if response.is_final:
                    stopped_reason = "final"
                    final_message = response.content
                    # The answer has to reach history too, or the next turn
                    # cannot see what was just said. A model that streams no
                    # content yields None, and a null content field is
                    # rejected by several servers on the next request.
                    context.append({"role": "assistant", "content": response.content or ""})
                    break

                # Record the assistant turn once per model reply.
                context.append(
                    {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in response.tool_calls
                        ],
                    }
                )

                for position, call in enumerate(response.tool_calls):
                    if self.aborted:
                        stopped_reason = "aborted"
                        # Every call the model declared must be answered,
                        # or the history ends with an assistant message
                        # whose tool calls have no results - which the
                        # server rejects on the next request.
                        self._cancel_calls(context, response.tool_calls[position:])
                        break
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
        except AbortError:
            aborted = True
            stopped_reason = "aborted"
            final_message = "(interrupted)"
        except (LLMError, SandboxError, ToolError) as exc:
            stopped_reason = "error"
            final_message = f"mantra error: {exc}"
            self._emit("run_error", {"task_id": task_id, "error": str(exc)})
        finally:
            # Last line of defence: whatever happened above, the history
            # handed back to the caller must not be one the server will
            # refuse on the next turn.
            self._balance_history(context)
            if aborted or stopped_reason == "aborted":
                evaluation = EvaluationResult(
                    passed=False, detail="interrupted before completion"
                )
            elif not provisioned:
                # Grading a sandbox that never came up runs the test
                # command in an empty directory and presents the outcome
                # as this run's verdict, which is simply untrue.
                evaluation = EvaluationResult(
                    passed=False,
                    detail="sandbox provisioning failed; the run was not evaluated",
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

    @staticmethod
    def _cancel_calls(context: ContextManager, calls) -> None:
        """Answer calls that will never run, so the history stays balanced."""
        for call in calls:
            context.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": "ERROR: cancelled - the operator interrupted this run",
                }
            )

    def _balance_history(self, context: ContextManager) -> None:
        """Answer any tool call left dangling at the end of the history.

        A trailing assistant message that declares calls without matching
        results is rejected outright by the chat-completions protocol, and
        because a console session reuses one context across turns, a
        single interruption would otherwise poison every later turn.
        """
        for index, message in enumerate(context.messages):
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            if not calls:
                continue
            kind = type(message).__name__
            del kind  # messages are plain dicts; kept for clarity in tracebacks
        # Only the final assistant message can be unbalanced: earlier ones
        # are closed by the loop before the next one is appended.
        if not context.messages:
            return
        tail = context.messages[-1]
        if tail.get("role") != "assistant" or not (tail.get("tool_calls") or []):
            return
        answered = {
            message.get("tool_call_id")
            for message in context.messages
            if message.get("role") == "tool"
        }
        pending = [c for c in tail["tool_calls"] if c.get("id") not in answered]
        if pending:
            self._cancel_calls(
                context,
                [
                    SimpleNamespace(id=c.get("id", ""), name=(c.get("function") or {}).get("name", ""))
                    for c in pending
                ],
            )

    def _dispatch_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Approve, then execute one tool call; every failure is an observation."""
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
        return self._execute_tool(task_id, step, call, metrics)

    def _execute_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Dispatch one tool call; every failure becomes an observation."""
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
        except TypeError as exc:
            observation = f"ERROR: bad arguments for '{call.name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface to the agent
            observation = f"ERROR: tool '{call.name}' failed: {exc}"
        if isinstance(observation, str) and observation.startswith("ERROR"):
            metrics["tool_errors"] += 1
        self._emit(
            "tool_result",
            {
                "task_id": task_id,
                "step": step,
                "tool": call.name,
                "seconds": round(time.monotonic() - started, 3),
                "ok": not str(observation).startswith("ERROR"),
            },
        )
        return observation

    def _seed_context(self, context: ContextManager, task: dict[str, Any]) -> None:
        """First turn pins system + task; later turns append the new request.

        Without this a REPL would start from scratch on every message and the
        model could never refer to anything it did a minute ago.
        """
        rendered = self._render_task(task)
        if not context.messages:
            context.seed(self.system_prompt, rendered)
        else:
            # Keep the pinned system message up to date if the caller
            # supplied a new system prompt for this turn (e.g. standing
            # goal or skill attachment). This covers reuse of the same
            # context across multiple runs.
            if context.messages[0].get("role") == "system" and context.messages[0].get("content") != self.system_prompt:
                context.messages[0]["content"] = self.system_prompt
                context.resync()
            context.append({"role": "user", "content": rendered})

    def _absorb_usage(self, response, metrics: dict[str, float]) -> None:
        usage = getattr(response, "usage", None)
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, (int, float)):
            metrics["tokens_in"] = metrics.get("tokens_in", 0) + prompt
        if isinstance(completion, (int, float)):
            metrics["tokens_out"] = metrics.get("tokens_out", 0) + completion
        # Prompt caching: providers report cached_tokens inside the usage
        # object.  The field lives at different nesting depths depending on
        # the provider (OpenAI uses prompt_tokens_details.cached_tokens,
        # some others flatten it), so we check both levels.
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens", 0) or 0
        elif "cached_tokens" in usage:
            cached = usage.get("cached_tokens", 0) or 0
        if isinstance(cached, (int, float)):
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
        self.events.emit(event, payload)
        self.logger.log(event, payload)


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
