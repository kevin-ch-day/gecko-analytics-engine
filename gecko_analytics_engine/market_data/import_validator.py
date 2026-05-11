"""Dry-run validator for candidate market price CSV imports."""

from __future__ import annotations

import csv
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
    WINDOW_END_DATE,
    WINDOW_START_DATE,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.market_data.index_audit import classify_gap_frequency
from gecko_analytics_engine.market_data.indexes import calculate_density_pct
from gecko_analytics_engine.utils.paths import AppPaths


DATE_COLUMNS = ("trade_date", "date", "timestamp", "time")
OPEN_COLUMNS = ("open_price", "open")
HIGH_COLUMNS = ("high_price", "high")
LOW_COLUMNS = ("low_price", "low")
CLOSE_COLUMNS = ("close_price", "close", "close_value", "last")
ADJUSTED_CLOSE_COLUMNS = ("adjusted_close", "adjusted_close_value", "adj_close", "adj close", "adjclose")
VOLUME_COLUMNS = ("volume", "vol")
SYMBOL_COLUMNS = ("ticker", "ticker_symbol", "symbol", "index_symbol", "market_index_symbol")
PRICE_FILE_TERMS = (
    "spx",
    "sp500",
    "s&p",
    "dji",
    "djia",
    "ixic",
    "nasdaq",
    "nasdaq_comp",
    "prices",
    "price",
    "daily",
)


@dataclass(frozen=True)
class KnownMarketSymbol:
    """Known security or index symbol from the database."""

    symbol: str
    entity_type: str
    entity_id: int
    display_name: str
    event_linked: bool = False


@dataclass(frozen=True)
class CandidatePriceFile:
    """Candidate local CSV file discovered for validation."""

    path: str
    area: str
    file_name: str
    size_bytes: int
    matched_terms: str


@dataclass(frozen=True)
class PriceFileProfile:
    """Profile of one candidate price CSV."""

    path: str
    file_name: str
    size_bytes: int
    detected_delimiter: str | None
    header_columns: str
    detected_date_column: str | None
    detected_open_column: str | None
    detected_high_column: str | None
    detected_low_column: str | None
    detected_close_column: str | None
    detected_adjusted_close_column: str | None
    detected_volume_column: str | None
    detected_symbol: str | None
    mapped_entity_type: str | None
    mapped_entity_id: int | None
    mapped_symbol: str | None
    min_date: str | None
    max_date: str | None
    row_count: int
    unique_date_count: int
    duplicate_date_count: int
    weekend_row_count: int
    weekday_distribution: str
    likely_frequency: str
    calendar_expected_trading_days: int | None
    calendar_density_pct: float | None
    status: str
    rejection_reason: str


@dataclass(frozen=True)
class PriceCoverageComparison:
    """Dry-run comparison of a candidate file to existing database coverage."""

    path: str
    mapped_entity_type: str | None
    mapped_entity_id: int | None
    mapped_symbol: str | None
    file_min_date: str | None
    file_max_date: str | None
    db_min_date: str | None
    db_max_date: str | None
    db_existing_dates_in_file_range: int | None
    file_dates_already_present: int | None
    file_dates_missing_from_db: int | None
    event_window_dates_filled: int | None
    benchmark_gap_dates_filled: int | None
    estimated_post_import_density_pct: float | None
    materially_improves_coverage: bool


@dataclass(frozen=True)
class PriceImportDryRunPlanRow:
    """One dry-run repair planning row for a candidate file."""

    path: str
    mapped_entity_type: str | None
    mapped_entity_id: int | None
    mapped_symbol: str | None
    decision: str
    priority: str
    reason: str
    file_frequency: str
    file_date_range: str
    missing_db_dates_filled: int | None
    event_window_dates_filled: int | None
    estimated_post_import_density_pct: float | None


