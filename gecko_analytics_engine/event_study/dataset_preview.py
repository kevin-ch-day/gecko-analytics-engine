"""Read-only event-study dataset eligibility preview."""

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
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class EventStudyCandidateRow:
    """One candidate event/security/window row for future event-study calculations."""

    cyber_event_id: int | None
    security_id: int | None
    ticker_symbol: str | None
    event_date: str | None
    event_date_type: str | None
    aligned_event_date: str | None
    window_code: str | None
    window_start_date: str | None
    window_end_date: str | None
    security_price_observations: int
    benchmark_price_observations: int
    eligible: bool
    exclusion_reason: str


@dataclass(frozen=True)
class DatasetPreviewIssue:
    """Dataset preview warning, blocker, or note."""

    severity: str
    message: str


@dataclass(frozen=True)
class EventStudyDatasetPreview:
    """Read-only event-study dataset preview report."""

    generated_at: str
    connection_ok: bool
    preview_status: str
    database_name: str | None = None
    design: EventStudyDesign = EventStudyDesign()
    total_candidates: int = 0
    eligible_candidates: int = 0
    excluded_candidates: int = 0
    missing_security_price_candidates: int = 0
    missing_benchmark_price_candidates: int = 0
    missing_event_date_candidates: int = 0
    missing_window_boundary_candidates: int = 0
    affected_events: int = 0
    affected_securities: int = 0
    eligibility_by_window: tuple[tuple[str, int, int], ...] = ()
    exclusion_reason_counts: tuple[tuple[str, int], ...] = ()
    linked_securities_without_price_coverage: int = 0
    events_without_eligible_windows: int = 0
    event_date_alignment_status: str = "Unknown"
    benchmark_availability: str = "Unknown"
    top_exclusions: tuple[EventStudyCandidateRow, ...] = ()
    issues: tuple[DatasetPreviewIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_event_study_dataset_preview(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
    design: EventStudyDesign | None = None,
) -> EventStudyDatasetPreview:
    """Run and export a read-only event-study dataset eligibility preview."""

    active_design = design or default_event_study_design()
    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            rows = _fetch_candidate_rows(connection, active_design)
    except DatabaseConnectionError as exc:
        result = EventStudyDatasetPreview(
            generated_at=generated_at,
            connection_ok=False,
            preview_status="BLOCKED",
            design=active_design,
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = EventStudyDatasetPreview(
            generated_at=generated_at,
            connection_ok=False,
            preview_status="BLOCKED",
            design=active_design,
            error_message=f"Event-study dataset preview failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_dataset_preview_issues(rows)
    result = EventStudyDatasetPreview(
        generated_at=generated_at,
        connection_ok=True,
        preview_status=determine_dataset_preview_status(issues),
        database_name=database_name,
        design=active_design,
        total_candidates=len(rows),
        eligible_candidates=sum(1 for row in rows if row.eligible),
        excluded_candidates=sum(1 for row in rows if not row.eligible),
        missing_security_price_candidates=sum(1 for row in rows if row.security_price_observations < active_design.minimum_security_price_observations),
        missing_benchmark_price_candidates=sum(1 for row in rows if row.benchmark_price_observations < active_design.minimum_benchmark_price_observations),
        missing_event_date_candidates=sum(1 for row in rows if MISSING_EVENT_DATE in row.exclusion_reason),
        missing_window_boundary_candidates=sum(1 for row in rows if MISSING_WINDOW_BOUNDARY in row.exclusion_reason),
        affected_events=len({row.cyber_event_id for row in rows if not row.eligible and row.cyber_event_id is not None}),
        affected_securities=len({row.security_id for row in rows if not row.eligible and row.security_id is not None}),
        eligibility_by_window=_eligibility_by_window(rows),
        exclusion_reason_counts=_exclusion_reason_counts(rows),
        linked_securities_without_price_coverage=len(
            {
                row.security_id
                for row in rows
                if row.security_id is not None
                and row.security_price_observations < active_design.minimum_security_price_observations
            }
        ),
        events_without_eligible_windows=_events_without_eligible_windows(rows),
        event_date_alignment_status=_event_date_alignment_status(rows),
        benchmark_availability=_benchmark_availability(rows, active_design),
        top_exclusions=tuple(row for row in rows if not row.eligible)[:25],
        issues=issues,
    )
    result = export_event_study_dataset_preview(result, rows, paths, logger)
    _log_report(result, logger)
    return result


def export_event_study_dataset_preview(
    result: EventStudyDatasetPreview,
    rows: tuple[EventStudyCandidateRow, ...],
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> EventStudyDatasetPreview:
    """Export event-study dataset preview artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "event_study_dataset_preview.json"
    candidates_csv = paths.exports_dir / "event_study_dataset_candidates.csv"
    exclusions_csv = paths.exports_dir / "event_study_dataset_exclusions.csv"
    export_paths = (json_path, candidates_csv, exclusions_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    _write_candidates_csv(candidates_csv, rows)
    _write_candidates_csv(exclusions_csv, tuple(row for row in rows if not row.eligible))
    write_dataclass_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Event-study dataset preview exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )
    return result_with_exports


def format_event_study_dataset_preview(result: EventStudyDatasetPreview) -> list[str]:
    """Format the event-study dataset preview for console output."""

    lines = ["", "Event Study Dataset Preview", "---------------------------"]
    if not result.connection_ok:
        lines.extend(["Overall status: BLOCKED", "Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            f"Overall status: {result.preview_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            f"Study design: {result.design.name}",
            "",
            "Summary:",
            f"  Candidate rows: {result.total_candidates:,}",
            f"  Eligible rows: {result.eligible_candidates:,}",
            f"  Excluded rows: {result.excluded_candidates:,}",
            f"  Missing security-price rows: {result.missing_security_price_candidates:,}",
            f"  Missing benchmark-price rows: {result.missing_benchmark_price_candidates:,}",
            f"  Missing event-date rows: {result.missing_event_date_candidates:,}",
            f"  Missing window-boundary rows: {result.missing_window_boundary_candidates:,}",
            f"  Affected events: {result.affected_events:,}",
            f"  Affected securities: {result.affected_securities:,}",
            f"  Linked securities without price coverage: {result.linked_securities_without_price_coverage:,}",
            f"  Events without eligible windows: {result.events_without_eligible_windows:,}",
            f"  Event date alignment status: {result.event_date_alignment_status}",
            f"  Benchmark availability: {result.benchmark_availability}",
        ]
    )

    if result.exclusion_reason_counts:
        lines.extend(["", "Top exclusion reasons:"])
        for reason, count in result.exclusion_reason_counts:
            lines.append(f"  {reason}: {count:,}")

    if result.eligibility_by_window:
        lines.extend(["", "Eligibility by window:"])
        for window_code, eligible, excluded in result.eligibility_by_window:
            lines.append(f"  {window_code}: eligible={eligible:,}, excluded={excluded:,}")

    if result.top_exclusions:
        lines.extend(["", "Top exclusions:"])
        for row in result.top_exclusions:
            lines.append(
                "  "
                f"event={row.cyber_event_id}, security={row.security_id}, ticker={row.ticker_symbol or 'Unknown'}, "
                f"event_date={row.event_date or 'Unknown'}, aligned_date={row.aligned_event_date or 'Unknown'}, "
                f"window={row.window_code}, reason={row.exclusion_reason}"
            )

    lines.extend(["", "Issues / notes:"])
    if result.issues:
        for issue in result.issues:
            lines.append(f"  [{issue.severity}] {issue.message}")
    else:
        lines.append("  No dataset preview blockers detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")
    return lines


def print_event_study_dataset_preview(result: EventStudyDatasetPreview) -> None:
    """Print event-study dataset preview report."""

    for line in format_event_study_dataset_preview(result):
        print(line)


def determine_dataset_preview_status(issues: tuple[DatasetPreviewIssue, ...]) -> str:
    """Return the event-study dataset preview status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if any(issue.severity == "WARNING" for issue in issues):
        return "PARTIAL"
    return "READY_FOR_RETURN_PROTOTYPE"


def build_dataset_preview_issues(
    rows: tuple[EventStudyCandidateRow, ...],
) -> tuple[DatasetPreviewIssue, ...]:
    """Build issues for the dataset preview."""

    if not rows:
        return (DatasetPreviewIssue("BLOCKER", "No event-study candidate rows could be built."),)

    issues: list[DatasetPreviewIssue] = []
    excluded = sum(1 for row in rows if not row.eligible)
    if excluded:
        issues.append(DatasetPreviewIssue("WARNING", f"{excluded} candidate rows are excluded by current study design rules."))
    issues.append(
        DatasetPreviewIssue(
            "INFO",
            "This preview validates dataset eligibility only; return calculations remain disabled.",
        )
    )
    return tuple(issues)


def _fetch_candidate_rows(connection: Any, design: EventStudyDesign) -> tuple[EventStudyCandidateRow, ...]:
    if not _has_required_tables_and_columns(connection):
        return ()

    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            ewb.{CYBER_EVENT_ID},
            ewb.{SECURITY_ID},
            s.{TICKER_SYMBOL},
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
            ) AS benchmark_price_observations
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        LEFT JOIN {SECURITIES} s ON s.{SECURITY_ID} = ewb.{SECURITY_ID}
        ORDER BY ewb.{CYBER_EVENT_ID}, ewb.{SECURITY_ID}, ewb.{WINDOW_CODE}
        """,
    )

    result: list[EventStudyCandidateRow] = []
    for row in rows:
        event_date = str(row[3]) if row[3] else None
        aligned_event_date = str(row[4]) if row[4] else None
        window_start_date = str(row[6]) if row[6] else None
        window_end_date = str(row[7]) if row[7] else None
        security_observations = int(row[8] or 0)
        benchmark_observations = int(row[9] or 0)
        exclusion_reason = _exclusion_reason(
            security_observations,
            benchmark_observations,
            event_date,
            aligned_event_date,
            window_start_date,
            window_end_date,
            design,
        )
        eligible = exclusion_reason == ""
        result.append(
            EventStudyCandidateRow(
                cyber_event_id=int(row[0]) if row[0] is not None else None,
                security_id=int(row[1]) if row[1] is not None else None,
                ticker_symbol=str(row[2]) if row[2] else None,
                event_date=event_date,
                event_date_type="disclosure",
                aligned_event_date=aligned_event_date,
                window_code=str(row[5]) if row[5] else None,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
                security_price_observations=security_observations,
                benchmark_price_observations=benchmark_observations,
                eligible=eligible,
                exclusion_reason=exclusion_reason,
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


def _eligibility_by_window(rows: tuple[EventStudyCandidateRow, ...]) -> tuple[tuple[str, int, int], ...]:
    counts: dict[str, list[int]] = {}
    for row in rows:
        bucket = counts.setdefault(row.window_code or "Unknown", [0, 0])
        bucket[0 if row.eligible else 1] += 1
    return tuple((window_code, values[0], values[1]) for window_code, values in sorted(counts.items()))


def _exclusion_reason_counts(rows: tuple[EventStudyCandidateRow, ...]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.exclusion_reason.split(";"):
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _events_without_eligible_windows(rows: tuple[EventStudyCandidateRow, ...]) -> int:
    events = {row.cyber_event_id for row in rows if row.cyber_event_id is not None}
    eligible_events = {row.cyber_event_id for row in rows if row.cyber_event_id is not None and row.eligible}
    return len(events - eligible_events)


def _event_date_alignment_status(rows: tuple[EventStudyCandidateRow, ...]) -> str:
    if not rows:
        return "Unavailable"
    missing = sum(1 for row in rows if row.event_date is None or row.aligned_event_date is None)
    if missing:
        return f"PARTIAL: {missing} rows missing event date alignment"
    shifted = sum(1 for row in rows if row.event_date != row.aligned_event_date)
    return f"OK: {shifted} rows align disclosure date to a different trading day"


def _benchmark_availability(rows: tuple[EventStudyCandidateRow, ...], design: EventStudyDesign) -> str:
    if not rows:
        return "Unavailable"
    missing = sum(
        1 for row in rows if row.benchmark_price_observations < design.minimum_benchmark_price_observations
    )
    if missing:
        return f"PARTIAL: {missing} rows missing benchmark observations"
    return "OK"


def _write_candidates_csv(path: Path, rows: tuple[EventStudyCandidateRow, ...]) -> None:
    write_rows_csv(
        path,
        rows,
        (
            "cyber_event_id",
            "security_id",
            "ticker_symbol",
            "event_date",
            "event_date_type",
            "aligned_event_date",
            "window_code",
            "window_start_date",
            "window_end_date",
            "security_price_observations",
            "benchmark_price_observations",
            "eligible",
            "exclusion_reason",
        ),
    )


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _log_report(result: EventStudyDatasetPreview, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Event-study dataset preview failed: %s", result.error_message)
        return
    logger.info(
        "Event-study dataset preview completed: database=%s status=%s candidates=%s eligible=%s",
        result.database_name,
        result.preview_status,
        result.total_candidates,
        result.eligible_candidates,
    )
