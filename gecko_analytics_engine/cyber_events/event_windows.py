"""Read-only event window readiness details."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import DatabaseConnectionError, database_connection
from gecko_analytics_engine.db.reads import (
    count_rows,
    get_table_columns,
    safe_fetch_all,
    safe_scalar,
    table_exists,
)
from gecko_analytics_engine.db.schema_contract import (
    BOUNDARY_STATUS,
    CALENDAR_DATE,
    CYBER_EVENT_DATES,
    CYBER_EVENT_ID,
    DATE_TYPE,
    DISCLOSURE_DATE,
    EVENT_DATE,
    EVENT_WINDOWS,
    FIRST_TRADING_DAY,
    IS_TRADING_DAY,
    MARKET_CALENDAR,
    POST_EVENT_DAYS,
    PRE_EVENT_DAYS,
    VW_EVENT_WINDOW_BOUNDARIES,
    WINDOW_CODE,
    WINDOW_END_DATE,
    WINDOW_START_DATE,
)
from gecko_analytics_engine.utils.paths import AppPaths


@dataclass(frozen=True)
class WindowMetric:
    """One event-window readiness metric."""

    category: str
    name: str
    value: int | str | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class WindowIssue:
    """A blocker, warning, or informational event-window issue."""

    severity: str
    message: str


@dataclass(frozen=True)
class EventWindowReadinessReport:
    """Read-only event-window readiness result."""

    generated_at: str
    connection_ok: bool
    readiness_status: str
    database_name: str | None = None
    metrics: tuple[WindowMetric, ...] = ()
    issues: tuple[WindowIssue, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None


def run_event_window_readiness_report(
    settings: AppSettings,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> EventWindowReadinessReport:
    """Run and export a read-only event-window readiness report."""

    generated_at = datetime.now(UTC).isoformat()

    try:
        with database_connection(settings) as connection:
            database_name = _selected_database(connection) or settings.db.name
            metrics = _collect_window_metrics(connection)
    except DatabaseConnectionError as exc:
        result = EventWindowReadinessReport(
            generated_at=generated_at,
            connection_ok=False,
            readiness_status="BLOCKED",
            error_message=str(exc),
        )
        _log_report(result, logger)
        return result
    except Exception as exc:
        result = EventWindowReadinessReport(
            generated_at=generated_at,
            connection_ok=False,
            readiness_status="BLOCKED",
            error_message=f"Event window readiness failed: {exc.__class__.__name__}: {exc}",
        )
        _log_report(result, logger)
        return result

    issues = build_window_issues(metrics)
    result = EventWindowReadinessReport(
        generated_at=generated_at,
        connection_ok=True,
        readiness_status=determine_window_readiness_status(issues),
        database_name=database_name,
        metrics=metrics,
        issues=issues,
    )
    result = export_event_window_readiness_report(result, paths, logger)
    _log_report(result, logger)
    return result


def export_event_window_readiness_report(
    result: EventWindowReadinessReport,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> EventWindowReadinessReport:
    """Export event-window readiness artifacts."""

    if not result.connection_ok:
        return result

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = paths.reports_dir / "event_window_readiness.json"
    summary_csv = paths.exports_dir / "event_window_readiness_summary.csv"
    issues_csv = paths.exports_dir / "event_window_readiness_issues.csv"
    export_paths = (json_path, summary_csv, issues_csv)
    result_with_exports = _copy_report_with_export_paths(result, export_paths)

    _write_metrics_csv(summary_csv, result.metrics)
    _write_issues_csv(issues_csv, result.issues)
    _write_report_json(json_path, result_with_exports)

    if logger is not None:
        logger.info(
            "Event window readiness exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def format_event_window_readiness_report(
    result: EventWindowReadinessReport,
) -> list[str]:
    """Format event-window readiness for the console."""

    lines = ["", "Event Window Readiness", "----------------------"]
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
            f"Overall status: {result.readiness_status}",
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Generated: {result.generated_at}",
            "",
            "Window checks:",
        ]
    )

    for metric in result.metrics:
        value = _format_value(metric.value)
        detail = f" ({metric.detail})" if metric.detail else ""
        lines.append(f"  [{metric.status}] {metric.category} - {metric.name}: {value}{detail}")

    lines.extend(["", "Issues:"])
    if result.issues:
        for issue in result.issues:
            lines.append(f"  [{issue.severity}] {issue.message}")
    else:
        lines.append("  No event-window blockers detected.")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def print_event_window_readiness_report(result: EventWindowReadinessReport) -> None:
    """Print event-window readiness to the console."""

    for line in format_event_window_readiness_report(result):
        print(line)


def determine_window_readiness_status(issues: tuple[WindowIssue, ...]) -> str:
    """Return the event-window readiness status."""

    if any(issue.severity == "BLOCKER" for issue in issues):
        return "BLOCKED"
    if issues:
        return "PARTIAL"
    return "READY_FOR_EVENT_STUDY_DATASET"


def build_window_issues(metrics: tuple[WindowMetric, ...]) -> tuple[WindowIssue, ...]:
    """Generate issues from event-window metrics."""

    values = {(metric.category, metric.name): metric for metric in metrics}
    issues: list[WindowIssue] = []

    for key, message in (
        (("window definitions", "event window definitions"), "event_windows is unavailable."),
        (("event date anchors", "events with disclosure dates"), "disclosure date anchors are unavailable."),
        (("market calendar", "trading-day calendar rows"), "market calendar is unavailable."),
        (("window boundaries view", "vw_event_window_boundaries rows"), "event window boundary view is unavailable."),
    ):
        metric = values.get(key)
        if metric is None or metric.status in {"MISSING", "UNAVAILABLE"}:
            issues.append(WindowIssue("BLOCKER", message))

    invalid_windows = values.get(("window definitions", "invalid window definitions"))
    if _positive_metric(invalid_windows):
        issues.append(WindowIssue("BLOCKER", f"{invalid_windows.value} event window definitions are invalid."))

    missing_boundaries = values.get(("window boundaries view", "rows missing window boundaries"))
    if _positive_metric(missing_boundaries):
        issues.append(WindowIssue("WARNING", f"{missing_boundaries.value} event-window boundary rows are incomplete."))

    non_trading = values.get(("event date anchors", "disclosure dates not on trading days"))
    if _positive_metric(non_trading):
        issues.append(WindowIssue("INFO", f"{non_trading.value} disclosure dates are not trading days and need alignment."))

    return tuple(issues)


def _collect_window_metrics(connection: Any) -> tuple[WindowMetric, ...]:
    metrics: list[WindowMetric] = []
    metrics.extend(_window_definition_metrics(connection))
    metrics.extend(_event_date_anchor_metrics(connection))
    metrics.extend(_calendar_metrics(connection))
    metrics.extend(_boundary_view_metrics(connection))
    return tuple(metrics)


def _window_definition_metrics(connection: Any) -> list[WindowMetric]:
    category = "window definitions"
    if not table_exists(connection, EVENT_WINDOWS):
        return [_missing_metric(category, "event window definitions", EVENT_WINDOWS)]

    columns = set(get_table_columns(connection, EVENT_WINDOWS))
    metrics = [_table_count_metric(connection, category, "event window definitions", EVENT_WINDOWS)]
    required = {WINDOW_CODE, PRE_EVENT_DAYS, POST_EVENT_DAYS}
    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns))
        metrics.append(_unavailable_metric(category, "invalid window definitions", f"missing columns: {missing}"))
        return metrics

    invalid = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {EVENT_WINDOWS}
        WHERE {PRE_EVENT_DAYS} < 0
           OR {POST_EVENT_DAYS} < 0
           OR {WINDOW_CODE} IS NULL
           OR {WINDOW_CODE} = ''
        """,
    )
    metrics.append(_scalar_metric(category, "invalid window definitions", invalid))
    for window_code, pre_days, post_days in safe_fetch_all(
        connection,
        f"""
        SELECT {WINDOW_CODE}, {PRE_EVENT_DAYS}, {POST_EVENT_DAYS}
        FROM {EVENT_WINDOWS}
        ORDER BY {PRE_EVENT_DAYS}, {POST_EVENT_DAYS}, {WINDOW_CODE}
        """,
    ):
        metrics.append(
            WindowMetric(
                category,
                f"window {window_code}",
                f"{pre_days} before / {post_days} after",
                "OK",
            )
        )
    return metrics


