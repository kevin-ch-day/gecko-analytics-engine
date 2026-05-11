"""Read-only benchmark import readiness audit."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import count_rows, get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import (
    CALENDAR_DATE,
    CYBER_EVENT_ID,
    DJI_DAILY_PRICES,
    FIRST_TRADING_DAY,
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
from gecko_analytics_engine.market_data.indexes import MINIMUM_DENSITY_PCT, calculate_density_pct
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class IndexAuditRow:
    """One index series audit row."""

    market_index_id: int | None
    symbol: str | None
    name: str | None
    row_count: int
    unique_trade_dates: int
    min_trade_date: str | None
    max_trade_date: str | None
    duplicate_index_date_rows: int | None
    weekend_rows: int | None
    non_trading_calendar_rows: int | None
    no_calendar_match_rows: int | None
    largest_gap_days: int | None
    largest_gap_start_date: str | None
    largest_gap_end_date: str | None
    median_gap_days: float | None
    frequency_classification: str
    expected_trading_days: int | None
    density_pct: float | None
    missing_trading_day_count: int | None


@dataclass(frozen=True)
class MissingBenchmarkDateRow:
    """Missing expected trading date for one benchmark index."""

    market_index_id: int | None
    symbol: str | None
    missing_trade_date: str
    position: str


@dataclass(frozen=True)
class EventWindowBenchmarkGapRow:
    """Benchmark gap summary for event-study windows."""

    market_index_id: int | None
    symbol: str | None
    window_code: str
    missing_windows: int
    available_windows: int


@dataclass(frozen=True)
class EventWindowBenchmarkGapFinding:
    """Concentrated event-window benchmark gap finding."""

    market_index_id: int | None
    symbol: str | None
    grouping: str
    value: str
    missing_windows: int


@dataclass(frozen=True)
class SourceFileCandidate:
    """Local file that may contain benchmark/index source data."""

    path: str
    area: str
    file_name: str
    size_bytes: int
    matched_terms: str


@dataclass(frozen=True)
class BenchmarkRepairPlan:
    """Read-only repair plan for benchmark imports."""

    recommended_canonical_table: str
    dji_daily_prices_policy: str
    first_index_to_import_or_repair: str
    target_date_range: str
    minimum_density_threshold: str
    row_repair_policy: str
    validation_rules: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkImportAudit:
    """Benchmark import readiness audit report."""

    generated_at: str
    connection_ok: bool
    audit_status: str
    database_name: str | None = None
    index_rows: tuple[IndexAuditRow, ...] = ()
    event_window_gap_rows: tuple[EventWindowBenchmarkGapRow, ...] = ()
    event_window_gap_findings: tuple[EventWindowBenchmarkGapFinding, ...] = ()
    missing_dates: tuple[MissingBenchmarkDateRow, ...] = ()
    source_file_candidates: tuple[SourceFileCandidate, ...] = ()
    repair_plan: BenchmarkRepairPlan | None = None
    dji_daily_price_rows: int | None = None
    total_candidate_window_requirements: int | None = None
    notes: tuple[str, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_benchmark_import_audit(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkImportAudit:
    """Run and export a read-only benchmark import readiness audit."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            index_rows = _build_index_audit_rows(connection)
            missing_dates = _sample_missing_dates(connection, index_rows)
            event_window_gaps = _build_event_window_gap_rows(connection, index_rows)
            event_window_findings = _build_event_window_gap_findings(connection, index_rows)
            dji_rows = count_rows(connection, DJI_DAILY_PRICES)
            total_requirements = _candidate_window_requirement_count(connection)
    except DatabaseConnectionError as exc:
        result = BenchmarkImportAudit(
            generated_at=generated_at,
            connection_ok=False,
            audit_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = BenchmarkImportAudit(
            generated_at=generated_at,
            connection_ok=False,
            audit_status="BLOCKED",
            error_message=f"Benchmark import audit failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    source_files = discover_benchmark_source_files(paths)
    repair_plan = build_benchmark_repair_plan(index_rows, dji_rows)
    notes = build_benchmark_audit_notes(index_rows, dji_rows, source_files)
    result = BenchmarkImportAudit(
        generated_at=generated_at,
        connection_ok=True,
        audit_status=determine_benchmark_audit_status(index_rows),
        database_name=database_name,
        index_rows=index_rows,
        event_window_gap_rows=event_window_gaps,
        event_window_gap_findings=event_window_findings,
        missing_dates=missing_dates,
        source_file_candidates=source_files,
        repair_plan=repair_plan,
        dji_daily_price_rows=dji_rows,
        total_candidate_window_requirements=total_requirements,
        notes=notes,
    )
    result = export_benchmark_import_audit(result, paths, logger)
    _log_report(result, logger)
    return result


def determine_benchmark_audit_status(rows: tuple[IndexAuditRow, ...]) -> str:
    """Classify benchmark import readiness."""

    if not rows:
        return "BLOCKED"
    if any((row.density_pct or 0.0) >= MINIMUM_DENSITY_PCT for row in rows):
        return "READY_AFTER_POLICY_REVIEW"
    return "NEEDS_IMPORT_REPAIR"


def classify_gap_frequency(gaps: tuple[int, ...]) -> str:
    """Classify a date series by observed gaps between rows."""

    if not gaps:
        return "insufficient_data"
    median_gap = median(gaps)
    largest_gap = max(gaps)
    if median_gap <= 4 and largest_gap <= 10:
        return "daily_like"
    if 5 <= median_gap <= 10 and largest_gap <= 21:
        return "weekly_like"
    if 25 <= median_gap <= 35:
        return "monthly_like"
    return "sparse_or_mixed"


def build_benchmark_repair_plan(
    rows: tuple[IndexAuditRow, ...],
    dji_daily_price_rows: int | None,
) -> BenchmarkRepairPlan:
    """Build a read-only benchmark repair plan."""

    first = _first_repair_target(rows)
    target_range = _target_date_range(rows)
    dji_policy = (
        "Keep dji_daily_prices ignored for now and treat index_daily_prices as canonical."
        if dji_daily_price_rows == 0
        else "Reconcile dji_daily_prices against index_daily_prices before choosing a canonical source."
    )
    return BenchmarkRepairPlan(
        recommended_canonical_table=INDEX_DAILY_PRICES,
        dji_daily_prices_policy=dji_policy,
        first_index_to_import_or_repair=first,
        target_date_range=target_range,
        minimum_density_threshold=f"{MINIMUM_DENSITY_PCT:.0f}% or higher over required trading days",
        row_repair_policy="Reconcile existing rows against trusted source files; do not blindly append or replace until duplicate and stale-row rules are checked.",
        validation_rules=(
            "One row per index per trading day.",
            "Adjusted close present when the source provides it.",
            "No duplicate index/date rows.",
            "No weekend rows unless a source-specific exception is documented.",
            "No stale repeated non-trading rows.",
            "Coverage must span event windows and later estimation windows.",
        ),
    )


def build_benchmark_audit_notes(
    rows: tuple[IndexAuditRow, ...],
    dji_daily_price_rows: int | None,
    source_files: tuple[SourceFileCandidate, ...],
) -> tuple[str, ...]:
    """Build notes and warnings for the audit."""

    notes: list[str] = []
    sparse = [row for row in rows if (row.density_pct or 0.0) < MINIMUM_DENSITY_PCT]
    if sparse:
        notes.append(f"{len(sparse)} benchmark series are below the {MINIMUM_DENSITY_PCT:.0f}% density threshold.")
    if dji_daily_price_rows == 0:
        notes.append("dji_daily_prices is empty; index_daily_prices should remain the canonical benchmark table unless a later migration says otherwise.")
    if source_files:
        notes.append(f"{len(source_files)} local candidate benchmark source files were discovered.")
    else:
        notes.append("No local candidate benchmark source files were found under data/raw, data/external, data/interim, or data/processed.")
    return tuple(notes)


def discover_benchmark_source_files(paths: AppPaths) -> tuple[SourceFileCandidate, ...]:
    """Discover local CSV files that might contain benchmark/index data."""

    terms = ("sp500", "s&p", "spx", "djia", "dji", "dow", "nasdaq", "ixic", "index", "benchmark")
    roots = (
        ("raw", paths.data_root / "raw"),
        ("external", paths.data_root / "external"),
        ("interim", paths.data_root / "interim"),
        ("processed", paths.data_root / "processed"),
    )
    result: list[SourceFileCandidate] = []
    for area, root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.csv")):
            lower_name = path.name.lower()
            matched = tuple(term for term in terms if term in lower_name)
            if not matched:
                continue
            result.append(
                SourceFileCandidate(
                    path=str(path),
                    area=area,
                    file_name=path.name,
                    size_bytes=path.stat().st_size,
                    matched_terms=", ".join(matched),
                )
            )
    return tuple(result)


def export_benchmark_import_audit(
    result: BenchmarkImportAudit,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> BenchmarkImportAudit:
    """Export benchmark import audit artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    audit_json = paths.reports_dir / "benchmark_import_audit.json"
    gap_csv = paths.exports_dir / "benchmark_index_gap_summary.csv"
    missing_dates_csv = paths.exports_dir / "benchmark_missing_dates.csv"
    source_csv = paths.exports_dir / "benchmark_source_file_candidates.csv"
    repair_json = paths.reports_dir / "benchmark_import_repair_plan.json"
    export_paths = (audit_json, gap_csv, missing_dates_csv, source_csv, repair_json)
    result_with_exports = replace(result, export_paths=export_paths)

    write_dataclass_json(audit_json, result_with_exports)
    write_rows_csv(gap_csv, result.index_rows, _index_audit_fieldnames())
    write_rows_csv(
        missing_dates_csv,
        result.missing_dates,
        ("market_index_id", "symbol", "missing_trade_date", "position"),
    )
    write_rows_csv(
        source_csv,
        result.source_file_candidates,
        ("path", "area", "file_name", "size_bytes", "matched_terms"),
    )
    if result.repair_plan is not None:
        write_dataclass_json(repair_json, result.repair_plan)
    if logger is not None:
        logger.info("Benchmark import audit exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def format_benchmark_import_audit(result: BenchmarkImportAudit) -> list[str]:
    """Format benchmark import audit for console output."""

    lines = ["", "Benchmark Import Readiness Audit", "--------------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            f"Overall status: {result.audit_status}",
            f"Database: {result.database_name or 'Unknown'}",
            f"dji_daily_prices rows: {_format_optional_int(result.dji_daily_price_rows)}",
            f"Candidate event/window requirements: {_format_optional_int(result.total_candidate_window_requirements)}",
            "",
            "Index density summary:",
        ]
    )
    for row in result.index_rows:
        lines.append(
            "  "
            f"{_index_label(row)}: rows={row.row_count:,}, unique_dates={row.unique_trade_dates:,}, "
            f"density={_format_optional_float(row.density_pct)}%, missing_days={_format_optional_int(row.missing_trading_day_count)}, "
            f"range={row.min_trade_date or 'Unknown'} to {row.max_trade_date or 'Unknown'}, "
            f"median_gap={_format_optional_float(row.median_gap_days)}, largest_gap={_format_optional_int(row.largest_gap_days)}, "
            f"class={row.frequency_classification}"
        )

    if result.index_rows:
        lines.extend(["", "Largest gaps:"])
        for row in sorted(result.index_rows, key=lambda item: item.largest_gap_days or 0, reverse=True):
            lines.append(
                "  "
                f"{_index_label(row)}: {_format_optional_int(row.largest_gap_days)} days "
                f"({row.largest_gap_start_date or 'Unknown'} to {row.largest_gap_end_date or 'Unknown'})"
            )

    if result.event_window_gap_rows:
        lines.extend(["", "Event-window benchmark gaps:"])
        for row in result.event_window_gap_rows[:20]:
            lines.append(
                "  "
                f"{row.symbol or row.market_index_id or 'Unknown'} {row.window_code}: "
                f"missing={row.missing_windows:,}, available={row.available_windows:,}"
            )

    if result.event_window_gap_findings:
        lines.extend(["", "Gap concentration findings:"])
        for finding in result.event_window_gap_findings[:30]:
            lines.append(
                "  "
                f"{finding.symbol or finding.market_index_id or 'Unknown'} {finding.grouping}={finding.value}: "
                f"missing_windows={finding.missing_windows:,}"
            )

    lines.extend(["", "Missing benchmark date samples:"])
    if result.missing_dates:
        for row in result.missing_dates[:20]:
            lines.append(f"  {row.symbol or row.market_index_id}: {row.missing_trade_date} ({row.position})")
    else:
        lines.append("  No missing trading-date samples available.")

    lines.extend(["", "Local source file candidates:"])
    if result.source_file_candidates:
        for candidate in result.source_file_candidates[:20]:
            lines.append(f"  {candidate.path} [{candidate.matched_terms}]")
    else:
        lines.append("  None found under data/raw, data/external, data/interim, or data/processed.")

    if result.repair_plan is not None:
        lines.extend(
            [
                "",
                "Recommended repair plan:",
                f"  Canonical table: {result.repair_plan.recommended_canonical_table}",
                f"  dji_daily_prices policy: {result.repair_plan.dji_daily_prices_policy}",
                f"  First repair target: {result.repair_plan.first_index_to_import_or_repair}",
                f"  Target date range: {result.repair_plan.target_date_range}",
                f"  Minimum density threshold: {result.repair_plan.minimum_density_threshold}",
                f"  Row repair policy: {result.repair_plan.row_repair_policy}",
                "  Validation rules:",
            ]
        )
        lines.extend(f"    {rule}" for rule in result.repair_plan.validation_rules)

    if result.notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"  {note}" for note in result.notes)
    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_benchmark_import_audit(result: BenchmarkImportAudit) -> None:
    """Print benchmark import audit."""

    for line in format_benchmark_import_audit(result):
        print(line)


def _build_index_audit_rows(connection: Any) -> tuple[IndexAuditRow, ...]:
    if not table_exists(connection, INDEX_DAILY_PRICES):
        return ()
    index_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    if not {MARKET_INDEX_ID, TRADE_DATE}.issubset(index_columns):
        return ()

    metadata = _index_metadata(connection)
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {MARKET_INDEX_ID}, COUNT(*), COUNT(DISTINCT {TRADE_DATE}), MIN({TRADE_DATE}), MAX({TRADE_DATE})
        FROM {INDEX_DAILY_PRICES}
        GROUP BY {MARKET_INDEX_ID}
        ORDER BY {MARKET_INDEX_ID}
        """,
    )
    result: list[IndexAuditRow] = []
    for index_id, row_count, unique_dates, min_date, max_date in rows:
        parsed_id = int(index_id) if index_id is not None else None
        symbol, name = metadata.get(parsed_id, (None, None))
        dates = _index_dates(connection, parsed_id)
        gaps = _date_gaps(dates)
        largest_gap = max(gaps) if gaps else None
        gap_start, gap_end = _largest_gap_bounds(dates, gaps)
        expected_days = _expected_trading_days(connection, min_date, max_date)
        parsed_row_count = int(row_count or 0)
        result.append(
            IndexAuditRow(
                market_index_id=parsed_id,
                symbol=symbol,
                name=name,
                row_count=parsed_row_count,
                unique_trade_dates=int(unique_dates or 0),
                min_trade_date=str(min_date) if min_date else None,
                max_trade_date=str(max_date) if max_date else None,
                duplicate_index_date_rows=_duplicate_rows(connection, parsed_id),
                weekend_rows=_weekend_rows(connection, parsed_id),
                non_trading_calendar_rows=_non_trading_rows(connection, parsed_id),
                no_calendar_match_rows=_no_calendar_match_rows(connection, parsed_id),
                largest_gap_days=largest_gap,
                largest_gap_start_date=str(gap_start) if gap_start else None,
                largest_gap_end_date=str(gap_end) if gap_end else None,
                median_gap_days=float(median(gaps)) if gaps else None,
                frequency_classification=classify_gap_frequency(gaps),
                expected_trading_days=expected_days,
                density_pct=calculate_density_pct(parsed_row_count, expected_days),
                missing_trading_day_count=max((expected_days or 0) - parsed_row_count, 0) if expected_days is not None else None,
            )
        )
    return tuple(result)


def _build_event_window_gap_rows(connection: Any, index_rows: tuple[IndexAuditRow, ...]) -> tuple[EventWindowBenchmarkGapRow, ...]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return ()
    boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    if not {WINDOW_CODE, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns):
        return ()
    result: list[EventWindowBenchmarkGapRow] = []
    for row in index_rows:
        if row.market_index_id is None:
            continue
        gap_rows = safe_fetch_all(
            connection,
            f"""
            SELECT
                ewb.{WINDOW_CODE},
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM {INDEX_DAILY_PRICES} idp
                    WHERE idp.{MARKET_INDEX_ID} = %s
                      AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
                ) THEN 0 ELSE 1 END) AS missing_windows,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM {INDEX_DAILY_PRICES} idp
                    WHERE idp.{MARKET_INDEX_ID} = %s
                      AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
                ) THEN 1 ELSE 0 END) AS available_windows
            FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
            GROUP BY ewb.{WINDOW_CODE}
            ORDER BY ewb.{WINDOW_CODE}
            """,
            (row.market_index_id, row.market_index_id),
        )
        for window_code, missing, available in gap_rows:
            result.append(
                EventWindowBenchmarkGapRow(
                    row.market_index_id,
                    row.symbol,
                    str(window_code) if window_code else "Unknown",
                    int(missing or 0),
                    int(available or 0),
                )
            )
    return tuple(result)


def _build_event_window_gap_findings(
    connection: Any,
    index_rows: tuple[IndexAuditRow, ...],
) -> tuple[EventWindowBenchmarkGapFinding, ...]:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return ()
    boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    if not {WINDOW_CODE, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns):
        return ()

    result: list[EventWindowBenchmarkGapFinding] = []
    for row in index_rows:
        if row.market_index_id is None:
            continue
        if CYBER_EVENT_ID in boundary_columns:
            result.extend(_gap_findings_for_group(connection, row, CYBER_EVENT_ID, "event"))
        if FIRST_TRADING_DAY in boundary_columns:
            result.extend(_gap_findings_for_group(connection, row, FIRST_TRADING_DAY, "aligned_date"))
            result.extend(_gap_findings_for_year(connection, row))
            result.extend(_gap_findings_for_d1_date(connection, row))
    return tuple(result)


def _gap_findings_for_group(
    connection: Any,
    row: IndexAuditRow,
    column_name: str,
    grouping: str,
) -> tuple[EventWindowBenchmarkGapFinding, ...]:
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT ewb.{column_name}, COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE NOT EXISTS (
            SELECT 1 FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        GROUP BY ewb.{column_name}
        ORDER BY COUNT(*) DESC, ewb.{column_name}
        LIMIT 10
        """,
        (row.market_index_id,),
    )
    return tuple(
        EventWindowBenchmarkGapFinding(
            row.market_index_id,
            row.symbol,
            grouping,
            str(value) if value is not None else "Unknown",
            int(count or 0),
        )
        for value, count in rows
    )


def _gap_findings_for_year(connection: Any, row: IndexAuditRow) -> tuple[EventWindowBenchmarkGapFinding, ...]:
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT YEAR(ewb.{FIRST_TRADING_DAY}), COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE NOT EXISTS (
            SELECT 1 FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        GROUP BY YEAR(ewb.{FIRST_TRADING_DAY})
        ORDER BY COUNT(*) DESC, YEAR(ewb.{FIRST_TRADING_DAY})
        LIMIT 10
        """,
        (row.market_index_id,),
    )
    return tuple(
        EventWindowBenchmarkGapFinding(
            row.market_index_id,
            row.symbol,
            "year",
            str(value) if value is not None else "Unknown",
            int(count or 0),
        )
        for value, count in rows
    )


def _gap_findings_for_d1_date(connection: Any, row: IndexAuditRow) -> tuple[EventWindowBenchmarkGapFinding, ...]:
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT ewb.{FIRST_TRADING_DAY}, COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        WHERE ewb.{WINDOW_CODE} = 'D1'
          AND NOT EXISTS (
            SELECT 1 FROM {INDEX_DAILY_PRICES} idp
            WHERE idp.{MARKET_INDEX_ID} = %s
              AND idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        )
        GROUP BY ewb.{FIRST_TRADING_DAY}
        ORDER BY ewb.{FIRST_TRADING_DAY}
        LIMIT 10
        """,
        (row.market_index_id,),
    )
    return tuple(
        EventWindowBenchmarkGapFinding(
            row.market_index_id,
            row.symbol,
            "d1_missing_date",
            str(value) if value is not None else "Unknown",
            int(count or 0),
        )
        for value, count in rows
    )


def _sample_missing_dates(
    connection: Any,
    index_rows: tuple[IndexAuditRow, ...],
    sample_size: int = 10,
) -> tuple[MissingBenchmarkDateRow, ...]:
    if not table_exists(connection, MARKET_CALENDAR):
        return ()
    result: list[MissingBenchmarkDateRow] = []
    for row in index_rows:
        if row.market_index_id is None or not row.min_trade_date or not row.max_trade_date:
            continue
        missing = _missing_dates_for_index(connection, row.market_index_id, row.min_trade_date, row.max_trade_date)
        first = missing[:sample_size]
        last = missing[-sample_size:] if len(missing) > sample_size else ()
        for value in first:
            result.append(MissingBenchmarkDateRow(row.market_index_id, row.symbol, str(value), "first"))
        for value in last:
            if value not in first:
                result.append(MissingBenchmarkDateRow(row.market_index_id, row.symbol, str(value), "last"))
    return tuple(result)


def _missing_dates_for_index(
    connection: Any,
    market_index_id: int,
    min_date: str,
    max_date: str,
) -> tuple[Any, ...]:
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns):
        return ()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT mc.{CALENDAR_DATE}
        FROM {MARKET_CALENDAR} mc
        LEFT JOIN {INDEX_DAILY_PRICES} idp
          ON idp.{TRADE_DATE} = mc.{CALENDAR_DATE}
         AND idp.{MARKET_INDEX_ID} = %s
        WHERE mc.{CALENDAR_DATE} BETWEEN %s AND %s
          AND mc.{IS_TRADING_DAY} = 1
          AND idp.{TRADE_DATE} IS NULL
        ORDER BY mc.{CALENDAR_DATE}
        """,
        (market_index_id, min_date, max_date),
    )
    return tuple(row[0] for row in rows)


def _candidate_window_requirement_count(connection: Any) -> int | None:
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None
    value = safe_scalar(connection, f"SELECT COUNT(*) FROM {VW_EVENT_WINDOW_BOUNDARIES}")
    return int(value) if value is not None else None


def _index_metadata(connection: Any) -> dict[int | None, tuple[str | None, str | None]]:
    if not table_exists(connection, MARKET_INDEXES):
        return {}
    columns = set(get_table_columns(connection, MARKET_INDEXES))
    if MARKET_INDEX_ID not in columns:
        return {}
    symbol_column = _first_existing(columns, ("index_symbol", "symbol", "ticker_symbol", "ticker", "index_code"))
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


def _index_dates(connection: Any, market_index_id: int | None) -> tuple[date, ...]:
    if market_index_id is None:
        return ()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT DISTINCT {TRADE_DATE}
        FROM {INDEX_DAILY_PRICES}
        WHERE {MARKET_INDEX_ID} = %s
        ORDER BY {TRADE_DATE}
        """,
        (market_index_id,),
    )
    return tuple(row[0] for row in rows if isinstance(row[0], date))


def _date_gaps(dates: tuple[date, ...]) -> tuple[int, ...]:
    return tuple((right - left).days for left, right in zip(dates, dates[1:]))


def _largest_gap_bounds(dates: tuple[date, ...], gaps: tuple[int, ...]) -> tuple[date | None, date | None]:
    if not dates or not gaps:
        return None, None
    index = gaps.index(max(gaps))
    return dates[index], dates[index + 1]


def _expected_trading_days(connection: Any, min_date: Any, max_date: Any) -> int | None:
    if not min_date or not max_date or not table_exists(connection, MARKET_CALENDAR):
        return None
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(calendar_columns):
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {MARKET_CALENDAR}
        WHERE {CALENDAR_DATE} BETWEEN %s AND %s
          AND {IS_TRADING_DAY} = 1
        """,
        (min_date, max_date),
    )
    return int(value) if value is not None else None


