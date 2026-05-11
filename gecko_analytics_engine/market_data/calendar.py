"""Read-only market calendar alignment diagnostics."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import (
    get_table_columns,
    safe_fetch_all,
    safe_scalar,
    table_exists,
)
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    EXCHANGE_CODE,
    EXCHANGE_ID,
    EXCHANGES,
    HOLIDAY_NAME,
    INDEX_DAILY_PRICES,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    MARKET_CODE,
    MARKET_INDEX_ID,
    SECURITIES,
    SECURITY_DAILY_PRICES,
    SECURITY_ID,
    TICKER_SYMBOL,
    TRADE_DATE,
)
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class CalendarDiagnosticMetric:
    """One calendar alignment diagnostic metric."""

    category: str
    name: str
    value: int | str | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class CalendarDiagnosticNote:
    """A diagnostic note, warning, or blocker."""

    severity: str
    message: str


@dataclass(frozen=True)
class NonTradingDateSummary:
    """Count of suspected non-trading rows for one date."""

    trade_date: str
    row_count: int
    weekday: str
    day_type: str
    holiday_name: str | None = None


@dataclass(frozen=True)
class NonTradingSecuritySummary:
    """Count of suspected non-trading rows for one security."""

    security_id: int | None
    ticker_symbol: str | None
    exchange_code: str | None
    row_count: int


@dataclass(frozen=True)
class MarketCalendarDiagnosticReport:
    """Read-only market calendar alignment diagnostic report."""

    generated_at: str
    connection_ok: bool
    diagnostic_status: str
    database_name: str | None = None
    metrics: tuple[CalendarDiagnosticMetric, ...] = ()
    notes: tuple[CalendarDiagnosticNote, ...] = ()
    top_dates: tuple[NonTradingDateSummary, ...] = ()
    top_securities: tuple[NonTradingSecuritySummary, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def classify_weekday(date_value: date | datetime | str) -> str:
    """Classify a date as weekday or weekend."""

    if isinstance(date_value, str):
        parsed = datetime.fromisoformat(date_value).date()
    elif isinstance(date_value, datetime):
        parsed = date_value.date()
    else:
        parsed = date_value
    return "weekend" if parsed.weekday() >= 5 else "weekday"


def run_market_calendar_diagnostic(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketCalendarDiagnosticReport:
    """Run and export the read-only market calendar alignment diagnostic."""

    generated_at = datetime.now(UTC).isoformat()

    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            metrics = _collect_calendar_metrics(connection)
            top_dates = _fetch_top_non_trading_dates(connection)
            top_securities = _fetch_top_non_trading_securities(connection)
    except DatabaseConnectionError as exc:
        result = MarketCalendarDiagnosticReport(
            generated_at=generated_at,
            connection_ok=False,
            diagnostic_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = MarketCalendarDiagnosticReport(
            generated_at=generated_at,
            connection_ok=False,
            diagnostic_status="BLOCKED",
            error_message=f"Market calendar diagnostic failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    notes = build_market_calendar_diagnostic_notes(metrics)
    result = MarketCalendarDiagnosticReport(
        generated_at=generated_at,
        connection_ok=True,
        diagnostic_status=determine_calendar_diagnostic_status(notes),
        database_name=database_name,
        metrics=metrics,
        notes=notes,
        top_dates=top_dates,
        top_securities=top_securities,
    )
    result = export_market_calendar_diagnostic(result, paths, logger)
    _log_report(result, logger)
    return result


def export_market_calendar_diagnostic(
    result: MarketCalendarDiagnosticReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketCalendarDiagnosticReport:
    """Export market calendar diagnostic artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "market_calendar_diagnostic_report.json"
    dates_csv = paths.exports_dir / "non_trading_price_dates_summary.csv"
    securities_csv = paths.exports_dir / "non_trading_price_securities_summary.csv"
    export_paths = (json_path, dates_csv, securities_csv)
    result_with_exports = _copy_report_with_export_paths(result, export_paths)

    _write_dates_csv(dates_csv, result.top_dates)
    _write_securities_csv(securities_csv, result.top_securities)
    _write_report_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Market calendar diagnostic exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def format_market_calendar_diagnostic(result: MarketCalendarDiagnosticReport) -> list[str]:
    """Format the calendar diagnostic for console output."""

    lines = ["", "Market Calendar Alignment Diagnostic", "------------------------------------"]
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
            f"Overall status: {result.diagnostic_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Diagnostic checks:",
        ]
    )

    for metric in result.metrics:
        value = _format_value(metric.value)
        detail = f" ({metric.detail})" if metric.detail else ""
        lines.append(f"  [{metric.status}] {metric.category} - {metric.name}: {value}{detail}")

    if result.top_dates:
        lines.extend(["", "Top suspected dates:"])
        for item in result.top_dates[:20]:
            holiday = f", holiday={item.holiday_name}" if item.holiday_name else ""
            lines.append(
                f"  {item.trade_date}: {item.row_count:,} rows, {item.weekday}, {item.day_type}{holiday}"
            )

    if result.top_securities:
        lines.extend(["", "Top affected securities:"])
        for item in result.top_securities[:20]:
            label = item.ticker_symbol or str(item.security_id)
            exchange = item.exchange_code or "unknown exchange"
            lines.append(f"  {label} ({exchange}): {item.row_count:,} rows")

    lines.extend(["", "Interpretation / notes:"])
    if result.notes:
        for note in result.notes:
            lines.append(f"  [{note.severity}] {note.message}")
    else:
        lines.append("  No calendar alignment issues detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def print_market_calendar_diagnostic(result: MarketCalendarDiagnosticReport) -> None:
    """Print market calendar diagnostic report."""

    for line in format_market_calendar_diagnostic(result):
        print(line)


def determine_calendar_diagnostic_status(
    notes: tuple[CalendarDiagnosticNote, ...],
) -> str:
    """Return diagnostic status."""

    if any(note.severity == "BLOCKER" for note in notes):
        return "BLOCKED"
    if notes:
        return "NEEDS_REVIEW"
    return "OK"


def build_market_calendar_diagnostic_notes(
    metrics: tuple[CalendarDiagnosticMetric, ...],
) -> tuple[CalendarDiagnosticNote, ...]:
    """Generate diagnostic interpretation notes."""

    values = {(metric.category, metric.name): metric for metric in metrics}
    notes: list[CalendarDiagnosticNote] = []

    for key, message in (
        (("calendar schema", "market_calendar columns"), "market_calendar is unavailable."),
        (("security price schema", "security_daily_prices columns"), "security_daily_prices is unavailable."),
    ):
        metric = values.get(key)
        if metric is None or metric.status in {"MISSING", "UNAVAILABLE"}:
            notes.append(CalendarDiagnosticNote("BLOCKER", message))

    no_calendar_match = values.get(("price/calendar comparison", "security price rows with no calendar match"))
    if _positive_metric(no_calendar_match):
        notes.append(
            CalendarDiagnosticNote(
                "WARNING",
                f"{no_calendar_match.value} security price rows have no matching market_calendar date.",
            )
        )

    marked_non_trading = values.get(("price/calendar comparison", "security price rows marked non-trading"))
    if _positive_metric(marked_non_trading):
        notes.append(
            CalendarDiagnosticNote(
                "WARNING",
                f"{marked_non_trading.value} security price rows match calendar dates marked non-trading.",
            )
        )

    index_marked_non_trading = values.get(("index price comparison", "index price rows marked non-trading"))
    if _positive_metric(index_marked_non_trading):
        notes.append(
            CalendarDiagnosticNote(
                "WARNING",
                f"{index_marked_non_trading.value} index price rows match calendar dates marked non-trading.",
            )
        )

    market_codes = values.get(("calendar schema", "market code values"))
    if market_codes and isinstance(market_codes.value, str) and market_codes.value:
        notes.append(
            CalendarDiagnosticNote(
                "INFO",
                f"market_calendar has market_code values: {market_codes.value}; verify this matches security exchanges.",
            )
        )

    exchange_distribution = values.get(("suspected non-trading rows", "distribution by exchange"))
    if exchange_distribution and exchange_distribution.value:
        notes.append(
            CalendarDiagnosticNote(
                "INFO",
                "Suspected rows can be joined to securities/exchanges; review exchange distribution for scope mismatch.",
            )
        )

    notes.append(
        CalendarDiagnosticNote(
            "INFO",
            "Do not treat suspected rows as bad data until market_code/exchange scope and calendar source are verified.",
        )
    )

    return tuple(notes)


def _collect_calendar_metrics(connection: Any) -> tuple[CalendarDiagnosticMetric, ...]:
    metrics: list[CalendarDiagnosticMetric] = []
    metrics.extend(_calendar_schema_metrics(connection))
    metrics.extend(_security_price_schema_metrics(connection))
    metrics.extend(_security_price_comparison_metrics(connection))
    metrics.extend(_index_price_comparison_metrics(connection))
    metrics.extend(_suspected_distribution_metrics(connection))
    return tuple(metrics)


def _calendar_schema_metrics(connection: Any) -> list[CalendarDiagnosticMetric]:
    category = "calendar schema"
    if not table_exists(connection, MARKET_CALENDAR):
        return [_missing_metric(category, "market_calendar columns", MARKET_CALENDAR)]

    columns = get_table_columns(connection, MARKET_CALENDAR)
    metrics = [
        CalendarDiagnosticMetric(category, "market_calendar columns", ", ".join(columns), "OK"),
        CalendarDiagnosticMetric(
            category,
            "date column used",
            CALENDAR_DATE if CALENDAR_DATE in columns else None,
            "OK" if CALENDAR_DATE in columns else "UNAVAILABLE",
        ),
        CalendarDiagnosticMetric(
            category,
            "trading-day flag used",
            IS_TRADING_DAY if IS_TRADING_DAY in columns else None,
            "OK" if IS_TRADING_DAY in columns else "UNAVAILABLE",
        ),
    ]

    code_columns = [column for column in (MARKET_CODE, EXCHANGE_CODE, MARKET_INDEX_ID) if column in columns]
    metrics.append(
        CalendarDiagnosticMetric(
            category,
            "market/exchange/index code columns",
            ", ".join(code_columns) if code_columns else "none",
            "OK" if code_columns else "WARNING",
        )
    )
    if MARKET_CODE in columns:
        values = safe_fetch_all(
            connection,
            f"SELECT DISTINCT {MARKET_CODE} FROM {MARKET_CALENDAR} ORDER BY {MARKET_CODE}",
        )
        metrics.append(
            CalendarDiagnosticMetric(
                category,
                "market code values",
                ", ".join(str(row[0]) for row in values),
                "OK",
            )
        )

    return metrics


def _security_price_schema_metrics(connection: Any) -> list[CalendarDiagnosticMetric]:
    category = "security price schema"
    if not table_exists(connection, SECURITY_DAILY_PRICES):
        return [_missing_metric(category, "security_daily_prices columns", SECURITY_DAILY_PRICES)]

    columns = get_table_columns(connection, SECURITY_DAILY_PRICES)
    metrics = [
        CalendarDiagnosticMetric(category, "security_daily_prices columns", ", ".join(columns), "OK"),
        CalendarDiagnosticMetric(
            category,
            "date column used",
            TRADE_DATE if TRADE_DATE in columns else None,
            "OK" if TRADE_DATE in columns else "UNAVAILABLE",
        ),
        CalendarDiagnosticMetric(
            category,
            "security identifier column used",
            SECURITY_ID if SECURITY_ID in columns else None,
            "OK" if SECURITY_ID in columns else "UNAVAILABLE",
        ),
    ]
    security_columns = set(get_table_columns(connection, SECURITIES)) if table_exists(connection, SECURITIES) else set()
    exchange_columns = set(get_table_columns(connection, EXCHANGES)) if table_exists(connection, EXCHANGES) else set()
    joinable = EXCHANGE_ID in security_columns and EXCHANGE_ID in exchange_columns
    metrics.append(
        CalendarDiagnosticMetric(
            category,
            "exchange links available",
            "yes" if joinable else "no",
            "OK" if joinable else "WARNING",
            "securities.exchange_id -> exchanges.exchange_id",
        )
    )
    return metrics


def _security_price_comparison_metrics(connection: Any) -> list[CalendarDiagnosticMetric]:
    category = "price/calendar comparison"
    if not _has_required_price_calendar_columns(connection):
        return [_unavailable_metric(category, "security price rows marked non-trading", "missing price/calendar columns")]

    no_match = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{CALENDAR_DATE} IS NULL
        """,
    )
    marked_non_trading = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        """,
    )
    marked_trading = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 1
        """,
    )
    distinct_dates = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT sdp.{TRADE_DATE})
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        """,
    )
    affected_securities = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT sdp.{SECURITY_ID})
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        """,
    )
    suspected_range = safe_scalar(
        connection,
        f"""
        SELECT CONCAT(MIN(sdp.{TRADE_DATE}), ' to ', MAX(sdp.{TRADE_DATE}))
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        """,
    )
    price_range = safe_scalar(
        connection,
        f"SELECT CONCAT(MIN({TRADE_DATE}), ' to ', MAX({TRADE_DATE})) FROM {SECURITY_DAILY_PRICES}",
    )
    calendar_range = safe_scalar(
        connection,
        f"SELECT CONCAT(MIN({CALENDAR_DATE}), ' to ', MAX({CALENDAR_DATE})) FROM {MARKET_CALENDAR}",
    )
    outside_calendar = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES}
        WHERE {TRADE_DATE} < (SELECT MIN({CALENDAR_DATE}) FROM {MARKET_CALENDAR})
           OR {TRADE_DATE} > (SELECT MAX({CALENDAR_DATE}) FROM {MARKET_CALENDAR})
        """,
    )

    return [
        _scalar_metric(category, "security price rows with no calendar match", no_match),
        _scalar_metric(category, "security price rows marked non-trading", marked_non_trading),
        _scalar_metric(category, "security price rows marked trading", marked_trading),
        _scalar_metric(category, "distinct suspected dates", distinct_dates),
        _scalar_metric(category, "distinct affected securities", affected_securities),
        _text_metric(category, "suspected date range", suspected_range),
        _text_metric(category, "security_daily_prices date range", price_range),
        _text_metric(category, "market_calendar date range", calendar_range),
        _scalar_metric(category, "price rows outside calendar range", outside_calendar),
    ]


