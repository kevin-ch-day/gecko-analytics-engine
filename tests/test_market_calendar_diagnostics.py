"""Unit tests for market calendar diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.market_data.calendar import (
    CalendarDiagnosticMetric,
    CalendarDiagnosticNote,
    MarketCalendarDiagnosticReport,
    NonTradingDateSummary,
    NonTradingSecuritySummary,
    build_market_calendar_diagnostic_notes,
    classify_weekday,
    determine_calendar_diagnostic_status,
    export_market_calendar_diagnostic,
    format_market_calendar_diagnostic,
)
from gecko_analytics_engine.utils.paths import AppPaths


class MarketCalendarDiagnosticTest(unittest.TestCase):
    def test_classify_weekday(self) -> None:
        self.assertEqual(classify_weekday("2026-05-09"), "weekend")
        self.assertEqual(classify_weekday("2026-05-08"), "weekday")

    def test_format_diagnostic_status(self) -> None:
        result = MarketCalendarDiagnosticReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            diagnostic_status="NEEDS_REVIEW",
            database_name="gecko",
            metrics=(
                CalendarDiagnosticMetric(
                    "price/calendar comparison",
                    "security price rows marked non-trading",
                    2356,
                    "OK",
                ),
            ),
            notes=(CalendarDiagnosticNote("WARNING", "calendar scope needs review"),),
        )

        output = "\n".join(format_market_calendar_diagnostic(result))

        self.assertIn("Overall status: NEEDS_REVIEW", output)
        self.assertIn("security price rows marked non-trading: 2,356", output)
        self.assertIn("calendar scope needs review", output)

    def test_missing_table_blocks(self) -> None:
        metrics = (
            CalendarDiagnosticMetric(
                "calendar schema",
                "market_calendar columns",
                None,
                "MISSING",
            ),
        )

        notes = build_market_calendar_diagnostic_notes(metrics)

        self.assertEqual(determine_calendar_diagnostic_status(notes), "BLOCKED")
        self.assertTrue(any("market_calendar is unavailable" in note.message for note in notes))

    def test_missing_column_formats_cleanly(self) -> None:
        result = MarketCalendarDiagnosticReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            diagnostic_status="BLOCKED",
            metrics=(
                CalendarDiagnosticMetric(
                    "price/calendar comparison",
                    "security price rows marked non-trading",
                    None,
                    "UNAVAILABLE",
                    "missing price/calendar columns",
                ),
            ),
        )

        output = "\n".join(format_market_calendar_diagnostic(result))

        self.assertIn("[UNAVAILABLE] price/calendar comparison - security price rows marked non-trading", output)
        self.assertIn("missing price/calendar columns", output)

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
            result = MarketCalendarDiagnosticReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                diagnostic_status="NEEDS_REVIEW",
                database_name="gecko",
                metrics=(
                    CalendarDiagnosticMetric(
                        "price/calendar comparison",
                        "security price rows marked non-trading",
                        1,
                        "OK",
                    ),
                ),
                notes=(CalendarDiagnosticNote("WARNING", "review needed"),),
                top_dates=(
                    NonTradingDateSummary("2026-05-09", 3, "weekend", "calendar_non_trading"),
                ),
                top_securities=(
                    NonTradingSecuritySummary(1, "ABC", "NASDAQ", 3),
                ),
            )

            exported = export_market_calendar_diagnostic(result, paths)

            json_path = paths.reports_dir / "market_calendar_diagnostic_report.json"
            dates_csv = paths.exports_dir / "non_trading_price_dates_summary.csv"
            securities_csv = paths.exports_dir / "non_trading_price_securities_summary.csv"
            self.assertEqual(exported.export_paths, (json_path, dates_csv, securities_csv))
            self.assertIn("2026-05-09", dates_csv.read_text(encoding="utf-8"))
            self.assertIn("ABC", securities_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostic_status"], "NEEDS_REVIEW")


if __name__ == "__main__":
    unittest.main()
