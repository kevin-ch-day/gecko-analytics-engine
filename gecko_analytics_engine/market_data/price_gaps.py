"""Read-only security price gap analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    EXCHANGE_CODE,
    EXCHANGE_ID,
    EXCHANGES,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    SECURITIES,
    SECURITY_DAILY_PRICES,
    SECURITY_ID,
    TICKER_SYMBOL,
    TRADE_DATE,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class SecurityPriceCoverageRow:
    """Per-security price coverage diagnostic row."""

    security_id: int | None
    ticker_symbol: str | None
    exchange_code: str | None
    price_rows: int
    distinct_price_dates: int
    first_trade_date: str | None
    last_trade_date: str | None
    trading_price_dates: int | None
    trading_days_in_span: int | None
    approximate_missing_trading_days: int | None
    non_trading_price_rows: int | None
    duplicate_trade_dates: int | None


@dataclass(frozen=True)
class PriceGapIssue:
    """A security price gap warning or blocker."""

    severity: str
    message: str


@dataclass(frozen=True)
class SecurityPriceGapReport:
    """Read-only security price gap analysis report."""

    generated_at: str
    connection_ok: bool
    gap_status: str
    database_name: str | None = None
    securities_analyzed: int = 0
    securities_with_prices: int = 0
    securities_without_prices: int = 0
    securities_with_missing_trading_days: int = 0
    total_approximate_missing_trading_days: int = 0
    securities_with_non_trading_prices: int = 0
    securities_with_duplicate_dates: int = 0
    top_gap_rows: tuple[SecurityPriceCoverageRow, ...] = ()
    top_non_trading_rows: tuple[SecurityPriceCoverageRow, ...] = ()
    no_price_rows: tuple[SecurityPriceCoverageRow, ...] = ()
    issues: tuple[PriceGapIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_security_price_gap_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> SecurityPriceGapReport:
    """Run and export the read-only security price gap report."""

    generated_at = datetime.now(UTC).isoformat()

    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            rows = _fetch_security_price_coverage_rows(connection)
    except DatabaseConnectionError as exc:
        result = SecurityPriceGapReport(
            generated_at=generated_at,
            connection_ok=False,
            gap_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = SecurityPriceGapReport(
            generated_at=generated_at,
            connection_ok=False,
            gap_status="BLOCKED",
            error_message=f"Security price gap report failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_security_price_gap_issues(rows)
    result = SecurityPriceGapReport(
        generated_at=generated_at,
        connection_ok=True,
        gap_status=determine_security_price_gap_status(issues),
        database_name=database_name,
        securities_analyzed=len(rows),
        securities_with_prices=sum(1 for row in rows if row.price_rows > 0),
        securities_without_prices=sum(1 for row in rows if row.price_rows == 0),
        securities_with_missing_trading_days=sum(
            1 for row in rows if (row.approximate_missing_trading_days or 0) > 0
        ),
        total_approximate_missing_trading_days=sum(row.approximate_missing_trading_days or 0 for row in rows),
        securities_with_non_trading_prices=sum(1 for row in rows if (row.non_trading_price_rows or 0) > 0),
        securities_with_duplicate_dates=sum(1 for row in rows if (row.duplicate_trade_dates or 0) > 0),
        top_gap_rows=_top_gap_rows(rows),
        top_non_trading_rows=_top_non_trading_rows(rows),
        no_price_rows=_no_price_rows(rows),
        issues=issues,
    )
    result = export_security_price_gap_report(result, rows, paths, logger)
    _log_report(result, logger)
    return result


def export_security_price_gap_report(
    result: SecurityPriceGapReport,
    rows: tuple[SecurityPriceCoverageRow, ...],
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> SecurityPriceGapReport:
    """Export the security price gap report artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "security_price_gap_report.json"
    detail_csv = paths.exports_dir / "security_price_gap_detail.csv"
    issues_csv = paths.exports_dir / "security_price_gap_issues.csv"
    export_paths = (json_path, detail_csv, issues_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    _write_detail_csv(detail_csv, rows)
    _write_issues_csv(issues_csv, result.issues)
    _write_report_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Security price gap exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def format_security_price_gap_report(result: SecurityPriceGapReport) -> list[str]:
    """Format the security price gap report for console output."""

    lines = ["", "Security Price Gap Analysis", "---------------------------"]
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
            f"Overall status: {result.gap_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Summary:",
            f"  Securities analyzed: {result.securities_analyzed:,}",
            f"  Securities with price rows: {result.securities_with_prices:,}",
            f"  Securities without price rows: {result.securities_without_prices:,}",
            f"  Securities with approximate trading-day gaps: {result.securities_with_missing_trading_days:,}",
            f"  Approximate missing trading days across securities: {result.total_approximate_missing_trading_days:,}",
            f"  Securities with non-trading price rows: {result.securities_with_non_trading_prices:,}",
            f"  Securities with duplicate trade dates: {result.securities_with_duplicate_dates:,}",
        ]
    )

    if result.top_gap_rows:
        lines.extend(["", "Top apparent trading-day gaps:"])
        for row in result.top_gap_rows:
            lines.append(
                "  "
                f"{_security_label(row)}: missing={_format_optional_int(row.approximate_missing_trading_days)}, "
                f"rows={row.price_rows:,}, range={row.first_trade_date or 'Unknown'} to {row.last_trade_date or 'Unknown'}"
            )

    if result.top_non_trading_rows:
        lines.extend(["", "Top non-trading price-row counts:"])
        for row in result.top_non_trading_rows:
            lines.append(
                "  "
                f"{_security_label(row)}: non-trading rows={_format_optional_int(row.non_trading_price_rows)}, "
                f"rows={row.price_rows:,}"
            )

    if result.no_price_rows:
        lines.extend(["", "Securities without price rows:"])
        for row in result.no_price_rows:
            lines.append(f"  {_security_label(row)}")

    lines.extend(["", "Issues / notes:"])
    if result.issues:
        for issue in result.issues:
            lines.append(f"  [{issue.severity}] {issue.message}")
    else:
        lines.append("  No security price gap issues detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def print_security_price_gap_report(result: SecurityPriceGapReport) -> None:
    """Print security price gap report."""

    for line in format_security_price_gap_report(result):
        print(line)


def determine_security_price_gap_status(issues: tuple[PriceGapIssue, ...]) -> str:
    """Return a high-level security price gap status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if issues:
        return "NEEDS_REVIEW"
    return "OK"


def build_security_price_gap_issues(
    rows: tuple[SecurityPriceCoverageRow, ...],
) -> tuple[PriceGapIssue, ...]:
    """Build warnings and blockers from per-security price coverage rows."""

    if not rows:
        return (PriceGapIssue("BLOCKER", "No securities could be analyzed for price coverage."),)

    issues: list[PriceGapIssue] = []
    without_prices = sum(1 for row in rows if row.price_rows == 0)
    if without_prices:
        issues.append(PriceGapIssue("WARNING", f"{without_prices} securities have no price rows."))

    with_gaps = sum(1 for row in rows if (row.approximate_missing_trading_days or 0) > 0)
    missing_days = sum(row.approximate_missing_trading_days or 0 for row in rows)
    if with_gaps:
        issues.append(
            PriceGapIssue(
                "WARNING",
                f"{with_gaps} securities have apparent trading-day gaps totaling about {missing_days:,} missing days.",
            )
        )

    non_trading = sum(1 for row in rows if (row.non_trading_price_rows or 0) > 0)
    if non_trading:
        issues.append(PriceGapIssue("WARNING", f"{non_trading} securities have price rows on non-trading dates."))

    duplicates = sum(1 for row in rows if (row.duplicate_trade_dates or 0) > 0)
    if duplicates:
        issues.append(PriceGapIssue("WARNING", f"{duplicates} securities have duplicate trade-date groups."))

    issues.append(
        PriceGapIssue(
            "INFO",
            "Approximate gaps compare price dates to the US market_calendar inside each security's observed date span.",
        )
    )
    issues.append(
        PriceGapIssue(
            "INFO",
            "Large full-span gaps may be acceptable for early event-study work if the event windows themselves are covered.",
        )
    )
    return tuple(issues)


def _fetch_security_price_coverage_rows(connection: Any) -> tuple[SecurityPriceCoverageRow, ...]:
    if not _has_required_tables_and_columns(connection):
        return ()

    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            s.{SECURITY_ID},
            s.{TICKER_SYMBOL},
            e.{EXCHANGE_CODE},
            COALESCE(pa.price_rows, 0) AS price_rows,
            COALESCE(pa.distinct_price_dates, 0) AS distinct_price_dates,
            pa.first_trade_date,
            pa.last_trade_date,
            COALESCE(tpa.trading_price_dates, 0) AS trading_price_dates,
            CASE
                WHEN pa.first_trade_date IS NULL THEN NULL
                ELSE (
                    SELECT COUNT(*)
                    FROM {MARKET_CALENDAR} mc_span
                    WHERE mc_span.{CALENDAR_DATE} BETWEEN pa.first_trade_date AND pa.last_trade_date
                      AND mc_span.{IS_TRADING_DAY} = 1
                )
            END AS trading_days_in_span,
            CASE
                WHEN pa.first_trade_date IS NULL THEN NULL
                ELSE GREATEST(
                    (
                        SELECT COUNT(*)
                        FROM {MARKET_CALENDAR} mc_span
                        WHERE mc_span.{CALENDAR_DATE} BETWEEN pa.first_trade_date AND pa.last_trade_date
                          AND mc_span.{IS_TRADING_DAY} = 1
                    ) - COALESCE(tpa.trading_price_dates, 0),
                    0
                )
            END AS approximate_missing_trading_days,
            COALESCE(nt.non_trading_price_rows, 0) AS non_trading_price_rows,
            COALESCE(dups.duplicate_trade_dates, 0) AS duplicate_trade_dates
        FROM {SECURITIES} s
        LEFT JOIN {EXCHANGES} e ON e.{EXCHANGE_ID} = s.{EXCHANGE_ID}
        LEFT JOIN (
            SELECT
                {SECURITY_ID},
                COUNT(*) AS price_rows,
                COUNT(DISTINCT {TRADE_DATE}) AS distinct_price_dates,
                MIN({TRADE_DATE}) AS first_trade_date,
                MAX({TRADE_DATE}) AS last_trade_date
            FROM {SECURITY_DAILY_PRICES}
            GROUP BY {SECURITY_ID}
        ) pa ON pa.{SECURITY_ID} = s.{SECURITY_ID}
        LEFT JOIN (
            SELECT
                sdp.{SECURITY_ID},
                COUNT(DISTINCT sdp.{TRADE_DATE}) AS trading_price_dates
            FROM {SECURITY_DAILY_PRICES} sdp
            INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
            WHERE mc.{IS_TRADING_DAY} = 1
            GROUP BY sdp.{SECURITY_ID}
        ) tpa ON tpa.{SECURITY_ID} = s.{SECURITY_ID}
        LEFT JOIN (
            SELECT
                sdp.{SECURITY_ID},
                COUNT(*) AS non_trading_price_rows
            FROM {SECURITY_DAILY_PRICES} sdp
            INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
            WHERE mc.{IS_TRADING_DAY} = 0
            GROUP BY sdp.{SECURITY_ID}
        ) nt ON nt.{SECURITY_ID} = s.{SECURITY_ID}
        LEFT JOIN (
            SELECT duplicate_groups.{SECURITY_ID}, COUNT(*) AS duplicate_trade_dates
            FROM (
                SELECT {SECURITY_ID}, {TRADE_DATE}
                FROM {SECURITY_DAILY_PRICES}
                GROUP BY {SECURITY_ID}, {TRADE_DATE}
                HAVING COUNT(*) > 1
            ) duplicate_groups
            GROUP BY duplicate_groups.{SECURITY_ID}
        ) dups ON dups.{SECURITY_ID} = s.{SECURITY_ID}
        ORDER BY approximate_missing_trading_days DESC, non_trading_price_rows DESC, s.{TICKER_SYMBOL}
        """,
    )

    return tuple(
        SecurityPriceCoverageRow(
            security_id=int(row[0]) if row[0] is not None else None,
            ticker_symbol=str(row[1]) if row[1] else None,
            exchange_code=str(row[2]) if row[2] else None,
            price_rows=int(row[3] or 0),
            distinct_price_dates=int(row[4] or 0),
            first_trade_date=str(row[5]) if row[5] else None,
            last_trade_date=str(row[6]) if row[6] else None,
            trading_price_dates=int(row[7]) if row[7] is not None else None,
            trading_days_in_span=int(row[8]) if row[8] is not None else None,
            approximate_missing_trading_days=int(row[9]) if row[9] is not None else None,
            non_trading_price_rows=int(row[10]) if row[10] is not None else None,
            duplicate_trade_dates=int(row[11]) if row[11] is not None else None,
        )
        for row in rows
    )


