"""Read-only database shape report for Project Gecko research data."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import (
    count_rows,
    get_table_columns,
    safe_scalar,
    table_exists,
)
from gecko_analytics_engine.db.schema_contract import (
    COMPANIES,
    CYBER_EVENT_SECURITIES,
    CYBER_EVENTS,
    INDEX_DAILY_PRICES,
    SECURITIES,
    SECURITY_DAILY_PRICES,
)
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class ShapeMetric:
    """One defensive research data shape metric."""

    name: str
    value: int | str | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class DatabaseShapeReport:
    """Database shape report result."""

    generated_at: str
    connection_ok: bool
    database_name: str | None = None
    metrics: tuple[ShapeMetric, ...] = ()
    export_path: Path | None = None
    error_message: str | None = None


def generate_database_shape_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DatabaseShapeReport:
    """Generate and export a read-only database shape report."""

    generated_at = datetime.now(UTC).isoformat()

    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            metrics = _collect_shape_metrics(connection)
    except DatabaseConnectionError as exc:
        report = DatabaseShapeReport(
            generated_at=generated_at,
            connection_ok=False,
            error_message=str(exc),
        )
        _log_report(report, logger)
        return report
    except Exception as exc:
        report = DatabaseShapeReport(
            generated_at=generated_at,
            connection_ok=False,
            error_message=f"Database shape report failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(report, logger)
        return report

    export_path = paths.reports_dir / "database_shape_report.json"
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report = DatabaseShapeReport(
        generated_at=generated_at,
        connection_ok=True,
        database_name=database_name,
        metrics=metrics,
        export_path=export_path,
    )
    _write_shape_report(export_path, report)
    _log_report(report, logger)
    return report


def format_database_shape_report(report: DatabaseShapeReport) -> list[str]:
    """Format a database shape report for console output."""

    lines = ["", "Database Shape Report", "---------------------"]
    if not report.connection_ok:
        lines.extend(["Connection: FAILED", f"Reason: {report.error_message}"])
        return lines

    lines.extend(
        [
            f"Database: {report.database_name or 'Unknown'}",
            f"Generated: {report.generated_at}",
            "",
            "Metrics:",
        ]
    )

    for metric in report.metrics:
        value = _format_value(metric.value)
        detail = f" ({metric.detail})" if metric.detail else ""
        lines.append(f"  [{metric.status}] {metric.name}: {value}{detail}")

    if report.export_path:
        lines.extend(["", f"Export: {report.export_path}"])

    return lines


def print_database_shape_report(report: DatabaseShapeReport) -> None:
    """Print a database shape report."""

    for line in format_database_shape_report(report):
        print(line)


def _collect_shape_metrics(connection: Any) -> tuple[ShapeMetric, ...]:
    metrics: list[ShapeMetric] = [
        _table_count_metric(connection, "total companies", COMPANIES),
        _table_count_metric(connection, "total securities", SECURITIES),
        _table_count_metric(connection, "total cyber events", CYBER_EVENTS),
        _table_count_metric(
            connection,
            "total security daily price rows",
            SECURITY_DAILY_PRICES,
        ),
        _table_count_metric(connection, "total index daily price rows", INDEX_DAILY_PRICES),
        _events_with_disclosure_dates(connection),
        _events_linked_to_securities(connection),
        _securities_with_price_rows(connection),
        _date_range_metric(connection, "security price trade-date range", SECURITY_DAILY_PRICES),
        _date_range_metric(connection, "index price trade-date range", INDEX_DAILY_PRICES),
    ]
    return tuple(metrics)


def _table_count_metric(connection: Any, label: str, table_name: str) -> ShapeMetric:
    if not table_exists(connection, table_name):
        return ShapeMetric(label, None, "MISSING", f"{table_name} table not found")

    row_count = count_rows(connection, table_name)
    if row_count is None:
        return ShapeMetric(label, None, "UNAVAILABLE", "row count query failed")
    if row_count == 0:
        return ShapeMetric(label, 0, "EMPTY")
    return ShapeMetric(label, row_count, "OK")


def _events_with_disclosure_dates(connection: Any) -> ShapeMetric:
    table_name = CYBER_EVENTS
    if not table_exists(connection, table_name):
        return ShapeMetric("events with disclosure dates", None, "MISSING", "cyber_events missing")

    columns = set(get_table_columns(connection, table_name))
    date_column = _first_present(
        columns,
        ("disclosure_date", "event_disclosure_date", "date_disclosed", "announcement_date"),
    )
    if date_column is None:
        return ShapeMetric("events with disclosure dates", None, "UNAVAILABLE", "no date column")

    value = safe_scalar(
        connection,
        f"SELECT COUNT(*) FROM `{table_name}` WHERE `{date_column}` IS NOT NULL",
    )
    return _scalar_metric("events with disclosure dates", value, f"column={date_column}")


def _events_linked_to_securities(connection: Any) -> ShapeMetric:
    table_name = CYBER_EVENT_SECURITIES
    if not table_exists(connection, table_name):
        return ShapeMetric(
            "events linked to securities",
            None,
            "MISSING",
            f"{CYBER_EVENT_SECURITIES} missing",
        )

    columns = set(get_table_columns(connection, table_name))
    event_column = _first_present(columns, ("cyber_event_id", "event_id"))
    if event_column is None:
        return ShapeMetric("events linked to securities", None, "UNAVAILABLE", "no event id column")

    value = safe_scalar(
        connection,
        f"SELECT COUNT(DISTINCT `{event_column}`) FROM `{table_name}`",
    )
    return _scalar_metric("events linked to securities", value, f"column={event_column}")


def _securities_with_price_rows(connection: Any) -> ShapeMetric:
    table_name = SECURITY_DAILY_PRICES
    if not table_exists(connection, table_name):
        return ShapeMetric(
            "securities with at least one price row",
            None,
            "MISSING",
            "security_daily_prices missing",
        )

    columns = set(get_table_columns(connection, table_name))
    security_column = _first_present(columns, ("security_id", "ticker", "symbol"))
    if security_column is None:
        return ShapeMetric(
            "securities with at least one price row",
            None,
            "UNAVAILABLE",
            "no security identifier column",
        )

    value = safe_scalar(
        connection,
        f"SELECT COUNT(DISTINCT `{security_column}`) FROM `{table_name}`",
    )
    return _scalar_metric(
        "securities with at least one price row",
        value,
        f"column={security_column}",
    )


def _date_range_metric(connection: Any, label: str, table_name: str) -> ShapeMetric:
    if not table_exists(connection, table_name):
        return ShapeMetric(label, None, "MISSING", f"{table_name} missing")

    columns = set(get_table_columns(connection, table_name))
    date_column = _first_present(columns, ("trade_date", "price_date", "date", "market_date"))
    if date_column is None:
        return ShapeMetric(label, None, "UNAVAILABLE", "no trade-date column")

    value = safe_scalar(
        connection,
        f"SELECT CONCAT(MIN(`{date_column}`), ' to ', MAX(`{date_column}`)) FROM `{table_name}`",
    )
    if value is None:
        return ShapeMetric(label, None, "UNAVAILABLE", f"column={date_column}")
    return ShapeMetric(label, str(value), "OK", f"column={date_column}")


def _scalar_metric(label: str, value: Any, detail: str) -> ShapeMetric:
    if value is None:
        return ShapeMetric(label, None, "UNAVAILABLE", detail)
    value = int(value)
    return ShapeMetric(label, value, "EMPTY" if value == 0 else "OK", detail)


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _first_present(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _write_shape_report(path: Path, report: DatabaseShapeReport) -> None:
    payload = asdict(report)
    if report.export_path is not None:
        payload["export_path"] = str(report.export_path)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _format_value(value: int | str | None) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, int):
        return f"{value:,}"
    return value


def _log_report(
    report: DatabaseShapeReport,
    logger: logging.Logger | None,
) -> None:
    if logger is None:
        return
    if not report.connection_ok:
        logger.warning("Database shape report failed: %s", report.error_message)
        return
    logger.info(
        "Database shape report generated: database=%s metrics=%s export=%s",
        report.database_name,
        len(report.metrics),
        report.export_path,
    )
