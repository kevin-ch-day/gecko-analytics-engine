"""Reports menu actions."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.reports.database_shape_report import (
    generate_database_shape_report,
    print_database_shape_report,
)
from gecko_analytics_engine.utils.menu import Menu, MenuAction, MenuItem
from gecko_analytics_engine.utils.paths import AppPaths


def reports_menu_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the reports submenu action."""

    def action() -> None:
        Menu(
            title="Project Gecko Analytics Engine",
            subtitle="Reports",
            exit_label="Back",
            exit_message="Returning to the main menu.",
            invalid_return_label="the reports menu",
            items=[
                MenuItem(
                    "1",
                    "Generate Database Shape Report",
                    database_shape_report_action(settings, paths, logger),
                ),
            ],
        ).run()

    return action


def database_shape_report_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only database shape report action."""

    def action() -> None:
        report = generate_database_shape_report(settings, paths, logger)
        print_database_shape_report(report)
        print()
        input("Press Enter to return to the reports menu...")

    return action
