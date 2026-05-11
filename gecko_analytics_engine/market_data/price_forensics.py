"""Read-only market-data frequency and event-window failure forensics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    CYBER_EVENT_ID,
    INDEX_DAILY_PRICES,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    MARKET_INDEXES,
    MARKET_INDEX_ID,
    SECURITIES,
    SECURITY_DAILY_PRICES,
    SECURITY_ID,
    TICKER_SYMBOL,
    TRADE_DATE,
    VW_EVENT_WINDOW_BOUNDARIES,
    WINDOW_CODE,
    WINDOW_END_DATE,
    WINDOW_START_DATE,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.market_data.index_audit import classify_gap_frequency
from gecko_analytics_engine.market_data.indexes import calculate_density_pct
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class SecurityPriceDensityRow:
    """Security price density and frequency diagnostics."""

    security_id: int
    ticker_symbol: str
    company_name: str | None
    is_event_linked: bool
    row_count: int
    unique_trade_dates: int
    min_trade_date: str | None
    max_trade_date: str | None
    expected_trading_days: int | None
    density_pct: float | None
    likely_frequency: str
    monday_rows: int
    weekday_distribution: str
    non_trading_rows: int
    largest_gap_days: int | None
    largest_gap_start_date: str | None
    largest_gap_end_date: str | None


@dataclass(frozen=True)
class IndexPriceDensityRow:
    """Index price density and frequency diagnostics."""

    market_index_id: int
    symbol: str | None
    name: str | None
    row_count: int
    unique_trade_dates: int
    min_trade_date: str | None
    max_trade_date: str | None
    expected_trading_days: int | None
    density_pct: float | None
    likely_frequency: str
    weekday_distribution: str
    non_trading_rows: int
    no_calendar_match_rows: int
    missing_trading_days: int | None
    largest_gap_days: int | None
    largest_gap_start_date: str | None
    largest_gap_end_date: str | None


@dataclass(frozen=True)
class EventWindowFailureRow:
    """Detailed failed event/security/window row."""

    cyber_event_id: int | None
    event_name: str | None
    security_id: int | None
    ticker_symbol: str | None
    window_code: str | None
    event_date: str | None
    aligned_event_date: str | None
    window_start_date: str | None
    window_end_date: str | None
    expected_trading_days: int | None
    security_price_rows: int
    benchmark_price_rows: int
    failure_reason: str
    missing_security_dates_sample: str
    missing_benchmark_dates_sample: str
    missing_security_offsets_sample: str
    missing_benchmark_offsets_sample: str


@dataclass(frozen=True)
class RepairPriorityRow:
    """Repair priority summary."""

    priority_type: str
    key: str
    affected_rows: int
    affected_events: int
    reason: str
    recommendation: str


@dataclass(frozen=True)
class MarketDataForensicsReport:
    """Read-only market-data forensics report."""

    generated_at: str
    connection_ok: bool
    forensic_status: str
    database_name: str | None = None
    security_rows: tuple[SecurityPriceDensityRow, ...] = ()
    index_rows: tuple[IndexPriceDensityRow, ...] = ()
    failure_rows: tuple[EventWindowFailureRow, ...] = ()
    repair_priorities: tuple[RepairPriorityRow, ...] = ()
    security_weekday_summary: str = ""
    current_study_support: str = "unknown"
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_market_data_forensics(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketDataForensicsReport:
    """Run and export read-only market-data forensics."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            security_rows = _security_density_rows(connection)
            index_rows = _index_density_rows(connection)
            failure_rows = _event_window_failures(connection)
    except DatabaseConnectionError as exc:
        result = MarketDataForensicsReport(
            generated_at=generated_at,
            connection_ok=False,
            forensic_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = MarketDataForensicsReport(
            generated_at=generated_at,
            connection_ok=False,
            forensic_status="BLOCKED",
            error_message=f"Market data forensics failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    priorities = build_repair_priorities(failure_rows)
    result = MarketDataForensicsReport(
        generated_at=generated_at,
        connection_ok=True,
        forensic_status=_forensic_status(security_rows, index_rows, failure_rows),
        database_name=database_name,
        security_rows=security_rows,
        index_rows=index_rows,
        failure_rows=failure_rows,
        repair_priorities=priorities,
        security_weekday_summary=_security_weekday_summary(security_rows),
        current_study_support=_study_support(security_rows, index_rows),
    )
    result = export_market_data_forensics(result, paths, logger)
    _log_report(result, logger)
    return result


def classify_density(density_pct: float | None) -> str:
    """Classify density as daily/weekly/monthly/sparse/unknown."""

    if density_pct is None:
        return "unknown"
    if density_pct >= 80:
        return "daily"
    if 15 <= density_pct <= 35:
        return "weekly"
    if 3 <= density_pct < 15:
        return "monthly_or_sparse"
    return "sparse"


def weekday_distribution_label(counts: dict[str, int]) -> str:
    """Format weekday distribution counts."""

    order = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    return "; ".join(f"{name}={counts.get(name, 0)}" for name in order if counts.get(name, 0))


def detect_largest_gap(dates: tuple[date, ...]) -> tuple[int | None, date | None, date | None]:
    """Return largest gap in days and its endpoints."""

    if len(dates) < 2:
        return None, None, None
    gaps = tuple((right - left).days for left, right in zip(dates, dates[1:]))
    index = gaps.index(max(gaps))
    return gaps[index], dates[index], dates[index + 1]


def build_repair_priorities(
    failure_rows: tuple[EventWindowFailureRow, ...],
) -> tuple[RepairPriorityRow, ...]:
    """Rank repair targets from failed event-window rows."""

    buckets: dict[tuple[str, str], list[EventWindowFailureRow]] = {}
    for row in failure_rows:
        if "missing_security_price" in row.failure_reason and row.ticker_symbol:
            buckets.setdefault(("security", row.ticker_symbol), []).append(row)
        if "missing_benchmark_price" in row.failure_reason:
            buckets.setdefault(("benchmark", "index_daily_prices"), []).append(row)
        if row.window_code:
            buckets.setdefault(("window", row.window_code), []).append(row)

    priorities: list[RepairPriorityRow] = []
    for (priority_type, key), affected_rows in buckets.items():
        events = {row.cyber_event_id for row in affected_rows if row.cyber_event_id is not None}
        if priority_type == "security":
            recommendation = f"Repair/import daily prices for {key}."
            reason = "Security prices missing in failed event windows."
        elif priority_type == "benchmark":
            recommendation = "Repair/import daily benchmark prices in index_daily_prices."
            reason = "Benchmark prices missing in failed event windows."
        else:
            recommendation = f"Prioritize {key} window coverage after market-data repair."
            reason = "Failures are concentrated in this window."
        priorities.append(
            RepairPriorityRow(priority_type, key, len(affected_rows), len(events), reason, recommendation)
        )
    return tuple(sorted(priorities, key=lambda row: (-row.affected_rows, row.priority_type, row.key)))


def export_market_data_forensics(
    result: MarketDataForensicsReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> MarketDataForensicsReport:
    """Export market-data forensics artifacts."""

    if not result.connection_ok:
        return result
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.reports_dir / "market_data_forensics_report.json"
    security_csv = paths.exports_dir / "security_price_density_by_security.csv"
    index_csv = paths.exports_dir / "index_price_density_by_index.csv"
    failures_csv = paths.exports_dir / "event_window_failure_drilldown.csv"
    priorities_csv = paths.exports_dir / "market_data_repair_priorities.csv"
    export_paths = (json_path, security_csv, index_csv, failures_csv, priorities_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    write_dataclass_json(json_path, result_with_exports)
    write_rows_csv(security_csv, result.security_rows, tuple(SecurityPriceDensityRow.__dataclass_fields__.keys()))
    write_rows_csv(index_csv, result.index_rows, tuple(IndexPriceDensityRow.__dataclass_fields__.keys()))
    write_rows_csv(failures_csv, result.failure_rows, tuple(EventWindowFailureRow.__dataclass_fields__.keys()))
    write_rows_csv(priorities_csv, result.repair_priorities, tuple(RepairPriorityRow.__dataclass_fields__.keys()))
    if logger is not None:
        logger.info("Market data forensics exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def format_market_data_forensics(result: MarketDataForensicsReport) -> list[str]:
    """Format market-data forensics for console output."""

    lines = ["", "Market Data Forensics", "---------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    daily_security = sum(1 for row in result.security_rows if row.likely_frequency == "daily")
    weekly_security = sum(1 for row in result.security_rows if row.likely_frequency == "weekly")
    daily_indexes = sum(1 for row in result.index_rows if row.likely_frequency == "daily")
    lines.extend(
        [
            f"Overall status: {result.forensic_status}",
            f"Database: {result.database_name or 'Unknown'}",
            f"Current study support: {result.current_study_support}",
            f"Security price frequency: daily={daily_security}, weekly={weekly_security}, total={len(result.security_rows)}",
            f"Security weekday summary: {result.security_weekday_summary}",
            f"Benchmark/index frequency: daily={daily_indexes}, total={len(result.index_rows)}",
            f"Failed event/window rows: {len(result.failure_rows):,}",
        ]
    )

    lines.extend(["", "Lowest-density linked securities:"])
    linked = [row for row in result.security_rows if row.is_event_linked]
    for row in sorted(linked, key=lambda item: item.density_pct if item.density_pct is not None else -1)[:12]:
        lines.append(
            f"  {row.ticker_symbol}: density={_fmt_float(row.density_pct)}%, rows={row.row_count:,}, "
            f"range={row.min_trade_date or 'Unknown'} to {row.max_trade_date or 'Unknown'}, "
            f"freq={row.likely_frequency}, largest_gap={_fmt_int(row.largest_gap_days)}"
        )

    lines.extend(["", "Benchmark/index density:"])
    for row in result.index_rows:
        lines.append(
            f"  {row.symbol or row.market_index_id}: density={_fmt_float(row.density_pct)}%, rows={row.row_count:,}, "
            f"freq={row.likely_frequency}, missing_days={_fmt_int(row.missing_trading_days)}, "
            f"non_trading={row.non_trading_rows:,}, no_calendar_match={row.no_calendar_match_rows:,}"
        )

    lines.extend(["", "Top failed event/window cases:"])
    for row in result.failure_rows[:15]:
        lines.append(
            f"  event={row.cyber_event_id}, ticker={row.ticker_symbol}, window={row.window_code}, "
            f"reason={row.failure_reason}, security_rows={row.security_price_rows}, benchmark_rows={row.benchmark_price_rows}"
        )

    lines.extend(["", "Top repair priorities:"])
    for row in result.repair_priorities[:12]:
        lines.append(
            f"  [{row.priority_type}] {row.key}: affected_rows={row.affected_rows:,}, "
            f"events={row.affected_events:,} - {row.recommendation}"
        )

    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_market_data_forensics(result: MarketDataForensicsReport) -> None:
    """Print market-data forensics."""

    for line in format_market_data_forensics(result):
        print(line)


def _security_density_rows(connection: Any) -> tuple[SecurityPriceDensityRow, ...]:
    if not table_exists(connection, SECURITIES) or not table_exists(connection, SECURITY_DAILY_PRICES):
        return ()
    linked_ids = {
        int(row[0])
        for row in safe_fetch_all(connection, "SELECT DISTINCT security_id FROM cyber_event_securities")
        if row[0] is not None
    } if table_exists(connection, "cyber_event_securities") else set()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT s.{SECURITY_ID}, s.{TICKER_SYMBOL}, c.display_name,
               COUNT(sdp.price_id), COUNT(DISTINCT sdp.{TRADE_DATE}), MIN(sdp.{TRADE_DATE}), MAX(sdp.{TRADE_DATE})
        FROM {SECURITIES} s
        LEFT JOIN companies c ON c.company_id = s.company_id
        LEFT JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = s.{SECURITY_ID}
        GROUP BY s.{SECURITY_ID}, s.{TICKER_SYMBOL}, c.display_name
        ORDER BY s.{TICKER_SYMBOL}
        """,
    )
    result: list[SecurityPriceDensityRow] = []
    for security_id, ticker, company, row_count, unique_dates, min_date, max_date in rows:
        dates = _dates_for_entity(connection, SECURITY_DAILY_PRICES, SECURITY_ID, int(security_id))
        expected = _expected_trading_days(connection, min_date, max_date)
        density = calculate_density_pct(int(unique_dates or 0), expected)
        largest_gap, gap_start, gap_end = detect_largest_gap(dates)
        weekday_counts = _weekday_counts(dates)
        result.append(
            SecurityPriceDensityRow(
                int(security_id),
                str(ticker),
                str(company) if company else None,
                int(security_id) in linked_ids,
                int(row_count or 0),
                int(unique_dates or 0),
                str(min_date) if min_date else None,
                str(max_date) if max_date else None,
                expected,
                density,
                _frequency_from_density_and_gaps(density, dates),
                weekday_counts.get("Monday", 0),
                weekday_distribution_label(weekday_counts),
                _non_trading_rows(connection, SECURITY_DAILY_PRICES, SECURITY_ID, int(security_id)),
                largest_gap,
                str(gap_start) if gap_start else None,
                str(gap_end) if gap_end else None,
            )
        )
    return tuple(result)


def _index_density_rows(connection: Any) -> tuple[IndexPriceDensityRow, ...]:
    if not table_exists(connection, INDEX_DAILY_PRICES):
        return ()
    metadata = _index_metadata(connection)
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT idp.{MARKET_INDEX_ID},
               COUNT(idp.index_price_id), COUNT(DISTINCT idp.{TRADE_DATE}), MIN(idp.{TRADE_DATE}), MAX(idp.{TRADE_DATE})
        FROM {INDEX_DAILY_PRICES} idp
        GROUP BY idp.{MARKET_INDEX_ID}
        ORDER BY idp.{MARKET_INDEX_ID}
        """,
    )
    result: list[IndexPriceDensityRow] = []
    for index_id, row_count, unique_dates, min_date, max_date in rows:
        symbol, name = metadata.get(int(index_id) if index_id is not None else None, (None, None))
        dates = _dates_for_entity(connection, INDEX_DAILY_PRICES, MARKET_INDEX_ID, int(index_id))
        expected = _expected_trading_days(connection, min_date, max_date)
        density = calculate_density_pct(int(unique_dates or 0), expected)
        largest_gap, gap_start, gap_end = detect_largest_gap(dates)
        non_trading = _non_trading_rows(connection, INDEX_DAILY_PRICES, MARKET_INDEX_ID, int(index_id))
        no_match = _no_calendar_match_rows(connection, INDEX_DAILY_PRICES, MARKET_INDEX_ID, int(index_id))
        result.append(
            IndexPriceDensityRow(
                int(index_id),
                str(symbol) if symbol else None,
                str(name) if name else None,
                int(row_count or 0),
                int(unique_dates or 0),
                str(min_date) if min_date else None,
                str(max_date) if max_date else None,
                expected,
                density,
                _frequency_from_density_and_gaps(density, dates),
                weekday_distribution_label(_weekday_counts(dates)),
                non_trading,
                no_match,
                max((expected or 0) - int(unique_dates or 0), 0) if expected is not None else None,
                largest_gap,
                str(gap_start) if gap_start else None,
                str(gap_end) if gap_end else None,
            )
        )
    return tuple(result)


def _event_window_failures(connection: Any) -> tuple[EventWindowFailureRow, ...]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return ()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT ewb.{CYBER_EVENT_ID}, ce.event_name, ewb.{SECURITY_ID}, s.{TICKER_SYMBOL},
               ewb.{WINDOW_CODE}, ewb.disclosure_date, ewb.first_trading_day,
               ewb.{WINDOW_START_DATE}, ewb.{WINDOW_END_DATE},
               (SELECT COUNT(DISTINCT sdp.{TRADE_DATE}) FROM {SECURITY_DAILY_PRICES} sdp
                 WHERE sdp.{SECURITY_ID}=ewb.{SECURITY_ID}
                   AND sdp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}) AS security_rows,
               (SELECT COUNT(DISTINCT idp.{TRADE_DATE}) FROM {INDEX_DAILY_PRICES} idp
                 WHERE idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}) AS benchmark_rows
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        LEFT JOIN cyber_events ce ON ce.{CYBER_EVENT_ID}=ewb.{CYBER_EVENT_ID}
        LEFT JOIN {SECURITIES} s ON s.{SECURITY_ID}=ewb.{SECURITY_ID}
        HAVING security_rows = 0 OR benchmark_rows = 0
        ORDER BY ewb.{WINDOW_CODE}, ewb.{CYBER_EVENT_ID}, ewb.{SECURITY_ID}
        """,
    )
    result: list[EventWindowFailureRow] = []
    for row in rows:
        event_id, event_name, security_id, ticker, window_code, event_date, aligned_date, start, end, security_rows, benchmark_rows = row
        expected_dates = _expected_trading_dates(connection, start, end)
        missing_security = _missing_dates_in_window(connection, SECURITY_DAILY_PRICES, SECURITY_ID, security_id, expected_dates)
        missing_benchmark = _missing_benchmark_dates_in_window(connection, expected_dates)
        reason = _failure_reason(int(security_rows or 0), int(benchmark_rows or 0))
        result.append(
            EventWindowFailureRow(
                int(event_id) if event_id is not None else None,
                str(event_name) if event_name else None,
                int(security_id) if security_id is not None else None,
                str(ticker) if ticker else None,
                str(window_code) if window_code else None,
                str(event_date) if event_date else None,
                str(aligned_date) if aligned_date else None,
                str(start) if start else None,
                str(end) if end else None,
                len(expected_dates) if expected_dates else None,
                int(security_rows or 0),
                int(benchmark_rows or 0),
                reason,
                _sample_dates(missing_security),
                _sample_dates(missing_benchmark),
                _sample_offsets(missing_security, aligned_date),
                _sample_offsets(missing_benchmark, aligned_date),
            )
        )
    return tuple(result)


def _dates_for_entity(connection: Any, table: str, id_column: str, entity_id: int) -> tuple[date, ...]:
    rows = safe_fetch_all(
        connection,
        f"SELECT DISTINCT {TRADE_DATE} FROM {table} WHERE {id_column}=%s ORDER BY {TRADE_DATE}",
        (entity_id,),
    )
    return tuple(row[0] for row in rows if isinstance(row[0], date))


def _expected_trading_days(connection: Any, min_date: Any, max_date: Any) -> int | None:
    if not min_date or not max_date or not table_exists(connection, MARKET_CALENDAR):
        return None
    value = safe_scalar(
        connection,
        f"SELECT COUNT(*) FROM {MARKET_CALENDAR} WHERE {CALENDAR_DATE} BETWEEN %s AND %s AND {IS_TRADING_DAY}=1",
        (min_date, max_date),
    )
    return int(value) if value is not None else None


def _expected_trading_dates(connection: Any, start: Any, end: Any) -> tuple[date, ...]:
    if not start or not end or not table_exists(connection, MARKET_CALENDAR):
        return ()
    rows = safe_fetch_all(
        connection,
        f"SELECT {CALENDAR_DATE} FROM {MARKET_CALENDAR} WHERE {CALENDAR_DATE} BETWEEN %s AND %s AND {IS_TRADING_DAY}=1 ORDER BY {CALENDAR_DATE}",
        (start, end),
    )
    return tuple(row[0] for row in rows if isinstance(row[0], date))


def _missing_dates_in_window(connection: Any, table: str, id_column: str, entity_id: Any, expected_dates: tuple[date, ...]) -> tuple[date, ...]:
    if entity_id is None or not expected_dates:
        return expected_dates
    rows = safe_fetch_all(
        connection,
        f"SELECT DISTINCT {TRADE_DATE} FROM {table} WHERE {id_column}=%s AND {TRADE_DATE} BETWEEN %s AND %s",
        (entity_id, expected_dates[0], expected_dates[-1]),
    )
    present = {row[0] for row in rows if isinstance(row[0], date)}
    return tuple(value for value in expected_dates if value not in present)


def _missing_benchmark_dates_in_window(connection: Any, expected_dates: tuple[date, ...]) -> tuple[date, ...]:
    if not expected_dates:
        return ()
    rows = safe_fetch_all(
        connection,
        f"SELECT DISTINCT {TRADE_DATE} FROM {INDEX_DAILY_PRICES} WHERE {TRADE_DATE} BETWEEN %s AND %s",
        (expected_dates[0], expected_dates[-1]),
    )
    present = {row[0] for row in rows if isinstance(row[0], date)}
    return tuple(value for value in expected_dates if value not in present)


def _non_trading_rows(connection: Any, table: str, id_column: str, entity_id: int) -> int:
    if not table_exists(connection, MARKET_CALENDAR):
        return 0
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {table} p
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE}=p.{TRADE_DATE}
        WHERE p.{id_column}=%s AND mc.{IS_TRADING_DAY}=0
        """,
        (entity_id,),
    )
    return int(value or 0)


def _no_calendar_match_rows(connection: Any, table: str, id_column: str, entity_id: int) -> int:
    if not table_exists(connection, MARKET_CALENDAR):
        return 0
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {table} p
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE}=p.{TRADE_DATE}
        WHERE p.{id_column}=%s AND mc.{CALENDAR_DATE} IS NULL
        """,
        (entity_id,),
    )
    return int(value or 0)


def _index_metadata(connection: Any) -> dict[int | None, tuple[str | None, str | None]]:
    if not table_exists(connection, MARKET_INDEXES):
        return {}
    columns = set(get_table_columns(connection, MARKET_INDEXES))
    if MARKET_INDEX_ID not in columns:
        return {}
    symbol_column = _first_existing(columns, ("index_symbol", "index_code", "symbol", "ticker_symbol", "ticker"))
    name_column = _first_existing(columns, ("index_name", "name", "market_index_name", "description"))
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {MARKET_INDEX_ID}, {symbol_column if symbol_column else "NULL"}, {name_column if name_column else "NULL"}
        FROM {MARKET_INDEXES}
        ORDER BY {MARKET_INDEX_ID}
        """,
    )
    return {
        int(row[0]) if row[0] is not None else None: (
            str(row[1]) if row[1] else None,
            str(row[2]) if row[2] else None,
        )
        for row in rows
    }


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _weekday_counts(dates: tuple[date, ...]) -> dict[str, int]:
    labels = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    counts: dict[str, int] = {}
    for value in dates:
        label = labels[value.weekday()]
        counts[label] = counts.get(label, 0) + 1
    return counts


