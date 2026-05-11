"""Unit tests for daily market-data repair scope planning."""

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
    build_daily_repair_priorities,
    classify_benchmark_priority,
    classify_security_priority,
    daily_study_policy_notes,
    export_daily_data_repair_scope,
    format_required_range,
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


class DailyDataRepairScopeTest(unittest.TestCase):
    def test_repair_target_priority_classification(self) -> None:
        self.assertEqual(classify_benchmark_priority("DJIA", 0), "high")
        self.assertEqual(classify_benchmark_priority("SP500", 0), "medium")
        self.assertEqual(classify_benchmark_priority("OTHER", 3), "high")
        self.assertEqual(classify_security_priority(0, True, "unknown"), "high")
        self.assertEqual(classify_security_priority(2, False, "weekly"), "high")
        self.assertEqual(classify_security_priority(0, False, "weekly"), "medium")
        self.assertEqual(classify_security_priority(0, False, "daily"), "low")

    def test_required_date_range_formatting(self) -> None:
        self.assertEqual(format_required_range("2020-01-01", "2020-12-31"), "2020-01-01 to 2020-12-31")
        self.assertEqual(format_required_range(None, "2020-12-31"), "Unavailable")

    def test_daily_study_policy_text(self) -> None:
        notes = daily_study_policy_notes()

        self.assertTrue(any("OHLCV" in note for note in notes))
        self.assertTrue(any("weekly-like" in note for note in notes))
        self.assertTrue(any("index_daily_prices" in note for note in notes))

    def test_priority_ranking_from_fake_targets(self) -> None:
        benchmark = BenchmarkImportTarget(
            1,
            "DJIA",
            "Dow",
            "index_daily_prices",
            844,
            "2020-01-01",
            "2026-01-01",
            52.0,
            "2013-01-01",
            "2026-01-01",
            3000,
            844,
            2156,
            25,
            20,
            "high",
        )
        security = SecurityImportTarget(
            6,
            "JBSAY",
            "JBS",
            0,
            None,
            None,
            None,
            "unknown",
            True,
            "2021-01-01",
            "2021-12-31",
            252,
            0,
            252,
            7,
            7,
            "high",
        )

        priorities = build_daily_repair_priorities((benchmark,), (security,))

        self.assertEqual(priorities[0].label, "DJIA")
        self.assertEqual(priorities[1].label, "JBSAY")
        self.assertEqual(priorities[0].priority, "high")

    def test_export_generation_from_fake_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            report = DailyDataRepairScopeReport(
                generated_at="2026-05-11T00:00:00+00:00",
                connection_ok=True,
                repair_status="DAILY_IMPORT_SCOPE_REQUIRED",
                database_name="gecko",
                event_window_required_range="2020-01-01 to 2026-01-01",
                policy_notes=daily_study_policy_notes(),
                benchmark_targets=(
                    BenchmarkImportTarget(1, "DJIA", "Dow", "index_daily_prices", 10, "2020-01-01", "2020-02-01", 50.0, "2020-01-01", "2020-02-01", 20, 10, 10, 2, 2, "high"),
                ),
                security_targets=(
                    SecurityImportTarget(1, "AAA", "Alpha", 10, "2020-01-01", "2020-02-01", 50.0, "weekly", False, "2020-01-01", "2020-02-01", 20, 10, 10, 1, 1, "high"),
                ),
            )
            report = DailyDataRepairScopeReport(
                **{**report.__dict__, "repair_priorities": build_daily_repair_priorities(report.benchmark_targets, report.security_targets)}
            )

            exported = export_daily_data_repair_scope(report, paths)

            self.assertEqual(
                exported.export_paths,
                (
                    paths.reports_dir / "daily_market_data_repair_scope.json",
                    paths.exports_dir / "benchmark_import_targets.csv",
                    paths.exports_dir / "security_import_targets.csv",
                    paths.exports_dir / "daily_data_repair_priorities.csv",
                ),
            )
            payload = json.loads((paths.reports_dir / "daily_market_data_repair_scope.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["repair_status"], "DAILY_IMPORT_SCOPE_REQUIRED")
            self.assertIn("DJIA", (paths.exports_dir / "benchmark_import_targets.csv").read_text(encoding="utf-8"))

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
