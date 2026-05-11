"""Daily price source-file collection manifest generation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import get_table_columns, safe_fetch_all, safe_scalar, table_exists
from gecko_analytics_engine.db.schema_contract import EXCHANGES, EXCHANGE_CODE, EXCHANGE_ID, SECURITIES, SECURITY_ID
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.market_data.repair_scope import (
    BenchmarkImportTarget,
    DailyDataRepairScopeReport,
    SecurityImportTarget,
    format_required_range,
    run_daily_data_repair_scope,
)
from gecko_analytics_engine.utils.paths import AppPaths


PREFERRED_DATE_RANGE = "2012-01-01 to 2026-05-01"
CSV_REQUIRED_COLUMNS = "Date, Open, High, Low, Close, Adj Close or adjusted_close, Volume"


@dataclass(frozen=True)
class BenchmarkSourceManifestRow:
    """Expected daily benchmark/index source file."""

    market_index_id: int | None
    index_code: str | None
    index_name: str | None
    expected_filename_patterns: str
    required_date_range: str
    preferred_date_range: str
    priority: str
    canonical_target_table: str
    required_columns: str
    notes: str


@dataclass(frozen=True)
class SecuritySourceManifestRow:
    """Expected daily linked-security source file."""

    security_id: int
    ticker_symbol: str
    company_name: str | None
    exchange_code: str | None
    expected_filename_patterns: str
    required_date_range: str
    preferred_date_range: str
    priority: str
    current_row_count: int
    current_density_pct: float | None
    affected_rows: int
    affected_windows: int
    canonical_target_table: str
    required_columns: str


@dataclass(frozen=True)
class DailyPriceSourceManifest:
    """Daily price file collection manifest."""

    generated_at: str
    connection_ok: bool
    manifest_status: str
    database_name: str | None = None
    indexes_folder: Path | None = None
    securities_folder: Path | None = None
    required_date_range: str = "Unavailable"
    preferred_date_range: str = PREFERRED_DATE_RANGE
    benchmark_rows: tuple[BenchmarkSourceManifestRow, ...] = ()
    security_rows: tuple[SecuritySourceManifestRow, ...] = ()
    checklist_path: Path | None = None
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_daily_price_source_manifest(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DailyPriceSourceManifest:
    """Generate a read-only daily source-file collection manifest."""

    ensure_source_folders(paths)
    generated_at = datetime.now(UTC).isoformat()
    repair_scope = run_daily_data_repair_scope(settings, paths, logger)
    if not repair_scope.connection_ok:
        result = DailyPriceSourceManifest(
            generated_at=generated_at,
            connection_ok=False,
            manifest_status="BLOCKED",
            indexes_folder=paths.data_root / "raw" / "indexes",
            securities_folder=paths.data_root / "raw" / "securities",
            error_message=repair_scope.error_message,
        )
        _log_report(result, logger)
        return result

    try:
        exchange_map = _security_exchange_map(settings)
    except DatabaseConnectionError:
        exchange_map = {}

    result = build_daily_price_source_manifest(repair_scope, paths, exchange_map, generated_at)
    result = export_daily_price_source_manifest(result, paths, logger)
    _log_report(result, logger)
    return result


def ensure_source_folders(paths: AppPaths) -> None:
    """Ensure raw daily price source folders and .gitkeep files exist."""

    for folder in (paths.data_root / "raw" / "indexes", paths.data_root / "raw" / "securities"):
        folder.mkdir(parents=True, exist_ok=True)
        gitkeep = folder / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")


def build_daily_price_source_manifest(
    repair_scope: DailyDataRepairScopeReport,
    paths: AppPaths,
    exchange_map: dict[int, str | None] | None = None,
    generated_at: str | None = None,
) -> DailyPriceSourceManifest:
    """Build source manifest rows from an existing repair scope."""

    exchange_map = exchange_map or {}
    benchmark_rows = tuple(_benchmark_manifest_row(target) for target in repair_scope.benchmark_targets)
    security_rows = tuple(
        _security_manifest_row(target, exchange_map.get(target.security_id))
        for target in repair_scope.security_targets
        if target.import_priority in {"high", "medium"}
    )
    status = "SOURCE_FILES_REQUIRED" if benchmark_rows or security_rows else "NO_SOURCE_FILES_REQUIRED"
    return DailyPriceSourceManifest(
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        connection_ok=True,
        manifest_status=status,
        database_name=repair_scope.database_name,
        indexes_folder=paths.data_root / "raw" / "indexes",
        securities_folder=paths.data_root / "raw" / "securities",
        required_date_range=repair_scope.event_window_required_range,
        preferred_date_range=PREFERRED_DATE_RANGE,
        benchmark_rows=benchmark_rows,
        security_rows=security_rows,
    )


def benchmark_filename_patterns(index_code: str | None) -> str:
    """Return expected filename patterns for one benchmark index."""

    normalized = (index_code or "").upper()
    if normalized == "DJIA":
        return "DJI_*.csv; DJIA_*.csv"
    if normalized == "SP500":
        return "SPX_*.csv; SP500_*.csv"
    if normalized == "NASDAQ_COMP":
        return "IXIC_*.csv; NASDAQ_COMP_*.csv; NASDAQ_*.csv"
    if normalized:
        return f"{normalized}_*.csv; {normalized}_daily_*.csv"
    return "INDEX_*.csv; benchmark_*.csv"


def security_filename_patterns(ticker_symbol: str) -> str:
    """Return expected filename patterns for one security ticker."""

    ticker = ticker_symbol.upper()
    return f"{ticker}_*.csv; {ticker}_daily_*.csv"


def export_daily_price_source_manifest(
    result: DailyPriceSourceManifest,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DailyPriceSourceManifest:
    """Export daily source manifest artifacts."""

    if not result.connection_ok:
        return result

    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.reports_dir / "daily_price_source_manifest.json"
    benchmark_csv = paths.exports_dir / "benchmark_source_manifest.csv"
    security_csv = paths.exports_dir / "security_source_manifest.csv"
    checklist_md = paths.reports_dir / "daily_price_collection_checklist.md"
    export_paths = (json_path, benchmark_csv, security_csv, checklist_md)
    result_with_exports = replace(result, checklist_path=checklist_md, export_paths=export_paths)

    write_dataclass_json(json_path, result_with_exports)
    write_rows_csv(benchmark_csv, result.benchmark_rows, tuple(BenchmarkSourceManifestRow.__dataclass_fields__.keys()))
    write_rows_csv(security_csv, result.security_rows, tuple(SecuritySourceManifestRow.__dataclass_fields__.keys()))
    checklist_md.write_text(build_collection_checklist(result_with_exports), encoding="utf-8")
    if logger is not None:
        logger.info("Daily price source manifest exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def build_collection_checklist(result: DailyPriceSourceManifest) -> str:
    """Build the human-readable source-file collection checklist."""

    lines = [
        "# Daily Price Collection Checklist",
        "",
        "## Where Files Go",
        "",
        f"1. Put benchmark files in `{result.indexes_folder}`.",
        f"2. Put security files in `{result.securities_folder}`.",
        "3. Use daily OHLCV CSVs.",
        f"4. Required minimum date range is `{result.required_date_range}`.",
        f"5. Preferred date range is wider, ideally `{result.preferred_date_range}`, to support estimation windows.",
        "6. Do not edit the database manually.",
        "7. After placing files, rerun `2 -> 6 Validate Candidate Price CSVs`.",
        "",
        "## Required Columns",
        "",
        f"`{CSV_REQUIRED_COLUMNS}`",
        "",
        "## Benchmark Files Needed",
        "",
    ]
    if result.benchmark_rows:
        for row in result.benchmark_rows:
            lines.append(f"- `{row.index_code or row.market_index_id}`: `{row.expected_filename_patterns}` ({row.priority})")
    else:
        lines.append("- None.")

    lines.extend(["", "## Security Files Needed", ""])
    if result.security_rows:
        for row in result.security_rows:
            lines.append(f"- `{row.ticker_symbol}`: `{row.expected_filename_patterns}` ({row.priority})")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def format_daily_price_source_manifest(result: DailyPriceSourceManifest) -> list[str]:
    """Format source manifest for console output."""

    lines = ["", "Daily Price Source Manifest", "---------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    high_security_count = sum(1 for row in result.security_rows if row.priority == "high")
    lines.extend(
        [
            f"Overall status: {result.manifest_status}",
            f"Database: {result.database_name or 'Unknown'}",
            f"Benchmark files needed: {len(result.benchmark_rows):,}",
            f"Security files needed: {len(result.security_rows):,} ({high_security_count:,} high priority)",
            f"Required date range: {result.required_date_range}",
            f"Preferred date range: {result.preferred_date_range}",
            f"Benchmark folder: {result.indexes_folder}",
            f"Security folder: {result.securities_folder}",
            "",
            "Benchmark source files:",
        ]
    )
    for row in result.benchmark_rows:
        lines.append(
            "  "
            f"{row.index_code or row.market_index_id}: priority={row.priority}, patterns={row.expected_filename_patterns}, "
            f"target={row.canonical_target_table}"
        )

    lines.extend(["", "Top priority security files:"])
    for row in sorted(result.security_rows, key=lambda item: (0 if item.priority == "high" else 1, -item.affected_rows, item.ticker_symbol))[:20]:
        lines.append(
            "  "
            f"{row.ticker_symbol}: priority={row.priority}, exchange={row.exchange_code or 'Unknown'}, "
            f"affected_rows={row.affected_rows:,}, density={_fmt_float(row.current_density_pct)}%, "
            f"patterns={row.expected_filename_patterns}"
        )

    lines.extend(
        [
            "",
            "Next step after files are placed:",
            "  Run Market Data -> Validate Candidate Price CSVs (`2 -> 6`).",
        ]
    )
    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_daily_price_source_manifest(result: DailyPriceSourceManifest) -> None:
    """Print source manifest."""

    for line in format_daily_price_source_manifest(result):
        print(line)


def _benchmark_manifest_row(target: BenchmarkImportTarget) -> BenchmarkSourceManifestRow:
    notes_by_code = {
        "SP500": "SP500 should be the primary benchmark candidate after repair.",
        "DJIA": "DJIA can be used as continuity benchmark for the prior paper.",
        "NASDAQ_COMP": "NASDAQ_COMP can be used as a tech-heavy robustness benchmark.",
    }
    return BenchmarkSourceManifestRow(
        target.market_index_id,
        target.index_code,
        target.index_name,
        benchmark_filename_patterns(target.index_code),
        format_required_range(target.required_start_date, target.required_end_date),
        PREFERRED_DATE_RANGE,
        target.import_priority,
        target.canonical_table,
        CSV_REQUIRED_COLUMNS,
        notes_by_code.get((target.index_code or "").upper(), "Collect daily benchmark OHLCV data for validation."),
    )


def _security_manifest_row(target: SecurityImportTarget, exchange_code: str | None) -> SecuritySourceManifestRow:
    return SecuritySourceManifestRow(
        target.security_id,
        target.ticker_symbol,
        target.company_name,
        exchange_code,
        security_filename_patterns(target.ticker_symbol),
        format_required_range(target.required_start_date, target.required_end_date),
        PREFERRED_DATE_RANGE,
        target.import_priority,
        target.current_row_count,
        target.current_density_pct,
        target.affected_candidate_rows,
        target.affected_event_windows,
        "security_daily_prices",
        CSV_REQUIRED_COLUMNS,
    )


def _security_exchange_map(settings: AppSettings) -> dict[int, str | None]:
    with database_connection(settings) as connection:
        if not table_exists(connection, SECURITIES) or not table_exists(connection, EXCHANGES):
            return {}
        security_columns = set(get_table_columns(connection, SECURITIES))
        exchange_columns = set(get_table_columns(connection, EXCHANGES))
        if SECURITY_ID not in security_columns or EXCHANGE_ID not in security_columns:
            return {}
        exchange_label = EXCHANGE_CODE if EXCHANGE_CODE in exchange_columns else _first_existing(exchange_columns, ("exchange_name", "name"))
        if EXCHANGE_ID not in exchange_columns or not exchange_label:
            return {}
        rows = safe_fetch_all(
            connection,
            f"""
            SELECT s.{SECURITY_ID}, e.{exchange_label}
            FROM {SECURITIES} s
            LEFT JOIN {EXCHANGES} e ON e.{EXCHANGE_ID} = s.{EXCHANGE_ID}
            """,
        )
        return {int(row[0]): str(row[1]) if row[1] else None for row in rows if row[0] is not None}


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _fmt_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _log_report(result: DailyPriceSourceManifest, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Daily price source manifest failed: %s", result.error_message)
        return
    logger.info(
        "Daily price source manifest completed: database=%s status=%s benchmarks=%s securities=%s",
        result.database_name,
        result.manifest_status,
        len(result.benchmark_rows),
        len(result.security_rows),
    )
