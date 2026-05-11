"""Read-only market data coverage report."""

from __future__ import annotations

import csv
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
    safe_fetch_all,
    safe_scalar,
    table_exists,
)
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    CYBER_EVENT_DATES,
    CYBER_EVENT_ID,
    CYBER_EVENT_SECURITIES,
    DJI_DAILY_PRICES,
    EVENT_WINDOWS,
    INDEX_DAILY_PRICES,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    MARKET_INDEXES,
    MARKET_INDEX_ID,
    SECURITY_DAILY_PRICES,
    SECURITY_ID,
    TRADE_DATE,
    VW_EVENT_WINDOW_BOUNDARIES,
    WINDOW_CODE,
    WINDOW_END_DATE,
    WINDOW_START_DATE,
    POST_EVENT_DAYS,
    PRE_EVENT_DAYS,
)
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class CoverageMetric:
    """One market data coverage metric."""

    category: str
    name: str
    value: int | str | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class CoverageIssue:
    """A market data coverage blocker, warning, or info item."""

    severity: str
    message: str


@dataclass(frozen=True)
class MarketDataCoverageReport:
    """Read-only market data coverage report."""

    generated_at: str
    connection_ok: bool
    market_data_status: str
    database_name: str | None = None
    metrics: tuple[CoverageMetric, ...] = ()
    issues: tuple[CoverageIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_market_data_coverage_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketDataCoverageReport:
    """Run and export the read-only market data coverage report."""

    generated_at = datetime.now(UTC).isoformat()

    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            metrics = _collect_coverage_metrics(connection)
    except DatabaseConnectionError as exc:
        result = MarketDataCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            market_data_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = MarketDataCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            market_data_status="BLOCKED",
            error_message=f"Market data coverage report failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_market_data_coverage_issues(metrics)
    result = MarketDataCoverageReport(
        generated_at=generated_at,
        connection_ok=True,
        market_data_status=determine_market_data_status(issues),
        database_name=database_name,
        metrics=metrics,
        issues=issues,
    )
    result = export_market_data_coverage_report(result, paths, logger)
    _log_report(result, logger)
    return result


def export_market_data_coverage_report(
    result: MarketDataCoverageReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketDataCoverageReport:
    """Export market data coverage artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "market_data_coverage_report.json"
    summary_csv = paths.exports_dir / "market_data_coverage_summary.csv"
    issues_csv = paths.exports_dir / "market_data_coverage_blockers.csv"
    export_paths = (json_path, summary_csv, issues_csv)
    result_with_exports = _copy_report_with_export_paths(result, export_paths)

    _write_metrics_csv(summary_csv, result.metrics)
    _write_issues_csv(issues_csv, result.issues)
    _write_report_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Market data coverage exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def format_market_data_coverage_report(result: MarketDataCoverageReport) -> list[str]:
    """Format a market data coverage report for console output."""

    lines = ["", "Market Data Coverage Report", "---------------------------"]
    if not result.connection_ok:
        lines.extend(
            [
                "Overall status: BLOCKED",
                "Connection: FAILED",
                f"Reason: {result.error_message}",
            ]
        )
        return lines

    lines.extend(
        [
            f"Overall status: {result.market_data_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Coverage checks:",
        ]
    )

    for metric in result.metrics:
        value = _format_value(metric.value)
        detail = f" ({metric.detail})" if metric.detail else ""
        lines.append(f"  [{metric.status}] {metric.category} - {metric.name}: {value}{detail}")

    lines.extend(["", "Blockers / warnings:"])
    if result.issues:
        for issue in result.issues:
            lines.append(f"  [{issue.severity}] {issue.message}")
    else:
        lines.append("  No market-data blockers detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def print_market_data_coverage_report(result: MarketDataCoverageReport) -> None:
    """Print market data coverage report."""

    for line in format_market_data_coverage_report(result):
        print(line)


def determine_market_data_status(issues: tuple[CoverageIssue, ...]) -> str:
    """Return the overall market data status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if issues:
        return "PARTIAL"
    return "READY_FOR_EVENT_STUDY_DATASET"


def build_market_data_coverage_issues(
    metrics: tuple[CoverageMetric, ...],
) -> tuple[CoverageIssue, ...]:
    """Generate coverage blockers and warnings from metrics."""

    values = {(metric.category, metric.name): metric for metric in metrics}
    issues: list[CoverageIssue] = []

    for key, message in (
        (("security price coverage", "total security price rows"), "security_daily_prices is unavailable."),
        (("index/benchmark coverage", "index_daily_prices rows"), "index_daily_prices is unavailable."),
        (("market calendar coverage", "market calendar rows"), "market_calendar is unavailable."),
    ):
        metric = values.get(key)
        if metric is None or metric.status in {"MISSING", "UNAVAILABLE"}:
            issues.append(CoverageIssue("BLOCKER", message))

    linked_without_prices = values.get(("linked event/security coverage", "linked securities without price rows"))
    if _positive_metric(linked_without_prices):
        issues.append(
            CoverageIssue(
                "WARNING",
                f"{linked_without_prices.value} linked securities have no price rows.",
            )
        )

    events_without_priced_security = values.get(("linked event/security coverage", "linked events with no priced security"))
    if _positive_metric(events_without_priced_security):
        issues.append(
            CoverageIssue(
                "WARNING",
                f"{events_without_priced_security.value} linked events have no priced security.",
            )
        )

    duplicates = values.get(("security price coverage", "duplicate security/date rows"))
    if _positive_metric(duplicates):
        issues.append(CoverageIssue("WARNING", f"{duplicates.value} duplicate security/date price rows detected."))

    non_trading_prices = values.get(("security price coverage", "price rows on non-trading days"))
    if _positive_metric(non_trading_prices):
        issues.append(CoverageIssue("WARNING", f"{non_trading_prices.value} price rows fall on non-trading days."))

    dji_rows = values.get(("index/benchmark coverage", "dji_daily_prices rows"))
    if dji_rows and dji_rows.value == 0:
        issues.append(CoverageIssue("WARNING", "dji_daily_prices is empty."))

    index_rows = values.get(("index/benchmark coverage", "index_daily_prices rows"))
    if index_rows and isinstance(index_rows.value, int) and index_rows.value > 0:
        issues.append(
            CoverageIssue(
                "WARNING",
                "Benchmark data appears to live in index_daily_prices; verify benchmark selection.",
            )
        )

    calendar_flag = values.get(("market calendar coverage", "trading-day flag detected"))
    if calendar_flag and calendar_flag.value == "no":
        issues.append(CoverageIssue("WARNING", "market_calendar is missing a trading-day flag."))

    sparse = values.get(("security price coverage", "sparse securities below 30 price rows"))
    if _positive_metric(sparse):
        issues.append(CoverageIssue("INFO", f"{sparse.value} securities have fewer than 30 price rows."))

    return tuple(issues)


def _collect_coverage_metrics(connection: Any) -> tuple[CoverageMetric, ...]:
    metrics: list[CoverageMetric] = []
    metrics.extend(_security_price_metrics(connection))
    metrics.extend(_linked_event_security_metrics(connection))
    metrics.extend(_index_coverage_metrics(connection))
    metrics.extend(_calendar_coverage_metrics(connection))
    metrics.extend(_event_window_support_metrics(connection))
    return tuple(metrics)


def _security_price_metrics(connection: Any) -> list[CoverageMetric]:
    category = "security price coverage"
    if not table_exists(connection, SECURITY_DAILY_PRICES):
        return [_missing_metric(category, "total security price rows", SECURITY_DAILY_PRICES)]

    columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    metrics = [_table_count_metric(connection, category, "total security price rows", SECURITY_DAILY_PRICES)]

    if SECURITY_ID in columns:
        priced_securities = safe_scalar(
            connection,
            f"SELECT COUNT(DISTINCT {SECURITY_ID}) FROM {SECURITY_DAILY_PRICES}",
        )
        metrics.append(_scalar_metric(category, "distinct securities with price rows", priced_securities))
        sparse = safe_scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT {SECURITY_ID}, COUNT(*) AS price_rows
                FROM {SECURITY_DAILY_PRICES}
                GROUP BY {SECURITY_ID}
                HAVING COUNT(*) < 30
            ) sparse_securities
            """,
        )
        metrics.append(_scalar_metric(category, "sparse securities below 30 price rows", sparse))
    else:
        metrics.append(_unavailable_metric(category, "distinct securities with price rows", "missing security_id column"))

    if TRADE_DATE in columns:
        date_range = safe_scalar(
            connection,
            f"SELECT CONCAT(MIN({TRADE_DATE}), ' to ', MAX({TRADE_DATE})) FROM {SECURITY_DAILY_PRICES}",
        )
        metrics.append(_text_metric(category, "security price trade-date range", date_range, f"column={TRADE_DATE}"))
    else:
        metrics.append(_unavailable_metric(category, "security price trade-date range", "missing trade_date column"))

    if {SECURITY_ID, TRADE_DATE}.issubset(columns):
        duplicates = safe_scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT {SECURITY_ID}, {TRADE_DATE}, COUNT(*) AS row_count
                FROM {SECURITY_DAILY_PRICES}
                GROUP BY {SECURITY_ID}, {TRADE_DATE}
                HAVING COUNT(*) > 1
            ) duplicate_prices
            """,
        )
        metrics.append(_scalar_metric(category, "duplicate security/date rows", duplicates))

    metrics.append(_non_trading_price_rows_metric(connection))
    return metrics


