"""Provisional observed-field models. Unknown is not zero."""
from datetime import date as Date, datetime as DateTime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceRef(Model):
    capture_id: str
    provider: str
    endpoint: str
    record_path: str
    field_paths: dict[str, str] = Field(default_factory=dict)


class Capture(Model):
    id: str
    provider: str
    endpoint: str
    request: dict = Field(default_factory=dict)
    payload: dict
    mode: Literal["AUDIT_REPLAY", "LIVE", "PROVIDER_CACHE"]
    captured_at: DateTime | None = None
    fetched_at: str | None = None
    original_path: str | None = None
    sha256: str


class Snapshot(Model):
    address: str
    as_of: Date
    captures: list[Capture] = Field(default_factory=list)
    requests: list[dict] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)


class Apartment(Model):
    id: str
    property_type: str
    rooms: float | None = Field(default=None, gt=0, le=20)
    built_area: float | None = Field(default=None, gt=0)
    balcony_area: float | None = Field(default=None, ge=0)
    garden_area: float | None = Field(default=None, ge=0)
    floors: list[int] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=list)
    source_rows: list[int]
    summary_rows: list[int] = Field(default_factory=list)
    floor_areas: dict[int, float] = Field(default_factory=dict)
    provenance: dict[str, str]
    attribute_states: dict[str, Literal['OBSERVED', 'INFERRED', 'UNKNOWN', 'MISSING', 'NOT_APPLICABLE']] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class GeographicContext(Model):
    address: str
    city: str
    city_code: str
    street: str
    number: str
    lat: float
    lon: float
    block: str
    parcel: str
    neighborhood: str | None = None
    stat_area: int | None = None
    precision: str
    parcel_points: dict[str, tuple[float, float]] = Field(default_factory=dict)
    refs: list[SourceRef]
    warnings: list[str] = Field(default_factory=list)


class Observation(Model):
    id: str
    refs: list[SourceRef]
    warnings: list[str] = Field(default_factory=list)
    rejections: list[str] = Field(default_factory=list)


class SaleTransaction(Observation):
    date: Date | None = None
    price: float | None = None
    reported_area: float | None = None
    built_area: float | None = None
    area_semantics: str = "reported_unknown"
    rooms: float | None = None
    floor: int | None = None
    property_type: str = "unknown"
    nature: str | None = None
    share: float | None = None
    block: str | None = None
    parcel: str | None = None
    subparcel: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    geo_basis: str | None = None
    distance_m: float | None = None
    balcony_area: float | None = None
    garden_area: float | None = None
    building_year: int | None = None
    duplicate_of: str | None = None


class MarketListing(Observation):
    price: float | None = None
    built_area: float | None = None
    total_area: float | None = None
    reported_area: float | None = None
    area_semantics: str = "unknown"
    balcony_area: float | None = None
    garden_area: float | None = None
    rooms: float | None = None
    floor: int | None = None
    property_type: str = "unknown"
    date: Date | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    geo_basis: str | None = None
    distance_m: float | None = None
    url: str | None = None
    duplicate_of: str | None = None


class ProjectOffering(Observation):
    name: str
    price: float | None = None
    area: float | None = None
    rooms: float | None = None
    property_type: str = "unknown"
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None
    price_basis: str
    date: Date | None = None


class AreaBenchmark(Observation):
    geography: str
    scope: str
    city_code: str
    stat_area: int | None = None
    member_areas: list[int] = Field(default_factory=list)
    property_type: str
    room_min: float
    room_max: float
    median_ppm: float | None = None
    count: int
    low_data: bool
    period_end: Date
    window_months: int
    upstream: str = "Israel Tax Authority derived statistics (not independent sales)"


class PriceIndexObservation(Observation):
    series: str
    period: Date
    value: float
    base: str
    provisional: bool
    published_at: Date | None = None
    period_definition: str = "CBS month label: overlapping two-month dwelling-sale periods"


