"""Small CSV writer helpers for generated Project Gecko exports."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def write_rows_csv(
    path: Path,
    rows: Iterable[Any],
    fieldnames: Sequence[str],
) -> None:
    """Write dataclass or mapping rows to a UTF-8 CSV file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=tuple(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_to_dict(row))


def _row_to_dict(row: Any) -> Mapping[str, Any]:
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    if isinstance(row, Mapping):
        return row
    raise TypeError(f"CSV rows must be dataclass instances or mappings, got {type(row).__name__}")