def _event_date_anchor_metrics(connection: Any) -> list[WindowMetric]:
    category = "event date anchors"
    if not table_exists(connection, CYBER_EVENT_DATES):
        return [_missing_metric(category, "events with disclosure dates", CYBER_EVENT_DATES)]

    columns = set(get_table_columns(connection, CYBER_EVENT_DATES))
    required = {CYBER_EVENT_ID, DATE_TYPE, EVENT_DATE}
    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns))
        return [_unavailable_metric(category, "events with disclosure dates", f"missing columns: {missing}")]

    disclosure_events = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT {CYBER_EVENT_ID})
        FROM {CYBER_EVENT_DATES}
        WHERE {DATE_TYPE} = 'disclosure'
        """,
    )
    first_trading_day_events = safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT {CYBER_EVENT_ID})
        FROM {CYBER_EVENT_DATES}
        WHERE {DATE_TYPE} = 'first_trading_day'
        """,
    )
    missing_first_trading_day = safe_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT {CYBER_EVENT_ID}
            FROM {CYBER_EVENT_DATES}
            WHERE {DATE_TYPE} = 'disclosure'
        ) disclosure_events
        LEFT JOIN (
            SELECT DISTINCT {CYBER_EVENT_ID}
            FROM {CYBER_EVENT_DATES}
            WHERE {DATE_TYPE} = 'first_trading_day'
        ) trading_events USING ({CYBER_EVENT_ID})
        WHERE trading_events.{CYBER_EVENT_ID} IS NULL
        """,
    )
    non_trading_disclosures = _count_non_trading_disclosures(connection)

    return [
        _scalar_metric(category, "events with disclosure dates", disclosure_events),
        _scalar_metric(category, "events with first trading day dates", first_trading_day_events),
        _scalar_metric(category, "disclosure events missing first trading day", missing_first_trading_day),
        _scalar_metric(category, "disclosure dates not on trading days", non_trading_disclosures),
    ]


def _calendar_metrics(connection: Any) -> list[WindowMetric]:
    category = "market calendar"
    if not table_exists(connection, MARKET_CALENDAR):
        return [_missing_metric(category, "trading-day calendar rows", MARKET_CALENDAR)]

    columns = set(get_table_columns(connection, MARKET_CALENDAR))
    metrics = [_table_count_metric(connection, category, "trading-day calendar rows", MARKET_CALENDAR)]
    if {CALENDAR_DATE, IS_TRADING_DAY}.issubset(columns):
        trading_days = safe_scalar(
            connection,
            f"SELECT COUNT(*) FROM {MARKET_CALENDAR} WHERE {IS_TRADING_DAY} = 1",
        )
        date_range = safe_scalar(
            connection,
            f"SELECT CONCAT(MIN({CALENDAR_DATE}), ' to ', MAX({CALENDAR_DATE})) FROM {MARKET_CALENDAR}",
        )
        metrics.append(_scalar_metric(category, "trading days", trading_days))
        metrics.append(_text_metric(category, "calendar date range", date_range, f"column={CALENDAR_DATE}"))
    else:
        metrics.append(_unavailable_metric(category, "trading days", "missing calendar_date/is_trading_day"))
    return metrics


def _boundary_view_metrics(connection: Any) -> list[WindowMetric]:
    category = "window boundaries view"
    if not table_exists(connection, VW_EVENT_WINDOW_BOUNDARIES):
        return [_missing_metric(category, "vw_event_window_boundaries rows", VW_EVENT_WINDOW_BOUNDARIES)]

    columns = set(get_table_columns(connection, VW_EVENT_WINDOW_BOUNDARIES))
    metrics = [_table_count_metric(connection, category, "vw_event_window_boundaries rows", VW_EVENT_WINDOW_BOUNDARIES)]
    if {WINDOW_START_DATE, WINDOW_END_DATE}.issubset(columns):
        missing_boundaries = safe_scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM {VW_EVENT_WINDOW_BOUNDARIES}
            WHERE {WINDOW_START_DATE} IS NULL
               OR {WINDOW_END_DATE} IS NULL
            """,
        )
        metrics.append(_scalar_metric(category, "rows missing window boundaries", missing_boundaries))
    else:
        metrics.append(_unavailable_metric(category, "rows missing window boundaries", "missing boundary columns"))

    if BOUNDARY_STATUS in columns:
        for status, count in safe_fetch_all(
            connection,
            f"""
            SELECT {BOUNDARY_STATUS}, COUNT(*)
            FROM {VW_EVENT_WINDOW_BOUNDARIES}
            GROUP BY {BOUNDARY_STATUS}
            ORDER BY {BOUNDARY_STATUS}
            """,
        ):
            metrics.append(_scalar_metric(category, f"boundary status: {status}", count))

    if {DISCLOSURE_DATE, FIRST_TRADING_DAY}.issubset(columns):
        aligned_rows = safe_scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM {VW_EVENT_WINDOW_BOUNDARIES}
            WHERE {DISCLOSURE_DATE} IS NOT NULL
              AND {FIRST_TRADING_DAY} IS NOT NULL
            """,
        )
        metrics.append(_scalar_metric(category, "rows with disclosure and first trading day", aligned_rows))

    return metrics


def _count_non_trading_disclosures(connection: Any) -> int | None:
    if not table_exists(connection, MARKET_CALENDAR):
        return None
    if not {CALENDAR_DATE, IS_TRADING_DAY}.issubset(set(get_table_columns(connection, MARKET_CALENDAR))):
        return None
    return safe_scalar(
        connection,
        f"""
        SELECT COUNT(DISTINCT ced.{CYBER_EVENT_ID})
        FROM {CYBER_EVENT_DATES} ced
        LEFT JOIN {MARKET_CALENDAR} mc ON mc.{CALENDAR_DATE} = ced.{EVENT_DATE}
        WHERE ced.{DATE_TYPE} = 'disclosure'
          AND COALESCE(mc.{IS_TRADING_DAY}, 0) = 0
        """,
    )


def _table_count_metric(
    connection: Any,
    category: str,
    label: str,
    table_name: str,
) -> WindowMetric:
    row_count = count_rows(connection, table_name) if table_exists(connection, table_name) else None
    if row_count is None:
        return _missing_metric(category, label, table_name)
    return WindowMetric(category, label, row_count, "EMPTY" if row_count == 0 else "OK")


def _scalar_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> WindowMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    value = int(value)
    return WindowMetric(category, label, value, "EMPTY" if value == 0 else "OK", detail)


def _text_metric(
    category: str,
    label: str,
    value: Any,
    detail: str = "",
) -> WindowMetric:
    if value is None:
        return _unavailable_metric(category, label, detail or "query returned no value")
    return WindowMetric(category, label, str(value), "OK", detail)


def _missing_metric(category: str, label: str, table_name: str) -> WindowMetric:
    return WindowMetric(category, label, None, "MISSING", f"{table_name} table not found")


def _unavailable_metric(category: str, label: str, detail: str) -> WindowMetric:
    return WindowMetric(category, label, None, "UNAVAILABLE", detail)


def _positive_metric(metric: WindowMetric | None) -> bool:
    return bool(metric and isinstance(metric.value, int) and metric.value > 0)


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None


def _write_metrics_csv(path: Path, metrics: tuple[WindowMetric, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("category", "name", "value", "status", "detail"),
        )
        writer.writeheader()
        for metric in metrics:
            writer.writerow(asdict(metric))


def _write_issues_csv(path: Path, issues: tuple[WindowIssue, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("severity", "message"))
        writer.writeheader()
        for issue in issues:
            writer.writerow(asdict(issue))


def _write_report_json(path: Path, result: EventWindowReadinessReport) -> None:
    payload = asdict(result)
    payload["export_paths"] = [str(path) for path in result.export_paths]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _copy_report_with_export_paths(
    result: EventWindowReadinessReport,
    export_paths: tuple[Path, ...],
) -> EventWindowReadinessReport:
    return EventWindowReadinessReport(
        generated_at=result.generated_at,
        connection_ok=result.connection_ok,
        readiness_status=result.readiness_status,
        database_name=result.database_name,
        metrics=result.metrics,
        issues=result.issues,
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
    result: EventWindowReadinessReport,
    logger: logging.Logger | None,
) -> None:
    if logger is None:
        return
    if not result.connection_ok:
        logger.warning("Event window readiness failed: %s", result.error_message)
        return
    logger.info(
        "Event window readiness completed: database=%s status=%s metrics=%s issues=%s",
        result.database_name,
        result.readiness_status,
        len(result.metrics),
        len(result.issues),
    )
