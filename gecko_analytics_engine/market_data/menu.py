"""Market data menu actions."""

from __future__ import annotations

import logging

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.market_data.calendar import (
    print_market_calendar_diagnostic,
    run_market_calendar_diagnostic,
)
from gecko_analytics_engine.market_data.coverage import (
    print_market_data_coverage_report,
    run_market_data_coverage_report,
)
from gecko_analytics_engine.market_data.indexes import (
    print_benchmark_selection_diagnostic,
    run_benchmark_selection_diagnostic,
)
from gecko_analytics_engine.market_data.index_audit import (
    print_benchmark_import_audit,
    run_benchmark_import_audit,
)
from gecko_analytics_engine.market_data.import_validator import (
    print_price_import_validator_report,
    run_price_import_validator,
)
from gecko_analytics_engine.market_data.price_forensics import (
    print_market_data_forensics,
    run_market_data_forensics,
)
from gecko_analytics_engine.market_data.repair_scope import (
    print_daily_data_repair_scope,
    run_daily_data_repair_scope,
)
from gecko_analytics_engine.market_data.source_manifest import (
    print_daily_price_source_manifest,
    run_daily_price_source_manifest,
)
from gecko_analytics_engine.utils.menu import Menu, MenuAction, MenuItem
from gecko_analytics_engine.utils.paths import AppPaths


def market_data_menu_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the market data submenu action."""

    def action() -> None:
        Menu(
            title="Project Gecko Analytics Engine",
            subtitle="Market Data",
            exit_label="Back",
            exit_message="Returning to the main menu.",
            invalid_return_label="the market data menu",
            items=[
                MenuItem(
                    "1",
                    "Run Market Data Coverage Report",
                    market_data_coverage_action(settings, paths, logger),
                ),
                MenuItem(
                    "2",
                    "Export Market Data Coverage Report",
                    market_data_coverage_action(settings, paths, logger),
                ),
                MenuItem(
                    "3",
                    "Diagnose Market Calendar Alignment",
                    market_calendar_diagnostic_action(settings, paths, logger),
                ),
                MenuItem(
                    "4",
                    "Diagnose Benchmark Selection",
                    benchmark_selection_action(settings, paths, logger),
                ),
                MenuItem(
                    "5",
                    "Audit Benchmark Import Readiness",
                    benchmark_import_audit_action(settings, paths, logger),
                ),
                MenuItem(
                    "6",
                    "Validate Candidate Price CSVs",
                    price_import_validator_action(settings, paths, logger),
                ),
                MenuItem(
                    "7",
                    "Run Market Data Forensics",
                    market_data_forensics_action(settings, paths, logger),
                ),
                MenuItem(
                    "8",
                    "Generate Daily Data Repair Scope",
                    daily_data_repair_scope_action(settings, paths, logger),
                ),
                MenuItem(
                    "9",
                    "Generate Daily Price Source Manifest",
                    daily_price_source_manifest_action(settings, paths, logger),
                ),
            ],
        ).run()

    return action


def market_calendar_diagnostic_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only market calendar diagnostic action."""

    def action() -> None:
        result = run_market_calendar_diagnostic(settings, paths, logger)
        print_market_calendar_diagnostic(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def market_data_coverage_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only market data coverage action."""

    def action() -> None:
        result = run_market_data_coverage_report(settings, paths, logger)
        print_market_data_coverage_report(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def benchmark_selection_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only benchmark selection diagnostic action."""

    def action() -> None:
        result = run_benchmark_selection_diagnostic(settings, paths, logger)
        print_benchmark_selection_diagnostic(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def benchmark_import_audit_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only benchmark import audit action."""

    def action() -> None:
        result = run_benchmark_import_audit(settings, paths, logger)
        print_benchmark_import_audit(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def price_import_validator_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the dry-run candidate price CSV validator action."""

    def action() -> None:
        result = run_price_import_validator(settings, paths, logger)
        print_price_import_validator_report(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def market_data_forensics_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only market-data forensic drilldown action."""

    def action() -> None:
        result = run_market_data_forensics(settings, paths, logger)
        print_market_data_forensics(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def daily_data_repair_scope_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only daily market-data repair scope action."""

    def action() -> None:
        result = run_daily_data_repair_scope(settings, paths, logger)
        print_daily_data_repair_scope(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action


def daily_price_source_manifest_action(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MenuAction:
    """Build the read-only daily price source manifest action."""

    def action() -> None:
        result = run_daily_price_source_manifest(settings, paths, logger)
        print_daily_price_source_manifest(result)
        print()
        input("Press Enter to return to the market data menu...")

    return action
