"""Unit tests for read-only database schema reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.db.schema_checks import (
    ColumnInventory,
    CoreTableShape,
    DatabaseHealthResult,
    TableHealth,
    TableInventory,
    export_database_inventory,
    format_database_health_result,
)
from gecko_analytics_engine.app import build_main_menu
from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.utils.paths import initialize_paths
from gecko_analytics_engine.utils.paths import AppPaths


class SchemaChecksTest(unittest.TestCase):
    def test_format_reports_missing_tables_without_crashing(self) -> None:
        result = DatabaseHealthResult(
            connection_ok=True,
            database_name="gecko",
            server_version="10.6.0-MariaDB",
            current_user="gecko@localhost",
            table_statuses=(
                TableHealth("companies", True),
                TableHealth("securities", False),
            ),
            core_table_shapes=(
                CoreTableShape("companies", True, 12, "OK"),
                CoreTableShape("securities", False, None, "MISSING"),
            ),
        )

        output = "\n".join(format_database_health_result(result))

        self.assertIn("Connection: OK", output)
        self.assertIn("[MISSING] securities", output)
        self.assertIn("[MISSING] securities: Unknown", output)

    def test_format_reports_clean_connection_failure(self) -> None:
        result = DatabaseHealthResult(
            connection_ok=False,
            error_message="Database connection failed: Access denied",
        )

        output = "\n".join(format_database_health_result(result))

        self.assertIn("Connection: FAILED", output)
        self.assertIn("Access denied", output)

    def test_export_database_inventory_writes_csv_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = AppPaths(
                project_root=root,
                data_root=root / "data",
                output_root=root / "output",
                logs_dir=root / "output" / "logs",
                runs_dir=root / "output" / "runs",
                exports_dir=root / "output" / "exports",
                reports_dir=root / "output" / "reports",
                figures_dir=root / "output" / "figures",
                models_dir=root / "output" / "models",
            )
            result = DatabaseHealthResult(
                connection_ok=True,
                database_name="gecko",
                server_version="10.6.0-MariaDB",
                current_user="gecko@localhost",
                table_statuses=(TableHealth("companies", True),),
                table_inventory=(TableInventory("companies", "BASE TABLE", 5, 3),),
                core_table_columns=(
                    ColumnInventory(
                        "companies",
                        "company_id",
                        "int",
                        "NO",
                        "PRI",
                        None,
                        "auto_increment",
                    ),
                ),
                core_table_shapes=(CoreTableShape("companies", True, 5, "OK"),),
            )

            exported = export_database_inventory(result, paths)

            schema_csv = paths.exports_dir / "schema_inventory.csv"
            columns_csv = paths.exports_dir / "core_table_columns.csv"
            counts_csv = paths.exports_dir / "core_table_counts.csv"
            report_json = paths.reports_dir / "database_inventory.json"

            self.assertEqual(
                exported.export_paths,
                (schema_csv, columns_csv, counts_csv, report_json),
            )
            self.assertIn("companies", schema_csv.read_text(encoding="utf-8"))
            self.assertIn("company_id", columns_csv.read_text(encoding="utf-8"))
            self.assertIn("row_count", counts_csv.read_text(encoding="utf-8"))
            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["database_name"], "gecko")
            self.assertIn("generated_at", payload)
            self.assertIn("export_paths", payload)

    def test_initialize_paths_creates_required_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = AppSettings(
                db=DatabaseSettings(
                    host="localhost",
                    port=3306,
                    name="gecko",
                    user="root",
                    password="",
                ),
                output_root=Path("output"),
                data_root=Path("data"),
                log_level="INFO",
            )

            paths = initialize_paths(settings, root)

            self.assertTrue(paths.data_root.exists())
            self.assertTrue(paths.logs_dir.exists())
            self.assertTrue(paths.exports_dir.exists())
            self.assertEqual(paths.project_root, root.resolve())

    def test_main_menu_constructs_with_reports_and_database_health(self) -> None:
        menu = build_main_menu()

        labels = [item.label for item in menu.items]

        self.assertIn("Database Health", labels)
        self.assertIn("Reports", labels)


if __name__ == "__main__":
    unittest.main()
