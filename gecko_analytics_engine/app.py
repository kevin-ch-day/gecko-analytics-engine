"""Application shell for the Project Gecko Analytics Engine."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings, load_settings
from gecko_analytics_engine.cyber_events.menu import cyber_event_readiness_menu_action
from gecko_analytics_engine.db.menu import database_health_action
from gecko_analytics_engine.event_study.menu import event_study_menu_action
from gecko_analytics_engine.market_data.menu import market_data_menu_action
from gecko_analytics_engine.reports.menu import reports_menu_action
from gecko_analytics_engine.utils.logging import configure_logging
from gecko_analytics_engine.utils.menu import (
    Menu,
    MenuItem,
    not_implemented_action,
)
from gecko_analytics_engine.utils.paths import AppPaths, initialize_paths, resolve_project_root


def build_main_menu(
    settings: AppSettings | None = None,
    paths: AppPaths | None = None,
    logger: logging.Logger | None = None,
) -> Menu:
    """Build the top-level operator menu."""

    return Menu(
        title="Project Gecko Analytics Engine",
        subtitle="Main Menu",
        items=[
            MenuItem(
                "1",
                "Database Health",
                database_health_action(settings, paths, logger)
                if settings is not None and paths is not None
                else not_implemented_action("Database Health"),
            ),
            MenuItem(
                "2",
                "Market Data",
                market_data_menu_action(settings, paths, logger)
                if settings is not None and paths is not None
                else not_implemented_action("Market Data"),
            ),
            MenuItem(
                "3",
                "Cyber Event Readiness",
                cyber_event_readiness_menu_action(settings, paths, logger)
                if settings is not None and paths is not None
                else not_implemented_action("Cyber Event Readiness"),
            ),
            MenuItem(
                "4",
                "Event Study Analysis",
                event_study_menu_action(settings, paths, logger)
                if settings is not None and paths is not None
                else not_implemented_action("Event Study Analysis"),
            ),
            MenuItem(
                "5",
                "Statistical Analysis",
                not_implemented_action("Statistical Analysis"),
            ),
            MenuItem("6", "Machine Learning", not_implemented_action("Machine Learning")),
            MenuItem("7", "Exports", not_implemented_action("Exports")),
            MenuItem(
                "8",
                "Reports",
                reports_menu_action(settings, paths, logger)
                if settings is not None and paths is not None
                else not_implemented_action("Reports"),
            ),
            MenuItem(
                "9",
                "Admin / Diagnostics",
                not_implemented_action("Admin / Diagnostics"),
            ),
        ],
    )


def main() -> int:
    """Run the application and return a process exit code."""

    project_root = resolve_project_root()
    settings = load_settings(project_root)
    paths = initialize_paths(settings, project_root)
    logger = configure_logging(paths.main_log_file, settings.log_level)

    logger.info("Project Gecko Analytics Engine startup.")
    logger.debug("Project root: %s", paths.project_root)
    logger.debug("Data root: %s", paths.data_root)
    logger.debug("Output root: %s", paths.output_root)

    try:
        build_main_menu(settings, paths, logger).run()
        return 0
    except Exception:
        logger.exception("Project Gecko Analytics Engine exited with an error.")
        raise
    finally:
        logger.info("Project Gecko Analytics Engine shutdown.")
        logging.shutdown()
