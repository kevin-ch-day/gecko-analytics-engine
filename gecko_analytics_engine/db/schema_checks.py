"""Read-only database schema and health checks."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings
from gecko_analytics_engine.db.connection import (
    DatabaseConnectionError,
    database_connection,
)
from gecko_analytics_engine.db.schema_contract import CORE_TABLES
from gecko_analytics_engine.utils.paths import AppPaths

REQUIRED_TABLES = CORE_TABLES


@dataclass(frozen=True)
class TableHealth:
    """Existence status for one required database table."""

    name: str
    exists: bool | None


@dataclass(frozen=True)
class TableInventory:
    """Inventory summary for one database table."""

    table_name: str
    table_type: str | None
    row_count: int | None
    column_count: int


@dataclass(frozen=True)
class ColumnInventory:
    """Column metadata for a core Project Gecko table."""

    table_name: str
    column_name: str
    data_type: str
    nullable: str
    key: str
    default: str | None
    extra: str


@dataclass(frozen=True)
class CoreTableShape:
    """Lightweight data-shape status for a required core table."""

    table_name: str
    exists: bool
    row_count: int | None
    status: str


@dataclass(frozen=True)
class DatabaseHealthResult:
    """Result of the read-only database health and inventory check."""

    connection_ok: bool
    database_name: str | None = None
    server_version: str | None = None
    current_user: str | None = None
    table_statuses: tuple[TableHealth, ...] = ()
    table_inventory: tuple[TableInventory, ...] = ()
    core_table_columns: tuple[ColumnInventory, ...] = ()
    core_table_shapes: tuple[CoreTableShape, ...] = ()
    possible_event_market_tables: tuple[str, ...] = ()
    export_paths: tuple[Path, ...] = ()
    error_message: str | None = None
    table_check_error: str | None = None
    inventory_error: str | None = None
    column_inventory_error: str | None = None


def run_database_health_check(
    settings: AppSettings,
    logger: logging.Logger | None = None,
) -> DatabaseHealthResult:
    """Run a read-only database health and schema inventory check."""

    try:
        with database_connection(settings) as connection:
            cursor = connection.cursor()
            try:
                database_name, server_version, current_user = _fetch_connection_info(cursor)
                effective_database = database_name or settings.db.name
                table_statuses, table_check_error = _fetch_table_statuses(
                    cursor,
                    effective_database,
                )
                table_inventory, inventory_error = _fetch_table_inventory(
                    cursor,
                    effective_database,
                )
                core_table_columns, column_inventory_error = _fetch_core_table_columns(
                    cursor,
                    effective_database,
                )
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
    except DatabaseConnectionError as exc:
        result = DatabaseHealthResult(connection_ok=False, error_message=str(exc))
        _log_result(result, logger)
        return result
    except Exception as exc:
        result = DatabaseHealthResult(
            connection_ok=False,
            error_message=f"Database health check failed: {exc.__class__.__name__}: {exc}",
        )
        _log_result(result, logger)
        return result

    core_table_shapes = _build_core_table_shapes(table_statuses, table_inventory)
    possible_event_market_tables = _find_possible_event_market_tables(table_inventory)
    result = DatabaseHealthResult(
        connection_ok=True,
        database_name=database_name,
        server_version=server_version,
        current_user=current_user,
        table_statuses=table_statuses,
        table_inventory=table_inventory,
        core_table_columns=core_table_columns,
        core_table_shapes=core_table_shapes,
        possible_event_market_tables=possible_event_market_tables,
        table_check_error=table_check_error,
        inventory_error=inventory_error,
        column_inventory_error=column_inventory_error,
    )
    _log_result(result, logger)
    return result


def export_database_inventory(
    result: DatabaseHealthResult,
    paths: AppPaths,
    logger: logging.Logger | None = None,
) -> DatabaseHealthResult:
    """Export schema inventory artifacts for a successful health check."""

    if not result.connection_ok:
        return result

    schema_csv = paths.exports_dir / "schema_inventory.csv"
    columns_csv = paths.exports_dir / "core_table_columns.csv"
    counts_csv = paths.exports_dir / "core_table_counts.csv"
    report_json = paths.reports_dir / "database_inventory.json"

    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)

    export_paths = (schema_csv, columns_csv, counts_csv, report_json)
    result_with_exports = _copy_result_with_export_paths(result, export_paths)

    _write_schema_inventory_csv(schema_csv, result.table_inventory)
    _write_core_columns_csv(columns_csv, result.core_table_columns)
    _write_core_counts_csv(counts_csv, result.core_table_shapes)
    _write_inventory_json(report_json, result_with_exports)
    if logger is not None:
        logger.info(
            "Database inventory exports written: %s",
            ", ".join(str(path) for path in export_paths),
        )

    return result_with_exports


def print_database_health_result(result: DatabaseHealthResult) -> None:
    """Print a readable health-check and inventory report to the console."""

    for line in format_database_health_result(result):
        print(line)


def format_database_health_result(result: DatabaseHealthResult) -> list[str]:
    """Format a health-check result for console output."""

    lines = ["", "Database Health and Schema Inventory", "------------------------------------"]

    if not result.connection_ok:
        lines.extend(["Connection: FAILED", f"Reason: {result.error_message}"])
        return lines

    lines.extend(
        [
            "Connection: OK",
            f"Database: {result.database_name or 'Unknown'}",
            f"Server version: {result.server_version or 'Unknown'}",
            f"Current user: {result.current_user or 'Unknown'}",
            "",
            "Required tables:",
        ]
    )

    if result.table_check_error:
        lines.append(f"  Table check could not be completed: {result.table_check_error}")

    for table in result.table_statuses:
        lines.append(f"  [{_table_status_label(table.exists)}] {table.name}")

    lines.extend(
        [
            "",
            "Table inventory summary:",
            f"  Tables found: {len(result.table_inventory)}",
            f"  Views found: {_count_views(result.table_inventory)}",
        ]
    )

    if result.inventory_error:
        lines.append(f"  Inventory could not be completed: {result.inventory_error}")
    elif result.table_inventory:
        lines.append("  Name | Type | Rows | Columns")
        for table in result.table_inventory:
            row_count = _format_count(table.row_count)
            table_type = table.table_type or "Unknown"
            lines.append(
                f"  {table.table_name} | {table_type} | {row_count} | {table.column_count}"
            )

    lines.extend(["", "Core table row counts:"])
    for shape in result.core_table_shapes:
        lines.append(
            f"  [{shape.status}] {shape.table_name}: {_format_count(shape.row_count)}"
        )

    if result.column_inventory_error:
        lines.extend(
            [
                "",
                f"Core column inventory could not be completed: {result.column_inventory_error}",
            ]
        )
    elif result.core_table_columns:
        lines.extend(
            [
                "",
                f"Core column inventory: {len(result.core_table_columns)} columns captured",
            ]
        )

    if result.possible_event_market_tables:
        lines.extend(["", "Possible event / market-data tables:"])
        for table_name in result.possible_event_market_tables:
            lines.append(f"  {table_name}")

    if result.export_paths:
        lines.extend(["", "Exports:"])
        for path in result.export_paths:
            lines.append(f"  {path}")

    return lines


def _fetch_connection_info(cursor: Any) -> tuple[str | None, str | None, str | None]:
    cursor.execute("SELECT DATABASE(), VERSION(), CURRENT_USER()")
    row = cursor.fetchone()
    if row is None:
        return None, None, None
    return _value_at(row, 0), _value_at(row, 1), _value_at(row, 2)


def _fetch_table_statuses(
    cursor: Any,
    database_name: str,
) -> tuple[tuple[TableHealth, ...], str | None]:
    try:
        placeholders = ", ".join(["%s"] * len(REQUIRED_TABLES))
        cursor.execute(
            f"""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name IN ({placeholders})
            """,
            (database_name, *REQUIRED_TABLES),
        )
        existing_tables = {_value_at(row, 0) for row in cursor.fetchall()}
        return (
            tuple(
                TableHealth(name=table_name, exists=table_name in existing_tables)
                for table_name in REQUIRED_TABLES
            ),
            None,
        )
    except Exception as exc:
        return (
            tuple(TableHealth(name=table_name, exists=None) for table_name in REQUIRED_TABLES),
            f"{exc.__class__.__name__}: {exc}",
        )


def _fetch_table_inventory(
    cursor: Any,
    database_name: str,
) -> tuple[tuple[TableInventory, ...], str | None]:
    try:
        cursor.execute(
            """
            SELECT
                t.table_name,
                t.table_type,
                t.table_rows,
                COUNT(c.column_name) AS column_count
            FROM information_schema.tables t
            LEFT JOIN information_schema.columns c
                ON c.table_schema = t.table_schema
               AND c.table_name = t.table_name
            WHERE t.table_schema = %s
            GROUP BY t.table_name, t.table_type, t.table_rows
            ORDER BY t.table_name
            """,
            (database_name,),
        )
        return (
            tuple(
                TableInventory(
                    table_name=_value_at(row, 0) or "",
                    table_type=_value_at(row, 1),
                    row_count=_int_value_at(row, 2),
                    column_count=_int_value_at(row, 3) or 0,
                )
                for row in cursor.fetchall()
            ),
            None,
        )
    except Exception as exc:
        return (), f"{exc.__class__.__name__}: {exc}"


def _fetch_core_table_columns(
    cursor: Any,
    database_name: str,
) -> tuple[tuple[ColumnInventory, ...], str | None]:
    try:
        placeholders = ", ".join(["%s"] * len(REQUIRED_TABLES))
        cursor.execute(
            f"""
            SELECT
                table_name,
                column_name,
                column_type,
                is_nullable,
                column_key,
                column_default,
                extra
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name IN ({placeholders})
            ORDER BY table_name, ordinal_position
            """,
            (database_name, *REQUIRED_TABLES),
        )
        return (
            tuple(
                ColumnInventory(
                    table_name=_value_at(row, 0) or "",
                    column_name=_value_at(row, 1) or "",
                    data_type=_value_at(row, 2) or "",
                    nullable=_value_at(row, 3) or "",
                    key=_value_at(row, 4) or "",
                    default=_value_at(row, 5),
                    extra=_value_at(row, 6) or "",
                )
                for row in cursor.fetchall()
            ),
            None,
        )
    except Exception as exc:
        return (), f"{exc.__class__.__name__}: {exc}"


def _build_core_table_shapes(
    table_statuses: tuple[TableHealth, ...],
    table_inventory: tuple[TableInventory, ...],
) -> tuple[CoreTableShape, ...]:
    inventory_by_name = {table.table_name: table for table in table_inventory}
    shapes: list[CoreTableShape] = []

    for table in table_statuses:
        if table.exists is not True:
            shapes.append(
                CoreTableShape(
                    table_name=table.name,
                    exists=False,
                    row_count=None,
                    status="MISSING" if table.exists is False else "UNKNOWN",
                )
            )
            continue

        inventory = inventory_by_name.get(table.name)
        row_count = inventory.row_count if inventory is not None else None
        if row_count is None:
            status = "UNKNOWN"
        elif row_count == 0:
            status = "EMPTY"
        else:
            status = "OK"
        shapes.append(
            CoreTableShape(
                table_name=table.name,
                exists=True,
                row_count=row_count,
                status=status,
            )
        )

    return tuple(shapes)


def _write_schema_inventory_csv(
    path: Path,
    table_inventory: tuple[TableInventory, ...],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("table_name", "table_type", "row_count", "column_count"),
        )
        writer.writeheader()
        for table in table_inventory:
            writer.writerow(asdict(table))


def _write_core_columns_csv(
    path: Path,
    core_table_columns: tuple[ColumnInventory, ...],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "table_name",
                "column_name",
                "data_type",
                "nullable",
                "key",
                "default",
                "extra",
            ),
        )
        writer.writeheader()
        for column in core_table_columns:
            writer.writerow(asdict(column))


def _write_core_counts_csv(
    path: Path,
    core_table_shapes: tuple[CoreTableShape, ...],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=("table_name", "exists", "row_count", "status"),
        )
        writer.writeheader()
        for shape in core_table_shapes:
            writer.writerow(asdict(shape))


def _write_inventory_json(path: Path, result: DatabaseHealthResult) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "connection_ok": result.connection_ok,
        "database_name": result.database_name,
        "server_version": result.server_version,
        "current_user": result.current_user,
        "required_tables": [asdict(table) for table in result.table_statuses],
        "core_table_counts": [asdict(shape) for shape in result.core_table_shapes],
        "export_paths": [str(path) for path in result.export_paths],
        "table_inventory": [asdict(table) for table in result.table_inventory],
        "core_table_columns": [asdict(column) for column in result.core_table_columns],
        "core_table_shapes": [asdict(shape) for shape in result.core_table_shapes],
        "possible_event_market_tables": list(result.possible_event_market_tables),
        "table_check_error": result.table_check_error,
        "inventory_error": result.inventory_error,
        "column_inventory_error": result.column_inventory_error,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _copy_result_with_export_paths(
    result: DatabaseHealthResult,
    export_paths: tuple[Path, ...],
) -> DatabaseHealthResult:
    return DatabaseHealthResult(
        connection_ok=result.connection_ok,
        database_name=result.database_name,
        server_version=result.server_version,
        current_user=result.current_user,
        table_statuses=result.table_statuses,
        table_inventory=result.table_inventory,
        core_table_columns=result.core_table_columns,
        core_table_shapes=result.core_table_shapes,
        possible_event_market_tables=result.possible_event_market_tables,
        export_paths=export_paths,
        error_message=result.error_message,
        table_check_error=result.table_check_error,
        inventory_error=result.inventory_error,
        column_inventory_error=result.column_inventory_error,
    )


def _value_at(row: Any, index: int) -> str | None:
    value = row[index]
    if value is None:
        return None
    return str(value)


def _int_value_at(row: Any, index: int) -> int | None:
    value = row[index]
    if value is None:
        return None
    return int(value)


def _table_status_label(exists: bool | None) -> str:
    if exists is True:
        return "OK"
    if exists is False:
        return "MISSING"
    return "UNKNOWN"


def _format_count(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:,}"


def _count_views(table_inventory: tuple[TableInventory, ...]) -> int:
    return sum(1 for table in table_inventory if (table.table_type or "").upper() == "VIEW")


def _find_possible_event_market_tables(
    table_inventory: tuple[TableInventory, ...],
) -> tuple[str, ...]:
    tokens = ("event", "cyber", "security", "securit", "price", "market", "index")
    names = [
        table.table_name
        for table in table_inventory
        if any(token in table.table_name.lower() for token in tokens)
    ]
    return tuple(sorted(names))


def _log_result(
    result: DatabaseHealthResult,
    logger: logging.Logger | None,
) -> None:
    if logger is None:
        return

    if not result.connection_ok:
        logger.warning("Database health check failed: %s", result.error_message)
        return

    missing_tables = [
        table.name for table in result.table_statuses if table.exists is False
    ]
    empty_tables = [
        shape.table_name for shape in result.core_table_shapes if shape.status == "EMPTY"
    ]
    unknown_tables = [
        table.name for table in result.table_statuses if table.exists is None
    ]

    logger.info(
        "Database health check succeeded: database=%s server_version=%s current_user=%s "
        "tables=%s core_columns=%s",
        result.database_name,
        result.server_version,
        result.current_user,
        len(result.table_inventory),
        len(result.core_table_columns),
    )
    if missing_tables:
        logger.warning("Database health check missing tables: %s", ", ".join(missing_tables))
    if empty_tables:
        logger.warning("Database health check empty core tables: %s", ", ".join(empty_tables))
    if unknown_tables:
        logger.warning("Database health check unknown table status: %s", ", ".join(unknown_tables))
    if result.table_check_error:
        logger.warning("Database health table check failed: %s", result.table_check_error)
    if result.inventory_error:
        logger.warning("Database inventory failed: %s", result.inventory_error)
    if result.column_inventory_error:
        logger.warning("Core column inventory failed: %s", result.column_inventory_error)
