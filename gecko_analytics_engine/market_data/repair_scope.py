"""Read-only daily market-data repair scope diagnostics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    CYBER_EVENT_SECURITIES,
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
from gecko_analytics_engine.market_data.indexes import MINIMUM_DENSITY_PCT, calculate_density_pct
from gecko_analytics_engine.market_data.price_forensics import classify_density
from gecko_analytics_engine.utils.paths import AppPaths


REQUIRED_PRICE_COLUMNS = "trade_date, open, high, low, close, adjusted_close, volume"
DATE_RANGE_UNAVAILABLE = "Unavailable"


@dataclass(frozen=True)
class BenchmarkImportTarget:
    """Daily index/benchmark import target."""

    market_index_id: int | None
    index_code: str | None
    index_name: str | None
    canonical_table: str
    current_row_count: int
    current_min_trade_date: str | None
    current_max_trade_date: str | None
    current_density_pct: float | None
    required_start_date: str | None
    required_end_date: str | None
    required_trading_days: int | None
    rows_in_required_range: int
    missing_required_trading_days: int | None
    affected_candidate_rows: int
    affected_d1_d3_rows: int
    import_priority: str
    required_columns: str = REQUIRED_PRICE_COLUMNS


@dataclass(frozen=True)
class SecurityImportTarget:
    """Daily linked-security import target."""

    security_id: int
    ticker_symbol: str
    company_name: str | None
    current_row_count: int
    current_min_trade_date: str | None
    current_max_trade_date: str | None
    current_density_pct: float | None
    likely_frequency: str
    completely_missing_prices: bool
    required_start_date: str | None
    required_end_date: str | None
    required_trading_days: int | None
    rows_in_required_range: int
    missing_required_trading_days: int | None
    affected_candidate_rows: int
    affected_event_windows: int
    import_priority: str
    required_columns: str = REQUIRED_PRICE_COLUMNS


@dataclass(frozen=True)
class DailyDataRepairPriority:
    """One ranked repair priority."""

    repair_target_type: str
    identifier: str
    label: str
    reason: str
    priority: str
    affected_rows: int
    affected_windows: int
    current_density_pct: float | None
    target_date_range: str
    required_columns: str


@dataclass(frozen=True)
class DailyDataRepairScopeReport:
    """Read-only daily market-data repair scope report."""

    generated_at: str
    connection_ok: bool
    repair_status: str
    database_name: str | None = None
    recommended_canonical_benchmark_table: str = INDEX_DAILY_PRICES
    event_window_required_range: str = DATE_RANGE_UNAVAILABLE
    estimation_window_note: str = "Estimation-window lookback is not finalized; extend imports once the model window is locked."
    policy_notes: tuple[str, ...] = ()
    benchmark_targets: tuple[BenchmarkImportTarget, ...] = ()
    security_targets: tuple[SecurityImportTarget, ...] = ()
    repair_priorities: tuple[DailyDataRepairPriority, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_daily_data_repair_scope(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DailyDataRepairScopeReport:
    """Build and export the read-only daily market-data repair scope."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            required_start, required_end = _event_window_required_range(connection)
            benchmark_targets = _benchmark_targets(connection, required_start, required_end)
            security_targets = _security_targets(connection, required_start, required_end)
    except DatabaseConnectionError as exc:
        result = DailyDataRepairScopeReport(
            generated_at=generated_at,
            connection_ok=False,
            repair_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = DailyDataRepairScopeReport(
            generated_at=generated_at,
            connection_ok=False,
            repair_status="BLOCKED",
            error_message=f"Daily data repair scope failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    result = DailyDataRepairScopeReport(
        generated_at=generated_at,
        connection_ok=True,
        repair_status=determine_repair_status(benchmark_targets, security_targets),
        database_name=database_name,
        event_window_required_range=format_required_range(required_start, required_end),
        policy_notes=daily_study_policy_notes(),
        benchmark_targets=benchmark_targets,
        security_targets=security_targets,
        repair_priorities=build_daily_repair_priorities(benchmark_targets, security_targets),
    )
    result = export_daily_data_repair_scope(result, paths, logger)
    _log_report(result, logger)
    return result


def classify_benchmark_priority(index_code: str | None, affected_d1_d3_rows: int) -> str:
    """Classify benchmark repair/import priority."""

    normalized = (index_code or "").upper()
    if normalized == "DJIA" or affected_d1_d3_rows > 0:
        return "high"
    if normalized in {"SP500", "NASDAQ_COMP"}:
        return "medium"
    return "low"


def classify_security_priority(
    affected_candidate_rows: int,
    completely_missing_prices: bool,
    likely_frequency: str,
) -> str:
    """Classify linked-security repair/import priority."""

    if completely_missing_prices or affected_candidate_rows > 0:
        return "high"
    if likely_frequency != "daily":
        return "medium"
    return "low"


def format_required_range(start: Any, end: Any) -> str:
    """Format a target import date range."""

    if not start or not end:
        return DATE_RANGE_UNAVAILABLE
    return f"{start} to {end}"


def daily_study_policy_notes() -> tuple[str, ...]:
    """Return the daily-study market-data policy."""

    return (
        "Daily event-study AR/CAR requires trading-day OHLCV rows for securities and benchmarks.",
        "Event windows should be evaluated on trading days only.",
        "Holiday and other non-trading rows should be excluded from calculations unless a documented source-specific exception applies.",
        "Existing weekly-like rows are not sufficient for research-grade daily AR/CAR.",
        f"Target coverage should be at least {MINIMUM_DENSITY_PCT:.0f}%, preferably close to full trading-day coverage for event and estimation windows.",
        "index_daily_prices is the recommended canonical benchmark table.",
        "dji_daily_prices is empty and should not be used as canonical unless a future migration explicitly populates or bridges it.",
    )


def determine_repair_status(
    benchmark_targets: tuple[BenchmarkImportTarget, ...],
    security_targets: tuple[SecurityImportTarget, ...],
) -> str:
    """Determine overall repair status."""

    if not benchmark_targets or not security_targets:
        return "BLOCKED"
    high = any(target.import_priority == "high" for target in benchmark_targets) or any(
        target.import_priority == "high" for target in security_targets
    )
    medium = any(target.import_priority == "medium" for target in benchmark_targets) or any(
        target.import_priority == "medium" for target in security_targets
    )
    if high:
        return "DAILY_IMPORT_SCOPE_REQUIRED"
    if medium:
        return "DAILY_DENSITY_REPAIR_RECOMMENDED"
    return "READY_FOR_POLICY_REVIEW"


def build_daily_repair_priorities(
    benchmark_targets: tuple[BenchmarkImportTarget, ...],
    security_targets: tuple[SecurityImportTarget, ...],
) -> tuple[DailyDataRepairPriority, ...]:
    """Rank benchmark and linked-security repair targets."""

    priorities: list[DailyDataRepairPriority] = []
    for target in benchmark_targets:
        reason = (
            "Primary benchmark candidate or D1/D3 gap recovery target."
            if target.import_priority == "high"
            else "Robustness benchmark or secondary coverage target."
        )
        priorities.append(
            DailyDataRepairPriority(
                "benchmark",
                str(target.market_index_id) if target.market_index_id is not None else "unknown",
                target.index_code or target.index_name or "Unknown index",
                reason,
                target.import_priority,
                target.affected_candidate_rows,
                target.affected_d1_d3_rows,
                target.current_density_pct,
                format_required_range(target.required_start_date, target.required_end_date),
                target.required_columns,
            )
        )

    for target in security_targets:
        if target.import_priority == "low":
            continue
        if target.completely_missing_prices:
            reason = "Linked security has no current price rows."
        elif target.affected_candidate_rows > 0:
            reason = "Linked security causes excluded event-window rows."
        else:
            reason = "Linked security is event-relevant but current data is not daily-like."
        priorities.append(
            DailyDataRepairPriority(
                "security",
                str(target.security_id),
                target.ticker_symbol,
                reason,
                target.import_priority,
                target.affected_candidate_rows,
                target.affected_event_windows,
                target.current_density_pct,
                format_required_range(target.required_start_date, target.required_end_date),
                target.required_columns,
            )
        )

    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return tuple(
        sorted(
            priorities,
            key=lambda row: (
                priority_rank.get(row.priority, 9),
                -row.affected_rows,
                row.repair_target_type,
                row.label,
            ),
        )
    )


def export_daily_data_repair_scope(
    result: DailyDataRepairScopeReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DailyDataRepairScopeReport:
    """Export daily data repair scope artifacts."""

    if not result.connection_ok:
        return result

    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    report_json = paths.reports_dir / "daily_market_data_repair_scope.json"
    benchmark_csv = paths.exports_dir / "benchmark_import_targets.csv"
    security_csv = paths.exports_dir / "security_import_targets.csv"
    priorities_csv = paths.exports_dir / "daily_data_repair_priorities.csv"
    export_paths = (report_json, benchmark_csv, security_csv, priorities_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    write_dataclass_json(report_json, result_with_exports)
    write_rows_csv(benchmark_csv, result.benchmark_targets, tuple(BenchmarkImportTarget.__dataclass_fields__.keys()))
    write_rows_csv(security_csv, result.security_targets, tuple(SecurityImportTarget.__dataclass_fields__.keys()))
    write_rows_csv(priorities_csv, result.repair_priorities, tuple(DailyDataRepairPriority.__dataclass_fields__.keys()))
    if logger is not None:
        logger.info("Daily data repair scope exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def format_daily_data_repair_scope(result: DailyDataRepairScopeReport) -> list[str]:
    """Format daily market-data repair scope for console output."""

    lines = ["", "Daily Market Data Repair Scope", "------------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            f"Overall repair status: {result.repair_status}",
            f"Database: {result.database_name or 'Unknown'}",
            f"Required event-window range: {result.event_window_required_range}",
            f"Canonical benchmark table: {result.recommended_canonical_benchmark_table}",
            f"Estimation-window note: {result.estimation_window_note}",
            "",
            "Benchmark import targets:",
        ]
    )
    for target in result.benchmark_targets:
        lines.append(
            "  "
            f"{target.index_code or target.index_name or target.market_index_id}: priority={target.import_priority}, "
            f"rows={target.current_row_count:,}, density={_fmt_float(target.current_density_pct)}%, "
            f"missing_required_days={_fmt_int(target.missing_required_trading_days)}, "
            f"affected_rows={target.affected_candidate_rows:,}, range={format_required_range(target.required_start_date, target.required_end_date)}"
        )

    lines.extend(["", "Top linked-security import targets:"])
    actionable = [target for target in result.security_targets if target.import_priority in {"high", "medium"}]
    for target in sorted(actionable, key=lambda row: (0 if row.import_priority == "high" else 1, -row.affected_candidate_rows, row.ticker_symbol))[:20]:
        lines.append(
            "  "
            f"{target.ticker_symbol}: priority={target.import_priority}, freq={target.likely_frequency}, "
            f"rows={target.current_row_count:,}, density={_fmt_float(target.current_density_pct)}%, "
            f"affected_rows={target.affected_candidate_rows:,}, range={format_required_range(target.required_start_date, target.required_end_date)}"
        )

    lines.extend(["", "Repair priority summary:"])
    for row in result.repair_priorities[:20]:
        lines.append(
            "  "
            f"[{row.priority}] {row.repair_target_type} {row.label}: affected_rows={row.affected_rows:,}, "
            f"windows={row.affected_windows:,}, density={_fmt_float(row.current_density_pct)}%, "
            f"range={row.target_date_range}"
        )

    lines.extend(["", "Policy warnings:"])
    lines.extend(f"  {note}" for note in result.policy_notes)
    lines.append("  AR/CAR remains blocked until daily security and benchmark coverage is repaired or a reduced study design is explicitly selected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_daily_data_repair_scope(result: DailyDataRepairScopeReport) -> None:
    """Print daily data repair scope."""

    for line in format_daily_data_repair_scope(result):
        print(line)


def _benchmark_targets(connection: Any, required_start: Any, required_end: Any) -> tuple[BenchmarkImportTarget, ...]:
    if not table_exists(connection, MARKET_INDEXES):
        return ()
    metadata = _index_metadata(connection)
    result: list[BenchmarkImportTarget] = []
    for index_id, index_code, index_name in metadata:
        current_row_count, current_min, current_max, unique_dates = _index_current_summary(connection, index_id)
        expected_current = _expected_trading_days(connection, current_min, current_max)
        rows_required = _index_rows_in_range(connection, index_id, required_start, required_end)
        required_days = _expected_trading_days(connection, required_start, required_end)
        affected_rows = _benchmark_affected_rows(connection, index_id)
        affected_d1_d3 = _benchmark_affected_rows(connection, index_id, ("D1", "D3"))
        result.append(
            BenchmarkImportTarget(
                index_id,
                index_code,
                index_name,
                INDEX_DAILY_PRICES,
                current_row_count,
                str(current_min) if current_min else None,
                str(current_max) if current_max else None,
                calculate_density_pct(unique_dates, expected_current),
                str(required_start) if required_start else None,
                str(required_end) if required_end else None,
                required_days,
                rows_required,
                max((required_days or 0) - rows_required, 0) if required_days is not None else None,
                affected_rows,
                affected_d1_d3,
                classify_benchmark_priority(index_code, affected_d1_d3),
            )
        )
    return tuple(result)


def _security_targets(connection: Any, required_start: Any, required_end: Any) -> tuple[SecurityImportTarget, ...]:
    if not table_exists(connection, CYBER_EVENT_SECURITIES) or not table_exists(connection, SECURITIES):
        return ()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT s.{SECURITY_ID}, s.{TICKER_SYMBOL}, c.display_name,
               COUNT(sdp.price_id), COUNT(DISTINCT sdp.{TRADE_DATE}), MIN(sdp.{TRADE_DATE}), MAX(sdp.{TRADE_DATE})
        FROM {CYBER_EVENT_SECURITIES} ces
        INNER JOIN {SECURITIES} s ON s.{SECURITY_ID} = ces.{SECURITY_ID}
        LEFT JOIN companies c ON c.company_id = s.company_id
        LEFT JOIN {SECURITY_DAILY_PRICES} sdp ON sdp.{SECURITY_ID} = s.{SECURITY_ID}
        GROUP BY s.{SECURITY_ID}, s.{TICKER_SYMBOL}, c.display_name
        ORDER BY s.{TICKER_SYMBOL}
        """,
    )
    result: list[SecurityImportTarget] = []
    for security_id, ticker, company, row_count, unique_dates, current_min, current_max in rows:
        parsed_id = int(security_id)
        security_start, security_end = _security_required_range(connection, parsed_id, required_start, required_end)
        expected_current = _expected_trading_days(connection, current_min, current_max)
        density = calculate_density_pct(int(unique_dates or 0), expected_current)
        required_days = _expected_trading_days(connection, security_start, security_end)
        rows_required = _security_rows_in_range(connection, parsed_id, security_start, security_end)
        affected_rows, affected_windows = _security_affected_counts(connection, parsed_id)
        frequency = classify_density(density)
        completely_missing = int(row_count or 0) == 0
        result.append(
            SecurityImportTarget(
                parsed_id,
                str(ticker),
                str(company) if company else None,
                int(row_count or 0),
                str(current_min) if current_min else None,
                str(current_max) if current_max else None,
                density,
                frequency,
                completely_missing,
                str(security_start) if security_start else None,
                str(security_end) if security_end else None,
                required_days,
                rows_required,
                max((required_days or 0) - rows_required, 0) if required_days is not None else None,
                affected_rows,
                affected_windows,
                classify_security_priority(affected_rows, completely_missing, frequency),
            )
        )
    return tuple(result)


def _event_window_required_range(connection: Any) -> tuple[Any, Any]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None, None
    columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    if not {WINDOW_START_DATE, WINDOW_END_DATE}.issubset(columns):
        return None, None
    rows = safe_fetch_all(
        connection,
        f"SELECT MIN({WINDOW_START_DATE}), MAX({WINDOW_END_DATE}) FROM {VW_EVENT_WINDOW_BOUNDARIES}",
    )
    if not rows:
        return None, None
    return rows[0][0], rows[0][1]


def _security_required_range(connection: Any, security_id: int, fallback_start: Any, fallback_end: Any) -> tuple[Any, Any]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return fallback_start, fallback_end
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT MIN({WINDOW_START_DATE}), MAX({WINDOW_END_DATE})
        FROM {VW_EVENT_WINDOW_BOUNDARIES}
        WHERE {SECURITY_ID} = %s
        """,
        (security_id,),
    )
    if not rows or not rows[0][0] or not rows[0][1]:
        return fallback_start, fallback_end
    return rows[0][0], rows[0][1]


def _index_metadata(connection: Any) -> tuple[tuple[int | None, str | None, str | None], ...]:
    columns = set(get_table_columns(connection, MARKET_INDEXES))
    if MARKET_INDEX_ID not in columns:
        return ()
    code_column = _first_existing(columns, ("index_code", "index_symbol", "symbol", "ticker_symbol", "ticker"))
    name_column = _first_existing(columns, ("index_name", "name", "market_index_name", "description"))
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {MARKET_INDEX_ID}, {code_column if code_column else "NULL"}, {name_column if name_column else "NULL"}
        FROM {MARKET_INDEXES}
        ORDER BY {MARKET_INDEX_ID}
        """,
    )
    return tuple(
        (
            int(row[0]) if row[0] is not None else None,
            str(row[1]) if row[1] else None,
            str(row[2]) if row[2] else None,
        )
        for row in rows
    )


def _index_current_summary(connection: Any, index_id: int | None) -> tuple[int, Any, Any, int]:
    if index_id is None or not table_exists(connection, INDEX_DAILY_PRICES):
        return 0, None, None, 0
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT COUNT(*), MIN({TRADE_DATE}), MAX({TRADE_DATE}), COUNT(DISTINCT {TRADE_DATE})
        FROM {INDEX_DAILY_PRICES}
        WHERE {MARKET_INDEX_ID} = %s
        """,
        (index_id,),
    )
    if not rows:
        return 0, None, None, 0
    row = rows[0]
    return int(row[0] or 0), row[1], row[2], int(row[3] or 0)


def _index_rows_in_range(connection: Any, index_id: int | None, start: Any, end: Any) -> int:
    if index_id is None or not start or not end:
        return 0
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT {TRADE_DATE})
        FROM {INDEX_DAILY_PRICES}
        WHERE {MARKET_INDEX_ID} = %s
          AND {TRADE_DATE} BETWEEN %s AND %s
        """,
        (index_id, start, end),
    )
    return int(value or 0)


def _security_rows_in_range(connection: Any, security_id: int, start: Any, end: Any) -> int:
    if not start or not end:
        return 0
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT {TRADE_DATE})
        FROM {SECURITY_DAILY_PRICES}
        WHERE {SECURITY_ID} = %s
          AND {TRADE_DATE} BETWEEN %s AND %s
        """,
        (security_id, start, end),
    )
    return int(value or 0)


