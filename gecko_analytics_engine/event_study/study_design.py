"""Event-study study design assumptions for Project Gecko."""

from __future__ import annotations

from dataclasses import dataclass

MISSING_SECURITY_PRICE = "missing_security_price"
MISSING_BENCHMARK_PRICE = "missing_benchmark_price"
MISSING_EVENT_DATE = "missing_event_date"
MISSING_WINDOW_BOUNDARY = "missing_window_boundary"

EXCLUSION_REASON_CODES = (
    MISSING_SECURITY_PRICE,
    MISSING_BENCHMARK_PRICE,
    MISSING_EVENT_DATE,
    MISSING_WINDOW_BOUNDARY,
)


@dataclass(frozen=True)
class EventStudyDesign:
    """Reusable assumptions for the first event-study dataset preview."""

    name: str = "Project Gecko Event Study v0"
    primary_event_anchor_rule: str = "Use first_trading_day when available; retain disclosure_date for audit."
    non_trading_event_date_rule: str = "Disclosure dates may fall on non-trading days; align to first_trading_day for day 0."
    benchmark_source: str = "index_daily_prices"
    default_benchmark_selection_rule: str = "Benchmark identity is not finalized; any index_daily_prices row in-window counts for preview eligibility."
    primary_benchmark_rule: str = (
        "Select the primary benchmark only after validating density, D1 coverage, event-window overlap, "
        "calendar alignment, and date-range coverage."
    )
    fallback_benchmark_rule: str = "If the preferred benchmark is sparse or misaligned, use the densest benchmark with adequate window coverage."
    robustness_benchmark_rule: str = "Retain alternate benchmarks, such as DJIA and NASDAQ_COMP, for robustness checks when coverage supports them."
    benchmark_density_threshold_pct: float = 80.0
    benchmark_validation_warning: str = "Benchmark selection must be validated before AR/CAR calculations; SP500 coverage may require import repair."
    event_windows_to_preview: tuple[str, ...] = ("D1", "D3", "D7", "D14", "D30", "D90", "D180")
    estimation_window_rule: str = "Not applied in Sprint 2E; define before market-model expected returns."
    minimum_security_price_observations: int = 1
    minimum_benchmark_price_observations: int = 1
    exclusion_reason_codes: tuple[str, ...] = EXCLUSION_REASON_CODES
    database_writes_enabled: bool = False
    leakage_warnings: tuple[str, ...] = (
        "Do not use post-event returns, CAR, recovery, or outcome-derived flags as ML features.",
        "Do not use manually curated post-event impact fields as predictors unless the prediction timestamp supports them.",
        "Separate feature availability by timestamp before any real-time or early-warning model.",
    )
    notes: tuple[str, ...] = (
        "This design validates dataset eligibility only; it does not compute returns, AR, or CAR.",
        "Rows are eligible when their event/security/window has at least one security price and one benchmark price.",
        "Benchmark selection must be finalized before production event-study calculations.",
    )


def default_event_study_design() -> EventStudyDesign:
    """Return the default event-study design contract."""

    return EventStudyDesign()


def format_event_study_design(design: EventStudyDesign) -> list[str]:
    """Format the study design for console output."""

    lines = [
        "",
        "Event Study Design Contract",
        "---------------------------",
        f"Name: {design.name}",
        f"Primary event anchor rule: {design.primary_event_anchor_rule}",
        f"Non-trading event date rule: {design.non_trading_event_date_rule}",
        f"Benchmark source: {design.benchmark_source}",
        f"Default benchmark selection rule: {design.default_benchmark_selection_rule}",
        "",
        "Benchmark policy:",
        f"  Primary benchmark rule: {design.primary_benchmark_rule}",
        f"  Fallback benchmark rule: {design.fallback_benchmark_rule}",
        f"  Robustness benchmark rule: {design.robustness_benchmark_rule}",
        f"  Density threshold assumption: {design.benchmark_density_threshold_pct:,.1f}%",
        f"  Validation warning: {design.benchmark_validation_warning}",
        "",
        f"Event windows to preview: {', '.join(design.event_windows_to_preview)}",
        f"Estimation window rule: {design.estimation_window_rule}",
        f"Minimum security price observations: {design.minimum_security_price_observations}",
        f"Minimum benchmark price observations: {design.minimum_benchmark_price_observations}",
        f"Exclusion reason codes: {', '.join(design.exclusion_reason_codes)}",
        f"Database writes enabled: {'yes' if design.database_writes_enabled else 'no'}",
        "",
        "Leakage warnings:",
    ]
    lines.extend(f"  {warning}" for warning in design.leakage_warnings)
    lines.extend([
        "",
        "Notes:",
    ])
    lines.extend(f"  {note}" for note in design.notes)
    return lines


def print_event_study_design(design: EventStudyDesign | None = None) -> None:
    """Print the event-study design contract."""

    for line in format_event_study_design(design or default_event_study_design()):
        print(line)
