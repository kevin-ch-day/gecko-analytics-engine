"""Cyber event menu actions."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.cyber_events.event_windows import (
    print_event_window_readiness_report,
    run_event_window_readiness_report,
)
from gecko_analytics_engine.cyber_events.readiness import (
    print_event_readiness_precheck,
    run_event_readiness_precheck,
)
from gecko_analytics_engine.cyber_events.window_coverage import (
    print_event_window_coverage_report,
    run_event_window_coverage_report,
)
from gecko_analytics_engine.utils.menu import Menu, MenuAction, MenuItem
from gecko_analytics_engine.utils.paths import AppPaths


def cyber_event_readiness_menu_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the cyber event readiness submenu action."""

    def action() -> None:
        Menu(
            title="Project Gecko Analytics Engine",
            subtitle="Cyber Event Readiness",
            exit_label="Back",
            exit_message="Returning to the main menu.",
            invalid_return_label="the cyber event readiness menu",
            items=[
                MenuItem(
                    "1",
                    "Run Event Readiness Precheck",
                    event_readiness_precheck_action(settings, paths, logger),
                ),
                MenuItem(
                    "2",
                    "Export Event Readiness Precheck",
                    event_readiness_precheck_action(settings, paths, logger),
                ),
                MenuItem(
                    "3",
                    "Run Event Window Readiness Detail",
                    event_window_readiness_action(settings, paths, logger),
                ),
                MenuItem(
                    "4",
                    "Analyze Event Window Price Coverage",
                    event_window_coverage_action(settings, paths, logger),
                ),
            ],
        ).run()

    return action


def event_readiness_precheck_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only event readiness precheck action."""

    def action() -> None:
        result = run_event_readiness_precheck(settings, paths, logger)
        print_event_readiness_precheck(result)
        print()
        input("Press Enter to return to the cyber event readiness menu...")

    return action


def event_window_readiness_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only event-window readiness action."""

    def action() -> None:
        result = run_event_window_readiness_report(settings, paths, logger)
        print_event_window_readiness_report(result)
        print()
        input("Press Enter to return to the cyber event readiness menu...")

    return action


def event_window_coverage_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only event-window price coverage action."""

    def action() -> None:
        result = run_event_window_coverage_report(settings, paths, logger)
        print_event_window_coverage_report(result)
        print()
        input("Press Enter to return to the cyber event readiness menu...")

    return action
