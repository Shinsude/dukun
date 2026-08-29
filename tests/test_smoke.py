"""End-to-end smoke test for the MANTRA harness core.

Runs the full agent loop offline: a scripted LLM fixes a seeded bug through
the real tools, the command evaluator grades the result, and edge behaviors
(context truncation, unknown components) are checked alongside.
Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.config import merge_defaults
from mantra.core.agent_loop import AgentLoop
from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.exceptions import ConfigError
from mantra.implementations.evaluators.command_evaluator import CommandEvaluator
from mantra.implementations.llm.mock_client import (
    ScriptedLLMClient,
    final_response,
    tool_call_response,
)
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.registry import build_tools

GREET_BUGGY = 'def greet():\n    return "helo"\n'
GREET_TESTS = (
    "import unittest\n"
    "from greet import greet\n\n"
    "class TestGreet(unittest.TestCase):\n"
    "    def test_greeting(self):\n"
    "        self.assertEqual(greet(), \"hello\")\n"
)
TEST_CMD = f'"{sys.executable}" -m unittest test_greet -v'


def make_workspace() -> str:
    root = tempfile.mkdtemp(prefix="mantra-smoke-")
    with open(os.path.join(root, "greet.py"), "w", encoding="utf-8") as handle:
        handle.write(GREET_BUGGY)
    with open(os.path.join(root, "test_greet.py"), "w", encoding="utf-8") as handle:
        handle.write(GREET_TESTS)
    return root


def build_loop(llm: ScriptedLLMClient, workspace: str, log_path: str) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        sandbox=LocalSandbox(workspace),
        tools=build_tools(
            ["read_file", "edit_file", "list_dir", "run_command"]
        ),
        evaluator=CommandEvaluator(test_cmd=TEST_CMD, timeout=60),
        logger=JsonlLogger(log_path),
        events=EventBus(),
        max_steps=10,
    )


TASK = {
    "task_id": "smoke-fix-greeting",
    "problem_statement": "greet.py returns 'helo'; make it return 'hello'.",
    "test_cmd": TEST_CMD,
}


class SmokeTest(unittest.TestCase):
    def test_full_run_passes(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response("list_dir", {"path": "."}),
                tool_call_response("read_file", {"path": "greet.py"}),
                tool_call_response(
                    "edit_file",
                    {"path": "greet.py", "old_string": '"helo"', "new_string": '"hello"'},
                ),
                tool_call_response("run_command", {"command": TEST_CMD}),
                final_response("Fixed the typo in greet()."),
            ]
        )
        log_path = os.path.join(workspace, "run.jsonl")
        result = build_loop(llm, workspace, log_path).run(TASK)

        self.assertTrue(result.passed, msg=result.evaluation_detail)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.steps_used, 5)

        # The edit actually landed on disk.
        with open(os.path.join(workspace, "greet.py"), encoding="utf-8") as handle:
            self.assertIn('"hello"', handle.read())

        # Structured JSONL evidence was written.
        with open(log_path, encoding="utf-8") as handle:
            events = [line.split('"event": "', 1)[1].split('"', 1)[0] for line in handle]
        self.assertIn("tool_call", events)
        self.assertIn("run_result", events)

    def test_failed_fix_reports_failure(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response(
                    "edit_file",
                    {"path": "greet.py", "old_string": '"helo"', "new_string": '"hi"'},
                ),
                final_response("Done (but wrong)."),
            ]
        )
        result = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl")).run(TASK)
        self.assertFalse(result.passed)
        self.assertEqual(result.stopped_reason, "final")

    def test_unknown_tool_becomes_observation_not_crash(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response("nonexistent_tool", {}),
                final_response("Giving up."),
            ]
        )
        result = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl")).run(TASK)
        self.assertFalse(result.passed)  # bug never fixed -> evaluation fails

    def test_max_steps_stops_the_loop(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [tool_call_response("list_dir", {"path": "."})] * 20
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 3
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(len(llm.script), 17)  # exactly max_steps calls consumed


class ContextTest(unittest.TestCase):
    def test_truncation_keeps_pinned_messages(self):
        ctx = ContextManager(max_messages=4)
        ctx.seed("system prompt", "task")
        for i in range(6):
            ctx.append({"role": "tool", "content": f"obs-{i}"})
        self.assertEqual(len(ctx.messages), 4)
        self.assertEqual(ctx.messages[0]["content"], "system prompt")
        self.assertEqual(ctx.messages[1]["content"], "task")
        self.assertEqual(ctx.messages[-1]["content"], "obs-5")


class RegistryConfigTest(unittest.TestCase):
    def test_unknown_tool_name_fails_loudly(self):
        with self.assertRaises(ConfigError):
            build_tools(["read_file", "no_such_tool"])

    def test_config_merge_fills_defaults(self):
        merged = merge_defaults({"evaluator": {"type": "command", "test_cmd": "echo ok"}})
        self.assertEqual(merged["evaluator"]["test_cmd"], "echo ok")
        self.assertIn("tools", merged)


class EditLedgerTest(unittest.TestCase):
    """Read-before-edit contract adopted from the workflow-layer harness."""

    def setUp(self):
        self.workspace = make_workspace()

    def _tools(self):
        return build_tools(["read_file", "write_file", "edit_file"])

    def _edit(self, tools, old, new, path="greet.py"):
        edit = next(t for t in tools if t.name == "edit_file")
        return edit.execute(LocalSandbox(self.workspace), path=path, old_string=old, new_string=new)

    def test_unread_edit_is_rejected(self):
        result = self._edit(self._tools(), '"helo"', '"hello"')
        self.assertTrue(result.startswith("ERROR"), msg=result)
        # File untouched.
        with open(os.path.join(self.workspace, "greet.py"), encoding="utf-8") as handle:
            self.assertIn('"helo"', handle.read())

    def test_read_then_edit_passes(self):
        tools = self._tools()
        reader = next(t for t in tools if t.name == "read_file")
        reader.execute(LocalSandbox(self.workspace), path="greet.py")
        result = self._edit(tools, '"helo"', '"hello"')
        self.assertTrue(result.startswith("OK"), msg=result)

    def test_stale_edit_after_external_change_is_rejected(self):
        tools = self._tools()
        sandbox = LocalSandbox(self.workspace)
        reader = next(t for t in tools if t.name == "read_file")
        reader.execute(sandbox, path="greet.py")
        # External mutation behind the agent's back.
        sandbox.write_file("greet.py", GREET_BUGGY + "# touched\n")
        result = self._edit(tools, '"helo"', '"hello"')
        self.assertIn("changed on disk since your last read", result)

    def test_write_then_edit_passes_without_read(self):
        tools = self._tools()
        writer = next(t for t in tools if t.name == "write_file")
        sandbox = LocalSandbox(self.workspace)
        writer.execute(sandbox, path="new.py", content="x = 1\n")
        edit = next(t for t in tools if t.name == "edit_file")
        result = edit.execute(sandbox, path="new.py", old_string="1", new_string="2")
        self.assertTrue(result.startswith("OK"), msg=result)


class KnowledgeTest(unittest.TestCase):
    def test_assemble_includes_known_failures_and_memory_tail(self):
        from mantra.core.knowledge import assemble_system_prompt

        kf = os.path.join(tempfile.mkdtemp(prefix="mantra-kf-"), "kf.md")
        mem = os.path.join(kf, os.pardir, "mem.md")
        with open(kf, "w", encoding="utf-8") as handle:
            handle.write("## KF-9 | never do the bad thing\n")
        with open(mem, "w", encoding="utf-8") as handle:
            handle.write("- 2026-08-26 | earlier note\n")
        prompt = assemble_system_prompt(
            "base prompt", known_failures_path=kf, memory_path=mem
        )
        self.assertIn("base prompt", prompt)
        self.assertIn("KF-9", prompt)
        self.assertIn("earlier note", prompt)

    def test_missing_files_yield_base_prompt_only(self):
        from mantra.core.knowledge import assemble_system_prompt

        prompt = assemble_system_prompt(
            "solo", known_failures_path="Z:/none.md", memory_path="Z:/none2.md"
        )
        self.assertEqual(prompt, "solo")

    def test_append_memory_prunes_oldest_beyond_cap(self):
        from mantra.core.knowledge import append_memory

        mem = os.path.join(make_workspace(), ".mantra", "memory.md")
        for i in range(50):
            append_memory(mem, f"- entry {i:03d} " + "x" * 200, cap=2000)
        size = os.path.getsize(mem)
        with open(mem, encoding="utf-8") as handle:
            content = handle.read()
        self.assertLessEqual(size, 2100)
        self.assertNotIn("entry 000", content)  # oldest pruned
        self.assertIn("entry 049", content)  # newest kept

    def test_workspace_instruction_file_discovered_and_injected(self):
        from mantra.core.knowledge import assemble_system_prompt, find_instructions_file

        ws = make_workspace()
        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("Always use tabs in this repo.\n")
        found = find_instructions_file(ws)
        self.assertIsNotNone(found)
        prompt = assemble_system_prompt("base", instructions_path=found)
        self.assertIn("Always use tabs", prompt)

    def test_instruction_file_preference_order(self):
        from mantra.core.knowledge import find_instructions_file

        ws = make_workspace()
        self.assertIsNone(find_instructions_file(ws))
        with open(os.path.join(ws, "CLAUDE.md"), "w", encoding="utf-8") as handle:
            handle.write("claude rules\n")
        self.assertTrue(find_instructions_file(ws).endswith("CLAUDE.md"))
        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("agents rules\n")
        self.assertTrue(find_instructions_file(ws).endswith("AGENTS.md"))


class SseStreamParseTest(unittest.TestCase):
    """Streaming parser must rebuild content and tool calls from chunks."""

    def _lines(self, *chunks):
        return [f"data: {json.dumps({'choices': [{'delta': c}]})}" for c in chunks] + [
            "data: [DONE]"
        ]

    def test_content_deltas_accumulate(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        seen = []
        result = parse_sse_stream(
            self._lines({"content": "Hel"}, {"content": "lo"}, {"content": "!"}),
            on_delta=seen.append,
        )
        self.assertEqual(result.content, "Hello!")
        self.assertEqual(seen, ["Hel", "lo", "!"])
        self.assertTrue(result.is_final)

    def test_tool_call_fragments_reassemble(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        lines = [
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "edit_", "arguments": ""}},
                ]}}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "file", "arguments": '{"path": '}},
                ]}}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '"a.py"}'}},
                ]}}]}
            ),
            "data: [DONE]",
        ]
        result = parse_sse_stream(lines)
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.name, "edit_file")
        self.assertEqual(call.arguments, {"path": "a.py"})
        self.assertIsNone(result.content)

    def test_done_sentinel_and_noise_tolerated(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        lines = [": keep-alive comment", "", "data: not-json{{", "data: [DONE]", "data: {}"]
        result = parse_sse_stream(lines)
        self.assertIsNone(result.content)


if __name__ == "__main__":
    unittest.main()
