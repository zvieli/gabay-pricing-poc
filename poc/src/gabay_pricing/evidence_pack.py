"""Bounded, traceable inputs for a pricing analyst; no pricing calculations."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    kind: str
    facts: dict[str, Any]
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EvidencePack(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unit_id: str
    unit: dict[str, Any]
    selected_sales: list[EvidenceItem] = Field(max_length=8)
    eligible_offers: list[EvidenceItem] = Field(max_length=5)
    excluded_offers: list[dict[str, Any]] = Field(max_length=5)
    benchmark: EvidenceItem | None = None
    valuation: dict[str, Any]
    adjustments: dict[str, Any]
    scenarios: dict[str, Any]
    policy: dict[str, Any]
    decision: dict[str, Any]
    warnings: list[str]
    special_proxy: dict[str, Any]


def build_evidence_pack(row: dict, apartment: dict, config: dict) -> EvidencePack:
    if str(row['apartment_id']) != str(apartment['id']):
        raise ValueError('Apartment and result do not match')
    decision = row.get('pricing_decision') or {}
    competitive = decision.get('competitive_evidence') or {}
    assessments = {x['id']: x for x in decision.get('selected_comparable_assessments') or []}
    sales = []
    for e in (row.get('evidence') or [])[:8]:
        assessment = assessments.get(e['observation_id'], {})
        sales.append(EvidenceItem(id=e['observation_id'], kind='sale_reported_by_provider',
            facts={k: e.get(k) for k in ('original_price', 'area_basis', 'area_basis_semantics', 'sale_date', 'score', 'reasons', 'distance_m', 'geo_basis')} | {
                'reported_area': assessment.get('reported_area'),
                'explicit_built_area': e.get('area_basis') if e.get('area_basis_semantics') == 'explicit_built' else None,
                'verified_built_area': (assessment.get('reviewer_attestation') or {}).get('verified_built_area'),
                'verified_built_area_basis': 'reviewer_attestation' if (assessment.get('reviewer_attestation') or {}).get('verified_built_area') is not None else 'not_verified',
                'property_type': assessment.get('property_type'),
                'construction_year': assessment.get('construction_year'),
                'construction_status': (assessment.get('reviewer_attestation') or {}).get('construction_status', 'NOT_VERIFIED'),
                'attestation': assessment.get('reviewer_attestation')},
            source_refs=e.get('refs') or [], limitations=(e.get('warnings') or []) + (assessment.get('limitations') or [])))
    offers = [EvidenceItem(id=e['id'], kind='asking_offer_not_sale', facts={k: v for k, v in e.items() if k not in {'id', 'refs', 'limitations'}},
                           source_refs=e.get('refs') or [], limitations=e.get('limitations') or [])
              for e in (competitive.get('eligible') or [])[:5]]
    benchmark = None
    if row.get('benchmark_id'):
        benchmark = EvidenceItem(id=row['benchmark_id'], kind='benchmark_not_transaction', facts={}, source_refs=row.get('benchmark_refs') or [])
    return EvidencePack(unit_id=str(row['apartment_id']),
        unit={k: apartment.get(k) for k in ('property_type', 'rooms', 'built_area', 'balcony_area', 'garden_area', 'floors', 'floor_areas', 'directions', 'attribute_states', 'provenance', 'warnings')},
        selected_sales=sales, eligible_offers=offers, excluded_offers=(competitive.get('excluded') or [])[:5], benchmark=benchmark,
        valuation={k: row.get(k) for k in ('estimated_market_value', 'value_low', 'value_high', 'adjusted_market_value', 'adjusted_value_low', 'adjusted_value_high', 'tier', 'status', 'pricing_classification', 'evidence_quality', 'evidence_strength', 'effective_count', 'independent_parcels', 'review_flags')}, 
        adjustments=row.get('unit_adjustments') or {},
        scenarios={k: (row.get('marketing_strategies') or {}).get(k) for k in ('quick_sale', 'balanced', 'high_opening')},
        policy={k: v for k, v in config.items() if k.startswith(('unit_', 'decision_', 'asking_gate_', 'marketing_')) and not isinstance(v, (dict, list))} | {'unit_orientation_rates': config.get('unit_orientation_rates'), 'calibration': 'Declared policy assumptions, not empirically calibrated'}, 
        decision={k: decision.get(k) for k in ('status', 'approval', 'blockers', 'missing_information', 'limitations')} | {
            'selected_sale_count': len(row.get('evidence') or []), 'eligible_offer_count': len(competitive.get('eligible') or []),
            'excluded_offer_count': len(competitive.get('excluded') or []), 'offer_rejection_counts': competitive.get('rejection_counts') or {}},
        warnings=row.get('warnings') or [], special_proxy=row.get('special_property_scenario') or {})
