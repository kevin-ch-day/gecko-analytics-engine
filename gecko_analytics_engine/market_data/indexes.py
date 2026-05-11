"""Read-only benchmark/index coverage diagnostics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import count_rows, get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    DJI_DAILY_PRICES,
    INDEX_DAILY_PRICES,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    MARKET_INDEXES,
    MARKET_INDEX_ID,
    TRADE_DATE,
    VW_EVENT_WINDOW_BOUNDARIES,
    WINDOW_CODE,
    WINDOW_END_DATE,
    WINDOW_START_DATE,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class BenchmarkCoverageRow:
    """Coverage details for one market index."""

    market_index_id: int | None
    symbol: str | None
    name: str | None
    price_rows: int
    first_trade_date: str | None
    last_trade_date: str | None
    event_window_overlap_rows: int | None
    expected_trading_days: int | None = None
    density_pct: float | None = None
    missing_trading_days: int | None = None
    non_trading_rows: int | None = None
    no_calendar_match_rows: int | None = None
    d1_overlap_rows: int | None = None
    exclusion_rows: int | None = None


@dataclass(frozen=True)
class BenchmarkCoverageReport:
    """Read-only benchmark coverage report."""

    generated_at: str
    connection_ok: bool
    coverage_status: str
    database_name: str | None = None
    index_rows: tuple[BenchmarkCoverageRow, ...] = ()
    recommended_benchmark_id: int | None = None
    recommended_benchmark_label: str | None = None
    dji_daily_price_rows: int | None = None
    dji_table_empty_matters: str = "Unknown"
    notes: tuple[str, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True)
class BenchmarkRecommendationRow:
    """One benchmark recommendation output row."""

    role: str
    market_index_id: int | None
    benchmark_label: str
    recommendation: str
    warning: str


@dataclass(frozen=True)
class BenchmarkSelectionDiagnostic:
    """Density-aware benchmark selection diagnostic."""

    generated_at: str
    connection_ok: bool
    diagnostic_status: str
    database_name: str | None = None
    density_rows: tuple[BenchmarkCoverageRow, ...] = ()
    recommendation_rows: tuple[BenchmarkRecommendationRow, ...] = ()
    recommended_primary_benchmark: str | None = None
    recommended_robustness_benchmarks: tuple[str, ...] = ()
    benchmark_policy_warning: str = ""
    dji_daily_price_rows: int | None = None
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


MINIMUM_DENSITY_PCT = 80.0


def run_benchmark_coverage_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkCoverageReport:
    """Run and export benchmark coverage detail."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            result = build_benchmark_coverage_report(connection, generated_at, database_name)
    except DatabaseConnectionError as exc:
        result = BenchmarkCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            coverage_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = BenchmarkCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            coverage_status="BLOCKED",
            error_message=f"Benchmark coverage failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    result = export_benchmark_coverage_report(result, paths, logger)
    _log_report(result, logger)
    return result


