"""Tests for the search tools: literal, regex, and file-name matching."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.implementations.tools.search_tools import FindFileTool, SearchCodeTool


def _workspace(files: dict[str, str]) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="mantra-search-")
    for name, content in files.items():
        full = os.path.join(tmp.name, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
    return tmp


class SearchCodeToolTest(unittest.TestCase):
    def setUp(self):
        self.tool = SearchCodeTool()
        self.tmp = _workspace(
            {
                "app.py": "def add(a, b):\n    return a + b\n# total = 1\n",
                "src/util.py": "TOTAL = 10\nMAX_TOTAL = 20\n",
                "README.md": "Total lines: 3\n",
            }
        )
        self.sandbox = LocalSandbox(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_literal_matches_are_case_sensitive(self):
        out = self.tool.execute(self.sandbox, query="total")
        self.assertIn("app.py:2:", out)
        self.assertNotIn("Total lines", out)

    def test_literal_uppercase_only_matches_uppercase(self):
        out = self.tool.execute(self.sandbox, query="TOTAL")
        self.assertIn("src" + os.sep + "util.py:", out)
        self.assertIn("TOTAL = 10", out)
        self.assertNotIn("total = 1", out)

    def test_regex_matches_both_cases_across_files(self):
        out = self.tool.execute(self.sandbox, query="(total|TOTAL|Total)", regex=True)
        self.assertIn("src" + os.sep + "util.py:0:", out)
        self.assertIn("src" + os.sep + "util.py:1:", out)
        self.assertIn("README.md:0:", out)
        self.assertNotIn("def add(a, b)", out)

    def test_regex_anchors_a_line_start(self):
        out = self.tool.execute(self.sandbox, query="^TOTAL = ", regex=True)
        self.assertEqual(out.count("TOTAL"), 1)

    def test_invalid_regex_returns_an_error(self):
        out = self.tool.execute(self.sandbox, query="([", regex=True)
        self.assertIn("invalid regex", out)

    def test_no_matches_says_so(self):
        out = self.tool.execute(self.sandbox, query="zzzz-not-here", regex=True)
        self.assertEqual(out, "(no matches)")

    def test_the_schema_has_a_regex_flag(self):
        params = self.tool.parameters["properties"]
        self.assertIn("regex", params)


class FindFileToolTest(unittest.TestCase):
    def setUp(self):
        self.tool = FindFileTool()
        self.tmp = _workspace(
            {
                "app.py": "",
                "src/app.py": "",
                "tests/test_app.py": "",
                "README.md": "",
            }
        )
        self.sandbox = LocalSandbox(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_substring_matches_paths(self):
        out = self.tool.execute(self.sandbox, pattern="app")
        self.assertIn("app.py", out)
        self.assertIn("src" + os.sep + "app.py", out)
        self.assertIn(os.path.join("tests", "test_app.py"), out)
        self.assertNotIn("README", out)


if __name__ == "__main__":
    unittest.main()