def _index_price_comparison_metrics(connection: Any) -> list[CalendarDiagnosticMetric]:
    category = "index price comparison"
    if not table_exists(connection, INDEX_DAILY_PRICES):
        return [_missing_metric(category, "index price rows marked non-trading", INDEX_DAILY_PRICES)]
    index_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR)) if table_exists(connection, MARKET_CALENDAR) else set()
    if TRADE_DATE not in index_columns or not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns):
        return [_unavailable_metric(category, "index price rows marked non-trading", "missing index/calendar columns")]

    marked_non_trading = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {INDEX_DAILY_PRICES} idp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = idp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        """,
    )
    no_match = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {INDEX_DAILY_PRICES} idp
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = idp.{TRADE_DATE}
        WHERE mc.{CALENDAR_DATE} IS NULL
        """,
    )
    date_range = safe_scalar(
        connection,
        f"SELECT CONCAT(MIN({TRADE_DATE}), ' to ', MAX({TRADE_DATE})) FROM {INDEX_DAILY_PRICES}",
    )
    distinct_indexes = safe_scalar(
        connection,
        f"SELECT COUNT(DISTINCT {MARKET_INDEX_ID}) FROM {INDEX_DAILY_PRICES}",
    ) if MARKET_INDEX_ID in index_columns else None
    return [
        _scalar_metric(category, "index price rows marked non-trading", marked_non_trading),
        _scalar_metric(category, "index price rows with no calendar match", no_match),
        _text_metric(category, "index price date range", date_range),
        _scalar_metric(category, "distinct affected indexes", distinct_indexes),
    ]


