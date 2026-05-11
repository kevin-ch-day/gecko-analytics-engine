"""Unit tests for benchmark density and selection diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.market_data.indexes import (
    BenchmarkCoverageReport,
    BenchmarkCoverageRow,
    build_benchmark_selection_diagnostic,
    calculate_density_pct,
    export_benchmark_selection_diagnostic,
    format_benchmark_selection_diagnostic,
    select_density_aware_benchmark,
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


class BenchmarkSelectionTest(unittest.TestCase):
    def test_density_calculation_from_fake_data(self) -> None:
        self.assertEqual(calculate_density_pct(80, 100), 80.0)
        self.assertEqual(calculate_density_pct(173, 1729), 10.01)
        self.assertIsNone(calculate_density_pct(10, None))

    def test_recommendation_prefers_density_over_sparse_high_overlap(self) -> None:
        sp500 = BenchmarkCoverageRow(
            2,
            "SP500",
            "S&P 500",
            173,
            "2012-01-02",
            "2026-05-01",
            265,
            expected_trading_days=1700,
            density_pct=10.18,
            d1_overlap_rows=40,
        )
        djia = BenchmarkCoverageRow(
            1,
            "DJIA",
            "Dow Jones Industrial Average",
            844,
            "2020-01-06",
            "2026-05-08",
            253,
            expected_trading_days=820,
            density_pct=102.93,
            d1_overlap_rows=50,
        )

        selected = select_density_aware_benchmark((sp500, djia))

        self.assertEqual(selected, djia)

    def test_recommendation_uses_high_overlap_when_density_is_acceptable(self) -> None:
        sp500 = BenchmarkCoverageRow(2, "SP500", "S&P 500", 900, None, None, 300, density_pct=95.0, d1_overlap_rows=80)
        djia = BenchmarkCoverageRow(1, "DJIA", "Dow Jones Industrial Average", 850, None, None, 250, density_pct=93.0, d1_overlap_rows=70)

        selected = select_density_aware_benchmark((sp500, djia))

        self.assertEqual(selected, sp500)

    def test_sparse_benchmark_warning_is_formatted(self) -> None:
        coverage = BenchmarkCoverageReport(
            generated_at="2026-05-11T00:00:00+00:00",
            connection_ok=True,
            coverage_status="OK",
            index_rows=(
                BenchmarkCoverageRow(2, "SP500", "S&P 500", 173, None, None, 265, density_pct=10.0, d1_overlap_rows=30),
            ),
            dji_daily_price_rows=0,
        )

        diagnostic = build_benchmark_selection_diagnostic(coverage)
        output = "\n".join(format_benchmark_selection_diagnostic(diagnostic))

        self.assertEqual(diagnostic.diagnostic_status, "NEEDS_REVIEW")
        self.assertIn("below the 80% density threshold", output)
        self.assertIn("SP500 has the strongest event-window overlap but appears sparse", output)

    def test_export_generation_with_fake_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            coverage = BenchmarkCoverageReport(
                generated_at="2026-05-11T00:00:00+00:00",
                connection_ok=True,
                coverage_status="OK",
                database_name="gecko",
                index_rows=(
                    BenchmarkCoverageRow(1, "DJIA", "Dow", 844, "2020-01-06", "2026-05-08", 253, density_pct=95.0),
                ),
            )

            diagnostic = build_benchmark_selection_diagnostic(coverage)
            exported = export_benchmark_selection_diagnostic(diagnostic, paths)

            json_path = paths.reports_dir / "benchmark_selection_diagnostic.json"
            density_csv = paths.exports_dir / "benchmark_density_detail.csv"
            recommendation_csv = paths.exports_dir / "benchmark_recommendation.csv"
            self.assertEqual(exported.export_paths, (json_path, density_csv, recommendation_csv))
            self.assertIn("density_pct", density_csv.read_text(encoding="utf-8"))
            self.assertIn("primary", recommendation_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["recommended_primary_benchmark"], "DJIA (id=1)")

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
