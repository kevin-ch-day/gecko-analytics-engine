"""Read-only event-window price coverage analysis."""

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
    INDEX_DAILY_PRICES,
    SECURITY_DAILY_PRICES,
    SECURITY_ID,
    TRADE_DATE,
    VW_EVENT_WINDOW_BOUNDARIES,
    WINDOW_CODE,
    WINDOW_END_DATE,
    WINDOW_START_DATE,
)
from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class EventWindowCoverageRow:
    """Price coverage for one event/security/window row."""

    cyber_event_id: int | None
    security_id: int | None
    window_code: str | None
    window_start_date: str | None
    window_end_date: str | None
    security_price_rows: int
    distinct_security_price_dates: int
    index_price_rows: int
    distinct_index_price_dates: int
    has_security_price: bool
    has_index_price: bool
    coverage_status: str


@dataclass(frozen=True)
class WindowCoverageIssue:
    """A blocker, warning, or informational coverage issue."""

    severity: str
    message: str


@dataclass(frozen=True)
class EventWindowCoverageReport:
    """Read-only event-window price coverage report."""

    generated_at: str
    connection_ok: bool
    coverage_status: str
    database_name: str | None = None
    total_window_rows: int = 0
    full_coverage_rows: int = 0
    missing_security_price_rows: int = 0
    missing_index_price_rows: int = 0
    missing_any_price_rows: int = 0
    affected_events: int = 0
    affected_securities: int = 0
    top_problem_rows: tuple[EventWindowCoverageRow, ...] = ()
    window_status_counts: tuple[tuple[str, str, int], ...] = ()
    issues: tuple[WindowCoverageIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_event_window_coverage_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> EventWindowCoverageReport:
    """Run and export a read-only event-window price coverage report."""

    generated_at = datetime.now(UTC).isoformat()
    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            rows = _fetch_event_window_coverage_rows(connection)
    except DatabaseConnectionError as exc:
        result = EventWindowCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            coverage_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = EventWindowCoverageReport(
            generated_at=generated_at,
            connection_ok=False,
            coverage_status="BLOCKED",
            error_message=f"Event window coverage failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_window_coverage_issues(rows)
    result = EventWindowCoverageReport(
        generated_at=generated_at,
        connection_ok=True,
        coverage_status=determine_window_coverage_status(issues),
        database_name=database_name,
        total_window_rows=len(rows),
        full_coverage_rows=sum(1 for row in rows if row.coverage_status == "COVERED"),
        missing_security_price_rows=sum(1 for row in rows if not row.has_security_price),
        missing_index_price_rows=sum(1 for row in rows if not row.has_index_price),
        missing_any_price_rows=sum(1 for row in rows if row.coverage_status != "COVERED"),
        affected_events=len({row.cyber_event_id for row in rows if row.coverage_status != "COVERED" and row.cyber_event_id is not None}),
        affected_securities=len({row.security_id for row in rows if row.coverage_status != "COVERED" and row.security_id is not None}),
        top_problem_rows=_top_problem_rows(rows),
        window_status_counts=_window_status_counts(rows),
        issues=issues,
    )
    result = export_event_window_coverage_report(result, rows, paths, logger)
    _log_report(result, logger)
    return result


def export_event_window_coverage_report(
    result: EventWindowCoverageReport,
    rows: tuple[EventWindowCoverageRow, ...],
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> EventWindowCoverageReport:
    """Export event-window price coverage artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "event_window_price_coverage.json"
    detail_csv = paths.exports_dir / "event_window_price_coverage_detail.csv"
    issues_csv = paths.exports_dir / "event_window_price_coverage_issues.csv"
    export_paths = (json_path, detail_csv, issues_csv)
    result_with_exports = replace(result, export_paths=export_paths)

    _write_detail_csv(detail_csv, rows)
    _write_issues_csv(issues_csv, result.issues)
    _write_report_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Event-window price coverage exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def format_event_window_coverage_report(result: EventWindowCoverageReport) -> list[str]:
    """Format event-window price coverage for console output."""

    lines = ["", "Event Window Price Coverage", "---------------------------"]
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
            f"Overall status: {result.coverage_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Summary:",
            f"  Event/security/window rows: {result.total_window_rows:,}",
            f"  Fully covered rows: {result.full_coverage_rows:,}",
            f"  Rows missing security prices: {result.missing_security_price_rows:,}",
            f"  Rows missing index prices: {result.missing_index_price_rows:,}",
            f"  Rows missing any price input: {result.missing_any_price_rows:,}",
            f"  Affected events: {result.affected_events:,}",
            f"  Affected securities: {result.affected_securities:,}",
        ]
    )

    if result.window_status_counts:
        lines.extend(["", "Coverage by window:"])
        for window_code, status, count in result.window_status_counts:
            lines.append(f"  {window_code or 'Unknown'} / {status}: {count:,}")

    if result.top_problem_rows:
        lines.extend(["", "Top problem rows:"])
        for row in result.top_problem_rows:
            lines.append(
                "  "
                f"event={row.cyber_event_id}, security={row.security_id}, window={row.window_code}, "
                f"status={row.coverage_status}, security_rows={row.security_price_rows:,}, "
                f"index_rows={row.index_price_rows:,}"
            )

    lines.extend(["", "Issues / notes:"])
    if result.issues:
        for issue in result.issues:
            lines.append(f"  [{issue.severity}] {issue.message}")
    else:
        lines.append("  No event-window price coverage issues detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def print_event_window_coverage_report(result: EventWindowCoverageReport) -> None:
    """Print event-window price coverage to the console."""

    for line in format_event_window_coverage_report(result):
        print(line)


def determine_window_coverage_status(issues: tuple[WindowCoverageIssue, ...]) -> str:
    """Return overall event-window price coverage status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if issues:
        return "PARTIAL"
    return "READY_FOR_EVENT_STUDY_CALCULATION"


def build_window_coverage_issues(
    rows: tuple[EventWindowCoverageRow, ...],
) -> tuple[WindowCoverageIssue, ...]:
    """Build window coverage issues from detail rows."""

    if not rows:
        return (WindowCoverageIssue("BLOCKER", "No event-window coverage rows could be analyzed."),)

    issues: list[WindowCoverageIssue] = []
    missing_security = sum(1 for row in rows if not row.has_security_price)
    missing_index = sum(1 for row in rows if not row.has_index_price)
    if missing_security:
        issues.append(WindowCoverageIssue("WARNING", f"{missing_security} event-window rows have no security prices."))
    if missing_index:
        issues.append(WindowCoverageIssue("WARNING", f"{missing_index} event-window rows have no index prices."))

    issues.append(
        WindowCoverageIssue(
            "INFO",
            "This is coverage validation only; it does not compute returns, abnormal returns, or CAR.",
        )
    )
    return tuple(issues)


def _fetch_event_window_coverage_rows(connection: Any) -> tuple[EventWindowCoverageRow, ...]:
    if not _has_required_tables_and_columns(connection):
        return ()

    rows = safe_fetch_all(
        connection,
        f"""
        SELECT
            ewb.{CYBER_EVENT_ID},
            ewb.{SECURITY_ID},
            ewb.{WINDOW_CODE},
            ewb.{WINDOW_START_DATE},
            ewb.{WINDOW_END_DATE},
            COUNT(DISTINCT sdp.{TRADE_DATE}) AS security_price_rows,
            COUNT(DISTINCT sdp.{TRADE_DATE}) AS distinct_security_price_dates,
            COUNT(DISTINCT idp.{TRADE_DATE}) AS index_price_rows,
            COUNT(DISTINCT idp.{TRADE_DATE}) AS distinct_index_price_dates
        FROM {VW_EVENT_WINDOW_BOUNDARIES} ewb
        LEFT JOIN {SECURITY_DAILY_PRICES} sdp
            ON sdp.{SECURITY_ID} = ewb.{SECURITY_ID}
           AND sdp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        LEFT JOIN {INDEX_DAILY_PRICES} idp
            ON idp.{TRADE_DATE} BETWEEN ewb.{WINDOW_START_DATE} AND ewb.{WINDOW_END_DATE}
        GROUP BY
            ewb.{CYBER_EVENT_ID},
            ewb.{SECURITY_ID},
            ewb.{WINDOW_CODE},
            ewb.{WINDOW_START_DATE},
            ewb.{WINDOW_END_DATE}
        ORDER BY ewb.{CYBER_EVENT_ID}, ewb.{SECURITY_ID}, ewb.{WINDOW_CODE}
        """,
    )

    result: list[EventWindowCoverageRow] = []
    for row in rows:
        security_price_rows = int(row[5] or 0)
        index_price_rows = int(row[7] or 0)
        result.append(
            EventWindowCoverageRow(
                cyber_event_id=int(row[0]) if row[0] is not None else None,
                security_id=int(row[1]) if row[1] is not None else None,
                window_code=str(row[2]) if row[2] else None,
                window_start_date=str(row[3]) if row[3] else None,
                window_end_date=str(row[4]) if row[4] else None,
                security_price_rows=security_price_rows,
                distinct_security_price_dates=int(row[6] or 0),
                index_price_rows=index_price_rows,
                distinct_index_price_dates=int(row[8] or 0),
                has_security_price=security_price_rows > 0,
                has_index_price=index_price_rows > 0,
                coverage_status=_coverage_status(security_price_rows, index_price_rows),
            )
        )
    return tuple(result)


def _has_required_tables_and_columns(connection: Any) -> bool:
    for table_name in (VW_EVENT_WINDOW_BOUNDARIES, SECURITY_DAILY_PRICES, INDEX_DAILY_PRICES):
        if not table_exists(connection, table_name):
            return False

    boundary_columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    security_price_columns = set(get_table_columns(connection, SECURITY_DAILY_PRICES))
    index_price_columns = set(get_table_columns(connection, INDEX_DAILY_PRICES))
    return (
        {CYBER_EVENT_ID, SECURITY_ID, WINDOW_CODE, WINDOW_START_DATE, WINDOW_END_DATE}.issubset(boundary_columns)
        and {SECURITY_ID, TRADE_DATE}.issubset(security_price_columns)
        and TRADE_DATE in index_price_columns
    )


def _coverage_status(security_rows: int, index_rows: int) -> str:
    if security_rows > 0 and index_rows > 0:
        return "COVERED"
    if security_rows == 0 and index_rows == 0:
        return "MISSING_SECURITY_AND_INDEX"
    if security_rows == 0:
        return "MISSING_SECURITY"
    return "MISSING_INDEX"


def _top_problem_rows(rows: tuple[EventWindowCoverageRow, ...]) -> tuple[EventWindowCoverageRow, ...]:
    return tuple(row for row in rows if row.coverage_status != "COVERED")[:25]


def _window_status_counts(rows: tuple[EventWindowCoverageRow, ...]) -> tuple[tuple[str, str, int], ...]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.window_code or "Unknown", row.coverage_status)
        counts[key] = counts.get(key, 0) + 1
    return tuple((window_code, status, count) for (window_code, status), count in sorted(counts.items()))


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _write_detail_csv(path: Path, rows: tuple[EventWindowCoverageRow, ...]) -> None:
    write_rows_csv(
        path,
        rows,
        (
            "cyber_event_id",
            "security_id",
            "window_code",
            "window_start_date",
            "window_end_date",
            "security_price_rows",
            "distinct_security_price_dates",
            "index_price_rows",
            "distinct_index_price_dates",
            "has_security_price",
            "has_index_price",
            "coverage_status",
        ),
    )


def _write_issues_csv(path: Path, issues: tuple[WindowCoverageIssue, ...]) -> None:
    write_rows_csv(path, issues, ("severity", "message"))


def _write_report_json(path: Path, result: EventWindowCoverageReport) -> None:
    write_dataclass_json(path, result)


def _log_report(result: EventWindowCoverageReport, logger: logging.Logger | None) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Event-window price coverage failed: %s", result.error_message)
        return
    logger.info(
        "Event-window price coverage completed: database=%s status=%s rows=%s issues=%s",
        result.database_name,
        result.coverage_status,
        result.total_window_rows,
        len(result.issues),
    )