def _suspected_distribution_metrics(connection: Any) -> list[CalendarDiagnosticMetric]:
    category = "suspected non-trading rows"
    if not _has_required_price_calendar_columns(connection):
        return [_unavailable_metric(category, "distribution by exchange", "missing price/calendar columns")]

    weekend_count = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
          AND WEEKDAY(sdp.{TRADE_DATE}) IN (5, 6)
        """,
    )
    weekday_count = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
          AND WEEKDAY(sdp.{TRADE_DATE}) NOT IN (5, 6)
        """,
    )
    holiday_count = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
          AND mc.{HOLIDAY_NAME} IS NOT NULL
        """,
    ) if HOLIDAY_NAME in set(get_table_columns(connection, MARKET_CALENDAR)) else None
    exchange_distribution = _exchange_distribution(connection)
    return [
        _scalar_metric(category, "suspected weekend rows", weekend_count),
        _scalar_metric(category, "suspected weekday rows", weekday_count),
        _scalar_metric(category, "suspected holiday rows", holiday_count),
        CalendarDiagnosticMetric(
            category,
            "distribution by exchange",
            exchange_distribution,
            "OK" if exchange_distribution else "UNAVAILABLE",
        ),
    ]


def _fetch_top_non_trading_dates(connection: Any) -> tuple[NonTradingDateSummary, ...]:
    if not _has_required_price_calendar_columns(connection):
        return ()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            sdp.{TRADE_DATE},
            COUNT(*) AS row_count,
            mc.{HOLIDAY_NAME}
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        WHERE mc.{IS_TRADING_DAY} = 0
        GROUP BY sdp.{TRADE_DATE}, mc.{HOLIDAY_NAME}
        ORDER BY row_count DESC, sdp.{TRADE_DATE}
        LIMIT 20
        """,
    )
    return tuple(
        NonTradingDateSummary(
            trade_date=str(row[0]),
            row_count=int(row[1]),
            weekday=classify_weekday(str(row[0])),
            day_type="holiday" if row[2] else "calendar_non_trading",
            holiday_name=str(row[2]) if row[2] else None,
        )
        for row in rows
    )


