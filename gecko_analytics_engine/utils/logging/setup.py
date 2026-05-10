"""Logging setup for the Project Gecko Analytics Engine."""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "gecko_analytics_engine"
_FILE_HANDLER_NAME = "gecko_analytics_engine_file"


def configure_logging(log_file: Path, log_level: str = "INFO") -> logging.Logger:
    """Configure and return the application logger."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_log_level(log_level))
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not _has_handler(logger, _FILE_HANDLER_NAME):
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.name = _FILE_HANDLER_NAME
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logger.level)
        logger.addHandler(file_handler)

    for handler in logger.handlers:
        if handler.name == _FILE_HANDLER_NAME:
            handler.setLevel(logger.level)

    return logger


def _has_handler(logger: logging.Logger, handler_name: str) -> bool:
    return any(handler.name == handler_name for handler in logger.handlers)


def _coerce_log_level(log_level: str) -> int:
    level = getattr(logging, log_level.upper(), None)
    if isinstance(level, int):
        return level
    return logging.INFO
