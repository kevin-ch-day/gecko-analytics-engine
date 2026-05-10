"""Tests for the canonical Gecko schema contract."""

from __future__ import annotations

import unittest

from gecko_analytics_engine.cyber_events import readiness
from gecko_analytics_engine.db import schema_checks
from gecko_analytics_engine.db.schema_contract import (
    CORE_TABLES,
    CYBER_EVENT_SECURITIES,
    LEGACY_TABLE_ALIASES,
    MARKET_INDEXES,
    READINESS_VIEWS,
    canonical_table_name,
)
from gecko_analytics_engine.reports import database_shape_report


class SchemaContractTest(unittest.TestCase):
    def test_core_tables_include_real_canonical_names(self) -> None:
        self.assertIn(CYBER_EVENT_SECURITIES, CORE_TABLES)
        self.assertIn(MARKET_INDEXES, CORE_TABLES)
        self.assertIn("cyber_event_dates", CORE_TABLES)
        self.assertIn("security_daily_prices", CORE_TABLES)

    def test_legacy_aliases_resolve_to_canonical_tables(self) -> None:
        self.assertEqual(
            LEGACY_TABLE_ALIASES["cyber_event_security_map"],
            CYBER_EVENT_SECURITIES,
        )
        self.assertEqual(LEGACY_TABLE_ALIASES["indexes"], MARKET_INDEXES)
        self.assertEqual(LEGACY_TABLE_ALIASES["market_indices"], MARKET_INDEXES)
        self.assertEqual(canonical_table_name("indexes"), MARKET_INDEXES)
        self.assertEqual(canonical_table_name(CYBER_EVENT_SECURITIES), CYBER_EVENT_SECURITIES)

    def test_schema_checks_use_canonical_required_tables(self) -> None:
        self.assertEqual(schema_checks.REQUIRED_TABLES, CORE_TABLES)
        self.assertNotIn("cyber_event_security_map", schema_checks.REQUIRED_TABLES)
        self.assertNotIn("indexes", schema_checks.REQUIRED_TABLES)
        self.assertNotIn("market_indices", schema_checks.REQUIRED_TABLES)

    def test_readiness_views_and_imports_do_not_cycle(self) -> None:
        self.assertIn("vw_event_study_event_readiness", READINESS_VIEWS)
        self.assertTrue(hasattr(readiness, "run_event_readiness_precheck"))
        self.assertTrue(hasattr(database_shape_report, "generate_database_shape_report"))


if __name__ == "__main__":
    unittest.main()
