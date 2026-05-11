"""Database menu actions."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db import (
    export_database_inventory,
    print_database_health_result,
    run_database_health_check,
)
from gecko_analytics_engine.utils.menu import Menu, MenuAction, MenuItem
from gecko_analytics_engine.utils.paths import AppPaths


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
        invalid_return_label="the database inventory menu",
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
