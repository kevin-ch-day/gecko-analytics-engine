"""Unit tests for market-data forensic diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.market_data.menu import market_data_menu_action
from gecko_analytics_engine.market_data.price_forensics import (
    EventWindowFailureRow,
    IndexPriceDensityRow,
    MarketDataForensicsReport,
    RepairPriorityRow,
    SecurityPriceDensityRow,
    build_repair_priorities,
    classify_density,
    detect_largest_gap,
    export_market_data_forensics,
    weekday_distribution_label,
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


class MarketDataForensicsTest(unittest.TestCase):
    def test_weekday_distribution_formatting(self) -> None:
        label = weekday_distribution_label({"Monday": 3, "Wednesday": 1, "Sunday": 2})

        self.assertEqual(label, "Monday=3; Wednesday=1; Sunday=2")

    def test_density_classification(self) -> None:
        self.assertEqual(classify_density(None), "unknown")
        self.assertEqual(classify_density(85.0), "daily")
        self.assertEqual(classify_density(20.8), "weekly")
        self.assertEqual(classify_density(8.0), "monthly_or_sparse")
        self.assertEqual(classify_density(1.0), "sparse")

    def test_largest_gap_detection(self) -> None:
        gap, start, end = detect_largest_gap(
            (
                date(2024, 1, 1),
                date(2024, 1, 8),
                date(2024, 2, 5),
                date(2024, 2, 12),
            )
        )

        self.assertEqual(gap, 28)
        self.assertEqual(start, date(2024, 1, 8))
        self.assertEqual(end, date(2024, 2, 5))

    def test_repair_priority_ranking_counts_rows_not_distinct_windows(self) -> None:
        failures = (
            EventWindowFailureRow(1, "Event A", 10, "AAA", "D1", None, None, None, None, None, 0, 0, "missing_security_price;missing_benchmark_price", "", "", "", ""),
            EventWindowFailureRow(1, "Event A", 11, "BBB", "D1", None, None, None, None, None, 0, 0, "missing_security_price;missing_benchmark_price", "", "", "", ""),
            EventWindowFailureRow(2, "Event B", 10, "AAA", "D3", None, None, None, None, None, 0, 1, "missing_security_price", "", "", "", ""),
        )

        priorities = build_repair_priorities(failures)
        by_key = {(row.priority_type, row.key): row for row in priorities}

        self.assertEqual(by_key[("benchmark", "index_daily_prices")].affected_rows, 2)
        self.assertEqual(by_key[("security", "AAA")].affected_rows, 2)
        self.assertEqual(by_key[("window", "D1")].affected_rows, 2)

    def test_export_generation_with_fake_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            result = MarketDataForensicsReport(
                generated_at="2026-05-11T00:00:00+00:00",
                connection_ok=True,
                forensic_status="NEEDS_MARKET_DATA_REPAIR",
                database_name="gecko",
                security_rows=(
                    SecurityPriceDensityRow(
                        1,
                        "AAA",
                        "Alpha",
                        True,
                        10,
                        10,
                        "2024-01-01",
                        "2024-03-01",
                        40,
                        25.0,
                        "weekly",
                        8,
                        "Monday=8; Tuesday=2",
                        1,
                        14,
                        "2024-01-08",
                        "2024-01-22",
                    ),
                ),
                index_rows=(
                    IndexPriceDensityRow(
                        1,
                        "DJIA",
                        "Dow",
                        10,
                        10,
                        "2024-01-01",
                        "2024-03-01",
                        40,
                        25.0,
                        "weekly",
                        "Monday=10",
                        1,
                        0,
                        30,
                        14,
                        "2024-01-08",
                        "2024-01-22",
                    ),
                ),
                failure_rows=(
                    EventWindowFailureRow(1, "Event A", 1, "AAA", "D1", "2024-01-01", "2024-01-02", "2024-01-01", "2024-01-03", 2, 0, 0, "missing_security_price;missing_benchmark_price", "2024-01-02", "2024-01-02", "2024-01-02:+0", "2024-01-02:+0"),
                ),
                repair_priorities=(
                    RepairPriorityRow("security", "AAA", 1, 1, "Missing prices", "Repair AAA"),
                ),
            )

            exported = export_market_data_forensics(result, paths)

            self.assertEqual(
                exported.export_paths,
                (
                    paths.reports_dir / "market_data_forensics_report.json",
                    paths.exports_dir / "security_price_density_by_security.csv",
                    paths.exports_dir / "index_price_density_by_index.csv",
                    paths.exports_dir / "event_window_failure_drilldown.csv",
                    paths.exports_dir / "market_data_repair_priorities.csv",
                ),
            )
            payload = json.loads((paths.reports_dir / "market_data_forensics_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["forensic_status"], "NEEDS_MARKET_DATA_REPAIR")
            self.assertIn("AAA", (paths.exports_dir / "security_price_density_by_security.csv").read_text(encoding="utf-8"))

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