def _linked_event_security_metrics(connection: Any) -> list[CoverageMetric]:
    category = "linked event/security coverage"
    if not table_exists(connection, CYBER_EVENT_SECURITIES):
        return [_missing_metric(category, "linked securities count", CYBER_EVENT_SECURITIES)]

    columns = set(get_table_columns(connection, CYBER_EVENT_SECURITIES))
    metrics: list[CoverageMetric] = []
    if SECURITY_ID in columns:
        linked_securities = safe_scalar(
            connection,
            f"SELECT COUNT(DISTINCT {SECURITY_ID}) FROM {CYBER_EVENT_SECURITIES}",
        )
        metrics.append(_scalar_metric(category, "linked securities count", linked_securities))
        if table_exists(connection, SECURITY_DAILY_PRICES):
            linked_with_prices = safe_scalar(
                connection,
                f"""
                SELECT COUNT(DISTINCT ces.{SECURITY_ID})
                FROM {CYBER_EVENT_SECURITIES} ces
                INNER JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = ces.{SECURITY_ID}
                """,
            )
            linked_without_prices = safe_scalar(
                connection,
                f"""
                SELECT COUNT(DISTINCT ces.{SECURITY_ID})
                FROM {CYBER_EVENT_SECURITIES} ces
                LEFT JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = ces.{SECURITY_ID}
                WHERE sdp.{SECURITY_ID} IS NULL
                """,
            )
            metrics.append(_scalar_metric(category, "linked securities with price rows", linked_with_prices))
            metrics.append(_scalar_metric(category, "linked securities without price rows", linked_without_prices))
    else:
        metrics.append(_unavailable_metric(category, "linked securities count", "missing security_id column"))

    if CYBER_EVENT_ID in columns and SECURITY_ID in columns and table_exists(connection, SECURITY_DAILY_PRICES):
        priced_events = safe_scalar(
            connection,
            f"""
            SELECT COUNT(DISTINCT ces.{CYBER_EVENT_ID})
            FROM {CYBER_EVENT_SECURITIES} ces
            INNER JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = ces.{SECURITY_ID}
            """,
        )
        events_without_priced_security = safe_scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT ces.{CYBER_EVENT_ID}
                FROM {CYBER_EVENT_SECURITIES} ces
                LEFT JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = ces.{SECURITY_ID}
                GROUP BY ces.{CYBER_EVENT_ID}
                HAVING COUNT(sdp.{SECURITY_ID}) = 0
            ) events_without_prices
            """,
        )
        metrics.append(_scalar_metric(category, "linked events with at least one priced security", priced_events))
        metrics.append(_scalar_metric(category, "linked events with no priced security", events_without_priced_security))

    return metrics


def _index_coverage_metrics(connection: Any) -> list[CoverageMetric]:
    category = "index/benchmark coverage"
    metrics = [
        _table_count_metric(connection, category, "market index metadata rows", MARKET_INDEXES),
        _table_count_metric(connection, category, "index_daily_prices rows", INDEX_DAILY_PRICES),
        _table_count_metric(connection, category, "dji_daily_prices rows", DJI_DAILY_PRICES),
    ]

    if table_exists(connection, INDEX_DAILY_PRICES):
        columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
        if MARKET_INDEX_ID in columns:
            distinct_indexes = safe_scalar(
                connection,
                f"SELECT COUNT(DISTINCT {MARKET_INDEX_ID}) FROM {INDEX_DAILY_PRICES}",
            )
            metrics.append(_scalar_metric(category, "distinct indexes with price rows", distinct_indexes))
        else:
            metrics.append(_unavailable_metric(category, "distinct indexes with price rows", "missing market_index_id column"))

        if TRADE_DATE in columns:
            date_range = safe_scalar(
                connection,
                f"SELECT CONCAT(MIN({TRADE_DATE}), ' to ', MAX({TRADE_DATE})) FROM {INDEX_DAILY_PRICES}",
            )
            metrics.append(_text_metric(category, "index trade-date range", date_range, f"column={TRADE_DATE}"))

    dji_rows = count_rows(connection, DJI_DAILY_PRICES)
    if dji_rows == 0:
        metrics.append(CoverageMetric(category, "DJIA-specific table empty", "yes", "WARNING"))

    index_rows = count_rows(connection, INDEX_DAILY_PRICES)
    if index_rows and index_rows > 0:
        metrics.append(CoverageMetric(category, "benchmark data appears in index_daily_prices", "yes", "WARNING"))

    return metrics


def _calendar_coverage_metrics(connection: Any) -> list[CoverageMetric]:
    category = "market calendar coverage"
    if not table_exists(connection, MARKET_CALENDAR):
        return [_missing_metric(category, "market calendar rows", MARKET_CALENDAR)]

    columns = set(get_table_columns(connection, MARKET_CALENDAR))
    metrics = [_table_count_metric(connection, category, "market calendar rows", MARKET_CALENDAR)]
    if CALENDAR_DATE in columns:
        date_range = safe_scalar(
            connection,
            f"SELECT CONCAT(MIN({CALENDAR_DATE}), ' to ', MAX({CALENDAR_DATE})) FROM {MARKET_CALENDAR}",
        )
        metrics.append(_text_metric(category, "market calendar date range", date_range, f"column={CALENDAR_DATE}"))

    if IS_TRADING_DAY in columns:
        trading_days = safe_scalar(
            connection,
            f"SELECT COUNT(*) FROM {MARKET_CALENDAR} WHERE {IS_TRADING_DAY} = 1",
        )
        non_trading_days = safe_scalar(
            connection,
            f"SELECT COUNT(*) FROM {MARKET_CALENDAR} WHERE {IS_TRADING_DAY} = 0",
        )
        metrics.append(_scalar_metric(category, "trading-day count", trading_days))
        metrics.append(_scalar_metric(category, "non-trading-day count", non_trading_days))
        metrics.append(CoverageMetric(category, "trading-day flag detected", "yes", "OK", f"column={IS_TRADING_DAY}"))
        metrics.append(CoverageMetric(category, "usable for trading-day alignment", "yes", "OK"))
    else:
        metrics.append(CoverageMetric(category, "trading-day flag detected", "no", "WARNING"))
        metrics.append(CoverageMetric(category, "usable for trading-day alignment", "partial", "WARNING"))

    return metrics


def _event_window_support_metrics(connection: Any) -> list[CoverageMetric]:
    category = "event window support"
    metrics = [_table_count_metric(connection, category, "event window definitions", EVENT_WINDOWS)]

    if table_exists(connection, EVENT_WINDOWS):
        columns = set(get_table_columns(connection, EVENT_WINDOWS))
        if {WINDOW_CODE, PRE_EVENT_DAYS, POST_EVENT_DAYS}.issubset(columns):
            for window_code, pre_days, post_days in safe_fetch_all(
                connection,
                f"""
                SELECT {WINDOW_CODE}, {PRE_EVENT_DAYS}, {POST_EVENT_DAYS}
                FROM {EVENT_WINDOWS}
                ORDER BY {PRE_EVENT_DAYS}, {POST_EVENT_DAYS}, {WINDOW_CODE}
                """,
            ):
                metrics.append(
                    CoverageMetric(
                        category,
                        f"window {window_code}",
                        f"{pre_days} before / {post_days} after",
                        "OK",
                    )
                )

    if table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
        if {CYBER_EVENT_ID, SECURITY_ID, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns):
            security_coverable = safe_scalar(
                connection,
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT ewb.{CYBER_EVENT_ID}, ewb.event_window_id
                    FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
                    INNER JOIN {SECURITY_DAILY_PRICES} sdp
                        ON sdp.{SECURITY_ID} = ewb.{SECURITY_ID}
                       AND sdp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
                ) coverable_security_windows
                """,
            ) if table_exists(connection, SECURITY_DAILY_PRICES) else None
            metrics.append(
                _scalar_metric(
                    category,
                    "event/window rows with at least one security price in window",
                    security_coverable,
                )
            )

        if {CYBER_EVENT_ID, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns):
            index_coverable = safe_scalar(
                connection,
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT ewb.{CYBER_EVENT_ID}, ewb.event_window_id
                    FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
                    INNER JOIN {INDEX_DAILY_PRICES} idp
                        ON idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
                ) coverable_index_windows
                """,
            ) if table_exists(connection, INDEX_DAILY_PRICES) else None
            metrics.append(
                _scalar_metric(
                    category,
                    "event/window rows with at least one index price in window",
                    index_coverable,
                )
            )

    return metrics


def _non_trading_price_rows_metric(connection: Any) -> CoverageMetric:
    category = "security price coverage"
    if not table_exists(connection, MARKET_CALENDAR):
        return _unavailable_metric(category, "price rows on non-trading days", "market_calendar missing")
    price_columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if TRADE_DATE not in price_columns or not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns):
        return _unavailable_metric(category, "price rows on non-trading days", "missing date/calendar columns")
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE COALESCE(mc.{IS_TRADING_DAY}, 0) = 0
        """,
    )
    return _scalar_metric(category, "price rows on non-trading days", value)


