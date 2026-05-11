"""Unit tests for security price gap analysis."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.market_data.price_gaps import (
    PriceGapIssue,
    SecurityPriceCoverageRow,
    SecurityPriceGapReport,
    build_security_price_gap_issues,
    determine_security_price_gap_status,
    export_security_price_gap_report,
    format_security_price_gap_report,
)
from gecko_analytics_engine.utils.paths import AppPaths


class SecurityPriceGapTest(unittest.TestCase):
    def test_status_with_warnings_needs_review(self) -> None:
        issues = (PriceGapIssue("WARNING", "1 security has no price rows."),)

        self.assertEqual(determine_security_price_gap_status(issues), "NEEDS_REVIEW")

    def test_empty_rows_block(self) -> None:
        issues = build_security_price_gap_issues(())

        self.assertEqual(determine_security_price_gap_status(issues), "BLOCKED")
        self.assertTrue(any("No securities could be analyzed" in issue.message for issue in issues))

    def test_build_issues_from_gap_rows(self) -> None:
        rows = (
            SecurityPriceCoverageRow(
                security_id=1,
                ticker_symbol="ABC",
                exchange_code="NYSE",
                price_rows=10,
                distinct_price_dates=10,
                first_trade_date="2026-01-01",
                last_trade_date="2026-01-31",
                trading_price_dates=10,
                trading_days_in_span=20,
                approximate_missing_trading_days=10,
                non_trading_price_rows=2,
                duplicate_trade_dates=0,
            ),
            SecurityPriceCoverageRow(
                security_id=2,
                ticker_symbol="DEF",
                exchange_code="NASDAQ",
                price_rows=0,
                distinct_price_dates=0,
                first_trade_date=None,
                last_trade_date=None,
                trading_price_dates=0,
                trading_days_in_span=None,
                approximate_missing_trading_days=None,
                non_trading_price_rows=0,
                duplicate_trade_dates=1,
            ),
        )

        messages = "\n".join(issue.message for issue in build_security_price_gap_issues(rows))

        self.assertIn("securities have no price rows", messages)
        self.assertIn("apparent trading-day gaps", messages)
        self.assertIn("10 missing days", messages)
        self.assertIn("non-trading dates", messages)
        self.assertIn("duplicate trade-date groups", messages)

    def test_format_gap_report(self) -> None:
        result = SecurityPriceGapReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            gap_status="NEEDS_REVIEW",
            database_name="gecko",
            securities_analyzed=1,
            securities_with_prices=1,
            securities_with_missing_trading_days=1,
            total_approximate_missing_trading_days=5,
            top_gap_rows=(
                SecurityPriceCoverageRow(
                    1,
                    "ABC",
                    "NYSE",
                    10,
                    10,
                    "2026-01-01",
                    "2026-01-31",
                    10,
                    15,
                    5,
                    1,
                    0,
                ),
            ),
            no_price_rows=(
                SecurityPriceCoverageRow(
                    2,
                    "DEF",
                    "NASDAQ",
                    0,
                    0,
                    None,
                    None,
                    0,
                    None,
                    None,
                    0,
                    0,
                ),
            ),
            issues=(PriceGapIssue("WARNING", "review gaps"),),
        )

        output = "\n".join(format_security_price_gap_report(result))

        self.assertIn("Overall status: NEEDS_REVIEW", output)
        self.assertIn("ABC (NYSE)", output)
        self.assertIn("DEF (NASDAQ)", output)
        self.assertIn("review gaps", output)

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
            rows = (
                SecurityPriceCoverageRow(
                    1,
                    "ABC",
                    "NYSE",
                    10,
                    10,
                    "2026-01-01",
                    "2026-01-31",
                    10,
                    15,
                    5,
                    1,
                    0,
                ),
            )
            result = SecurityPriceGapReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                gap_status="NEEDS_REVIEW",
                database_name="gecko",
                securities_analyzed=1,
                top_gap_rows=rows,
                issues=(PriceGapIssue("WARNING", "review gaps"),),
            )

            exported = export_security_price_gap_report(result, rows, paths)

            json_path = paths.reports_dir / "security_price_gap_report.json"
            detail_csv = paths.exports_dir / "security_price_gap_detail.csv"
            issues_csv = paths.exports_dir / "security_price_gap_issues.csv"
            self.assertEqual(exported.export_paths, (json_path, detail_csv, issues_csv))
            self.assertIn("ABC", detail_csv.read_text(encoding="utf-8"))
            self.assertIn("review gaps", issues_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["gap_status"], "NEEDS_REVIEW")


if __name__ == "__main__":
    unittest.main()
