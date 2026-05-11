"""Small JSON writer helpers for generated Project Gecko exports."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    """Write JSON with path/dataclass values converted to serializable shapes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_ready(payload), indent=2), encoding="utf-8")


def write_dataclass_json(path: Path, value: Any) -> None:
    """Write a dataclass instance as JSON."""

    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError(f"Expected dataclass instance, got {type(value).__name__}")
    write_json(path, asdict(value))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value