def run_benchmark_selection_diagnostic(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkSelectionDiagnostic:
    """Run and export a density-aware benchmark selection diagnostic."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            coverage = build_benchmark_coverage_report(connection, generated_at, database_name)
            result = build_benchmark_selection_diagnostic(coverage)
    except DatabaseConnectionError as exc:
        result = BenchmarkSelectionDiagnostic(
            generated_at=generated_at,
            connection_ok=False,
            diagnostic_status="BLOCKED",
            error_message=str(exc),
        )
        _log_selection_report(result, logger)
        return result
    except Exception as exc:
        result = BenchmarkSelectionDiagnostic(
            generated_at=generated_at,
            connection_ok=False,
            diagnostic_status="BLOCKED",
            error_message=f"Benchmark selection diagnostic failed: {exc.__class__.__name__}: {exc}",
        )
        _log_selection_report(result, logger)
        return result

    result = export_benchmark_selection_diagnostic(result, paths, logger)
    _log_selection_report(result, logger)
    return result


def build_benchmark_selection_diagnostic(
    coverage: BenchmarkCoverageReport,
) -> BenchmarkSelectionDiagnostic:
    """Build density-aware benchmark recommendation from coverage rows."""

    primary = select_density_aware_benchmark(coverage.index_rows)
    robustness = tuple(
        _benchmark_label(row)
        for row in sorted(
            (row for row in coverage.index_rows if row != primary and row.price_rows > 0),
            key=_density_score,
            reverse=True,
        )[:2]
    )
    warning = _benchmark_policy_warning(primary, coverage.index_rows)
    recommendation_rows = _recommendation_rows(primary, robustness, coverage.index_rows, warning)
    return BenchmarkSelectionDiagnostic(
        generated_at=coverage.generated_at,
        connection_ok=coverage.connection_ok,
        diagnostic_status="NEEDS_REVIEW" if warning else coverage.coverage_status,
        database_name=coverage.database_name,
        density_rows=coverage.index_rows,
        recommendation_rows=recommendation_rows,
        recommended_primary_benchmark=_benchmark_label(primary) if primary else None,
        recommended_robustness_benchmarks=robustness,
        benchmark_policy_warning=warning,
        dji_daily_price_rows=coverage.dji_daily_price_rows,
        error_message=coverage.error_message,
    )


def export_benchmark_selection_diagnostic(
    result: BenchmarkSelectionDiagnostic,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkSelectionDiagnostic:
    """Export benchmark selection diagnostic artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.reports_dir / "benchmark_selection_diagnostic.json"
    density_csv = paths.exports_dir / "benchmark_density_detail.csv"
    recommendation_csv = paths.exports_dir / "benchmark_recommendation.csv"
    export_paths = (json_path, density_csv, recommendation_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    write_rows_csv(density_csv, result.density_rows, _benchmark_density_fieldnames())
    write_rows_csv(
        recommendation_csv,
        result.recommendation_rows,
        ("role", "market_index_id", "benchmark_label", "recommendation", "warning"),
    )
    write_dataclass_json(json_path, result_with_exports)
    if logger is not None:
        logger.info("Benchmark selection diagnostic exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def build_benchmark_coverage_report(
    connection: Any,
    generated_at: str | None = None,
    database_name: str | None = None,
) -> BenchmarkCoverageReport:
    """Build benchmark coverage from an existing connection."""

    generated_at = generated_at or datetime.now(UTC).isoformat()
    if not table_exists(connection, INDEX_DAILY_PRICES):
        return BenchmarkCoverageReport(
            generated_at=generated_at,
            connection_ok=True,
            coverage_status="BLOCKED",
            database_name=database_name,
            notes=(f"{INDEX_DAILY_PRICES} table is unavailable.",),
        )

    index_rows = _fetch_benchmark_rows(connection)
    recommended = select_recommended_benchmark(index_rows)
    dji_rows = count_rows(connection, DJI_DAILY_PRICES)
    notes = _benchmark_notes(index_rows, recommended, dji_rows)
    return BenchmarkCoverageReport(
        generated_at=generated_at,
        connection_ok=True,
        coverage_status="OK" if recommended is not None else "BLOCKED",
        database_name=database_name,
        index_rows=index_rows,
        recommended_benchmark_id=recommended.market_index_id if recommended else None,
        recommended_benchmark_label=_benchmark_label(recommended) if recommended else None,
        dji_daily_price_rows=dji_rows,
        dji_table_empty_matters=_dji_empty_interpretation(dji_rows, index_rows),
        notes=notes,
    )


def export_benchmark_coverage_report(
    result: BenchmarkCoverageReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkCoverageReport:
    """Export benchmark coverage detail."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    detail_csv = paths.exports_dir / "benchmark_coverage_detail.csv"
    export_paths = (detail_csv,)
    result_with_exports = replace(result, export_paths=export_paths)

    write_rows_csv(
        detail_csv,
        result.index_rows,
        (
            "market_index_id",
            "symbol",
            "name",
            "price_rows",
            "first_trade_date",
            "last_trade_date",
            "event_window_overlap_rows",
            "expected_trading_days",
            "density_pct",
            "missing_trading_days",
            "non_trading_rows",
            "no_calendar_match_rows",
            "d1_overlap_rows",
            "exclusion_rows",
        ),
    )
    if logger is not None:
        logger.info("Benchmark coverage exports written: %s", detail_csv)
    return result_with_exports


def select_recommended_benchmark(
    rows: tuple[BenchmarkCoverageRow, ...],
) -> BenchmarkCoverageRow | None:
    """Select the benchmark with the strongest density-aware usable coverage."""

    return select_density_aware_benchmark(rows)


def select_density_aware_benchmark(
    rows: tuple[BenchmarkCoverageRow, ...],
    minimum_density_pct: float = MINIMUM_DENSITY_PCT,
) -> BenchmarkCoverageRow | None:
    """Select a benchmark using overlap, D1 coverage, density, range, and row quality."""

    usable = [row for row in rows if row.price_rows > 0]
    if not usable:
        return None
    dense = [row for row in usable if (row.density_pct or 0.0) >= minimum_density_pct]
    candidates = dense or usable
    return sorted(candidates, key=_density_score, reverse=True)[0]


def format_benchmark_coverage_report(result: BenchmarkCoverageReport) -> list[str]:
    """Format benchmark coverage detail for console output."""

    lines = ["", "Benchmark Coverage Detail", "-------------------------"]
    if not result.connection_ok:
        lines.extend(["Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            f"Overall status: {result.coverage_status}",
            f"Recommended benchmark: {result.recommended_benchmark_label or 'Unavailable'}",
            f"dji_daily_prices rows: {_format_optional_int(result.dji_daily_price_rows)}",
            f"DJIA-specific table interpretation: {result.dji_table_empty_matters}",
            "",
            "Indexes:",
        ]
    )
    for row in result.index_rows:
        lines.append(
            "  "
            f"{_benchmark_label(row)}: rows={row.price_rows:,}, "
            f"range={row.first_trade_date or 'Unknown'} to {row.last_trade_date or 'Unknown'}, "
            f"event-window overlaps={_format_optional_int(row.event_window_overlap_rows)}"
        )
    if result.notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"  {note}" for note in result.notes)
    if result.export_paths:
        lines.extend(["", "Benchmark exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_benchmark_coverage_report(result: BenchmarkCoverageReport) -> None:
    """Print benchmark coverage detail."""

    for line in format_benchmark_coverage_report(result):
        print(line)


def format_benchmark_selection_diagnostic(result: BenchmarkSelectionDiagnostic) -> list[str]:
    """Format density-aware benchmark selection diagnostic."""

    lines = ["", "Benchmark Selection Diagnostic", "------------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines
    lines.extend(
        [
            f"Overall status: {result.diagnostic_status}",
            f"Recommended primary benchmark: {result.recommended_primary_benchmark or 'Unavailable'}",
            "Recommended robustness benchmarks: "
            + (", ".join(result.recommended_robustness_benchmarks) if result.recommended_robustness_benchmarks else "Unavailable"),
            f"dji_daily_prices rows: {_format_optional_int(result.dji_daily_price_rows)}",
        ]
    )
    if result.benchmark_policy_warning:
        lines.append(f"Benchmark policy warning: {result.benchmark_policy_warning}")

    lines.extend(["", "Benchmark density:"])
    for row in result.density_rows:
        lines.append(
            "  "
            f"{_benchmark_label(row)}: density={_format_optional_float(row.density_pct)}%, "
            f"rows={row.price_rows:,}, expected_days={_format_optional_int(row.expected_trading_days)}, "
            f"missing_days={_format_optional_int(row.missing_trading_days)}, "
            f"range={row.first_trade_date or 'Unknown'} to {row.last_trade_date or 'Unknown'}, "
            f"overlap={_format_optional_int(row.event_window_overlap_rows)}, "
            f"D1={_format_optional_int(row.d1_overlap_rows)}, "
            f"non_trading={_format_optional_int(row.non_trading_rows)}, "
            f"no_calendar_match={_format_optional_int(row.no_calendar_match_rows)}"
        )

    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_benchmark_selection_diagnostic(result: BenchmarkSelectionDiagnostic) -> None:
    """Print density-aware benchmark selection diagnostic."""

    for line in format_benchmark_selection_diagnostic(result):
        print(line)


def _fetch_benchmark_rows(connection: Any) -> tuple[BenchmarkCoverageRow, ...]:
    index_price_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    if MARKET_INDEX_ID not in index_price_columns or TRADE_DATE not in index_price_columns:
        total_rows = count_rows(connection, INDEX_DAILY_PRICES) or 0
        date_range = safe_fetch_all(
            connection,
            f"SELECT MIN({TRADE_DATE}), MAX({TRADE_DATE}) FROM {INDEX_DAILY_PRICES}",
        ) if TRADE_DATE in index_price_columns else ()
        first_date = str(date_range[0][0]) if date_range and date_range[0][0] else None
        last_date = str(date_range[0][1]) if date_range and date_range[0][1] else None
        return (BenchmarkCoverageRow(None, None, "All index prices", total_rows, first_date, last_date, None),)

    metadata = _fetch_index_metadata(connection)
    aggregate_rows = safe_fetch_all(
        connection,
        f"""
        SELECT {MARKET_INDEX_ID}, COUNT(*), MIN({TRADE_DATE}), MAX({TRADE_DATE})
        FROM {INDEX_DAILY_PRICES}
        GROUP BY {MARKET_INDEX_ID}
        ORDER BY {MARKET_INDEX_ID}
        """,
    )
    result: list[BenchmarkCoverageRow] = []
    seen_ids: set[int] = set()
    for index_id, price_rows, first_date, last_date in aggregate_rows:
        parsed_id = int(index_id) if index_id is not None else None
        if parsed_id is not None:
            seen_ids.add(parsed_id)
        symbol, name = metadata.get(parsed_id, (None, None))
        parsed_price_rows = int(price_rows or 0)
        expected_trading_days = _expected_trading_days(connection, first_date, last_date)
        result.append(
            BenchmarkCoverageRow(
                parsed_id,
                symbol,
                name,
                parsed_price_rows,
                str(first_date) if first_date else None,
                str(last_date) if last_date else None,
                _event_window_overlap_count(connection, parsed_id),
                expected_trading_days,
                calculate_density_pct(parsed_price_rows, expected_trading_days),
                _missing_trading_days(parsed_price_rows, expected_trading_days),
                _non_trading_rows(connection, parsed_id),
                _no_calendar_match_rows(connection, parsed_id),
                _event_window_overlap_count(connection, parsed_id, "D1"),
                _exclusion_rows_for_index(connection, parsed_id),
            )
        )

    for index_id, (symbol, name) in metadata.items():
        if index_id not in seen_ids:
            result.append(BenchmarkCoverageRow(index_id, symbol, name, 0, None, None, 0))
    return tuple(result)


def _fetch_index_metadata(connection: Any) -> dict[int | None, tuple[str | None, str | None]]:
    if not table_exists(connection, MARKET_INDEXES):
        return {}
    columns = set(get_table_columns(connection, MARKET_INDEXES))
    if MARKET_INDEX_ID not in columns:
        return {}
    symbol_column = _first_existing(columns, ("index_symbol", "symbol", "ticker_symbol", "ticker", "index_code"))
    name_column = _first_existing(columns, ("index_name", "name", "market_index_name", "description"))
    symbol_expr = symbol_column if symbol_column else "NULL"
    name_expr = name_column if name_column else "NULL"
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {MARKET_INDEX_ID}, {symbol_expr}, {name_expr}
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


def _event_window_overlap_count(
    connection: Any,
    market_index_id: int | None,
    window_code: str | None = None,
) -> int | None:
    if market_index_id is None or not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None
    boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    if not {WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns):
        return None
    params: tuple[Any, ...] = (market_index_id,)
    window_filter = ""
    if window_code and WINDOW_CODE in boundary_columns:
        window_filter = f"AND ewb.{WINDOW_CODE} = %s"
        params = (market_index_id, window_code)
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE EXISTS (
            SELECT 1
            FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        {window_filter}
        """,
        params,
    )
    return int(value) if value is not None else None


def _expected_trading_days(connection: Any, first_date: Any, last_date: Any) -> int | None:
    if not first_date or not last_date or not table_exists(connection, MARKET_CALENDAR):
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
        (first_date, last_date),
    )
    return int(value) if value is not None else None


def calculate_density_pct(price_rows: int, expected_days: int | None) -> float | None:
    """Calculate benchmark price density versus expected trading days."""

    if not expected_days:
        return None
    return round((price_rows / expected_days) * 100, 2)


def _missing_trading_days(price_rows: int, expected_days: int | None) -> int | None:
    if expected_days is None:
        return None
    return max(expected_days - price_rows, 0)


def _non_trading_rows(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None or not table_exists(connection, MARKET_CALENDAR):
        return None
    index_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {MARKET_INDEX_ID, TRADE_DATE}.issubset(index_columns):
        return None
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns):
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {INDEX_DAILY_PRICES} idp
        INNER JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = idp.{TRADE_DATE}
        WHERE idp.{MARKET_INDEX_ID} = %s
          AND mc.{IS_TRADING_DAY} = 0
        """,
        (market_index_id,),
    )
    return int(value) if value is not None else None


def _no_calendar_match_rows(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None or not table_exists(connection, MARKET_CALENDAR):
        return None
    index_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {MARKET_INDEX_ID, TRADE_DATE}.issubset(index_columns):
        return None
    if CALENDAR_DATE not in calendar_columns:
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {INDEX_DAILY_PRICES} idp
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = idp.{TRADE_DATE}
        WHERE idp.{MARKET_INDEX_ID} = %s
          AND mc.{CALENDAR_DATE} IS NULL
        """,
        (market_index_id,),
    )
    return int(value) if value is not None else None