def _table_count_metric(
    connection: Any,
    category: str,
    label: str,
    table_name: str,
) -> CoverageMetric:
    if not table_exists(connection, table_name):
        return _missing_metric(category, label, table_name)
    row_count = count_rows(connection, table_name)
    if row_count is None:
        return _unavailable_metric(category, label, "row count unavailable")
    return CoverageMetric(category, label, row_count, "EMPTY" if row_count == 0 else "OK")


def _scalar_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> CoverageMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    value = int(value)
    return CoverageMetric(category, label, value, "EMPTY" if value == 0 else "OK", detail)


def _text_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> CoverageMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    return CoverageMetric(category, label, str(value), "OK", detail)


def _missing_metric(category: str, label: str, table_name: str) -> CoverageMetric:
    return CoverageMetric(category, label, None, "MISSING", f"{table_name} table not found")


def _unavailable_metric(category: str, label: str, detail: str) -> CoverageMetric:
    return CoverageMetric(category, label, None, "UNAVAILABLE", detail)


def _positive_metric(metric: CoverageMetric | None) -> bool:
    return bool(metric and isinstance(metric.value, int) and metric.value > 0)


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _write_metrics_csv(path: Path, metrics: tuple[CoverageMetric, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("category", "name", "value", "status", "detail"),
        )
        writer.writeheader()
        for metric in metrics:
            writer.writerow(asdict(metric))


def _write_issues_csv(path: Path, issues: tuple[CoverageIssue, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("severity", "message"))
        writer.writeheader()
        for issue in issues:
            writer.writerow(asdict(issue))


def _write_report_json(path: Path, result: MarketDataCoverageReport) -> None:
    payload = asdict(result)
    payload["export_paths"] = [str(path) for path in result.export_paths]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _copy_report_with_export_paths(
    result: MarketDataCoverageReport,
    export_paths: tuple[Path, ...],
) -> MarketDataCoverageReport:
    return MarketDataCoverageReport(
        generated_at=result.generated_at,
        connection_ok=result.connection_ok,
        market_data_status=result.market_data_status,
        database_name=result.database_name,
        metrics=result.metrics,
        issues=result.issues,
        export_paths=export_paths,
        error_message=result.error_message,
    )


def _format_value(value: int | str | None) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, int):
        return f"{value:,}"
    return value


def _log_report(
    result: MarketDataCoverageReport,
    logger: logging.Logger | None,
) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Market data coverage report failed: %s", result.error_message)
        return
    logger.info(
        "Market data coverage report completed: database=%s status=%s metrics=%s issues=%s",
        result.database_name,
        result.market_data_status,
        len(result.metrics),
        len(result.issues),
    )
