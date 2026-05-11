"""Unit tests for daily price source manifest generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.market_data.menu import market_data_menu_action
from gecko_analytics_engine.market_data.repair_scope import (
    BenchmarkImportTarget,
    DailyDataRepairScopeReport,
    SecurityImportTarget,
)
from gecko_analytics_engine.market_data.source_manifest import (
    PREFERRED_DATE_RANGE,
    benchmark_filename_patterns,
    build_collection_checklist,
    build_daily_price_source_manifest,
    ensure_source_folders,
    export_daily_price_source_manifest,
    security_filename_patterns,
)
from gecko_analytics_engine.utils.paths import AppPaths


def _paths(root: Path) -> AppPaths:
    return AppPaths(
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


def _repair_scope() -> DailyDataRepairScopeReport:
    return DailyDataRepairScopeReport(
        generated_at="2026-05-11T00:00:00+00:00",
        connection_ok=True,
        repair_status="DAILY_IMPORT_SCOPE_REQUIRED",
        database_name="gecko",
        event_window_required_range="2013-04-05 to 2025-09-03",
        benchmark_targets=(
            BenchmarkImportTarget(1, "DJIA", "Dow Jones Industrial Average", "index_daily_prices", 844, "2020-01-01", "2026-05-01", 52.98, "2013-04-05", "2025-09-03", 3000, 500, 2500, 97, 37, "high"),
            BenchmarkImportTarget(2, "SP500", "S&P 500", "index_daily_prices", 173, "2012-01-01", "2026-05-01", 5.16, "2013-04-05", "2025-09-03", 3000, 100, 2900, 85, 70, "high"),
        ),
        security_targets=(
            SecurityImportTarget(6, "JBSAY", "JBS", 0, None, None, None, "unknown", True, "2020-09-11", "2022-02-16", 360, 0, 360, 7, 7, "high"),
            SecurityImportTarget(11, "TMUS", "T-Mobile", 2928, "2017-01-02", "2026-05-04", 20.81, "weekly", False, "2017-12-06", "2024-01-17", 1500, 300, 1200, 1, 1, "high"),
            SecurityImportTarget(99, "ZZZ", "No Need", 3000, "2017-01-02", "2026-05-04", 95.0, "daily", False, "2017-01-02", "2026-05-04", 2200, 2200, 0, 0, 0, "low"),
        ),
    )


class DailyPriceSourceManifestTest(unittest.TestCase):
    def test_expected_filename_patterns(self) -> None:
        self.assertEqual(benchmark_filename_patterns("DJIA"), "DJI_*.csv; DJIA_*.csv")
        self.assertEqual(benchmark_filename_patterns("SP500"), "SPX_*.csv; SP500_*.csv")
        self.assertIn("IXIC_*.csv", benchmark_filename_patterns("NASDAQ_COMP"))
        self.assertEqual(security_filename_patterns("tmus"), "TMUS_*.csv; TMUS_daily_*.csv")

    def test_source_folders_and_gitkeep_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))

            ensure_source_folders(paths)

            self.assertTrue((paths.data_root / "raw" / "indexes" / ".gitkeep").exists())
            self.assertTrue((paths.data_root / "raw" / "securities" / ".gitkeep").exists())

    def test_manifest_builds_required_and_preferred_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))

            manifest = build_daily_price_source_manifest(_repair_scope(), paths, {6: "OTC", 11: "NASDAQ"})

            self.assertEqual(manifest.required_date_range, "2013-04-05 to 2025-09-03")
            self.assertEqual(manifest.preferred_date_range, PREFERRED_DATE_RANGE)
            self.assertEqual(len(manifest.benchmark_rows), 2)
            self.assertEqual(len(manifest.security_rows), 2)
            self.assertEqual(manifest.security_rows[0].exchange_code, "OTC")

    def test_checklist_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            manifest = build_daily_price_source_manifest(_repair_scope(), paths, {6: "OTC"})

            checklist = build_collection_checklist(manifest)

            self.assertIn("data", checklist)
            self.assertIn("2013-04-05 to 2025-09-03", checklist)
            self.assertIn("2012-01-01 to 2026-05-01", checklist)
            self.assertIn("2 -> 6 Validate Candidate Price CSVs", checklist)
            self.assertIn("JBSAY", checklist)

    def test_manifest_export_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            manifest = build_daily_price_source_manifest(_repair_scope(), paths, {6: "OTC"})

            exported = export_daily_price_source_manifest(manifest, paths)

            self.assertEqual(
                exported.export_paths,
                (
                    paths.reports_dir / "daily_price_source_manifest.json",
                    paths.exports_dir / "benchmark_source_manifest.csv",
                    paths.exports_dir / "security_source_manifest.csv",
                    paths.reports_dir / "daily_price_collection_checklist.md",
                ),
            )
            payload = json.loads((paths.reports_dir / "daily_price_source_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["manifest_status"], "SOURCE_FILES_REQUIRED")
            self.assertIn("SPX_*.csv", (paths.exports_dir / "benchmark_source_manifest.csv").read_text(encoding="utf-8"))
            self.assertIn("JBSAY", (paths.reports_dir / "daily_price_collection_checklist.md").read_text(encoding="utf-8"))

    def test_market_data_submenu_action_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = AppSettings(
                db=DatabaseSettings("localhost", 3306, "gecko", "user", ""),
                output_root=root / "output",
                data_root=root / "data",
                log_level="INFO",
            )

            action = market_data_menu_action(settings, _paths(root))

            self.assertTrue(callable(action))


if __name__ == "__main__":
    unittest.main()
