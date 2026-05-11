"""Unit tests for market data coverage reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.market_data.coverage import (
    CoverageIssue,
    CoverageMetric,
    MarketDataCoverageReport,
    build_market_data_coverage_issues,
    determine_market_data_status,
    export_market_data_coverage_report,
    format_market_data_coverage_report,
)
from gecko_analytics_engine.utils.paths import AppPaths


class MarketDataCoverageTest(unittest.TestCase):
    def test_format_market_data_status(self) -> None:
        result = MarketDataCoverageReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            market_data_status="PARTIAL",
            database_name="gecko",
            metrics=(
                CoverageMetric("security price coverage", "total security price rows", 25595, "OK"),
            ),
            issues=(CoverageIssue("WARNING", "benchmark coverage needs verification"),),
        )

        output = "\n".join(format_market_data_coverage_report(result))

        self.assertIn("Overall status: PARTIAL", output)
        self.assertIn("total security price rows: 25,595", output)
        self.assertIn("benchmark coverage needs verification", output)

    def test_missing_required_table_blocks(self) -> None:
        metrics = (
            CoverageMetric(
                "security price coverage",
                "total security price rows",
                None,
                "MISSING",
            ),
        )

        issues = build_market_data_coverage_issues(metrics)

        self.assertEqual(determine_market_data_status(issues), "BLOCKED")
        self.assertTrue(any("security_daily_prices is unavailable" in issue.message for issue in issues))

    def test_missing_column_formats_cleanly(self) -> None:
        result = MarketDataCoverageReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            market_data_status="BLOCKED",
            metrics=(
                CoverageMetric(
                    "security price coverage",
                    "security price trade-date range",
                    None,
                    "UNAVAILABLE",
                    "missing trade_date column",
                ),
            ),
        )

        output = "\n".join(format_market_data_coverage_report(result))

        self.assertIn("[UNAVAILABLE] security price coverage - security price trade-date range", output)
        self.assertIn("missing trade_date column", output)

    def test_warning_only_status_is_partial(self) -> None:
        issues = (CoverageIssue("WARNING", "dji_daily_prices is empty."),)

        self.assertEqual(determine_market_data_status(issues), "PARTIAL")

    def test_export_generation_with_fake_report(self) -> None:
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
            result = MarketDataCoverageReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                market_data_status="READY_FOR_EVENT_STUDY_DATASET",
                database_name="gecko",
                metrics=(CoverageMetric("security price coverage", "total security price rows", 1, "OK"),),
                issues=(),
            )

            exported = export_market_data_coverage_report(result, paths)

            json_path = paths.reports_dir / "market_data_coverage_report.json"
            summary_csv = paths.exports_dir / "market_data_coverage_summary.csv"
            issues_csv = paths.exports_dir / "market_data_coverage_blockers.csv"
            self.assertEqual(exported.export_paths, (json_path, summary_csv, issues_csv))
            self.assertIn("total security price rows", summary_csv.read_text(encoding="utf-8"))
            self.assertIn("severity,message", issues_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["market_data_status"], "READY_FOR_EVENT_STUDY_DATASET")


if __name__ == "__main__":
    unittest.main()
