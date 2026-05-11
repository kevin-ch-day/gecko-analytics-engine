"""Unit tests for event-window price coverage reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.cyber_events.window_coverage import (
    EventWindowCoverageReport,
    EventWindowCoverageRow,
    WindowCoverageIssue,
    build_window_coverage_issues,
    determine_window_coverage_status,
    export_event_window_coverage_report,
    format_event_window_coverage_report,
)
from gecko_analytics_engine.utils.paths import AppPaths


class EventWindowCoverageTest(unittest.TestCase):
    def test_status_with_warnings_is_partial(self) -> None:
        issues = (WindowCoverageIssue("WARNING", "1 row has no security prices."),)

        self.assertEqual(determine_window_coverage_status(issues), "PARTIAL")

    def test_empty_rows_block(self) -> None:
        issues = build_window_coverage_issues(())

        self.assertEqual(determine_window_coverage_status(issues), "BLOCKED")
        self.assertTrue(any("No event-window coverage rows" in issue.message for issue in issues))

    def test_build_issues_from_rows(self) -> None:
        rows = (
            EventWindowCoverageRow(
                cyber_event_id=1,
                security_id=10,
                window_code="D7",
                window_start_date="2026-01-01",
                window_end_date="2026-01-10",
                security_price_rows=0,
                distinct_security_price_dates=0,
                index_price_rows=5,
                distinct_index_price_dates=5,
                has_security_price=False,
                has_index_price=True,
                coverage_status="MISSING_SECURITY",
            ),
            EventWindowCoverageRow(
                cyber_event_id=2,
                security_id=11,
                window_code="D7",
                window_start_date="2026-01-01",
                window_end_date="2026-01-10",
                security_price_rows=5,
                distinct_security_price_dates=5,
                index_price_rows=0,
                distinct_index_price_dates=0,
                has_security_price=True,
                has_index_price=False,
                coverage_status="MISSING_INDEX",
            ),
        )

        messages = "\n".join(issue.message for issue in build_window_coverage_issues(rows))

        self.assertIn("no security prices", messages)
        self.assertIn("no index prices", messages)

    def test_format_event_window_coverage_report(self) -> None:
        row = EventWindowCoverageRow(
            1,
            10,
            "D7",
            "2026-01-01",
            "2026-01-10",
            0,
            0,
            5,
            5,
            False,
            True,
            "MISSING_SECURITY",
        )
        result = EventWindowCoverageReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            coverage_status="PARTIAL",
            database_name="gecko",
            total_window_rows=1,
            missing_security_price_rows=1,
            missing_any_price_rows=1,
            top_problem_rows=(row,),
            window_status_counts=(("D7", "MISSING_SECURITY", 1),),
            issues=(WindowCoverageIssue("WARNING", "review event windows"),),
        )

        output = "\n".join(format_event_window_coverage_report(result))

        self.assertIn("Overall status: PARTIAL", output)
        self.assertIn("D7 / MISSING_SECURITY: 1", output)
        self.assertIn("review event windows", output)

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
                EventWindowCoverageRow(
                    1,
                    10,
                    "D7",
                    "2026-01-01",
                    "2026-01-10",
                    0,
                    0,
                    5,
                    5,
                    False,
                    True,
                    "MISSING_SECURITY",
                ),
            )
            result = EventWindowCoverageReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                coverage_status="PARTIAL",
                database_name="gecko",
                total_window_rows=1,
                top_problem_rows=rows,
                issues=(WindowCoverageIssue("WARNING", "review event windows"),),
            )

            exported = export_event_window_coverage_report(result, rows, paths)

            json_path = paths.reports_dir / "event_window_price_coverage.json"
            detail_csv = paths.exports_dir / "event_window_price_coverage_detail.csv"
            issues_csv = paths.exports_dir / "event_window_price_coverage_issues.csv"
            self.assertEqual(exported.export_paths, (json_path, detail_csv, issues_csv))
            self.assertIn("MISSING_SECURITY", detail_csv.read_text(encoding="utf-8"))
            self.assertIn("review event windows", issues_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["coverage_status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
