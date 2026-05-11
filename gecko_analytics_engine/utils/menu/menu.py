"""Reusable text menu primitives for the operator interface."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gecko_analytics_engine.utils.menu.actions import MenuAction
from gecko_analytics_engine.utils.menu.template import MenuTemplate

InputReader = Callable[[str], str]


@dataclass(frozen=True)
class MenuItem:
    """A selectable menu option."""

    key: str
    label: str
    action: MenuAction


class Menu:
    """Interactive menu loop with a shared Project Gecko presentation style."""

    def __init__(
        self,
        title: str,
        items: Sequence[MenuItem],
        *,
        subtitle: str | None = None,
        exit_key: str = "0",
        exit_label: str = "Exit",
        exit_message: str = "Exiting Project Gecko Analytics Engine.",
        invalid_return_label: str = "this menu",
        input_reader: InputReader = input,
        template: MenuTemplate | None = None,
    ) -> None:
        self.title = title
        self.subtitle = subtitle
        self.items = list(items)
        self.exit_key = exit_key
        self.exit_label = exit_label
        self.exit_message = exit_message
        self.invalid_return_label = invalid_return_label
        self.input_reader = input_reader
        self.template = template or MenuTemplate()
        self._items_by_key = {item.key: item for item in self.items}

    def run(self) -> None:
        """Display the menu until the operator exits."""

        while True:
            print(self.template.render(self))
            choice = self.input_reader("Select an option: ").strip()

            if choice == self.exit_key:
                print()
                print(self.exit_message)
                return

            selected_item = self._items_by_key.get(choice)
            if selected_item is None:
                print()
                print(f"'{choice}' is not a valid option.")
                self.input_reader(f"Press Enter to return to {self.invalid_return_label}...")
                continue

            selected_item.action()
