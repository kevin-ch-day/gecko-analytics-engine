"""Database connection helpers for Project Gecko.

Sprint 1C introduces read-only health checks only. This module centralizes
connection creation so menus and analytics modules do not grow ad hoc database
connection code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from gecko_analytics_engine.config.settings import AppSettings


class DatabaseConnectionError(RuntimeError):
    """Raised when the application cannot open a database connection."""


@contextmanager
def database_connection(settings: AppSettings) -> Iterator[Any]:
    """Open a MariaDB/MySQL connection using configured settings."""

    connection = _connect_with_available_driver(settings)
    try:
        yield connection
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def _connect_with_available_driver(settings: AppSettings) -> Any:
    connector_error: Exception | None = None

    try:
        return _connect_with_mysql_connector(settings)
    except ModuleNotFoundError as exc:
        connector_error = exc
    except Exception as exc:
        raise DatabaseConnectionError(_safe_connection_error(exc, settings)) from exc

    try:
        return _connect_with_pymysql(settings)
    except ModuleNotFoundError:
        raise DatabaseConnectionError(
            "No MariaDB/MySQL Python driver is installed. Install "
            "`mysql-connector-python` or `PyMySQL` to run database health checks."
        ) from connector_error
    except Exception as exc:
        raise DatabaseConnectionError(_safe_connection_error(exc, settings)) from exc


def _connect_with_mysql_connector(settings: AppSettings) -> Any:
    import mysql.connector  # type: ignore[import-not-found]

    return mysql.connector.connect(
        host=settings.db.host,
        port=settings.db.port,
        database=settings.db.name,
        user=settings.db.user,
        password=settings.db.password,
        autocommit=True,
        connection_timeout=5,
    )


def _connect_with_pymysql(settings: AppSettings) -> Any:
    import pymysql  # type: ignore[import-not-found]

    return pymysql.connect(
        host=settings.db.host,
        port=settings.db.port,
        database=settings.db.name,
        user=settings.db.user,
        password=settings.db.password,
        autocommit=True,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        charset="utf8mb4",
    )


def _safe_connection_error(error: Exception, settings: AppSettings) -> str:
    """Return a useful connection error without exposing configured secrets."""

    message = str(error)
    if settings.db.password:
        message = message.replace(settings.db.password, "[redacted]")
    return f"Database connection failed: {error.__class__.__name__}: {message}"