def _frequency_from_density_and_gaps(density: float | None, dates: tuple[date, ...]) -> str:
    if len(dates) >= 2:
        gaps = tuple((right - left).days for left, right in zip(dates, dates[1:]))
        gap_class = classify_gap_frequency(gaps)
        if gap_class == "daily_like":
            return "daily"
        if gap_class == "weekly_like":
            return "weekly"
        if gap_class == "monthly_like":
            return "monthly"
    return classify_density(density)


def _failure_reason(security_rows: int, benchmark_rows: int) -> str:
    reasons: list[str] = []
    if security_rows == 0:
        reasons.append("missing_security_price")
    if benchmark_rows == 0:
        reasons.append("missing_benchmark_price")
    return ";".join(reasons)


def _sample_dates(values: tuple[date, ...], limit: int = 10) -> str:
    return "; ".join(str(value) for value in values[:limit])


def _sample_offsets(values: tuple[date, ...], aligned_date: Any, limit: int = 10) -> str:
    if not aligned_date:
        return ""
    anchor = aligned_date if isinstance(aligned_date, date) else None
    if anchor is None:
        return ""
    return "; ".join(f"{value}:{(value - anchor).days:+d}" for value in values[:limit])


def _forensic_status(
    security_rows: tuple[SecurityPriceDensityRow, ...],
    index_rows: tuple[IndexPriceDensityRow, ...],
    failure_rows: tuple[EventWindowFailureRow, ...],
) -> str:
    if not security_rows or not index_rows:
        return "BLOCKED"
    if _study_support(security_rows, index_rows) == "DAILY_STUDY_READY":
        return "READY_FOR_DAILY_STUDY_DESIGN"
    if failure_rows:
        return "NEEDS_MARKET_DATA_REPAIR"
    return "NEEDS_POLICY_REVIEW"


