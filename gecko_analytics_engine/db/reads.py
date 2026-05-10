"""Reusable read-only database helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


SqlParams = Sequence[Any] | dict[str, Any] | None


def table_exists(connection: Any, table_name: str, database_name: str | None = None) -> bool:
    """Return whether a table exists in the selected database."""

    schema = database_name or _selected_database(connection)
    if not schema:
        return False

    value = safe_scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, table_name),
    )
    return bool(value)


def get_table_columns(
    connection: Any,
    table_name: str,
    database_name: str | None = None,
) -> tuple[str, ...]:
    """Return column names for a table, or an empty tuple when unavailable."""

    schema = database_name or _selected_database(connection)
    if not schema:
        return ()

    rows = safe_fetch_all(
        connection,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table_name),
    )
    return tuple(str(row[0]) for row in rows)


def count_rows(connection: Any, table_name: str) -> int | None:
    """Count rows in a table, returning None when the count is unavailable."""

    if not table_name.replace("_", "").isalnum():
        return None
    if not table_exists(connection, table_name):
        return None
    value = safe_scalar(connection, f"SELECT COUNT(*) FROM `{table_name}`")
    return int(value) if value is not None else None


def safe_scalar(connection: Any, sql: str, params: SqlParams = None) -> Any:
    """Execute a read-only scalar query and return None on failure."""

    rows = safe_fetch_all(connection, sql, params)
    if not rows:
        return None
    return rows[0][0]


def safe_fetch_all(
    connection: Any,
    sql: str,
    params: SqlParams = None,
) -> tuple[Any, ...]:
    """Execute a read-only query and return an empty tuple on failure."""

    cursor = connection.cursor()
    try:
        cursor.execute(sql, params or ())
        return tuple(cursor.fetchall())
    except Exception:
        return ()
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _selected_database(connection: Any) -> str | None:
    value = safe_scalar(connection, "SELECT DATABASE()")
    return str(value) if value else None
