"""Reusable actions for menu placeholders and simple shell commands."""

from __future__ import annotations

from collections.abc import Callable

MenuAction = Callable[[], None]


def not_implemented_action(feature_name: str) -> MenuAction:
    """Return a placeholder action for a menu feature not implemented yet."""

    def action() -> None:
        print()
        print(f"{feature_name} is not implemented yet.")
        print("This menu slot is reserved for a future Project Gecko sprint.")
        input("Press Enter to return to the main menu...")

    return action
