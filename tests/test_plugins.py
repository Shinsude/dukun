"""Tests for external tool plugins: discovery, registration, conflicts."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.core.exceptions import ConfigError
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.registry import TOOL_REGISTRY, build_tools

_SNAPSHOT = None


def _snapshot_registry():
    return dict(TOOL_REGISTRY)


def setUpModule():
    global _SNAPSHOT
    _SNAPSHOT = _snapshot_registry()


def tearDownModule():
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(_SNAPSHOT)


PLUGIN_SRC = '''"""A third-party tool that sums two numbers."""
from mantra.interfaces.tool import Tool


class AddTool(Tool):
    name = "add_two"
    description = "Add two integers."
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
    }

    def execute(self, sandbox, a, b):
        return str(int(a) + int(b))


def an_extra_helper():
    return "not a tool"
'''


class PluginLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mantra-plugin-")
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "maths_tools.py"), "w", encoding="utf-8") as handle:
            handle.write(PLUGIN_SRC)
        TOOL_REGISTRY.pop("add_two", None)

    def test_a_plugin_tool_becomes_buildable(self):
        self.assertNotIn("add_two", TOOL_REGISTRY)
        tools = build_tools(["add_two"], plugins=[self.tmp.name])
        self.assertIn("add_two", TOOL_REGISTRY)
        self.assertEqual([t.name for t in tools], ["add_two"])

    def test_executes_against_a_sandbox(self):
        tools = build_tools(["add_two"], plugins=[self.tmp.name])
        (add,) = tools
        out = add.execute(LocalSandbox(self.tmp.name), a=2, b=3)
        self.assertEqual(out, "5")

    def test_env_var_dir_is_honoured(self):
        with mock.patch.dict(os.environ, {"MANTRA_PLUGINS": self.tmp.name}):
            build_tools(["add_two"])
        self.assertIn("add_two", TOOL_REGISTRY)

    def test_duplicate_name_raises(self):
        TOOL_REGISTRY["add_two"] = type("Shadow", (), {"name": "add_two"})
        try:
            with self.assertRaises(ConfigError):
                build_tools([], plugins=[self.tmp.name])
        finally:
            TOOL_REGISTRY.pop("add_two", None)

    def test_missing_directory_raises(self):
        with self.assertRaises(ConfigError):
            build_tools([], plugins=[os.path.join(self.tmp.name, "nope")])


if __name__ == "__main__":
    unittest.main()