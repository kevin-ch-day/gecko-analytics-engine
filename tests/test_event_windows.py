"""Unit tests for event-window readiness reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.cyber_events.event_windows import (
    EventWindowReadinessReport,
    WindowIssue,
    WindowMetric,
    build_window_issues,
    determine_window_readiness_status,
    export_event_window_readiness_report,
    format_event_window_readiness_report,
)
from gecko_analytics_engine.utils.paths import AppPaths


class EventWindowReadinessTest(unittest.TestCase):
    def test_format_window_readiness_report(self) -> None:
        result = EventWindowReadinessReport(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            readiness_status="PARTIAL",
            database_name="gecko",
            metrics=(WindowMetric("window definitions", "event window definitions", 7, "OK"),),
            issues=(WindowIssue("INFO", "1 disclosure date needs alignment."),),
        )

        output = "\n".join(format_event_window_readiness_report(result))

        self.assertIn("Overall status: PARTIAL", output)
        self.assertIn("event window definitions: 7", output)
        self.assertIn("1 disclosure date needs alignment", output)

    def test_missing_required_window_inputs_block(self) -> None:
        metrics = (
            WindowMetric("window definitions", "event window definitions", None, "MISSING"),
            WindowMetric("event date anchors", "events with disclosure dates", 50, "OK"),
        )

        issues = build_window_issues(metrics)

        self.assertEqual(determine_window_readiness_status(issues), "BLOCKED")
        self.assertTrue(any("event_windows is unavailable" in issue.message for issue in issues))

    def test_warning_only_status_is_partial(self) -> None:
        issues = (WindowIssue("WARNING", "1 boundary row is incomplete."),)

        self.assertEqual(determine_window_readiness_status(issues), "PARTIAL")

    def test_export_event_window_readiness_report(self) -> None:
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
            result = EventWindowReadinessReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                readiness_status="READY_FOR_EVENT_STUDY_DATASET",
                database_name="gecko",
                metrics=(WindowMetric("window definitions", "event window definitions", 7, "OK"),),
                issues=(),
            )

            exported = export_event_window_readiness_report(result, paths)

            json_path = paths.reports_dir / "event_window_readiness.json"
            summary_csv = paths.exports_dir / "event_window_readiness_summary.csv"
            issues_csv = paths.exports_dir / "event_window_readiness_issues.csv"
            self.assertEqual(exported.export_paths, (json_path, summary_csv, issues_csv))
            self.assertIn("event window definitions", summary_csv.read_text(encoding="utf-8"))
            self.assertIn("severity,message", issues_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["readiness_status"], "READY_FOR_EVENT_STUDY_DATASET")


if __name__ == "__main__":
    unittest.main()
