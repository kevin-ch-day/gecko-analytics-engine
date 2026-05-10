"""Application shell for the Project Gecko Analytics Engine."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings, load_settings
from gecko_analytics_engine.cyber_events.event_windows import (
    print_event_window_readiness_report,
    run_event_window_readiness_report,
)
from gecko_analytics_engine.cyber_events.readiness import (
    print_event_readiness_precheck,
    run_event_readiness_precheck,
)
from gecko_analytics_engine.db import (
    export_database_inventory,
    print_database_health_result,
    run_database_health_check,
)
from gecko_analytics_engine.reports.database_shape_report import (
    generate_database_shape_report,
    print_database_shape_report,
)
from gecko_analytics_engine.utils.logging import configure_logging
from gecko_analytics_engine.utils.menu import (
    Menu,
    MenuAction,
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
            MenuItem("2", "Market Data", not_implemented_action("Market Data")),
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
                not_implemented_action("Event Study Analysis"),
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


def database_health_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the menu action for the read-only database health check."""

    def action() -> None:
        result = run_database_health_check(settings, logger)
        result = export_database_inventory(result, paths, logger)
        print_database_health_result(result)
        print()
        choice = input(
            "Press Enter to return to the main menu, or type 'm' for inventory actions: "
        ).strip().lower()
        if choice == "m":
            build_database_inventory_menu(settings, paths, logger).run()

    return action


def build_database_inventory_menu(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> Menu:
    """Build optional database inventory actions."""

    return Menu(
        title="Project Gecko Analytics Engine",
        subtitle="Database Inventory",
        exit_label="Back",
        exit_message="Returning to the main menu.",
        items=[
            MenuItem("1", "Run connection health check", database_health_action(settings, paths, logger)),
            MenuItem("2", "Export schema inventory", database_export_action(settings, paths, logger)),
            MenuItem("3", "Export core table columns", database_export_action(settings, paths, logger)),
            MenuItem("4", "Show core table row counts", core_table_counts_action(settings, logger)),
            MenuItem(
                "5",
                "Show possible event/market-data tables",
                possible_event_market_tables_action(settings, logger),
            ),
        ],
    )


def database_export_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build an action that refreshes database inventory exports."""

    def action() -> None:
        result = export_database_inventory(run_database_health_check(settings, logger), paths, logger)
        if not result.connection_ok:
            print_database_health_result(result)
        else:
            print()
            print("Database inventory exports refreshed:")
            for path in result.export_paths:
                print(f"  {path}")
        print()
        input("Press Enter to return to the database inventory menu...")

    return action


def core_table_counts_action(
    settings: AppSettings,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build an action that shows core table row counts."""

    def action() -> None:
        result = run_database_health_check(settings, logger)
        print()
        print("Core Table Row Counts")
        print("---------------------")
        if not result.connection_ok:
            print(f"Connection failed: {result.error_message}")
        else:
            for shape in result.core_table_shapes:
                count = "Unknown" if shape.row_count is None else f"{shape.row_count:,}"
                print(f"  [{shape.status}] {shape.table_name}: {count}")
        print()
        input("Press Enter to return to the database inventory menu...")

    return action


def possible_event_market_tables_action(
    settings: AppSettings,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build an action that shows possible event and market-data tables."""

    def action() -> None:
        result = run_database_health_check(settings, logger)
        print()
        print("Possible Event / Market-Data Tables")
        print("-----------------------------------")
        if not result.connection_ok:
            print(f"Connection failed: {result.error_message}")
        elif not result.possible_event_market_tables:
            print("No likely event or market-data tables detected.")
        else:
            for table_name in result.possible_event_market_tables:
                print(f"  {table_name}")
        print()
        input("Press Enter to return to the database inventory menu...")

    return action


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
            items=[
                MenuItem(
                    "1",
                    "Generate Database Shape Report",
                    database_shape_report_action(settings, paths, logger),
                ),
            ],
        ).run()

    return action


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