def _duplicate_rows(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None:
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COALESCE(SUM(row_count - 1), 0)
        FROM (
            SELECT COUNT(*) AS row_count
            FROM {INDEX_DAILY_PRICES}
            WHERE {MARKET_INDEX_ID} = %s
            GROUP BY {MARKET_INDEX_ID}, {TRADE_DATE}
            HAVING COUNT(*) > 1
        ) duplicate_rows
        """,
        (market_index_id,),
    )
    return int(value) if value is not None else None


def _weekend_rows(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None:
        return None
    value = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {INDEX_DAILY_PRICES}
        WHERE {MARKET_INDEX_ID} = %s
          AND DAYOFWEEK({TRADE_DATE}) IN (1, 7)
        """,
        (market_index_id,),
    )
    return int(value) if value is not None else None


def _non_trading_rows(connection: Any, market_index_id: int | None) -> int | None:
    if market_index_id is None or not table_exists(connection, MARKET_CALENDAR):
        return None
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
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
    calendar_columns = set(get_table_columns(connection, MARKET_CALENDAR))
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


def _first_repair_target(rows: tuple[IndexAuditRow, ...]) -> str:
    if not rows:
        return "Import a primary broad-market benchmark into index_daily_prices."
    sorted_rows = sorted(rows, key=lambda row: ((row.density_pct or 0.0), row.row_count), reverse=True)
    best = sorted_rows[0]
    if (best.density_pct or 0.0) < MINIMUM_DENSITY_PCT:
        return f"Repair {best.symbol or best.name or best.market_index_id} first because it is currently the densest available benchmark."
    return f"Validate {best.symbol or best.name or best.market_index_id} first; then repair SP500 if it remains the preferred broad-market benchmark."