class MarketData(Model):
    context: GeographicContext
    sales: list[SaleTransaction] = Field(default_factory=list)
    listings: list[MarketListing] = Field(default_factory=list)
    projects: list[ProjectOffering] = Field(default_factory=list)
    benchmarks: list[AreaBenchmark] = Field(default_factory=list)
    indices: list[PriceIndexObservation] = Field(default_factory=list)
    issues: list[dict] = Field(default_factory=list)
    duplicates: list[dict] = Field(default_factory=list)


class PricingEvidence(Model):
    observation_id: str
    refs: list[SourceRef]
    score: float
    reasons: list[str]
    warnings: list[str]
    original_price: float
    area_basis: float
    area_basis_semantics: str = 'unknown'
    raw_ppm: float
    adjusted_ppm: float
    time_factor: float = 1
    index_sale_id: str | None = None
    index_reference_id: str | None = None
    sale_date: Date
    distance_m: float | None = None
    geo_basis: str | None = None


class ReviewFlag(Model):
    observation_id: str
    classification: Literal["PROVEN_INVALID", "UNVERIFIED_ANOMALY", "LEGITIMATE_UNUSUAL"]
    reason: str
    refs: list[SourceRef] = Field(default_factory=list)
    raw_ppm: float | None = None
    peer_median_ppm: float | None = None


class PricingResult(Model):
    apartment_id: str
    property_type: str
    built_area: float | None
    rooms: float | None
    status: Literal["PRICED", "REVIEW_REQUIRED", "UNPRICED"]
    tier: Literal["TIER_1", "TIER_2", "TIER_3", "NONE"]
    estimated_market_value: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    implied_ppm: float | None = None
    median_estimate: float | None = None
    weighted_median_estimate: float | None = None
    effective_count: float = 0
    independent_parcels: int = 0
    eligible_recent: int = 0
    eligible_historical: int = 0
    median_age_days: float | None = None
    median_age_basis: str | None = None
    dispersion_iqr_ppm: float | None = None
    evidence_strength: str = "INSUFFICIENT"
    evidence_quality: str = "INSUFFICIENT"
    feature_completeness: str = "UNKNOWN"
    methodology_strength: str = "INSUFFICIENT"
    marketing_approval: str = "NOT_APPROVED"
    classification_reasons: list[str] = Field(default_factory=list)
    review_flags: list[ReviewFlag] = Field(default_factory=list)
    quarantine_sensitivity: dict = Field(default_factory=dict)
    differentiation_scenario: dict = Field(default_factory=dict)
    special_property_scenario: dict = Field(default_factory=dict)
    unit_adjustment_scenario: dict = Field(default_factory=dict)
    competitive_context: dict = Field(default_factory=dict)
    marketing_strategies: dict = Field(default_factory=dict)
    pricing_decision: dict = Field(default_factory=dict)
    valuation_reference: str
    evidence: list[PricingEvidence] = Field(default_factory=list)
    benchmark_id: str | None = None
    benchmark_refs: list[SourceRef] = Field(default_factory=list)
    adjusted_market_value: float | None = None
    adjusted_value_low: float | None = None
    adjusted_value_high: float | None = None
    unit_adjustments: dict = Field(default_factory=dict)
    pricing_classification: str = "INSUFFICIENT_EVIDENCE"
    competitor_context: dict = Field(default_factory=dict)
    suggested_asking_price: float | None = None
    market_positioning: str = "No defensible current competitive context"
    listing_refs: list[str] = Field(default_factory=list)
    project_refs: list[str] = Field(default_factory=list)
    strategy_assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AskingOverride(Model):
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=10, max_length=1000)
    accept_gate_failures: list[Literal['insufficient_credible_listings', 'geographic_precision_or_distance', 'excessive_price_dispersion', 'built_area_compatibility', 'property_type_compatibility', 'listing_freshness_uncertain']] = Field(min_length=1)

    @model_validator(mode='after')
    def meaningful_text(self):
        if not self.reviewer.strip() or len(self.reason.strip()) < 10:
            raise ValueError('Override requires reviewer and a meaningful reason')
        return self


