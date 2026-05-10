"""Application settings for the Project Gecko Analytics Engine."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatabaseSettings:
    """Database connection settings.

    These values are loaded now so later sprints can connect deliberately. Sprint
    1B does not open a database connection.
    """

    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class AppSettings:
    """Resolved application settings."""

    db: DatabaseSettings
    output_root: Path
    data_root: Path
    log_level: str


def load_settings(project_root: Path | None = None) -> AppSettings:
    """Load settings from `.env` and process environment variables."""

    root = project_root or Path.cwd()
    _load_dotenv(root / ".env")

    return AppSettings(
        db=DatabaseSettings(
            host=_get_env("GECKO_DB_HOST", "localhost"),
            port=_get_int_env("GECKO_DB_PORT", 3306),
            name=_get_env("GECKO_DB_NAME", "gecko_research"),
            user=_get_env("GECKO_DB_USER", "gecko_user"),
            password=_get_env("GECKO_DB_PASSWORD", ""),
        ),
        output_root=Path(_get_env("GECKO_OUTPUT_ROOT", "output")),
        data_root=Path(_get_env("GECKO_DATA_ROOT", "data")),
        log_level=_get_env("GECKO_LOG_LEVEL", "INFO").upper(),
    )


def _load_dotenv(dotenv_path: Path) -> None:
    """Load simple KEY=VALUE pairs from a local `.env` file when present."""

    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _normalize_env_value(value.strip())

        if key and key not in os.environ:
            os.environ[key] = value


def _normalize_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _get_env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