@dataclass(frozen=True)
class PriceImportValidatorReport:
    """Dry-run candidate price import validator report."""

    generated_at: str
    connection_ok: bool
    validation_status: str
    database_name: str | None = None
    candidate_files: tuple[CandidatePriceFile, ...] = ()
    file_profiles: tuple[PriceFileProfile, ...] = ()
    coverage_comparisons: tuple[PriceCoverageComparison, ...] = ()
    dry_run_plan: tuple[PriceImportDryRunPlanRow, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_price_import_validator(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> PriceImportValidatorReport:
    """Run the dry-run price import validator."""

    generated_at = datetime.now(UTC).isoformat()
    candidates: tuple[CandidatePriceFile, ...] = ()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            known_symbols = _known_market_symbols(connection)
            candidates = discover_candidate_price_files(paths, known_symbols)
            profiles = tuple(profile_price_csv(Path(candidate.path), known_symbols, connection) for candidate in candidates)
            comparisons = tuple(_coverage_comparison(connection, profile) for profile in profiles)
            dry_run_plan = tuple(build_dry_run_plan_row(profile, comparison) for profile, comparison in zip(profiles, comparisons))
    except DatabaseConnectionError as exc:
        result = PriceImportValidatorReport(
            generated_at=generated_at,
            connection_ok=False,
            validation_status="BLOCKED",
            candidate_files=candidates,
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = PriceImportValidatorReport(
            generated_at=generated_at,
            connection_ok=False,
            validation_status="BLOCKED",
            candidate_files=candidates,
            error_message=f"Price import validator failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    result = PriceImportValidatorReport(
        generated_at=generated_at,
        connection_ok=True,
        validation_status=determine_validation_status(dry_run_plan),
        database_name=database_name,
        candidate_files=candidates,
        file_profiles=profiles,
        coverage_comparisons=comparisons,
        dry_run_plan=dry_run_plan,
    )
    result = export_price_import_validator_report(result, paths, logger)
    _log_report(result, logger)
    return result


def discover_candidate_price_files(
    paths: AppPaths,
    known_symbols: tuple[KnownMarketSymbol, ...] = (),
) -> tuple[CandidatePriceFile, ...]:
    """Discover local CSV files that look like price data."""

    symbol_terms = tuple(symbol.symbol.lower() for symbol in known_symbols)
    roots = (
        ("raw", paths.data_root / "raw"),
        ("external", paths.data_root / "external"),
        ("interim", paths.data_root / "interim"),
        ("processed", paths.data_root / "processed"),
    )
    results: list[CandidatePriceFile] = []
    for area, root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.csv")):
            lower_name = path.name.lower()
            matched = tuple(term for term in (*PRICE_FILE_TERMS, *symbol_terms) if term and term in lower_name)
            if not matched:
                continue
            results.append(
                CandidatePriceFile(
                    path=str(path),
                    area=area,
                    file_name=path.name,
                    size_bytes=path.stat().st_size,
                    matched_terms=", ".join(dict.fromkeys(matched)),
                )
            )
    return tuple(results)


def profile_price_csv(
    path: Path,
    known_symbols: tuple[KnownMarketSymbol, ...] = (),
    connection: Any | None = None,
) -> PriceFileProfile:
    """Profile one candidate CSV without importing it."""

    size_bytes = path.stat().st_size if path.exists() else 0
    delimiter = detect_delimiter(path)
    try:
        rows, headers = _read_csv_rows(path, delimiter)
    except Exception as exc:
        return _profile_rejected(path, size_bytes, delimiter, f"CSV read failed: {exc.__class__.__name__}: {exc}")

    normalized = {_normalize_header(header): header for header in headers}
    date_column = detect_column(headers, DATE_COLUMNS)
    open_column = detect_column(headers, OPEN_COLUMNS)
    high_column = detect_column(headers, HIGH_COLUMNS)
    low_column = detect_column(headers, LOW_COLUMNS)
    close_column = detect_column(headers, CLOSE_COLUMNS)
    adjusted_close_column = detect_column(headers, ADJUSTED_CLOSE_COLUMNS)
    volume_column = detect_column(headers, VOLUME_COLUMNS)
    symbol_column = detect_column(headers, SYMBOL_COLUMNS)
    detected_symbol = detect_symbol(path.name, rows, symbol_column, known_symbols)
    mapped = _map_symbol(detected_symbol, known_symbols)
    parsed_dates = _parsed_dates(rows, date_column)
    unique_dates = tuple(sorted(set(parsed_dates)))
    gaps = tuple((right - left).days for left, right in zip(unique_dates, unique_dates[1:]))
    weekday_distribution = _weekday_distribution(parsed_dates)
    expected_days = _expected_trading_days(connection, unique_dates[0], unique_dates[-1]) if connection is not None and unique_dates else None
    rejection_reasons = _profile_rejection_reasons(
        date_column,
        close_column,
        adjusted_close_column,
        rows,
        parsed_dates,
    )
    status = "REJECT" if rejection_reasons else "PROFILED"
    return PriceFileProfile(
        path=str(path),
        file_name=path.name,
        size_bytes=size_bytes,
        detected_delimiter=delimiter,
        header_columns=", ".join(headers),
        detected_date_column=date_column,
        detected_open_column=open_column,
        detected_high_column=high_column,
        detected_low_column=low_column,
        detected_close_column=close_column,
        detected_adjusted_close_column=adjusted_close_column,
        detected_volume_column=volume_column,
        detected_symbol=detected_symbol,
        mapped_entity_type=mapped.entity_type if mapped else None,
        mapped_entity_id=mapped.entity_id if mapped else None,
        mapped_symbol=mapped.symbol if mapped else None,
        min_date=str(unique_dates[0]) if unique_dates else None,
        max_date=str(unique_dates[-1]) if unique_dates else None,
        row_count=len(rows),
        unique_date_count=len(unique_dates),
        duplicate_date_count=max(len(parsed_dates) - len(unique_dates), 0),
        weekend_row_count=sum(1 for value in parsed_dates if value.weekday() >= 5),
        weekday_distribution=weekday_distribution,
        likely_frequency=classify_frequency_from_dates(unique_dates),
        calendar_expected_trading_days=expected_days,
        calendar_density_pct=calculate_density_pct(len(unique_dates), expected_days),
        status=status,
        rejection_reason="; ".join(rejection_reasons),
    )


def detect_delimiter(path: Path) -> str | None:
    """Detect a likely CSV delimiter."""

    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:4096]
    if not sample.strip():
        return None
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def detect_column(headers: tuple[str, ...], candidates: tuple[str, ...]) -> str | None:
    """Detect a column by normalized accepted names."""

    normalized_candidates = {_normalize_header(candidate) for candidate in candidates}
    for header in headers:
        if _normalize_header(header) in normalized_candidates:
            return header
    return None


def detect_symbol(
    file_name: str,
    rows: tuple[dict[str, str], ...],
    symbol_column: str | None,
    known_symbols: tuple[KnownMarketSymbol, ...],
) -> str | None:
    """Detect ticker/index symbol from filename or contents."""

    lower_name = file_name.lower()
    for symbol in sorted((item.symbol for item in known_symbols), key=len, reverse=True):
        if symbol.lower() in lower_name:
            return symbol
    aliases = {
        "spx": "SP500",
        "sp500": "SP500",
        "dji": "DJIA",
        "djia": "DJIA",
        "ixic": "NASDAQ_COMP",
        "nasdaq": "NASDAQ_COMP",
    }
    for token, symbol in aliases.items():
        if token in lower_name:
            return symbol
    if symbol_column and rows:
        values = [row.get(symbol_column, "").strip().upper() for row in rows[:25] if row.get(symbol_column, "").strip()]
        if values and len(set(values)) == 1:
            return values[0]
    return None


def classify_frequency_from_dates(dates: tuple[date, ...]) -> str:
    """Classify likely CSV frequency from unique dates."""

    if len(dates) < 2:
        return "unknown"
    gaps = tuple((right - left).days for left, right in zip(dates, dates[1:]))
    classification = classify_gap_frequency(gaps)
    if classification == "daily_like":
        return "daily"
    if classification == "weekly_like":
        return "weekly"
    if classification == "monthly_like":
        return "monthly"
    return "sparse/mixed"


def build_dry_run_plan_row(
    profile: PriceFileProfile,
    comparison: PriceCoverageComparison,
) -> PriceImportDryRunPlanRow:
    """Build a dry-run import planning row."""

    if profile.status == "REJECT":
        decision = "reject"
        priority = "reject"
        reason = profile.rejection_reason
    elif profile.mapped_entity_type is None:
        decision = "reject"
        priority = "reject"
        reason = "Could not map file to a known security or market index."
    elif profile.likely_frequency not in {"daily"}:
        decision = "reject"
        priority = "reject"
        reason = f"File appears {profile.likely_frequency}, not daily enough for event-study repair."
    elif (comparison.file_dates_missing_from_db or 0) <= 0:
        decision = "usable"
        priority = "low"
        reason = "File maps cleanly but appears mostly duplicate with existing DB dates."
    elif profile.mapped_entity_type == "index":
        decision = "usable"
        priority = "high"
        reason = "Daily benchmark file would repair benchmark coverage."
    elif (comparison.event_window_dates_filled or 0) > 0:
        decision = "usable"
        priority = "high"
        reason = "Daily security file would fill event-window coverage gaps."
    else:
        decision = "usable"
        priority = "medium"
        reason = "Daily security file improves coverage but is not currently event-window critical."

    date_range = f"{profile.min_date or 'Unknown'} to {profile.max_date or 'Unknown'}"
    return PriceImportDryRunPlanRow(
        path=profile.path,
        mapped_entity_type=profile.mapped_entity_type,
        mapped_entity_id=profile.mapped_entity_id,
        mapped_symbol=profile.mapped_symbol,
        decision=decision,
        priority=priority,
        reason=reason,
        file_frequency=profile.likely_frequency,
        file_date_range=date_range,
        missing_db_dates_filled=comparison.file_dates_missing_from_db,
        event_window_dates_filled=comparison.event_window_dates_filled,
        estimated_post_import_density_pct=comparison.estimated_post_import_density_pct,
    )


def determine_validation_status(plan: tuple[PriceImportDryRunPlanRow, ...]) -> str:
    """Return high-level validator status."""

    if not plan:
        return "NO_CANDIDATE_FILES"
    if any(row.priority == "high" for row in plan):
        return "HIGH_PRIORITY_REPAIR_CANDIDATES"
    if any(row.decision == "usable" for row in plan):
        return "USABLE_FILES_FOUND"
    return "NO_USABLE_FILES"


def export_price_import_validator_report(
    result: PriceImportValidatorReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> PriceImportValidatorReport:
    """Export dry-run validator artifacts."""

    if not result.connection_ok:
        return result
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.reports_dir / "price_import_validator_report.json"
    profiles_csv = paths.exports_dir / "price_import_file_profiles.csv"
    comparison_csv = paths.exports_dir / "price_import_coverage_comparison.csv"
    plan_csv = paths.exports_dir / "price_import_dry_run_plan.csv"
    export_paths = (json_path, profiles_csv, comparison_csv, plan_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    write_dataclass_json(json_path, result_with_exports)
    write_rows_csv(profiles_csv, result.file_profiles, _profile_fieldnames())
    write_rows_csv(comparison_csv, result.coverage_comparisons, _comparison_fieldnames())
    write_rows_csv(plan_csv, result.dry_run_plan, _plan_fieldnames())
    if logger is not None:
        logger.info("Price import validator exports written: %s", ", ".join(str(path) for path in export_paths))
    return result_with_exports


def format_price_import_validator_report(result: PriceImportValidatorReport) -> list[str]:
    """Format validator report for console output."""

    lines = ["", "Candidate Price CSV Validator", "-----------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    usable = [row for row in result.dry_run_plan if row.decision == "usable"]
    rejected = [row for row in result.dry_run_plan if row.decision == "reject"]
    daily = [row for row in result.file_profiles if row.likely_frequency == "daily"]
    high_priority = [row for row in result.dry_run_plan if row.priority == "high"]
    lines.extend(
        [
            f"Overall status: {result.validation_status}",
            f"Database: {result.database_name or 'Unknown'}",
            f"Candidate files discovered: {len(result.candidate_files):,}",
            f"Usable files: {len(usable):,}",
            f"Rejected files: {len(rejected):,}",
            f"Daily-like files: {len(daily):,}",
            f"High-priority repair candidates: {len(high_priority):,}",
        ]
    )
    lines.extend(["", "Candidate files:"])
    if result.candidate_files:
        lines.extend(f"  {candidate.path} [{candidate.matched_terms}]" for candidate in result.candidate_files[:25])
    else:
        lines.append("  None found under data/raw, data/external, data/interim, or data/processed.")

    if result.file_profiles:
        lines.extend(["", "Profile summary:"])
        for profile in result.file_profiles[:25]:
            lines.append(
                "  "
                f"{profile.file_name}: symbol={profile.detected_symbol or 'Unknown'}, "
                f"mapped={profile.mapped_entity_type or 'unmapped'}:{profile.mapped_symbol or 'Unknown'}, "
                f"rows={profile.row_count:,}, dates={profile.unique_date_count:,}, "
                f"range={profile.min_date or 'Unknown'} to {profile.max_date or 'Unknown'}, "
                f"frequency={profile.likely_frequency}, status={profile.status}"
            )

    if result.dry_run_plan:
        lines.extend(["", "Dry-run repair plan:"])
        for row in result.dry_run_plan[:25]:
            lines.append(
                "  "
                f"[{row.priority}] {row.mapped_symbol or Path(row.path).name}: {row.decision} - {row.reason} "
                f"(fills={_format_optional_int(row.missing_db_dates_filled)}, density_after={_format_optional_float(row.estimated_post_import_density_pct)}%)"
            )

    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_price_import_validator_report(result: PriceImportValidatorReport) -> None:
    """Print validator report."""

    for line in format_price_import_validator_report(result):
        print(line)


def _coverage_comparison(connection: Any, profile: PriceFileProfile) -> PriceCoverageComparison:
    file_dates = _dates_from_profile_file(profile)
    db_min, db_max = _db_date_range(connection, profile)
    existing_dates = _existing_db_dates(connection, profile, file_dates)
    expected_days = _expected_trading_days(connection, file_dates[0], file_dates[-1]) if file_dates else None
    merged_unique_count = _post_import_unique_count(connection, profile, file_dates)
    event_fills = _event_window_dates_filled(connection, profile, file_dates)
    benchmark_fills = _benchmark_gap_dates_filled(connection, profile, file_dates)
    density = calculate_density_pct(merged_unique_count, expected_days)
    missing_from_db = len(set(file_dates) - existing_dates) if profile.mapped_entity_type else None
    return PriceCoverageComparison(
        path=profile.path,
        mapped_entity_type=profile.mapped_entity_type,
        mapped_entity_id=profile.mapped_entity_id,
        mapped_symbol=profile.mapped_symbol,
        file_min_date=profile.min_date,
        file_max_date=profile.max_date,
        db_min_date=str(db_min) if db_min else None,
        db_max_date=str(db_max) if db_max else None,
        db_existing_dates_in_file_range=len(existing_dates) if profile.mapped_entity_type else None,
        file_dates_already_present=len(set(file_dates) & existing_dates) if profile.mapped_entity_type else None,
        file_dates_missing_from_db=missing_from_db,
        event_window_dates_filled=event_fills,
        benchmark_gap_dates_filled=benchmark_fills,
        estimated_post_import_density_pct=density,
        materially_improves_coverage=(missing_from_db or 0) > 0,
    )


def _known_market_symbols(connection: Any) -> tuple[KnownMarketSymbol, ...]:
    symbols: list[KnownMarketSymbol] = []
    linked_security_ids = {
        int(row[0])
        for row in safe_fetch_all(connection, "SELECT DISTINCT security_id FROM cyber_event_securities")
        if row[0] is not None
    } if table_exists(connection, "cyber_event_securities") else set()
    if table_exists(connection, SECURITIES):
        rows = safe_fetch_all(
            connection,
            f"SELECT {SECURITY_ID}, {TICKER_SYMBOL}, security_name FROM {SECURITIES} WHERE {TICKER_SYMBOL} IS NOT NULL",
        )
        symbols.extend(
            KnownMarketSymbol(str(symbol).upper(), "security", int(security_id), str(name or symbol), int(security_id) in linked_security_ids)
            for security_id, symbol, name in rows
            if symbol
        )
    if table_exists(connection, MARKET_INDEXES):
        columns = set(get_table_columns(connection, MARKET_INDEXES))
        symbol_column = _first_existing(columns, ("index_symbol", "symbol", "ticker_symbol", "ticker", "index_code"))
        name_column = _first_existing(columns, ("index_name", "name", "market_index_name", "description"))
        if MARKET_INDEX_ID in columns and symbol_column:
            rows = safe_fetch_all(
                connection,
                f"SELECT {MARKET_INDEX_ID}, {symbol_column}, {name_column if name_column else 'NULL'} FROM {MARKET_INDEXES}",
            )
            symbols.extend(
                KnownMarketSymbol(str(symbol).upper(), "index", int(index_id), str(name or symbol), True)
                for index_id, symbol, name in rows
                if symbol
            )
    return tuple(symbols)


def _read_csv_rows(path: Path, delimiter: str | None) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as file:
        reader = csv.DictReader(file, delimiter=delimiter or ",")
        headers = tuple(reader.fieldnames or ())
        rows = tuple(dict(row) for row in reader)
    return rows, headers


def _parsed_dates(rows: tuple[dict[str, str], ...], date_column: str | None) -> tuple[date, ...]:
    if not date_column:
        return ()
    result: list[date] = []
    for row in rows:
        parsed = _parse_date(row.get(date_column, ""))
        if parsed is not None:
            result.append(parsed)
    return tuple(result)


def _parse_date(raw_value: str) -> date | None:
    value = str(raw_value).strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _profile_rejection_reasons(
    date_column: str | None,
    close_column: str | None,
    adjusted_close_column: str | None,
    rows: tuple[dict[str, str], ...],
    parsed_dates: tuple[date, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not rows:
        reasons.append("No data rows found.")
    if not date_column:
        reasons.append("No accepted date column found.")
    if not close_column and not adjusted_close_column:
        reasons.append("No close or adjusted close column found.")
    if date_column and rows and not parsed_dates:
        reasons.append("Date column exists but no parseable dates were found.")
    return tuple(reasons)


def _profile_rejected(path: Path, size_bytes: int, delimiter: str | None, reason: str) -> PriceFileProfile:
    return PriceFileProfile(
        path=str(path),
        file_name=path.name,
        size_bytes=size_bytes,
        detected_delimiter=delimiter,
        header_columns="",
        detected_date_column=None,
        detected_open_column=None,
        detected_high_column=None,
        detected_low_column=None,
        detected_close_column=None,
        detected_adjusted_close_column=None,
        detected_volume_column=None,
        detected_symbol=None,
        mapped_entity_type=None,
        mapped_entity_id=None,
        mapped_symbol=None,
        min_date=None,
        max_date=None,
        row_count=0,
        unique_date_count=0,
        duplicate_date_count=0,
        weekend_row_count=0,
        weekday_distribution="",
        likely_frequency="unknown",
        calendar_expected_trading_days=None,
        calendar_density_pct=None,
        status="REJECT",
        rejection_reason=reason,
    )


def _dates_from_profile_file(profile: PriceFileProfile) -> tuple[date, ...]:
    if not profile.detected_date_column:
        return ()
    rows, _ = _read_csv_rows(Path(profile.path), profile.detected_delimiter)
    return tuple(sorted(set(_parsed_dates(rows, profile.detected_date_column))))


def _db_date_range(connection: Any, profile: PriceFileProfile) -> tuple[Any | None, Any | None]:
    table, id_column = _price_table_and_id_column(profile)
    if not table or not id_column or profile.mapped_entity_id is None or not table_exists(connection, table):
        return None, None
    value = safe_fetch_all(
        connection,
        f"SELECT MIN({TRADE_DATE}), MAX({TRADE_DATE}) FROM {table} WHERE {id_column} = %s",
        (profile.mapped_entity_id,),
    )
    if not value:
        return None, None
    return value[0][0], value[0][1]


def _existing_db_dates(connection: Any, profile: PriceFileProfile, file_dates: tuple[date, ...]) -> set[date]:
    table, id_column = _price_table_and_id_column(profile)
    if not table or not id_column or profile.mapped_entity_id is None or not file_dates or not table_exists(connection, table):
        return set()
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {TRADE_DATE}
        FROM {table}
        WHERE {id_column} = %s
          AND {TRADE_DATE} BETWEEN %s AND %s
        """,
        (profile.mapped_entity_id, file_dates[0], file_dates[-1]),
    )
    return {row[0] for row in rows if isinstance(row[0], date)}


def _post_import_unique_count(connection: Any, profile: PriceFileProfile, file_dates: tuple[date, ...]) -> int | None:
    table, id_column = _price_table_and_id_column(profile)
    if not table or not id_column or profile.mapped_entity_id is None or not file_dates or not table_exists(connection, table):
        return None
    rows = safe_fetch_all(
        connection,
        f"SELECT DISTINCT {TRADE_DATE} FROM {table} WHERE {id_column} = %s AND {TRADE_DATE} BETWEEN %s AND %s",
        (profile.mapped_entity_id, file_dates[0], file_dates[-1]),
    )
    return len({row[0] for row in rows if isinstance(row[0], date)} | set(file_dates))


def _event_window_dates_filled(connection: Any, profile: PriceFileProfile, file_dates: tuple[date, ...]) -> int | None:
    if profile.mapped_entity_type != "security" or profile.mapped_entity_id is None or not file_dates or not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None
    existing = _existing_db_dates(connection, profile, file_dates)
    candidate_new_dates = set(file_dates) - existing
    if not candidate_new_dates:
        return 0
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT {WINDOW_START_DATE}, {WINDOW_END_DATE}
        FROM {VW_EVENT_WINDOW_BOUNDARIES}
        WHERE {SECURITY_ID} = %s
        """,
        (profile.mapped_entity_id,),
    )
    count = 0
    for value in candidate_new_dates:
        if any(start <= value <= end for start, end in rows if start and end):
            count += 1
    return count


def _benchmark_gap_dates_filled(connection: Any, profile: PriceFileProfile, file_dates: tuple[date, ...]) -> int | None:
    if profile.mapped_entity_type != "index" or profile.mapped_entity_id is None or not file_dates or not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return None
    existing = _existing_db_dates(connection, profile, file_dates)
    candidate_new_dates = set(file_dates) - existing
    if not candidate_new_dates:
        return 0
    rows = safe_fetch_all(connection, f"SELECT {WINDOW_START_DATE}, {WINDOW_END_DATE} FROM {VW_EVENT_WINDOW_BOUNDARIES}")
    count = 0
    for value in candidate_new_dates:
        if any(start <= value <= end for start, end in rows if start and end):
            count += 1
    return count


def _price_table_and_id_column(profile: PriceFileProfile) -> tuple[str | None, str | None]:
    if profile.mapped_entity_type == "security":
        return SECURITY_DAILY_PRICES, SECURITY_ID
    if profile.mapped_entity_type == "index":
        return INDEX_DAILY_PRICES, MARKET_INDEX_ID
    return None, None


def _expected_trading_days(connection: Any | None, min_date: date, max_date: date) -> int | None:
    if connection is None or not table_exists(connection, MARKET_CALENDAR):
        return None
    columns = set(get_table_columns(connection, MARKET_CALENDAR))
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(columns):
        return None
    value = safe_scalar(
        connection,
        f"SELECT COUNT(*) FROM {MARKET_CALENDAR} WHERE {CALENDAR_DATE} BETWEEN %s AND %s AND {IS_TRADING_DAY} = 1",
        (min_date, max_date),
    )
    return int(value) if value is not None else None


def _map_symbol(symbol: str | None, known_symbols: tuple[KnownMarketSymbol, ...]) -> KnownMarketSymbol | None:
    if not symbol:
        return None
    normalized = symbol.upper()
    aliases = {"SPX": "SP500", "DJI": "DJIA", "IXIC": "NASDAQ_COMP", "NASDAQ": "NASDAQ_COMP"}
    normalized = aliases.get(normalized, normalized)
    return next((item for item in known_symbols if item.symbol.upper() == normalized), None)


def _weekday_distribution(dates: tuple[date, ...]) -> str:
    labels = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    counts: dict[str, int] = {}
    for value in dates:
        label = labels[value.weekday()]
        counts[label] = counts.get(label, 0) + 1
    return "; ".join(f"{label}={counts[label]}" for label in labels if label in counts)


def _normalize_header(value: str) -> str:
    return value.strip().lower().replace("_", " ").replace("-", " ")


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _profile_fieldnames() -> tuple[str, ...]:
    return tuple(PriceFileProfile.__dataclass_fields__.keys())


def _comparison_fieldnames() -> tuple[str, ...]:
    return tuple(PriceCoverageComparison.__dataclass_fields__.keys())


def _plan_fieldnames() -> tuple[str, ...]:
    return tuple(PriceImportDryRunPlanRow.__dataclass_fields__.keys())


def _format_optional_int(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def _format_optional_float(value: float | None) -> str:
    return "Unknown" if value is None else f"{value:,.2f}"


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: PriceImportValidatorReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Price import validator failed: %s", result.error_message)
        return
    logger.info(
        "Price import validator completed: database=%s status=%s candidates=%s usable=%s",
        result.database_name,
        result.validation_status,
        len(result.candidate_files),
        sum(1 for row in result.dry_run_plan if row.decision == "usable"),
    )