class SaleVerification(Model):
    reviewer: str = Field(min_length=1, max_length=120)
    evidence_reference: str = Field(min_length=5, max_length=500)
    verified_built_area: float = Field(gt=0, le=2000)
    property_type: Literal['apartment', 'garden', 'duplex', 'triplex']
    construction_status: Literal['completed', 'off_plan']
    full_market_consideration_verified: Literal[True]

    @model_validator(mode='after')
    def nonblank(self):
        if not self.reviewer.strip() or len(self.evidence_reference.strip()) < 5:
            raise ValueError('Sale verification requires a reviewer and source reference')
        return self


class AnomalyReview(Model):
    reviewer: str = Field(min_length=1, max_length=120)
    evidence_reference: str = Field(min_length=5, max_length=500)
    disposition: Literal['legitimate', 'exclude']
    reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.reviewer.strip() or len(self.evidence_reference.strip()) < 5 or len(self.reason.strip()) < 10:
            raise ValueError('Anomaly review requires a reviewer, source reference and reason')
        return self


class PricingConfig(Model):
    as_of: Date
    recent_months: int = Field(default=24, ge=1, le=60)
    history_years: int = Field(default=10, ge=2, le=30)
    radius_m: float = Field(default=1500, gt=0, le=5000)
    max_area_difference: float = Field(default=.35, ge=0, le=1)
    max_room_difference: float = Field(default=1, ge=0, le=3)
    min_comps: int = Field(default=3, ge=2)
    max_comps: int = Field(default=10, ge=3)
    min_parcels: int = Field(default=2, ge=1)
    allow_reported_sale_area: bool = True
    method: Literal["median", "weighted_median"] = "weighted_median"
    min_range_fraction: float = Field(default=.15, ge=.1, le=.8)
    benchmark_range_fraction: float = Field(default=.25, ge=0, le=1)
    benchmark_low_data_range_fraction: float = Field(default=.35, ge=0, le=1)
    review_low_ratio: float = Field(default=.5, gt=0, le=1)
    review_high_ratio: float = Field(default=2, ge=1, le=10)
    matching_area_weight: float = Field(default=3, ge=0, le=100)
    matching_room_weight: float = Field(default=1, ge=0, le=100)
    matching_age_months_scale: float = Field(default=24, gt=0, le=1200)
    matching_neighborhood_penalty: float = Field(default=1.25, ge=0, le=100)
    matching_other_geo_penalty: float = Field(default=.8, ge=0, le=100)
    matching_unknown_type_penalty: float = Field(default=.35, ge=0, le=100)
    matching_floor_penalty: float = Field(default=.03, ge=0, le=10)
    matching_floor_cap: int = Field(default=10, ge=0, le=100)
    min_benchmark_count: int = Field(default=3, ge=1)
    allow_special_benchmark_proxy: bool = False
    balcony_equivalent_fraction: float = Field(default=0, ge=0, le=1)
    garden_equivalent_fraction: float = Field(default=0, ge=0, le=1)
    marketing_adjustment: float = Field(default=0, ge=-.15, le=.15)
    marketing_rounding: int = Field(default=10000, ge=1)
    benchmark_ask_conflict_fraction: float = Field(default=.35, ge=0, le=1)
    quarantine_policy: Literal["retain_for_review", "exclude_flagged"] = "retain_for_review"
    scenario_floor_rate: float = Field(default=0, ge=-.1, le=.1)
    scenario_floor_reference: int = 1
    scenario_orientation_rates: dict[str, float] = Field(default_factory=dict)
    scenario_balcony_ils_per_sqm: float = Field(default=0, ge=0)
    scenario_garden_ils_per_sqm: float = Field(default=0, ge=0)
    unit_balcony_equivalent: float = Field(default=.25, ge=0, le=1)
    unit_garden_equivalent: float = Field(default=.20, ge=0, le=1)
    unit_floor_rate: float = Field(default=0, ge=-.1, le=.1)
    unit_reference_floor: float | None = None
    unit_orientation_rates: dict[str, float] = Field(default_factory=dict)
    garden_column_overrides: dict[str, Literal['garden', 'balcony', 'unknown']] = Field(default_factory=dict)
    special_listing_radius_m: float = Field(default=5000, gt=0, le=5000)
    special_listing_min_count: int = Field(default=2, ge=2, le=20)
    asking_gate_min_credible: int = Field(default=3, ge=2, le=20)
    asking_gate_radius_m: float = Field(default=1500, gt=0, le=5000)
    asking_gate_max_price_ratio: float = Field(default=2, ge=1, le=10)
    asking_gate_max_area_difference: float = Field(default=.20, ge=0, le=1)
    asking_gate_max_listing_age_days: int = Field(default=90, ge=1, le=730)
    asking_overrides: dict[str, AskingOverride] = Field(default_factory=dict)
    marketing_fast_adjustment: float = Field(default=-.03, ge=-.3, le=.3)
    marketing_balanced_adjustment: float = Field(default=0, ge=-.3, le=.3)
    marketing_high_adjustment: float = Field(default=.05, ge=-.3, le=.3)
    marketing_valuation_basis: Literal['base', 'unit_scenario'] = 'base'
    acknowledge_target_only_double_counting: bool = False
    decision_objective: Literal['faster', 'balanced', 'higher'] = 'balanced'
    decision_faster_target_fraction: float = Field(default=.25, ge=0, le=1)
    decision_balanced_target_fraction: float = Field(default=.50, ge=0, le=1)
    decision_higher_target_fraction: float = Field(default=.75, ge=0, le=1)
    sale_verifications: dict[str, SaleVerification] = Field(default_factory=dict)
    anomaly_reviews: dict[str, AnomalyReview] = Field(default_factory=dict)
    decision_min_competitors: int = Field(default=3, ge=3, le=20)
    decision_max_area_difference: float = Field(default=.10, ge=0, le=.35)
    decision_max_floor_difference: int = Field(default=2, ge=0, le=20)
    decision_require_attestations: bool = False
    unit_adjustment_mode: Literal['off', 'policy'] = 'off'
    unit_min_market_floor_pairs: int = Field(default=3, ge=2, le=50)
    unit_duplex_premium: float = Field(default=0, ge=-.30, le=.50)
    unit_triplex_premium: float = Field(default=0, ge=-.30, le=.50)
    unit_apply_outdoor_to_base: bool = False
    competitor_context_radius_m: float = Field(default=3000, gt=0, le=10000)
    research_max_area_difference: float = Field(default=.20, ge=0, le=1)
    research_max_distance_m: float = Field(default=1500, gt=0, le=10000)
    research_max_floor_difference: int = Field(default=5, ge=0, le=50)
    research_max_listing_age_days: int = Field(default=120, ge=1, le=730)
    project_address_verified: bool = False
    project_location_note: str = "Configured anchor is an explicit assumption; the workbook does not establish the project address"

    @model_validator(mode="after")
    def coherent_counts(self):
        if not self.decision_faster_target_fraction <= self.decision_balanced_target_fraction <= self.decision_higher_target_fraction:
            raise ValueError('Decision objective fractions must be ordered')
        if self.max_comps < self.min_comps or self.min_parcels > self.max_comps:
            raise ValueError("Comparable count/parcel requirements exceed selected-comparable limit")
        if self.balcony_equivalent_fraction or self.garden_equivalent_fraction:
            raise ValueError('Legacy benchmark-area premiums alter the evidence base; use separate unit_* scenarios instead')
        if not self.marketing_fast_adjustment <= self.marketing_balanced_adjustment <= self.marketing_high_adjustment:
            raise ValueError('Marketing scenario adjustments must be ordered fast <= balanced <= high')
        if any(not -.3 <= value <= .3 for value in list(self.scenario_orientation_rates.values()) + list(self.unit_orientation_rates.values())):
            raise ValueError("Orientation scenario rates must be between -0.3 and 0.3")
        if (set(self.unit_orientation_rates) | set(self.scenario_orientation_rates)) - {'מזרח', 'מערב', 'צפון', 'דרום'}:
            raise ValueError('Unknown orientation policy key')
        if self.unit_adjustment_mode == 'policy' and self.unit_apply_outdoor_to_base:
            raise ValueError('Outdoor policy cannot be added to comparable-based base value: comparable outdoor areas are unknown, so double counting cannot be excluded')
        return self
