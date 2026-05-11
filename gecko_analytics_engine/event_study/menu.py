"""Event study menu actions."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.event_study.dataset_preview import (
    print_event_study_dataset_preview,
    run_event_study_dataset_preview,
)
from gecko_analytics_engine.event_study.exclusion_review import (
    print_dataset_exclusion_review,
    run_dataset_exclusion_review,
)
from gecko_analytics_engine.event_study.study_design import (
    default_event_study_design,
    print_event_study_design,
)
from gecko_analytics_engine.utils.menu import Menu, MenuAction, MenuItem
from gecko_analytics_engine.utils.paths import AppPaths


def event_study_menu_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the event study submenu action."""

    def action() -> None:
        Menu(
            title="Project Gecko Analytics Engine",
            subtitle="Event Study Analysis",
            exit_label="Back",
            exit_message="Returning to the main menu.",
            invalid_return_label="the event study menu",
            items=[
                MenuItem("1", "Show Study Design Contract", study_design_action()),
                MenuItem(
                    "2",
                    "Run Event-Study Dataset Preview",
                    dataset_preview_action(settings, paths, logger),
                ),
                MenuItem(
                    "3",
                    "Export Event-Study Dataset Preview",
                    dataset_preview_action(settings, paths, logger),
                ),
                MenuItem(
                    "4",
                    "Review Dataset Exclusions",
                    exclusion_review_action(settings, paths, logger),
                ),
            ],
        ).run()

    return action


def study_design_action() -> MenuAction:
    """Build a study design display action."""

    def action() -> None:
        print_event_study_design(default_event_study_design())
        print()
        input("Press Enter to return to the event study menu...")

    return action


def dataset_preview_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only event-study dataset preview action."""

    def action() -> None:
        result = run_event_study_dataset_preview(settings, paths, logger)
        print_event_study_dataset_preview(result)
        print()
        input("Press Enter to return to the event study menu...")

    return action


def exclusion_review_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only dataset exclusion review action."""

    def action() -> None:
        result = run_dataset_exclusion_review(settings, paths, logger)
        print_dataset_exclusion_review(result)
        print()
        input("Press Enter to return to the event study menu...")

    return action
