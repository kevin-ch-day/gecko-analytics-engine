"""Unit tests for cyber event readiness precheck reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.cyber_events.readiness import (
    EventReadinessPrecheck,
    ReadinessBlocker,
    ReadinessMetric,
    build_readiness_blockers,
    determine_readiness_status,
    export_event_readiness_precheck,
    format_event_readiness_precheck,
)
from gecko_analytics_engine.utils.paths import AppPaths


class EventReadinessTest(unittest.TestCase):
    def test_readiness_status_formatting(self) -> None:
        result = EventReadinessPrecheck(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            readiness_status="PARTIAL",
            database_name="gecko",
            metrics=(
                ReadinessMetric("event counts", "total cyber events", 44, "OK"),
            ),
            blockers=(ReadinessBlocker("WARNING", "benchmark coverage needs verification"),),
        )

        output = "\n".join(format_event_readiness_precheck(result))

        self.assertIn("Overall status: PARTIAL", output)
        self.assertIn("total cyber events: 44", output)
        self.assertIn("benchmark coverage needs verification", output)

    def test_missing_table_and_blocker_generation(self) -> None:
        metrics = (
            ReadinessMetric("event counts", "total cyber events", 44, "OK"),
            ReadinessMetric(
                "event date readiness",
                "events with at least one date row",
                None,
                "MISSING",
            ),
            ReadinessMetric(
                "security linkage readiness",
                "linked events",
                40,
                "OK",
            ),
            ReadinessMetric(
                "security linkage readiness",
                "events with no linked security",
                4,
                "WARNING",
            ),
        )

        blockers = build_readiness_blockers(metrics)

        self.assertEqual(determine_readiness_status(blockers), "BLOCKED")
        self.assertTrue(any("date rows are unavailable" in b.message for b in blockers))
        self.assertTrue(any("missing security links" in b.message for b in blockers))

    def test_missing_column_unavailable_metric_formats_cleanly(self) -> None:
        result = EventReadinessPrecheck(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            readiness_status="BLOCKED",
            metrics=(
                ReadinessMetric(
                    "event date readiness",
                    "date type distribution",
                    None,
                    "UNAVAILABLE",
                    "missing date_type column",
                ),
            ),
        )

        output = "\n".join(format_event_readiness_precheck(result))

        self.assertIn("[UNAVAILABLE] event date readiness - date type distribution", output)
        self.assertIn("missing date_type column", output)

    def test_data_quality_warnings_make_status_partial(self) -> None:
        blockers = (
            ReadinessBlocker("WARNING", "1 linked securities have no price rows."),
            ReadinessBlocker("INFO", "event_study_runs is empty."),
        )

        self.assertEqual(determine_readiness_status(blockers), "PARTIAL")

    def test_export_generation_with_fake_precheck(self) -> None:
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
            result = EventReadinessPrecheck(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                readiness_status="READY_FOR_WINDOW_VALIDATION",
                database_name="gecko",
                metrics=(ReadinessMetric("event counts", "total cyber events", 44, "OK"),),
                blockers=(),
            )

            exported = export_event_readiness_precheck(result, paths)

            json_path = paths.reports_dir / "event_readiness_precheck.json"
            summary_csv = paths.exports_dir / "event_readiness_precheck_summary.csv"
            blockers_csv = paths.exports_dir / "event_readiness_blockers.csv"
            self.assertEqual(exported.export_paths, (json_path, summary_csv, blockers_csv))
            self.assertIn("total cyber events", summary_csv.read_text(encoding="utf-8"))
            self.assertIn("severity,message", blockers_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["readiness_status"], "READY_FOR_WINDOW_VALIDATION")


if __name__ == "__main__":
    unittest.main()
