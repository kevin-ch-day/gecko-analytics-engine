"""Unit tests for dataset exclusion review and benchmark coverage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.event_study.exclusion_review import (
    DatasetExclusionReview,
    ExclusionReviewIssue,
    ExclusionReviewRow,
    build_exclusion_review_issues,
    determine_exclusion_review_status,
    export_dataset_exclusion_review,
    format_dataset_exclusion_review,
)
from gecko_analytics_engine.event_study.menu import event_study_menu_action
from gecko_analytics_engine.event_study.study_design import MISSING_BENCHMARK_PRICE, MISSING_SECURITY_PRICE
from gecko_analytics_engine.market_data.indexes import (
    BenchmarkCoverageReport,
    BenchmarkCoverageRow,
    select_recommended_benchmark,
)
from gecko_analytics_engine.utils.paths import AppPaths


class DatasetExclusionReviewTest(unittest.TestCase):
    def test_benchmark_candidate_selection_prefers_overlap_then_rows(self) -> None:
        rows = (
            BenchmarkCoverageRow(1, "A", "Index A", 1000, "2020-01-01", "2024-01-01", 10),
            BenchmarkCoverageRow(2, "B", "Index B", 500, "2020-01-01", "2024-01-01", 20),
        )

        recommended = select_recommended_benchmark(rows)

        self.assertIsNotNone(recommended)
        self.assertEqual(recommended.market_index_id, 2)

    def test_missing_benchmark_candidate_blocks(self) -> None:
        issues = build_exclusion_review_issues((), None)

        self.assertEqual(determine_exclusion_review_status(issues), "BLOCKED")
        self.assertTrue(any("No recommended benchmark" in issue.message for issue in issues))

    def test_format_exclusion_review(self) -> None:
        result = DatasetExclusionReview(
            generated_at="2026-05-10T00:00:00+00:00",
            connection_ok=True,
            review_status="NEEDS_REVIEW",
            database_name="gecko",
            total_exclusions=1,
            missing_both_security_and_benchmark=1,
            reason_counts=(),
            top_exclusions=(
                ExclusionReviewRow(
                    f"{MISSING_SECURITY_PRICE};{MISSING_BENCHMARK_PRICE}",
                    1,
                    "Event",
                    10,
                    "ABC",
                    "ABC Corp",
                    "D1",
                    "2026-01-01",
                    "2026-01-02",
                    "2026-01-01",
                    "2026-01-03",
                    0,
                    0,
                    0,
                    100,
                    "missing_entirely",
                    "missing_for_window",
                ),
            ),
            benchmark_coverage=BenchmarkCoverageReport(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                coverage_status="OK",
                recommended_benchmark_id=1,
                recommended_benchmark_label="SPX (id=1)",
                index_rows=(BenchmarkCoverageRow(1, "SPX", "S&P 500", 100, "2020-01-01", "2026-01-01", 1),),
            ),
            issues=(ExclusionReviewIssue("WARNING", "review exclusions"),),
        )

        output = "\n".join(format_dataset_exclusion_review(result))

        self.assertIn("Total exclusions: 1", output)
        self.assertIn("Rows missing both security and benchmark prices: 1", output)
        self.assertIn("Recommended benchmark: SPX (id=1)", output)
        self.assertIn("review exclusions", output)

    def test_export_generation_with_fake_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _fake_paths(Path(temp_dir))
            rows = (
                ExclusionReviewRow(
                    MISSING_SECURITY_PRICE,
                    1,
                    "Event",
                    10,
                    "ABC",
                    "ABC Corp",
                    "D1",
                    "2026-01-01",
                    "2026-01-02",
                    "2026-01-01",
                    "2026-01-03",
                    0,
                    5,
                    0,
                    100,
                    "missing_entirely",
                    "covered",
                ),
            )
            result = DatasetExclusionReview(
                generated_at="2026-05-10T00:00:00+00:00",
                connection_ok=True,
                review_status="NEEDS_REVIEW",
                database_name="gecko",
                total_exclusions=1,
                top_exclusions=rows,
                benchmark_coverage=BenchmarkCoverageReport(
                    generated_at="2026-05-10T00:00:00+00:00",
                    connection_ok=True,
                    coverage_status="OK",
                    index_rows=(BenchmarkCoverageRow(1, "SPX", "S&P 500", 100, "2020-01-01", "2026-01-01", 1),),
                ),
            )

            exported = export_dataset_exclusion_review(result, rows, paths)

            json_path = paths.reports_dir / "event_study_exclusion_review.json"
            review_csv = paths.exports_dir / "event_study_exclusion_review.csv"
            benchmark_csv = paths.exports_dir / "benchmark_coverage_detail.csv"
            self.assertEqual(exported.export_paths, (json_path, review_csv, benchmark_csv))
            self.assertIn("ABC", review_csv.read_text(encoding="utf-8"))
            self.assertIn("SPX", benchmark_csv.read_text(encoding="utf-8"))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["review_status"], "NEEDS_REVIEW")

    def test_event_study_menu_action_constructs(self) -> None:
        self.assertTrue(callable(event_study_menu_action(_fake_settings(), _fake_paths(Path(".")))))


def _fake_settings() -> AppSettings:
    return AppSettings(
        db=DatabaseSettings("localhost", 3306, "gecko", "user", ""),
        output_root=Path("output"),
        data_root=Path("data"),
        log_level="INFO",
    )


def _fake_paths(root: Path) -> AppPaths:
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


if __name__ == "__main__":
    unittest.main()