def _study_support(security_rows: tuple[SecurityPriceDensityRow, ...], index_rows: tuple[IndexPriceDensityRow, ...]) -> str:
    linked = [row for row in security_rows if row.is_event_linked and row.row_count > 0]
    linked_daily_share = sum(1 for row in linked if row.likely_frequency == "daily") / len(linked) if linked else 0
    index_daily = any(row.likely_frequency == "daily" and (row.density_pct or 0) >= 80 for row in index_rows)
    if linked_daily_share >= 0.8 and index_daily:
        return "DAILY_STUDY_READY"
    if linked and sum(1 for row in linked if row.likely_frequency in {"weekly", "daily"}) / len(linked) >= 0.8:
        return "WEEKLY_STUDY_ONLY_OR_NEEDS_DAILY_REPAIR"
    return "NEEDS_REPAIR"


def _security_weekday_summary(rows: tuple[SecurityPriceDensityRow, ...]) -> str:
    monday = sum(row.monday_rows for row in rows)
    total = sum(row.unique_trade_dates for row in rows)
    pct = round((monday / total) * 100, 2) if total else 0
    return f"Monday={monday:,} of {total:,} unique security-date observations ({pct}%)"


def _fmt_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _fmt_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: MarketDataForensicsReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Market data forensics failed: %s", result.error_message)
        return
    logger.info(
        "Market data forensics completed: database=%s status=%s failures=%s priorities=%s",
        result.database_name,
        result.forensic_status,
        len(result.failure_rows),
        len(result.repair_priorities),
    )