def _fetch_top_non_trading_securities(connection: Any) -> tuple[NonTradingSecuritySummary, ...]:
    if not _has_required_price_calendar_columns(connection):
        return ()
    if not table_exists(connection, SECURITIES):
        return ()
    security_columns = set(get_table_columns(connection, SECURITIES))
    exchange_join = table_exists(connection, EXCHANGES) and EXCHANGE_ID in security_columns
    exchange_select = f"e.{EXCHANGE_CODE}" if exchange_join else "NULL"
    exchange_join_sql = f"LEFT JOIN {EXCHANGES} e ON e.{EXCHANGE_ID} = s.{EXCHANGE_ID}" if exchange_join else ""
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            sdp.{SECURITY_ID},
            s.{TICKER_SYMBOL},
            {exchange_select},
            COUNT(*) AS row_count
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        LEFT JOIN {SECURITIES} s ON s.{SECURITY_ID} = sdp.{SECURITY_ID}
        {exchange_join_sql}
        WHERE mc.{IS_TRADING_DAY} = 0
        GROUP BY sdp.{SECURITY_ID}, s.{TICKER_SYMBOL}, {exchange_select}
        ORDER BY row_count DESC, sdp.{SECURITY_ID}
        LIMIT 20
        """,
    )
    return tuple(
        NonTradingSecuritySummary(
            security_id=int(row[0]) if row[0] is not None else None,
            ticker_symbol=str(row[1]) if row[1] else None,
            exchange_code=str(row[2]) if row[2] else None,
            row_count=int(row[3]),
        )
        for row in rows
    )


def _exchange_distribution(connection: Any) -> str | None:
    if not table_exists(connection, SECURITIES) or not table_exists(connection, EXCHANGES):
        return None
    security_columns = set(get_table_columns(connection, SECURITIES))
    exchange_columns = set(get_table_columns(connection, EXCHANGES))
    if EXCHANGE_ID not in security_columns or EXCHANGE_ID not in exchange_columns:
        return None
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT e.{EXCHANGE_CODE}, COUNT(*) AS row_count
        FROM {SECURITY_DAILY_PRICES} sdp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = sdp.{TRADE_DATE}
        LEFT JOIN {SECURITIES} s ON s.{SECURITY_ID} = sdp.{SECURITY_ID}
        LEFT JOIN {EXCHANGES} e ON e.{EXCHANGE_ID} = s.{EXCHANGE_ID}
        WHERE mc.{IS_TRADING_DAY} = 0
        GROUP BY e.{EXCHANGE_CODE}
        ORDER BY row_count DESC
        """,
    )
    if not rows:
        return None
    return "; ".join(f"{row[0] or 'UNKNOWN'}={int(row[1]):,}" for row in rows)