def _benchmark_affected_rows(connection: Any, index_id: int | None, windows: tuple[str, ...] | None = None) -> int:
    if index_id is None or not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return 0
    params: tuple[Any, ...] = (index_id,)
    window_filter = ""
    if windows:
        placeholders = ", ".join(["%s"] * len(windows))
        window_filter = f"AND ewb.{WINDOW_CODE} IN ({placeholders})"
        params = (index_id, *windows)
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE NOT EXISTS (
            SELECT 1 FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        {window_filter}
        """,
        params,
    )
    return int(value or 0)


def _security_affected_counts(connection: Any, security_id: int) -> tuple[int, int]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return 0, 0
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT {WINDOW_CODE})
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE ewb.{SECURITY_ID} = %s
          AND NOT EXISTS (
              SELECT 1 FROM {SECURITY_DAILY_PRICES} sdp
              WHERE sdp.{SECURITY_ID} = ewb.{SECURITY_ID}
                AND sdp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
          )
        """,
        (security_id,),
    )
    if not rows:
        return 0, 0
    return int(rows[0][0] or 0), int(rows[0][1] or 0)


def _expected_trading_days(connection: Any, start: Any, end: Any) -> int | None:
    if not start or not end or not table_exists(connection, MARKET_CALENDAR):
        return None
    columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(columns):
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {MARKET_CALENDAR}
        WHERE {CALENDAR_DATE} BETWEEN %s AND %s
          AND {IS_TRADING_DAY} = 1
        """,
        (start, end),
    )
    return int(value) if value is not None else None


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _fmt_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _fmt_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _log_report(result: DailyDataRepairScopeReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Daily data repair scope failed: %s", result.error_message)
        return
    logger.info(
        "Daily data repair scope completed: database=%s status=%s benchmarks=%s securities=%s priorities=%s",
        result.database_name,
        result.repair_status,
        len(result.benchmark_targets),
        len(result.security_targets),
        len(result.repair_priorities),
    )
