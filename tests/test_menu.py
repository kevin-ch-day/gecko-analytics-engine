"""Unit tests for reusable menu behavior."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from gecko_analytics_engine.utils.menu import Menu, MenuItem


class MenuBehaviorTest(unittest.TestCase):
    def test_invalid_choice_uses_configured_input_reader_and_label(self) -> None:
        prompts: list[str] = []
        choices = iter(("x", "", "0"))

        def input_reader(prompt: str) -> str:
            prompts.append(prompt)
            return next(choices)

        menu = Menu(
            title="Project Gecko Analytics Engine",
            subtitle="Test Menu",
            items=(),
            exit_label="Back",
            exit_message="Done.",
            invalid_return_label="the test menu",
            input_reader=input_reader,
        )

        with redirect_stdout(io.StringIO()) as output:
            menu.run()

        self.assertIn("'x' is not a valid option.", output.getvalue())
        self.assertIn("Done.", output.getvalue())
        self.assertEqual(
            prompts,
            [
                "Select an option: ",
                "Press Enter to return to the test menu...",
                "Select an option: ",
            ],
        )

    def test_valid_choice_runs_action(self) -> None:
        calls: list[str] = []
        choices = iter(("1", "0"))

        def action() -> None:
            calls.append("ran")

        menu = Menu(
            title="Project Gecko Analytics Engine",
            items=(MenuItem("1", "Run Action", action),),
            input_reader=lambda _prompt: next(choices),
        )

        with redirect_stdout(io.StringIO()):
            menu.run()

        self.assertEqual(calls, ["ran"])


if __name__ == "__main__":
    unittest.main()