def _exclusion_rows_for_index(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None or not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE NOT EXISTS (
            SELECT 1
            FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        """,
        (market_index_id,),
    )
    return int(value) if value is not None else None


def _benchmark_notes(
    rows: tuple[BenchmarkCoverageRow, ...],
    recommended: BenchmarkCoverageRow | None,
    dji_rows: int | None,
) -> tuple[str, ...]:
    notes: list[str] = []
    if recommended is not None:
        notes.append(
            f"Recommended benchmark candidate is {_benchmark_label(recommended)} "
            "based on density, D1 coverage, event-window overlap, row count, and calendar quality."
        )
    if dji_rows == 0 and any(row.price_rows > 0 for row in rows):
        notes.append("dji_daily_prices is empty, but benchmark coverage exists in index_daily_prices.")
    if not rows:
        notes.append("No benchmark/index coverage rows were available.")
    return tuple(notes)


def _dji_empty_interpretation(dji_rows: int | None, rows: tuple[BenchmarkCoverageRow, ...]) -> str:
    if dji_rows is None:
        return "dji_daily_prices table is unavailable."
    if dji_rows > 0:
        return "dji_daily_prices contains rows."
    if any(row.price_rows > 0 for row in rows):
        return "Probably not blocking if DJIA or another benchmark is represented in index_daily_prices."
    return "Blocking unless another benchmark source is selected."


def _benchmark_label(row: BenchmarkCoverageRow | None) -> str:
    if row is None:
        return "Unavailable"
    label = row.symbol or row.name or f"market_index_id={row.market_index_id}"
    if row.market_index_id is not None:
        return f"{label} (id={row.market_index_id})"
    return label


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _format_optional_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _format_optional_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _density_score(row: BenchmarkCoverageRow) -> tuple[float, int, int, int, int]:
    quality_penalty = (row.non_trading_rows or 0) + (row.no_calendar_match_rows or 0)
    return (
        row.density_pct or 0.0,
        row.d1_overlap_rows or 0,
        row.event_window_overlap_rows or 0,
        row.price_rows,
        -quality_penalty,
    )


def _benchmark_policy_warning(
    primary: BenchmarkCoverageRow | None,
    rows: tuple[BenchmarkCoverageRow, ...],
) -> str:
    if primary is None:
        return "No usable benchmark rows were found."
    warnings: list[str] = []
    if (primary.density_pct or 0.0) < MINIMUM_DENSITY_PCT:
        warnings.append(f"{_benchmark_label(primary)} is below the {MINIMUM_DENSITY_PCT:.0f}% density threshold and needs import repair before AR/CAR.")
    if (primary.non_trading_rows or 0) > 0:
        warnings.append(f"{_benchmark_label(primary)} has {primary.non_trading_rows} non-trading benchmark rows.")
    if (primary.no_calendar_match_rows or 0) > 0:
        warnings.append(f"{_benchmark_label(primary)} has {primary.no_calendar_match_rows} benchmark rows with no calendar match.")
    sp500 = next((row for row in rows if (row.symbol or "").upper() in {"SP500", "S&P500", "SPX"}), None)
    if sp500 is not None and (sp500.density_pct or 0.0) < MINIMUM_DENSITY_PCT:
        warnings.append("SP500 has the strongest event-window overlap but appears sparse; validate/import repair before final benchmark selection.")
    return " ".join(warnings)


def _recommendation_rows(
    primary: BenchmarkCoverageRow | None,
    robustness: tuple[str, ...],
    rows: tuple[BenchmarkCoverageRow, ...],
    warning: str,
) -> tuple[BenchmarkRecommendationRow, ...]:
    result: list[BenchmarkRecommendationRow] = []
    if primary is not None:
        result.append(
            BenchmarkRecommendationRow(
                "primary",
                primary.market_index_id,
                _benchmark_label(primary),
                "Use as primary only after density/calendar warnings are accepted or repaired.",
                warning,
            )
        )
    for label in robustness:
        row = next((candidate for candidate in rows if _benchmark_label(candidate) == label), None)
        result.append(
            BenchmarkRecommendationRow(
                "robustness",
                row.market_index_id if row else None,
                label,
                "Use as robustness benchmark.",
                "",
            )
        )
    return tuple(result)


def _benchmark_density_fieldnames() -> tuple[str, ...]:
    return (
        "market_index_id",
        "symbol",
        "name",
        "price_rows",
        "first_trade_date",
        "last_trade_date",
        "event_window_overlap_rows",
        "expected_trading_days",
        "density_pct",
        "missing_trading_days",
        "non_trading_rows",
        "no_calendar_match_rows",
        "d1_overlap_rows",
        "exclusion_rows",
    )


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: BenchmarkCoverageReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Benchmark coverage failed: %s", result.error_message)
        return
    logger.info(
        "Benchmark coverage completed: database=%s status=%s recommended=%s",
        result.database_name,
        result.coverage_status,
        result.recommended_benchmark_label,
    )


def _log_selection_report(result: BenchmarkSelectionDiagnostic, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Benchmark selection diagnostic failed: %s", result.error_message)
        return
    logger.info(
        "Benchmark selection diagnostic completed: database=%s status=%s primary=%s",
        result.database_name,
        result.diagnostic_status,
        result.recommended_primary_benchmark,
    )
