"""Unit tests for generated artifact export helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from gecko_analytics_engine.exports import write_dataclass_json, write_rows_csv


@dataclass(frozen=True)
class ExportRow:
    name: str
    count: int


@dataclass(frozen=True)
class ExportPayload:
    name: str
    export_paths: tuple[Path, ...]
    rows: tuple[ExportRow, ...]


class ExportHelpersTest(unittest.TestCase):
    def test_write_rows_csv_from_dataclasses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "exports" / "rows.csv"

            write_rows_csv(path, (ExportRow("alpha", 1),), ("name", "count"))

            contents = path.read_text(encoding="utf-8")
            self.assertIn("name,count", contents)
            self.assertIn("alpha,1", contents)

    def test_write_rows_csv_from_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "exports" / "rows.csv"

            write_rows_csv(path, ({"name": "beta", "count": 2},), ("name", "count"))

            self.assertIn("beta,2", path.read_text(encoding="utf-8"))

    def test_write_dataclass_json_converts_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "reports" / "payload.json"
            payload = ExportPayload("report", (root / "exports" / "rows.csv",), (ExportRow("alpha", 1),))

            write_dataclass_json(path, payload)

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "report")
            self.assertEqual(data["rows"][0]["name"], "alpha")
            self.assertIsInstance(data["export_paths"][0], str)


if __name__ == "__main__":
    unittest.main()
