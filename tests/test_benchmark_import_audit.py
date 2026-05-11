"""Unit tests for benchmark import readiness audit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.market_data.index_audit import (
    BenchmarkImportAudit,
    BenchmarkRepairPlan,
    IndexAuditRow,
    MissingBenchmarkDateRow,
    build_benchmark_repair_plan,
    classify_gap_frequency,
    discover_benchmark_source_files,
    export_benchmark_import_audit,
    format_benchmark_import_audit,
)
from gecko_analytics_engine.market_data.menu import market_data_menu_action
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


class BenchmarkImportAuditTest(unittest.TestCase):
    def test_gap_classification(self) -> None:
        self.assertEqual(classify_gap_frequency((1, 1, 3, 1)), "daily_like")
        self.assertEqual(classify_gap_frequency((7, 7, 7)), "weekly_like")
        self.assertEqual(classify_gap_frequency((30, 31, 29)), "monthly_like")
        self.assertEqual(classify_gap_frequency((1, 45, 3, 90)), "sparse_or_mixed")

    def test_duplicate_summary_formatting(self) -> None:
        result = BenchmarkImportAudit(
            generated_at="2026-05-11T00:00:00+00:00",
            connection_ok=True,
            audit_status="NEEDS_IMPORT_REPAIR",
            index_rows=(
                IndexAuditRow(
                    1,
                    "DJIA",
                    "Dow",
                    10,
                    9,
                    "2020-01-01",
                    "2020-01-15",
                    duplicate_index_date_rows=1,
                    weekend_rows=0,
                    non_trading_calendar_rows=2,
                    no_calendar_match_rows=0,
                    largest_gap_days=5,
                    largest_gap_start_date="2020-01-03",
                    largest_gap_end_date="2020-01-08",
                    median_gap_days=1.0,
                    frequency_classification="daily_like",
                    expected_trading_days=11,
                    density_pct=90.91,
                    missing_trading_day_count=1,
                ),
            ),
            repair_plan=BenchmarkRepairPlan(
                "index_daily_prices",
                "ignore dji_daily_prices",
                "Repair DJIA first",
                "2020-01-01 to 2020-01-15",
                "80%",
                "reconcile",
                ("one row per index per trading day",),
            ),
        )

        output = "\n".join(format_benchmark_import_audit(result))

        self.assertIn("DJIA (id=1): rows=10", output)
        self.assertIn("largest_gap=5", output)
        self.assertIn("Canonical table: index_daily_prices", output)

    def test_missing_date_export_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            result = BenchmarkImportAudit(
                generated_at="2026-05-11T00:00:00+00:00",
                connection_ok=True,
                audit_status="NEEDS_IMPORT_REPAIR",
                index_rows=(
                    IndexAuditRow(1, "DJIA", "Dow", 1, 1, "2020-01-01", "2020-01-01", 0, 0, 0, 0, None, None, None, None, "insufficient_data", 1, 100.0, 0),
                ),
                missing_dates=(MissingBenchmarkDateRow(1, "DJIA", "2020-01-02", "first"),),
                repair_plan=build_benchmark_repair_plan((), 0),
            )

            exported = export_benchmark_import_audit(result, paths)

            self.assertEqual(
                exported.export_paths,
                (
                    paths.reports_dir / "benchmark_import_audit.json",
                    paths.exports_dir / "benchmark_index_gap_summary.csv",
                    paths.exports_dir / "benchmark_missing_dates.csv",
                    paths.exports_dir / "benchmark_source_file_candidates.csv",
                    paths.reports_dir / "benchmark_import_repair_plan.json",
                ),
            )
            self.assertIn("2020-01-02", (paths.exports_dir / "benchmark_missing_dates.csv").read_text(encoding="utf-8"))
            payload = json.loads((paths.reports_dir / "benchmark_import_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["audit_status"], "NEEDS_IMPORT_REPAIR")

    def test_source_file_discovery_with_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            (paths.data_root / "raw").mkdir(parents=True)
            (paths.data_root / "external").mkdir(parents=True)
            (paths.data_root / "raw" / "sp500_prices.csv").write_text("date,close\n", encoding="utf-8")
            (paths.data_root / "external" / "notes.csv").write_text("not,matched\n", encoding="utf-8")

            candidates = discover_benchmark_source_files(paths)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].file_name, "sp500_prices.csv")
            self.assertIn("sp500", candidates[0].matched_terms)

    def test_repair_plan_prefers_densest_available(self) -> None:
        rows = (
            IndexAuditRow(2, "SP500", "S&P", 100, 100, None, None, 0, 0, 0, 0, None, None, None, None, "sparse_or_mixed", 1000, 10.0, 900),
            IndexAuditRow(1, "DJIA", "Dow", 800, 800, None, None, 0, 0, 0, 0, None, None, None, None, "daily_like", 1500, 53.33, 700),
        )

        plan = build_benchmark_repair_plan(rows, 0)

        self.assertIn("DJIA", plan.first_index_to_import_or_repair)
        self.assertEqual(plan.recommended_canonical_table, "index_daily_prices")

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
