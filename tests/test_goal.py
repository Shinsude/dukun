"""Tests for /goal - the standing objective a session works toward.

The claim that matters: a goal set on turn one still shapes turn ten.
Everything else is bookkeeping.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import mantra.core.sessions as sessions
from mantra.console import ConsoleSession, Style, _goal


def _messages(count=2):
    out = [{"role": "system", "content": "you are MANTRA"}]
    for i in range(count):
        out.append({"role": "user", "content": f"question {i}"})
        out.append({"role": "assistant", "content": f"answer {i}"})
    return out


class GoalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", sessions._OVERRIDE_ENV):
            self.addCleanup(os.environ.pop, var, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        os.environ[sessions._OVERRIDE_ENV] = os.path.join(self.tmp, "sessions")

        from mantra.config import merge_defaults
        from mantra.implementations.llm.mock_client import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",
        )

    # ---- setting and showing -----------------------------------------

    def test_a_goal_is_set_from_the_whole_line(self):
        _goal(self.session, "ship the dashboard")
        self.assertEqual(self.session.goal, "ship the dashboard")

    def test_a_multi_word_goal_keeps_its_spaces(self):
        _goal(self.session, "make the container fill the frame")
        self.assertEqual(self.session.goal, "make the container fill the frame")

    def test_an_empty_invocation_shows_the_goal(self):
        _goal(self.session, "ship it")
        with mock.patch.object(self.session, "show_goal") as shown:
            _goal(self.session, "")
        shown.assert_called_once()

    def test_showing_with_no_goal_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        self.assertTrue(any("no goal set" in str(p) for p in printed))

    def test_showing_prints_the_goal(self):
        _goal(self.session, "fix the borders")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        self.assertIn("fix the borders", " ".join(str(p) for p in printed))

    # ---- the injection is the point ----------------------------------

    def test_the_goal_reaches_the_system_prompt(self):
        _goal(self.session, "ship the dashboard")
        self.assertIn("ship the dashboard", self.session._effective_system_prompt())

    def test_without_a_goal_the_prompt_is_untouched(self):
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    def test_the_goal_outlives_individual_turns(self):
        # The whole reason this exists: turn ten must still be aiming at
        # what turn one was told to do.
        _goal(self.session, "ship the dashboard")
        for _ in range(9):
            self.session.message_count += 1
        self.assertIn("ship the dashboard", self.session._effective_system_prompt())

    def test_the_prompt_tells_the_agent_to_say_when_it_is_done(self):
        _goal(self.session, "x")
        self.assertIn("GOAL COMPLETE", self.session._effective_system_prompt())

    def test_the_base_instructions_survive_the_goal_being_set(self):
        base = self.session.system_prompt
        _goal(self.session, "y")
        self.assertTrue(self.session._effective_system_prompt().startswith(base))

    # ---- notes --------------------------------------------------------

    def test_a_note_is_recorded(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note use the light frame")
        self.assertEqual(self.session.goal_notes, ["use the light frame"])

    def test_notes_reach_the_system_prompt(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note use the light frame")
        self.assertIn("use the light frame", self.session._effective_system_prompt())

    def test_a_note_without_a_goal_is_refused(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _goal(self.session, "note orphan")
        self.assertEqual(self.session.goal_notes, [])
        self.assertTrue(any("set a goal first" in str(p) for p in printed))

    def test_an_empty_note_is_a_usage_message_not_a_blank_note(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note")
        self.assertEqual(self.session.goal_notes, [])

    def test_notes_are_shown_with_the_goal(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note first")
        _goal(self.session, "note second")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        shown = " ".join(str(p) for p in printed)
        self.assertIn("first", shown)
        self.assertIn("second", shown)

    # ---- clearing -----------------------------------------------------

    def test_done_clears_the_goal(self):
        _goal(self.session, "ship it")
        _goal(self.session, "done")
        self.assertEqual(self.session.goal, "")

    def test_clear_and_drop_also_clear(self):
        for word in ("clear", "drop"):
            _goal(self.session, "ship it")
            _goal(self.session, word)
            self.assertEqual(self.session.goal, "", word)

    def test_clearing_clears_the_notes_too(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note a note")
        _goal(self.session, "done")
        self.assertEqual(self.session.goal_notes, [])

    def test_clearing_a_goal_that_was_never_set_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.clear_goal()
        self.assertTrue(any("no goal set" in str(p) for p in printed))

    def test_cleared_the_goal_leaves_the_prompt_alone(self):
        _goal(self.session, "ship it")
        _goal(self.session, "done")
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    # ---- completion detection -----------------------------------------

    def _result(self, text):
        class R:
            final_message = text

        return R()

    def test_an_agent_reporting_completion_is_surfaced(self):
        _goal(self.session, "ship it")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session._check_goal_completion(self._result("GOAL COMPLETE: shipped"))
        self.assertTrue(any("reports the goal is met" in str(p) for p in printed))

    def test_completion_detection_is_case_insensitive(self):
        _goal(self.session, "ship it")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session._check_goal_completion(self._result("Goal complete."))
        self.assertTrue(any("reports the goal is met" in str(p) for p in printed))

    def test_an_ordinary_reply_is_not_read_as_completion(self):
        _goal(self.session, "ship it")
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_goal_completion(self._result("I made some progress."))
        printed.assert_not_called()

    def test_completion_is_ignored_when_no_goal_is_set(self):
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_goal_completion(self._result("GOAL COMPLETE"))
        printed.assert_not_called()

    def test_the_agent_clears_nothing_itself(self):
        # Reporting is not clearing: a wrong claim must not lose the goal.
        _goal(self.session, "ship it")
        self.session._check_goal_completion(self._result("GOAL COMPLETE"))
        self.assertEqual(self.session.goal, "ship it")

    # ---- persistence ---------------------------------------------------

    def test_the_goal_survives_a_resume(self):
        _goal(self.session, "ship the dashboard")
        _goal(self.session, "note use the light frame")
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name

        self.session.goal = ""
        self.session.goal_notes = []
        self.session.resume_session(name)
        self.assertEqual(self.session.goal, "ship the dashboard")
        self.assertEqual(self.session.goal_notes, ["use the light frame"])

    def test_a_session_without_a_goal_restores_to_no_goal(self):
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name
        self.session.goal = "stale"
        self.session.resume_session(name)
        self.assertEqual(self.session.goal, "")

    # ---- dispatch ------------------------------------------------------

    def test_goal_is_in_the_command_table(self):
        from mantra.console import SLASH_COMMANDS

        self.assertIn("/goal", [c for c, _ in SLASH_COMMANDS])

    def test_goal_is_in_the_help_text(self):
        from mantra.console import HELP_TEXT

        self.assertIn("/goal", HELP_TEXT)

    def test_dispatch_routes_to_the_goal_handler(self):
        from mantra.console import dispatch

        with mock.patch("mantra.console._goal") as handled:
            dispatch(self.session, "/goal do the thing")
        handled.assert_called_once_with(self.session, "do the thing")


if __name__ == "__main__":
    unittest.main()
