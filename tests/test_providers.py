"""Offline tests for the Anthropic and Gemini request translators.

None of these touch the network: they exercise the pure translation and
parser helpers, plus stream parsing over canned SSE buffers.
"""

from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.implementations.llm.anthropic_client import (
    AnthropicClient,
    parse_stream as anthropic_parse_stream,
    translate_messages as anthropic_messages,
    translate_tools as anthropic_tools,
)
from mantra.implementations.llm.gemini_client import (
    GeminiClient,
    parse_stream as gemini_parse_stream,
    translate_body as gemini_body,
    translate_tools as gemini_tools,
)
from mantra.registry import LLM_REGISTRY


def _history() -> list[dict]:
    return [
        {"role": "system", "content": "You are a senior engineer."},
        {"role": "user", "content": "create a file"},
        {
            "role": "assistant",
            "content": "Let me write it.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "hi"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "OK: wrote 2 chars"},
        {"role": "user", "content": "what now?"},
    ]


class AnthropicTranslationTest(unittest.TestCase):
    def test_system_is_split_out(self):
        system, body = anthropic_messages(_history())
        self.assertIn("senior engineer", system)
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in body))
        self.assertEqual(body[0]["role"], "user")

    def test_tool_use_and_result_round_trip(self):
        _, body = anthropic_messages(_history())
        assistant = next(m for m in body if m["role"] == "assistant")
        self.assertEqual(assistant["content"][1]["type"], "tool_use")
        self.assertEqual(assistant["content"][1]["name"], "write_file")
        self.assertEqual(assistant["content"][1]["input"], {"path": "a.txt", "content": "hi"})
        user_blocks = [m for m in body if m["role"] == "user"]
        results = [b for m in user_blocks for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(results[0]["tool_use_id"], "call_1")
        self.assertIn("wrote 2 chars", results[0]["content"])

    def test_consecutive_same_role_messages_are_merged(self):
        msgs = [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        _, body = anthropic_messages(msgs)
        roles = [m["role"] for m in body]
        self.assertEqual(roles, ["user", "assistant"])

    def test_tools_are_transformed(self):
        schema = [{"type": "function", "function": {"name": "read_file", "description": "reads", "parameters": {"type": "object"}}}]
        out = anthropic_tools(schema)
        self.assertEqual(out[0]["name"], "read_file")
        self.assertIn("input_schema", out[0])

    def test_anthropic_is_registered(self):
        self.assertIs(LLM_REGISTRY["anthropic"], AnthropicClient)


class AnthropicStreamTest(unittest.TestCase):
    def test_text_and_tool_are_assembled(self):
        events = [
            '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            '{"type":"content_block_stop","index":0}',
            '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"write_file","input":{}}}',
            '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
            '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"a.txt\\"}"}}',
            '{"type":"content_block_stop","index":1}',
            '{"type":"message_stop"}',
        ]
        deltas = []
        response = anthropic_parse_stream(iter(("data: " + e + "\n" for e in events)), deltas.append)
        self.assertEqual("".join(deltas), "Hello")
        self.assertEqual(response.content, None)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "write_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.txt"})


class GeminiTranslationTest(unittest.TestCase):
    def test_system_and_contents_are_split(self):
        system, contents = gemini_body(_history(), None)
        self.assertIn("senior engineer", system)
        roles = [c["role"] for c in contents]
        self.assertEqual(roles, ["user", "model", "user"])

    def test_function_call_uses_model_role(self):
        _, contents = gemini_body(_history(), None)
        model_msgs = [c for c in contents if c["role"] == "model"]
        call = model_msgs[0]["parts"][1]["functionCall"]
        self.assertEqual(call["name"], "write_file")
        self.assertEqual(call["args"], {"path": "a.txt", "content": "hi"})

    def test_tool_result_becomes_function_response(self):
        _, contents = gemini_body(_history(), None)
        user_msgs = [c for c in contents if c["role"] == "user"]
        responses = [p for m in user_msgs for p in m["parts"] if "functionResponse" in p]
        self.assertEqual(responses[0]["functionResponse"]["name"], "write_file")
        self.assertIn("wrote 2 chars", responses[0]["functionResponse"]["response"]["result"])

    def test_tools_are_transformed(self):
        schema = [{"type": "function", "function": {"name": "list_dir", "parameters": {"type": "object", "properties": {}}}}]
        out = gemini_tools(schema)
        self.assertEqual(out[0]["functionDeclarations"][0]["name"], "list_dir")

    def test_gemini_is_registered(self):
        self.assertIs(LLM_REGISTRY["gemini"], GeminiClient)


class GeminiStreamTest(unittest.TestCase):
    def test_text_deltas_are_forwarded(self):
        chunks = [
            '{"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}',
            '{"candidates":[{"content":{"parts":[{"text":" world"}]}}]}',
        ]
        deltas = []
        response = gemini_parse_stream(iter(("data: " + c + "\n" for c in chunks)), deltas.append)
        self.assertEqual("".join(deltas), "Hello world")
        self.assertEqual(response.content, "Hello world")

    def test_function_call_chunk_is_parsed(self):
        chunks = [
            '{"candidates":[{"content":{"parts":[{"functionCall":{"name":"read_file","args":{"path":"a.py"}}}]}}]}',
        ]
        response = gemini_parse_stream(iter(("data: " + c + "\n" for c in chunks)))
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.py"})


if __name__ == "__main__":
    unittest.main()