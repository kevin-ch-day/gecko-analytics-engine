"""Unit tests for event-study dataset preview."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.event_study.dataset_preview import (
    DatasetPreviewIssue,
    EventStudyCandidateRow,
    EventStudyDatasetPreview,
    build_dataset_preview_issues,
    determine_dataset_preview_status,
    export_event_study_dataset_preview,
    format_event_study_dataset_preview,
)
from gecko_analytics_engine.event_study.study_design import (
    EXCLUSION_REASON_CODES,
    MISSING_BENCHMARK_PRICE,
    MISSING_SECURITY_PRICE,
    default_event_study_design,
    format_event_study_design,
)
from gecko_analytics_engine.utils.paths import AppPaths


class EventStudyDatasetPreviewTest(unittest.TestCase):
    def test_default_study_design_formats(self) -> None:
        output = "\n".join(format_event_study_design(default_event_study_design()))

        self.assertIn("Project Gecko Event Study v0", output)
        self.assertIn("Primary event anchor rule", output)
        self.assertIn("Event windows to preview", output)
        self.assertIn("Database writes enabled: no", output)
        self.assertIn("Leakage warnings", output)

    def test_exclusion_reason_constants(self) -> None:
        self.assertIn(MISSING_SECURITY_PRICE, EXCLUSION_REASON_CODES)
        self.assertIn(MISSING_BENCHMARK_PRICE, EXCLUSION_REASON_CODES)

    def test_empty_candidates_block(self) -> None:
        issues = build_dataset_preview_issues(())

        self.assertEqual(determine_dataset_preview_status(issues), "BLOCKED")
        self.assertTrue(any("No event-study candidate rows" in issue.message for issue in issues))

    def test_excluded_candidates_make_status_partial(self) -> None:
        rows = (
            EventStudyCandidateRow(
                1,
                10,
                "ABC",
                "2026-01-01",
                "disclosure",
                "2026-01-02",
                "D7",
                "2026-01-01",
                "2026-01-10",
                0,
                5,
                False,
                MISSING_SECURITY_PRICE,
            ),
        )
        issues = build_dataset_preview_issues(rows)

        self.assertEqual(determine_dataset_preview_status(issues), "PARTIAL")

    def test_format_dataset_preview(self) -> None:
        result = EventStudyDatasetPreview(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            preview_status="PARTIAL",
            database_name="gecko",
            total_candidates=1,
            eligible_candidates=0,
            excluded_candidates=1,
            top_exclusions=(
                EventStudyCandidateRow(
                    1,
                    10,
                    "ABC",
                    "2026-01-01",
                    "disclosure",
                    "2026-01-02",
                    "D7",
                    "2026-01-01",
                    "2026-01-10",
                    0,
                    5,
                    False,
                    MISSING_SECURITY_PRICE,
                ),
            ),
            eligibility_by_window=(("D7", 0, 1),),
            exclusion_reason_counts=((MISSING_SECURITY_PRICE, 1),),
            issues=(DatasetPreviewIssue("WARNING", "review candidates"),),
        )

        output = "\n".join(format_event_study_dataset_preview(result))

        self.assertIn("Overall status: PARTIAL", output)
        self.assertIn("D7: eligible=0, excluded=1", output)
        self.assertIn(MISSING_SECURITY_PRICE, output)

    def test_export_dataset_preview(self) -> None:
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
                EventStudyCandidateRow(
                    1,
                    10,
                    "ABC",
                    "2026-01-01",
                    "disclosure",
                    "2026-01-02",
                    "D7",
                    "2026-01-01",
                    "2026-01-10",
                    0,
                    5,
                    False,
                    MISSING_SECURITY_PRICE,
                ),
            )
            result = EventStudyDatasetPreview(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                preview_status="PARTIAL",
                database_name="gecko",
                total_candidates=1,
                top_exclusions=rows,
                issues=(DatasetPreviewIssue("WARNING", "review candidates"),),
            )

            exported = export_event_study_dataset_preview(result, rows, paths)

            json_path = paths.reports_dir / "event_study_dataset_preview.json"
            candidates_csv = paths.exports_dir / "event_study_dataset_candidates.csv"
            exclusions_csv = paths.exports_dir / "event_study_dataset_exclusions.csv"
            self.assertEqual(exported.export_paths, (json_path, candidates_csv, exclusions_csv))
            self.assertIn("ABC", candidates_csv.read_text(encoding="utf-8"))
            self.assertIn("event_date,event_date_type,aligned_event_date", candidates_csv.read_text(encoding="utf-8"))
            self.assertIn(MISSING_SECURITY_PRICE, exclusions_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["preview_status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