def _has_required_tables_and_columns(connection: Any) -> bool:
    for table_name in (SECURITIES, SECURITY_DAILY_PRICES, MARKET_CALENDAR):
        if not table_exists(connection, table_name):
            return False

    securities_columns = set(get_table_columns(connection, SECURITIES))
    price_columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    required_securities = {SECURITY_ID, TICKER_SYMBOL}
    required_prices = {SECURITY_ID, TRADE_DATE}
    required_calendar = {CALENDAR_DATE, IS_TRADING_DAY}
    return (
        required_securities.issubset(securities_columns)
        and required_prices.issubset(price_columns)
        and required_calendar.issubset(calendar_columns)
    )


def _top_gap_rows(rows: tuple[SecurityPriceCoverageRow, ...]) -> tuple[SecurityPriceCoverageRow, ...]:
    return tuple(
        row
        for row in sorted(
            rows,
            key=lambda item: (item.approximate_missing_trading_days or 0, item.non_trading_price_rows or 0),
            reverse=True,
        )
        if (row.approximate_missing_trading_days or 0) > 0
    )[:20]


def _top_non_trading_rows(rows: tuple[SecurityPriceCoverageRow, ...]) -> tuple[SecurityPriceCoverageRow, ...]:
    return tuple(
        row
        for row in sorted(rows, key=lambda item: item.non_trading_price_rows or 0, reverse=True)
        if (row.non_trading_price_rows or 0) > 0
    )[:20]