def _has_required_price_calendar_columns(connection: Any) -> bool:
    if not table_exists(connection, SECURITY_DAILY_PRICES) or not table_exists(connection, MARKET_CALENDAR):
        return False
    price_columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    return TRADE_DATE in price_columns and {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns)


def _missing_metric(category: str, label: str, table_name: str) -> CalendarDiagnosticMetric:
    return CalendarDiagnosticMetric(category, label, None, "MISSING", f"{table_name} table not found")


def _unavailable_metric(category: str, label: str, detail: str) -> CalendarDiagnosticMetric:
    return CalendarDiagnosticMetric(category, label, None, "UNAVAILABLE", detail)


def _scalar_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> CalendarDiagnosticMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    value = int(value)
    return CalendarDiagnosticMetric(category, label, value, "EMPTY" if value == 0 else "OK", detail)


def _text_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> CalendarDiagnosticMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    return CalendarDiagnosticMetric(category, label, str(value), "OK", detail)


def _positive_metric(metric: CalendarDiagnosticMetric | None) -> bool:
    return bool(metric and isinstance(metric.value, int) and metric.value > 0)


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _write_dates_csv(path: Path, rows: tuple[NonTradingDateSummary, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("trade_date", "row_count", "weekday", "day_type", "holiday_name"),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_securities_csv(path: Path, rows: tuple[NonTradingSecuritySummary, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("security_id", "ticker_symbol", "exchange_code", "row_count"),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_report_json(path: Path, result: MarketCalendarDiagnosticReport) -> None:
    payload = asdict(result)
    payload["export_paths"] = [str(path) for path in result.export_paths]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _copy_report_with_export_paths(
    result: MarketCalendarDiagnosticReport,
    export_paths: tuple[Path, ...],
) -> MarketCalendarDiagnosticReport:
    return MarketCalendarDiagnosticReport(
        generated_at=result.generated_at,
        connection_ok=result.connection_ok,
        diagnostic_status=result.diagnostic_status,
        database_name=result.database_name,
        metrics=result.metrics,
        notes=result.notes,
        top_dates=result.top_dates,
        top_securities=result.top_securities,
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
    result: MarketCalendarDiagnosticReport,
    logger: logging.Logger | None,
) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Market calendar diagnostic failed: %s", result.error_message)
        return
    logger.info(
        "Market calendar diagnostic completed: database=%s status=%s metrics=%s notes=%s",
        result.database_name,
        result.diagnostic_status,
        len(result.metrics),
        len(result.notes),
    )
