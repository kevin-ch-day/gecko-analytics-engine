"""Unit tests for dry-run candidate price import validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gecko_analytics_engine.config.settings import AppSettings, DatabaseSettings
from gecko_analytics_engine.market_data.import_validator import (
    CandidatePriceFile,
    KnownMarketSymbol,
    PriceCoverageComparison,
    PriceFileProfile,
    PriceImportValidatorReport,
    build_dry_run_plan_row,
    classify_frequency_from_dates,
    detect_column,
    discover_candidate_price_files,
    export_price_import_validator_report,
    profile_price_csv,
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


class PriceImportValidatorTest(unittest.TestCase):
    def test_filename_pattern_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            (paths.data_root / "raw" / "indexes").mkdir(parents=True)
            (paths.data_root / "raw" / "indexes" / "SP500_daily_prices.csv").write_text("Date,Close\n", encoding="utf-8")
            (paths.data_root / "raw" / "notes.csv").write_text("Date,Close\n", encoding="utf-8")

            candidates = discover_candidate_price_files(paths)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].file_name, "SP500_daily_prices.csv")

    def test_header_and_ohlcv_detection(self) -> None:
        headers = ("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume")

        self.assertEqual(detect_column(headers, ("date", "trade_date")), "Date")
        self.assertEqual(detect_column(headers, ("adjusted_close", "adj close")), "Adj Close")
        self.assertEqual(detect_column(headers, ("volume",)), "Volume")

    def test_csv_profile_detects_columns_and_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "DJIA_daily.csv"
            path.write_text(
                "Date,Open,High,Low,Close,Adj Close,Volume\n"
                "2024-01-02,1,2,1,2,2,100\n"
                "2024-01-03,2,3,2,3,3,100\n",
                encoding="utf-8",
            )
            known = (KnownMarketSymbol("DJIA", "index", 1, "Dow"),)

            profile = profile_price_csv(path, known)

            self.assertEqual(profile.detected_date_column, "Date")
            self.assertEqual(profile.detected_close_column, "Close")
            self.assertEqual(profile.detected_adjusted_close_column, "Adj Close")
            self.assertEqual(profile.detected_symbol, "DJIA")
            self.assertEqual(profile.mapped_entity_type, "index")

    def test_frequency_classification_from_fake_dates(self) -> None:
        from datetime import date

        self.assertEqual(classify_frequency_from_dates((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))), "daily")
        self.assertEqual(classify_frequency_from_dates((date(2024, 1, 1), date(2024, 1, 8), date(2024, 1, 15))), "weekly")
        self.assertEqual(classify_frequency_from_dates((date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))), "monthly")

    def test_dry_run_priority_classification(self) -> None:
        profile = PriceFileProfile(
            path="sp500.csv",
            file_name="sp500.csv",
            size_bytes=100,
            detected_delimiter=",",
            header_columns="Date,Close",
            detected_date_column="Date",
            detected_open_column=None,
            detected_high_column=None,
            detected_low_column=None,
            detected_close_column="Close",
            detected_adjusted_close_column=None,
            detected_volume_column=None,
            detected_symbol="SP500",
            mapped_entity_type="index",
            mapped_entity_id=2,
            mapped_symbol="SP500",
            min_date="2024-01-02",
            max_date="2024-01-05",
            row_count=4,
            unique_date_count=4,
            duplicate_date_count=0,
            weekend_row_count=0,
            weekday_distribution="Tuesday=1",
            likely_frequency="daily",
            calendar_expected_trading_days=4,
            calendar_density_pct=100.0,
            status="PROFILED",
            rejection_reason="",
        )
        comparison = PriceCoverageComparison(
            path="sp500.csv",
            mapped_entity_type="index",
            mapped_entity_id=2,
            mapped_symbol="SP500",
            file_min_date="2024-01-02",
            file_max_date="2024-01-05",
            db_min_date=None,
            db_max_date=None,
            db_existing_dates_in_file_range=0,
            file_dates_already_present=0,
            file_dates_missing_from_db=4,
            event_window_dates_filled=None,
            benchmark_gap_dates_filled=4,
            estimated_post_import_density_pct=100.0,
            materially_improves_coverage=True,
        )

        plan = build_dry_run_plan_row(profile, comparison)

        self.assertEqual(plan.decision, "usable")
        self.assertEqual(plan.priority, "high")

    def test_export_generation_with_fake_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            report = PriceImportValidatorReport(
                generated_at="2026-05-11T00:00:00+00:00",
                connection_ok=True,
                validation_status="NO_CANDIDATE_FILES",
                candidate_files=(CandidatePriceFile("x.csv", "raw", "x.csv", 1, "prices"),),
                file_profiles=(),
                coverage_comparisons=(),
                dry_run_plan=(),
            )

            exported = export_price_import_validator_report(report, paths)

            self.assertEqual(
                exported.export_paths,
                (
                    paths.reports_dir / "price_import_validator_report.json",
                    paths.exports_dir / "price_import_file_profiles.csv",
                    paths.exports_dir / "price_import_coverage_comparison.csv",
                    paths.exports_dir / "price_import_dry_run_plan.csv",
                ),
            )
            payload = json.loads((paths.reports_dir / "price_import_validator_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["validation_status"], "NO_CANDIDATE_FILES")

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