def _target_date_range(rows: tuple[IndexAuditRow, ...]) -> str:
    min_values = [row.min_trade_date for row in rows if row.min_trade_date]
    max_values = [row.max_trade_date for row in rows if row.max_trade_date]
    if not min_values or not max_values:
        return "At least the full event-window and future estimation-window range."
    return f"{min(min_values)} to {max(max_values)}, plus estimation-window lookback once defined."


def _index_label(row: IndexAuditRow) -> str:
    label = row.symbol or row.name or f"market_index_id={row.market_index_id}"
    if row.market_index_id is not None:
        return f"{label} (id={row.market_index_id})"
    return label


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _index_audit_fieldnames() -> tuple[str, ...]:
    return (
        "market_index_id",
        "symbol",
        "name",
        "row_count",
        "unique_trade_dates",
        "min_trade_date",
        "max_trade_date",
        "duplicate_index_date_rows",
        "weekend_rows",
        "non_trading_calendar_rows",
        "no_calendar_match_rows",
        "largest_gap_days",
        "largest_gap_start_date",
        "largest_gap_end_date",
        "median_gap_days",
        "frequency_classification",
        "expected_trading_days",
        "density_pct",
        "missing_trading_day_count",
    )


def _format_optional_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _format_optional_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: BenchmarkImportAudit, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Benchmark import audit failed: %s", result.error_message)
        return
    logger.info(
        "Benchmark import audit completed: database=%s status=%s indexes=%s",
        result.database_name,
        result.audit_status,
        len(result.index_rows),
    )