def _no_price_rows(rows: tuple[SecurityPriceCoverageRow, ...]) -> tuple[SecurityPriceCoverageRow, ...]:
    return tuple(sorted((row for row in rows if row.price_rows == 0), key=lambda item: item.ticker_symbol or ""))[:20]


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _write_detail_csv(path: Path, rows: tuple[SecurityPriceCoverageRow, ...]) -> None:
    write_rows_csv(
        path,
        rows,
        (
            "security_id",
            "ticker_symbol",
            "exchange_code",
            "price_rows",
            "distinct_price_dates",
            "first_trade_date",
            "last_trade_date",
            "trading_price_dates",
            "trading_days_in_span",
            "approximate_missing_trading_days",
            "non_trading_price_rows",
            "duplicate_trade_dates",
        ),
    )


def _write_issues_csv(path: Path, issues: tuple[PriceGapIssue, ...]) -> None:
    write_rows_csv(path, issues, ("severity", "message"))


def _write_report_json(path: Path, result: SecurityPriceGapReport) -> None:
    write_dataclass_json(path, result)


def _security_label(row: SecurityPriceCoverageRow) -> str:
    label = row.ticker_symbol or str(row.security_id)
    if row.exchange_code:
        return f"{label} ({row.exchange_code})"
    return label


def _format_optional_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _log_report(result: SecurityPriceGapReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Security price gap report failed: %s", result.error_message)
        return
    logger.info(
        "Security price gap report completed: database=%s status=%s securities=%s issues=%s",
        result.database_name,
        result.gap_status,
        result.securities_analyzed,
        len(result.issues),
    )
