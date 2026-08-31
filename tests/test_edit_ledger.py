"""Tests for the edit ledger: read-before-edit enforcement and truncation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.implementations.tools.edit_ledger import EditLedger, was_clipped
from mantra.implementations.tools.file_tools import EditFileTool, ReadFileTool, WriteFileTool
from mantra.registry import build_tools


def _sandbox(content: str) -> tuple[LocalSandbox, str]:
    tmp = tempfile.TemporaryDirectory(prefix="mantra-ledger-")
    path = "note.txt"
    with open(os.path.join(tmp.name, path), "w", encoding="utf-8") as handle:
        handle.write(content)
    return LocalSandbox(tmp.name), tmp


class LedgerFlagTest(unittest.TestCase):
    def setUp(self):
        self.ledger = EditLedger()

    def test_remember_records_a_hash(self):
        self.ledger.remember("a.txt", "hello")
        self.assertTrue(self.ledger.has_seen("a.txt"))
        self.assertTrue(self.ledger.is_current("a.txt", "hello"))
        self.assertFalse(self.ledger.is_current("a.txt", "world"))

    def test_is_current_is_false_after_an_external_change(self):
        self.ledger.remember("a.txt", "v1")
        self.assertFalse(self.ledger.is_current("a.txt", "v2"))

    def test_truncated_flag_is_recorded_and_cleared(self):
        self.ledger.remember("big.txt", "x" * 100, truncated=True)
        self.assertTrue(self.ledger.was_truncated("big.txt"))
        self.ledger.remember("big.txt", "small")
        self.assertFalse(self.ledger.was_truncated("big.txt"))

    def test_was_clipped_detects_sandbox_marker_only_at_the_tail(self):
        self.assertTrue(was_clipped("abc\n... [truncated]"))
        self.assertFalse(was_clipped("a [truncated] note in the middle"))
        self.assertFalse(was_clipped("plain content"))


class TruncationEditTest(unittest.TestCase):
    """A file clipped at the sandbox size cap must refuse edits."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="mantra-trunc-")
        self.sandbox = LocalSandbox(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)
        tools = build_tools(["read_file", "edit_file", "write_file"])
        (self.read_file,) = [t for t in tools if isinstance(t, ReadFileTool)]
        (self.edit_file,) = [t for t in tools if isinstance(t, EditFileTool)]
        (self.write_file,) = [t for t in tools if isinstance(t, WriteFileTool)]

    def test_a_clipped_read_blocks_the_edit(self):
        huge = "z" * 600_000
        self.write_file.execute(self.sandbox, path="huge.txt", content=huge)
        out = self.read_file.execute(self.sandbox, path="huge.txt")
        self.assertTrue(self.read_file.ledger.was_truncated("huge.txt"))
        edit = self.edit_file.execute(
            self.sandbox,
            path="huge.txt",
            old_string="zz",
            new_string="yy",
        )
        self.assertIn("too large to edit", edit)

    def test_a_literal_truncation_marker_does_not_block_edits(self):
        # A small file that genuinely contains the marker text must still
        # be editable: the old "[truncated] in content" heuristic refused it.
        self.write_file.execute(
            self.sandbox,
            path="notes.md",
            content="meeting notes [truncated] by design\nsecond line\n",
        )
        self.read_file.execute(self.sandbox, path="notes.md")
        out = self.edit_file.execute(
            self.sandbox,
            path="notes.md",
            old_string="[truncated]",
            new_string="(see minutes)",
        )
        self.assertIn("OK: edited", out)


if __name__ == "__main__":
    unittest.main()