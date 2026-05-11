"""Read-only event-study dataset exclusion review."""

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
    COMPANIES,
    CYBER_EVENTS,
    CYBER_EVENT_ID,
    DISCLOSURE_DATE,
    FIRST_TRADING_DAY,
    INDEX_DAILY_PRICES,
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
from gecko_analytics_engine.event_study.study_design import (
    MISSING_BENCHMARK_PRICE,
    MISSING_EVENT_DATE,
    MISSING_SECURITY_PRICE,
    MISSING_WINDOW_BOUNDARY,
    EventStudyDesign,
    default_event_study_design,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.market_data.indexes import (
    BenchmarkCoverageReport,
    build_benchmark_coverage_report,
    format_benchmark_coverage_report,
)
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class ExclusionReviewRow:
    """Detailed excluded event-study candidate row."""

    exclusion_reason: str
    cyber_event_id: int | None
    event_title: str | None
    security_id: int | None
    ticker_symbol: str | None
    company_name: str | None
    window_code: str | None
    event_date: str | None
    aligned_event_date: str | None
    window_start_date: str | None
    window_end_date: str | None
    security_price_observations: int
    benchmark_price_observations: int
    security_price_total_rows: int
    benchmark_price_total_rows: int
    security_gap_scope: str
    benchmark_gap_scope: str


@dataclass(frozen=True)
class ExclusionSummaryRow:
    """Count summary row for exclusions."""

    category: str
    label: str
    count: int


@dataclass(frozen=True)
class ExclusionReviewIssue:
    """Exclusion review warning, blocker, or note."""

    severity: str
    message: str


@dataclass(frozen=True)
class DatasetExclusionReview:
    """Read-only dataset exclusion review report."""

    generated_at: str
    connection_ok: bool
    review_status: str
    database_name: str | None = None
    total_exclusions: int = 0
    missing_both_security_and_benchmark: int = 0
    events_with_no_eligible_rows: int = 0
    reason_counts: tuple[ExclusionSummaryRow, ...] = ()
    top_events: tuple[ExclusionSummaryRow, ...] = ()
    top_securities: tuple[ExclusionSummaryRow, ...] = ()
    top_tickers: tuple[ExclusionSummaryRow, ...] = ()
    window_counts: tuple[ExclusionSummaryRow, ...] = ()
    benchmark_gap_date_ranges: tuple[ExclusionSummaryRow, ...] = ()
    top_exclusions: tuple[ExclusionReviewRow, ...] = ()
    benchmark_coverage: BenchmarkCoverageReport | None = None
    issues: tuple[ExclusionReviewIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_dataset_exclusion_review(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
    design: EventStudyDesign | None = None,
) -> DatasetExclusionReview:
    """Run and export the read-only dataset exclusion review."""

    active_design = design or default_event_study_design()
    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            rows = _fetch_exclusion_rows(connection, active_design)
            events_with_no_eligible_rows = _events_with_no_eligible_rows(connection, rows)
            benchmark_coverage = build_benchmark_coverage_report(connection, generated_at, database_name)
    except DatabaseConnectionError as exc:
        result = DatasetExclusionReview(
            generated_at=generated_at,
            connection_ok=False,
            review_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = DatasetExclusionReview(
            generated_at=generated_at,
            connection_ok=False,
            review_status="BLOCKED",
            error_message=f"Dataset exclusion review failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_exclusion_review_issues(rows, benchmark_coverage)
    result = DatasetExclusionReview(
        generated_at=generated_at,
        connection_ok=True,
        review_status=determine_exclusion_review_status(issues),
        database_name=database_name,
        total_exclusions=len(rows),
        missing_both_security_and_benchmark=sum(
            1
            for row in rows
            if MISSING_SECURITY_PRICE in row.exclusion_reason and MISSING_BENCHMARK_PRICE in row.exclusion_reason
        ),
        events_with_no_eligible_rows=events_with_no_eligible_rows,
        reason_counts=_count_split_reasons(rows),
        top_events=_top_counts(rows, "event"),
        top_securities=_top_counts(rows, "security"),
        top_tickers=_top_counts(rows, "ticker"),
        window_counts=_top_counts(rows, "window"),
        benchmark_gap_date_ranges=_benchmark_gap_date_ranges(rows),
        top_exclusions=rows[:25],
        benchmark_coverage=benchmark_coverage,
        issues=issues,
    )
    result = export_dataset_exclusion_review(result, rows, paths, logger)
    _log_report(result, logger)
    return result


def export_dataset_exclusion_review(
    result: DatasetExclusionReview,
    rows: tuple[ExclusionReviewRow, ...],
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DatasetExclusionReview:
    """Export dataset exclusion review artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "event_study_exclusion_review.json"
    review_csv = paths.exports_dir / "event_study_exclusion_review.csv"
    benchmark_csv = paths.exports_dir / "benchmark_coverage_detail.csv"
    export_paths = (json_path, review_csv, benchmark_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    write_rows_csv(review_csv, rows, _exclusion_fieldnames())
    write_rows_csv(
        benchmark_csv,
        result.benchmark_coverage.index_rows if result.benchmark_coverage else (),
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
    write_dataclass_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Dataset exclusion review exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )
    return result_with_exports


def format_dataset_exclusion_review(result: DatasetExclusionReview) -> list[str]:
    """Format the exclusion review for console output."""

    lines = ["", "Dataset Exclusion Review", "------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            f"Overall status: {result.review_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Summary:",
            f"  Total exclusions: {result.total_exclusions:,}",
            f"  Rows missing both security and benchmark prices: {result.missing_both_security_and_benchmark:,}",
            f"  Events with no eligible rows: {result.events_with_no_eligible_rows:,}",
        ]
    )

    _append_summary_section(lines, "Top exclusion reasons:", result.reason_counts)
    _append_summary_section(lines, "Top excluded events:", result.top_events)
    _append_summary_section(lines, "Top excluded securities:", result.top_securities)
    _append_summary_section(lines, "Top excluded tickers:", result.top_tickers)
    _append_summary_section(lines, "Excluded rows by window:", result.window_counts)
    _append_summary_section(lines, "Benchmark gap date ranges:", result.benchmark_gap_date_ranges)

    if result.top_exclusions:
        lines.extend(["", "Top exclusion rows:"])
        for row in result.top_exclusions:
            lines.append(
                "  "
                f"event={row.cyber_event_id}, ticker={row.ticker_symbol or 'Unknown'}, "
                f"window={row.window_code}, reason={row.exclusion_reason}, "
                f"security_scope={row.security_gap_scope}, benchmark_scope={row.benchmark_gap_scope}"
            )

    if result.benchmark_coverage is not None:
        lines.extend(format_benchmark_coverage_report(result.benchmark_coverage))

    lines.extend(["", "Issues / notes:"])
    if result.issues:
        lines.extend(f"  [{issue.severity}] {issue.message}" for issue in result.issues)
    else:
        lines.append("  No exclusion review blockers detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        lines.extend(f"  {path}" for path in result.export_paths)
    return lines


def print_dataset_exclusion_review(result: DatasetExclusionReview) -> None:
    """Print dataset exclusion review."""

    for line in format_dataset_exclusion_review(result):
        print(line)


def determine_exclusion_review_status(issues: tuple[ExclusionReviewIssue, ...]) -> str:
    """Return exclusion review status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if any(issue.severity == "WARNING" for issue in issues):
        return "NEEDS_REVIEW"
    return "OK"


def build_exclusion_review_issues(
    rows: tuple[ExclusionReviewRow, ...],
    benchmark_coverage: BenchmarkCoverageReport | None,
) -> tuple[ExclusionReviewIssue, ...]:
    """Build issues from exclusion and benchmark coverage data."""

    issues: list[ExclusionReviewIssue] = []
    if not rows:
        issues.append(ExclusionReviewIssue("INFO", "No excluded candidate rows were found."))
    else:
        issues.append(ExclusionReviewIssue("WARNING", f"{len(rows)} candidate rows are excluded from the current dataset preview."))
    if benchmark_coverage is None or benchmark_coverage.recommended_benchmark_id is None:
        issues.append(ExclusionReviewIssue("BLOCKER", "No recommended benchmark candidate is available."))
    elif benchmark_coverage.dji_daily_price_rows == 0:
        issues.append(
            ExclusionReviewIssue(
                "INFO",
                "dji_daily_prices is empty, but benchmark data appears available in index_daily_prices.",
            )
        )
    return tuple(issues)


def _fetch_exclusion_rows(connection: Any, design: EventStudyDesign) -> tuple[ExclusionReviewRow, ...]:
    if not _has_required_tables_and_columns(connection):
        return ()

    event_title_expr, event_join = _event_title_sql(connection)
    company_name_expr, company_join = _company_name_sql(connection)
    total_benchmark_rows = count_rows(connection, INDEX_DAILY_PRICES) or 0
    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            ewb.{CYBER_EVENT_ID},
            {event_title_expr} AS event_title,
            ewb.{SECURITY_ID},
            s.{TICKER_SYMBOL},
            {company_name_expr} AS company_name,
            ewb.{DISCLOSURE_DATE},
            ewb.{FIRST_TRADING_DAY},
            ewb.{WINDOW_CODE},
            ewb.{WINDOW_START_DATE},
            ewb.{WINDOW_END_DATE},
            (
                SELECT COUNT(DISTINCT sdp.{TRADE_DATE})
                FROM {SECURITY_DAILY_PRICES} sdp
                WHERE sdp.{SECURITY_ID} = ewb.{SECURITY_ID}
                  AND sdp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
            ) AS security_price_observations,
            (
                SELECT COUNT(DISTINCT idp.{TRADE_DATE})
                FROM {INDEX_DAILY_PRICES} idp
                WHERE idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
            ) AS benchmark_price_observations,
            (
                SELECT COUNT(*)
                FROM {SECURITY_DAILY_PRICES} sdp_all
                WHERE sdp_all.{SECURITY_ID} = ewb.{SECURITY_ID}
            ) AS security_price_total_rows
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        LEFT JOIN {SECURITIES} s ON s.{SECURITY_ID} = ewb.{SECURITY_ID}
        {company_join}
        {event_join}
        ORDER BY ewb.{CYBER_EVENT_ID}, ewb.{SECURITY_ID}, ewb.{WINDOW_CODE}
        """,
    )

    result: list[ExclusionReviewRow] = []
    for row in rows:
        security_observations = int(row[10] or 0)
        benchmark_observations = int(row[11] or 0)
        security_total = int(row[12] or 0)
        reason = _exclusion_reason(
            security_observations,
            benchmark_observations,
            str(row[5]) if row[5] else None,
            str(row[6]) if row[6] else None,
            str(row[8]) if row[8] else None,
            str(row[9]) if row[9] else None,
            design,
        )
        if not reason:
            continue
        result.append(
            ExclusionReviewRow(
                exclusion_reason=reason,
                cyber_event_id=int(row[0]) if row[0] is not None else None,
                event_title=str(row[1]) if row[1] else None,
                security_id=int(row[2]) if row[2] is not None else None,
                ticker_symbol=str(row[3]) if row[3] else None,
                company_name=str(row[4]) if row[4] else None,
                window_code=str(row[7]) if row[7] else None,
                event_date=str(row[5]) if row[5] else None,
                aligned_event_date=str(row[6]) if row[6] else None,
                window_start_date=str(row[8]) if row[8] else None,
                window_end_date=str(row[9]) if row[9] else None,
                security_price_observations=security_observations,
                benchmark_price_observations=benchmark_observations,
                security_price_total_rows=security_total,
                benchmark_price_total_rows=total_benchmark_rows,
                security_gap_scope=_gap_scope(security_observations, security_total),
                benchmark_gap_scope=_gap_scope(benchmark_observations, total_benchmark_rows),
            )
        )
    return tuple(result)


def _has_required_tables_and_columns(connection: Any) -> bool:
    for table_name in (VW_EVENT_WINDOW_BOUNDARIES, SECURITIES, SECURITY_DAILY_PRICES, INDEX_DAILY_PRICES):
        if not table_exists(connection, table_name):
            return False
    boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    securities_columns = set(get_table_columns(connection, SECURITIES))
    security_price_columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    index_price_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    return (
        {CYBER_EVENT_ID, SECURITY_ID, DISCLOSURE_DATE, FIRST_TRADING_DAY, WINDOW_CODE, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns)
        and {SECURITY_ID, TICKER_SYMBOL}.issubset(securities_columns)
        and {SECURITY_ID, TRADE_DATE}.issubset(security_price_columns)
        and TRADE_DATE in index_price_columns
    )


def _event_title_sql(connection: Any) -> tuple[str, str]:
    if not table_exists(connection, CYBER_EVENTS):
        return "NULL", ""
    columns = set(get_table_columns(connection, CYBER_EVENTS))
    if CYBER_EVENT_ID not in columns:
        return "NULL", ""
    title_column = _first_existing(columns, ("event_title", "title", "event_name", "name", "summary"))
    title_expr = f"ce.{title_column}" if title_column else "NULL"
    return title_expr, f"LEFT JOIN {CYBER_EVENTS} ce ON ce.{CYBER_EVENT_ID} = ewb.{CYBER_EVENT_ID}"


def _company_name_sql(connection: Any) -> tuple[str, str]:
    if not table_exists(connection, COMPANIES):
        return "NULL", ""
    security_columns = set(get_table_columns(connection, SECURITIES))
    company_columns = set(get_table_columns(connection, COMPANIES))
    if "company_id" not in security_columns or "company_id" not in company_columns:
        return "NULL", ""
    name_column = _first_existing(company_columns, ("company_name", "name", "legal_name", "display_name"))
    name_expr = f"c.{name_column}" if name_column else "NULL"
    return name_expr, f"LEFT JOIN {COMPANIES} c ON c.company_id = s.company_id"


def _exclusion_reason(
    security_observations: int,
    benchmark_observations: int,
    event_date: str | None,
    aligned_event_date: str | None,
    window_start_date: str | None,
    window_end_date: str | None,
    design: EventStudyDesign,
) -> str:
    reasons: list[str] = []
    if event_date is None or aligned_event_date is None:
        reasons.append(MISSING_EVENT_DATE)
    if window_start_date is None or window_end_date is None:
        reasons.append(MISSING_WINDOW_BOUNDARY)
    if security_observations < design.minimum_security_price_observations:
        reasons.append(MISSING_SECURITY_PRICE)
    if benchmark_observations < design.minimum_benchmark_price_observations:
        reasons.append(MISSING_BENCHMARK_PRICE)
    return ";".join(reasons)


def _events_with_no_eligible_rows(connection: Any, rows: tuple[ExclusionReviewRow, ...]) -> int:
    excluded_counts: dict[int, int] = {}
    for row in rows:
        if row.cyber_event_id is not None:
            excluded_counts[row.cyber_event_id] = excluded_counts.get(row.cyber_event_id, 0) + 1
    if not excluded_counts:
        return 0
    total_rows = safe_fetch_all(
        connection,
        f"""
        SELECT {CYBER_EVENT_ID}, COUNT(*)
        FROM {VW_EVENT_WINDOW_BOUNDARIES}
        GROUP BY {CYBER_EVENT_ID}
        """,
    )
    totals = {int(row[0]): int(row[1]) for row in total_rows if row[0] is not None}
    return sum(1 for event_id, excluded_count in excluded_counts.items() if excluded_count >= totals.get(event_id, 0))


def _count_split_reasons(rows: tuple[ExclusionReviewRow, ...]) -> tuple[ExclusionSummaryRow, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.exclusion_reason.split(";"):
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
    return tuple(ExclusionSummaryRow("reason", label, count) for label, count in _sorted_counts(counts))


def _top_counts(rows: tuple[ExclusionReviewRow, ...], category: str) -> tuple[ExclusionSummaryRow, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        if category == "event":
            label = f"{row.cyber_event_id}: {row.event_title or 'Untitled'}"
        elif category == "security":
            label = str(row.security_id)
        elif category == "ticker":
            label = row.ticker_symbol or "Unknown"
        elif category == "window":
            label = row.window_code or "Unknown"
        else:
            label = "Unknown"
        counts[label] = counts.get(label, 0) + 1
    return tuple(ExclusionSummaryRow(category, label, count) for label, count in _sorted_counts(counts)[:10])


def _benchmark_gap_date_ranges(rows: tuple[ExclusionReviewRow, ...]) -> tuple[ExclusionSummaryRow, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        if MISSING_BENCHMARK_PRICE not in row.exclusion_reason:
            continue
        label = f"{row.window_start_date or 'Unknown'} to {row.window_end_date or 'Unknown'}"
        counts[label] = counts.get(label, 0) + 1
    return tuple(ExclusionSummaryRow("benchmark_gap_date_range", label, count) for label, count in _sorted_counts(counts)[:10])


def _sorted_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _gap_scope(window_observations: int, total_rows: int) -> str:
    if window_observations > 0:
        return "covered"
    if total_rows == 0:
        return "missing_entirely"
    return "missing_for_window"


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _append_summary_section(lines: list[str], title: str, rows: tuple[ExclusionSummaryRow, ...]) -> None:
    if not rows:
        return
    lines.extend(["", title])
    for row in rows:
        lines.append(f"  {row.label}: {row.count:,}")


def _exclusion_fieldnames() -> tuple[str, ...]:
    return (
        "exclusion_reason",
        "cyber_event_id",
        "event_title",
        "security_id",
        "ticker_symbol",
        "company_name",
        "window_code",
        "event_date",
        "aligned_event_date",
        "window_start_date",
        "window_end_date",
        "security_price_observations",
        "benchmark_price_observations",
        "security_price_total_rows",
        "benchmark_price_total_rows",
        "security_gap_scope",
        "benchmark_gap_scope",
    )


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: DatasetExclusionReview, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Dataset exclusion review failed: %s", result.error_message)
        return
    logger.info(
        "Dataset exclusion review completed: database=%s status=%s exclusions=%s",
        result.database_name,
        result.review_status,
        result.total_exclusions,
    )
