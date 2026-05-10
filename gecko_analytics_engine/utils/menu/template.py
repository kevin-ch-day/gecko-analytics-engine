"""Shared menu rendering template for Project Gecko operator menus."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gecko_analytics_engine.utils.menu.menu import Menu


class MenuTemplate:
    """Render menus with a consistent Project Gecko console style."""

    def render(self, menu: Menu) -> str:
        """Return a formatted menu screen."""

        width = 72
        border = "=" * width
        lines = [
            "",
            border,
            menu.title.center(width),
        ]

        if menu.subtitle:
            lines.append(menu.subtitle.center(width))

        lines.extend(
            [
                border,
                "",
            ]
        )

        for item in menu.items:
            lines.append(f"  {item.key}. {item.label}")

        lines.extend(
            [
                "",
                f"  {menu.exit_key}. {menu.exit_label}",
                "",
                "-" * width,
            ]
        )

        return "\n".join(lines)
