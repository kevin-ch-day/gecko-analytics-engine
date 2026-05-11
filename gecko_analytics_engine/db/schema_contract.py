"""Canonical Gecko database schema contract.

This module is the single home for known table, view, column, and legacy alias
names used by the analytics engine. It is intentionally simple: constants here
make schema assumptions explicit without turning the project into a schema ORM.
"""

from __future__ import annotations

COMPANIES = "companies"
SECURITIES = "securities"
CYBER_EVENTS = "cyber_events"
CYBER_EVENT_DATES = "cyber_event_dates"
CYBER_EVENT_FEATURES = "cyber_event_features"
CYBER_EVENT_IMPACTS = "cyber_event_impacts"
CYBER_EVENT_SECURITIES = "cyber_event_securities"
CYBER_EVENT_SOURCES = "cyber_event_sources"
SECURITY_DAILY_PRICES = "security_daily_prices"
INDEX_DAILY_PRICES = "index_daily_prices"
DJI_DAILY_PRICES = "dji_daily_prices"
MARKET_CALENDAR = "market_calendar"
MARKET_INDEXES = "market_indexes"
EXCHANGES = "exchanges"
EVENT_WINDOWS = "event_windows"
EVENT_STUDY_RUNS = "event_study_runs"
EVENT_STUDY_RESULTS = "event_study_results"
STATISTICAL_TEST_RESULTS = "statistical_test_results"

CORE_TABLES = (
    COMPANIES,
    SECURITIES,
    CYBER_EVENTS,
    CYBER_EVENT_DATES,
    CYBER_EVENT_FEATURES,
    CYBER_EVENT_IMPACTS,
    CYBER_EVENT_SECURITIES,
    CYBER_EVENT_SOURCES,
    SECURITY_DAILY_PRICES,
    INDEX_DAILY_PRICES,
    DJI_DAILY_PRICES,
    MARKET_CALENDAR,
    MARKET_INDEXES,
    EXCHANGES,
    EVENT_WINDOWS,
    EVENT_STUDY_RUNS,
    EVENT_STUDY_RESULTS,
    STATISTICAL_TEST_RESULTS,
)

EVENT_TABLES = (
    CYBER_EVENTS,
    CYBER_EVENT_DATES,
    CYBER_EVENT_FEATURES,
    CYBER_EVENT_IMPACTS,
    CYBER_EVENT_SECURITIES,
    CYBER_EVENT_SOURCES,
    EVENT_WINDOWS,
)

MARKET_DATA_TABLES = (
    SECURITIES,
    SECURITY_DAILY_PRICES,
    INDEX_DAILY_PRICES,
    DJI_DAILY_PRICES,
    MARKET_CALENDAR,
    MARKET_INDEXES,
    EXCHANGES,
)

OUTPUT_TABLES = (
    EVENT_STUDY_RUNS,
    EVENT_STUDY_RESULTS,
    STATISTICAL_TEST_RESULTS,
)

EMPTY_EXPECTED_AT_START_TABLES = (
    EVENT_STUDY_RUNS,
    EVENT_STUDY_RESULTS,
    STATISTICAL_TEST_RESULTS,
)

SOURCE_PROVENANCE_TABLES = (
    CYBER_EVENT_SOURCES,
)

VW_EVENT_CONTAMINATION_FLAGS = "vw_event_contamination_flags"
VW_EVENT_IMPACT_QUALITY_FLAGS = "vw_event_impact_quality_flags"
VW_EVENT_NEARBY_CYBER_CLUSTERS = "vw_event_nearby_cyber_clusters"
VW_EVENT_RESEARCH_READINESS_FLAGS = "vw_event_research_readiness_flags"
VW_EVENT_SAME_TICKER_WINDOW_OVERLAPS = "vw_event_same_ticker_window_overlaps"
VW_EVENT_STUDY_EVENT_READINESS = "vw_event_study_event_readiness"
VW_EVENT_WINDOW_BOUNDARIES = "vw_event_window_boundaries"
VW_MARKET_DATA_IMPORT_PLAN = "vw_market_data_import_plan"
VW_SECURITY_PRICE_IMPORT_TARGETS = "vw_security_price_import_targets"
VW_US_TRADING_DAYS = "vw_us_trading_days"

READINESS_VIEWS = (
    VW_EVENT_CONTAMINATION_FLAGS,
    VW_EVENT_IMPACT_QUALITY_FLAGS,
    VW_EVENT_NEARBY_CYBER_CLUSTERS,
    VW_EVENT_RESEARCH_READINESS_FLAGS,
    VW_EVENT_SAME_TICKER_WINDOW_OVERLAPS,
    VW_EVENT_STUDY_EVENT_READINESS,
    VW_EVENT_WINDOW_BOUNDARIES,
    VW_MARKET_DATA_IMPORT_PLAN,
    VW_SECURITY_PRICE_IMPORT_TARGETS,
    VW_US_TRADING_DAYS,
)

LEGACY_TABLE_ALIASES = {
    "cyber_event_security_map": CYBER_EVENT_SECURITIES,
    "indexes": MARKET_INDEXES,
    "market_indices": MARKET_INDEXES,
}

CYBER_EVENT_ID = "cyber_event_id"
SECURITY_ID = "security_id"
MARKET_INDEX_ID = "market_index_id"
EXCHANGE_ID = "exchange_id"
EXCHANGE_CODE = "exchange_code"
TICKER_SYMBOL = "ticker_symbol"
MARKET_CODE = "market_code"
HOLIDAY_NAME = "holiday_name"
EVENT_DATE = "event_date"
DATE_TYPE = "date_type"
TRADE_DATE = "trade_date"
CALENDAR_DATE = "calendar_date"
IS_TRADING_DAY = "is_trading_day"
WINDOW_CODE = "window_code"
PRE_EVENT_DAYS = "pre_event_days"
POST_EVENT_DAYS = "post_event_days"
WINDOW_START_DATE = "window_start_date"
WINDOW_END_DATE = "window_end_date"
BOUNDARY_STATUS = "boundary_status"
FIRST_TRADING_DAY = "first_trading_day"
DISCLOSURE_DATE = "disclosure_date"


def canonical_table_name(table_name: str) -> str:
    """Return the canonical table name for a known legacy alias."""

    return LEGACY_TABLE_ALIASES.get(table_name, table_name)
