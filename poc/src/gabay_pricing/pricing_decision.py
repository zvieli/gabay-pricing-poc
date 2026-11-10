"""Evidence-gated, reproducible decision support; never an approved appraisal."""
from collections import Counter
from statistics import median

from gabay_pricing.valuation import quantile, value_apartment



def _capture_fresh(refs, captures, as_of):
    return any(r.capture_id in captures and captures[r.capture_id].captured_at
               and 0 <= (as_of - captures[r.capture_id].captured_at.date()).days <= 30 for r in refs)


def _floor_pairs(evidence, sales):
    """Descriptive pairs are not identification of a causal floor effect."""
    rows = [sales[e.observation_id] for e in evidence if e.observation_id in sales]
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i+1:]:
            if (a.block and a.parcel and (a.block, a.parcel) == (b.block, b.parcel)
                    and a.floor is not None and b.floor is not None and a.floor != b.floor
                    and a.rooms == b.rooms and a.reported_area and b.reported_area
                    and abs(a.reported_area-b.reported_area) <= .01
                    and a.price and b.price):
                lower, upper = sorted([a, b], key=lambda x: x.floor)
                pairs.append({'lower_sale_id': lower.id, 'upper_sale_id': upper.id,
                              'floors': [lower.floor, upper.floor],
                              'prices': [lower.price, upper.price],
                              'dates': [str(lower.date), str(upper.date)],
                              'price_difference': upper.price-lower.price,
                              'refs': [[r.model_dump(mode='json') for r in x.refs] for x in (lower, upper)]})
    return pairs


def _competitors(apartment, market, snapshot, config):
    captures = {c.id: c for c in snapshot.captures}
    eligible, rejected, rejected_rows = [], Counter(), []
    is_special = apartment.property_type in {'duplex', 'triplex', 'garden'}
    effective_radius = config.special_listing_radius_m if is_special else config.asking_gate_radius_m
    for kind, items in [('listing', market.listings), ('project', market.projects)]:
        project_seen = set()
        seen_ids = set()
        for item in sorted(items, key=lambda x: (x.id, x.model_dump_json())):
            if item.id in seen_ids or getattr(item, 'duplicate_of', None):
                rejected_rows.append({'id': item.id, 'kind': kind, 'reasons': ['duplicate']})
                rejected['duplicate'] += 1
                continue
            failures = [r for r in item.rejections if not (is_special and r == 'outside_market_radius')]
            if item.property_type != apartment.property_type:
                failures.append('property_type')
            area = item.built_area if kind == 'listing' else item.area
            if not apartment.built_area or not area or abs(area-apartment.built_area)/apartment.built_area > config.decision_max_area_difference:
                failures.append('built_area')
            if not apartment.rooms or item.rooms != apartment.rooms:
                failures.append('rooms')
            if not item.price or item.price <= 0:
                failures.append('price')
            if item.distance_m is None or item.distance_m > effective_radius:
                failures.append('distance')
            if kind == 'listing' and item.geo_basis != 'source_point':
                failures.append('point_precision')
            if not item.date or not 0 <= (config.as_of-item.date).days <= config.asking_gate_max_listing_age_days:
                failures.append('listing_or_offer_date')
            if not _capture_fresh(item.refs, captures, config.as_of):
                failures.append('capture_date')
            if kind == 'listing' and (not apartment.floors or item.floor is None or abs(item.floor-min(apartment.floors)) > config.decision_max_floor_difference):
                failures.append('floor_unknown_or_different')
            if kind == 'project' and item.price_basis != 'offered_unit_type':
                failures.append('starting_price_not_unit_offer')
            if failures:
                rejected.update(set(failures))
                rejected_rows.append({'id': item.id, 'kind': kind, 'reasons': sorted(set(failures))})
                continue
            if kind == 'project':
                key = item.id.split(':')[1]
                if key in project_seen:
                    rejected['same_project_not_independent'] += 1
                    rejected_rows.append({'id': item.id, 'kind': kind, 'reasons': ['same_project_not_independent']})
                    continue
                project_seen.add(key)
            seen_ids.add(item.id)
            eligible.append({'id': item.id, 'kind': kind, 'observed_price': item.price,
                'area': area, 'rooms': item.rooms, 'floor': getattr(item, 'floor', None),
                'distance_m': item.distance_m, 'date': str(item.date),
                'refs': [r.model_dump(mode='json') for r in item.refs],
                'limitations': ['Availability and concessions unverified',
                    'Floor unknown for project offering' if kind == 'project' else 'Condition, new-build status and rights unverified']})
    return sorted(eligible, key=lambda x: (x['observed_price'], x['id'])), dict(sorted(rejected.items())), rejected_rows


def apply_pricing_decision(result, apartment, market, snapshot, config):
    sales = {s.id: s for s in market.sales if not s.duplicate_of}
    assessments = []
    flags = {f.observation_id for f in result.review_flags}
    for e in result.evidence:
        s = sales[e.observation_id]
        v = config.sale_verifications.get(s.id)
        assessments.append({'id': s.id, 'legacy_selected': True, 'review_flag': s.id in flags,
            'provider_reported_price': s.price, 'reported_area': s.reported_area,
            'provider_built_area': s.built_area, 'rooms': s.rooms, 'floor': s.floor,
            'property_type': s.property_type, 'construction_year': s.building_year,
            'date': str(s.date), 'share': s.share, 'geo_basis': s.geo_basis,
            'distance_m': s.distance_m, 'block': s.block, 'parcel': s.parcel,
            'subparcel': s.subparcel, 'legacy_score': e.score, 'refs': [r.model_dump(mode='json') for r in e.refs],
            'reviewer_attestation': v.model_dump(mode='json') if v else None,
            'strict_eligibility': 'REQUIRES_MATCHING_AND_REVIEW' if v else 'EXCLUDED_NO_INDEPENDENT_REVIEW',
            'limitations': s.warnings + ['Provider-reported transfer is not independent proof of terms',
                'Parking, storage, outdoor rights and construction comparability unverified']})
    verified_sales = []
    for sale in market.sales:
        v = config.sale_verifications.get(sale.id)
        review = config.anomaly_reviews.get(sale.id)
        if not v or sale.duplicate_of or review and review.disposition == 'exclude':
            continue
        # An attestation is a documented human claim, not a substitute for automated hard filters.
        verified_sales.append(sale.model_copy(update={'built_area': v.verified_built_area,
            'area_semantics': 'reviewer_attested_built', 'property_type': v.property_type}))
    strict_market = market.model_copy(update={'sales': verified_sales, 'benchmarks': []})
    strict_config = config.model_copy(update={'allow_reported_sale_area': False, 'allow_special_benchmark_proxy': False})
    strict = value_apartment(apartment, strict_market, strict_config)
    selected = {e.observation_id for e in strict.evidence}
    for row in assessments:
        if row['reviewer_attestation']:
            row['strict_eligibility'] = 'SELECTED_ATTESTED' if row['id'] in selected else 'NOT_SELECTED_OR_FAILED_HARD_FILTERS'
    competitors, rejected, rejected_rows = _competitors(apartment, market, snapshot, config)
    starting_price_context = [{'id': p.id, 'name': p.name, 'price': p.price, 'area': p.area, 'rooms': p.rooms,
        'price_basis': p.price_basis, 'distance_m': round(p.distance_m, 1) if p.distance_m is not None else None,
        'date': str(p.date) if p.date else None} for p in market.projects
        if not p.rejections and p.price and p.distance_m is not None and p.distance_m <= config.competitor_context_radius_m]
    reference = strict if strict.evidence and strict.tier in {'TIER_1', 'TIER_2'} else (result if not config.decision_require_attestations and result.evidence else None)
    conditional_mode = reference is not None and not (strict.evidence and strict.tier in {'TIER_1', 'TIER_2'})
    blockers = []
    if not strict.evidence and config.decision_require_attestations:
        blockers.append('insufficient_reviewer_verified_sale_comparables_and_distinct_parcels')
    if strict.evidence and strict.tier in {'TIER_1', 'TIER_2'} and (
            {e.observation_id: e.adjusted_ppm for e in strict.evidence} !=
            {e.observation_id: e.adjusted_ppm for e in result.evidence} or
            strict.estimated_market_value != result.estimated_market_value):
        blockers.append('scenario_basis_differs_from_verified_sale_reference')
    unresolved = {f.observation_id for f in strict.review_flags + result.review_flags} - set(config.anomaly_reviews)
    if unresolved and strict.evidence:
        blockers.append('selected_sale_anomalies_require_documented_review')
    if len(competitors) < config.decision_min_competitors:
        blockers.append('insufficient_current_compatible_competitor_offers')
    if competitors and competitors[-1]['observed_price']/competitors[0]['observed_price'] > config.asking_gate_max_price_ratio:
        blockers.append('competitor_price_dispersion_excessive')
    target_fraction = getattr(config, f'decision_{config.decision_objective}_target_fraction')
    decision = {'status': 'BLOCKED_REQUIRES_REVIEW', 'approval': 'NOT_APPROVED',
        'objective': config.decision_objective, 'objective_target_fraction_below': target_fraction,
        'legacy_reference': {'value': result.estimated_market_value, 'tier': result.tier,
            'range': [result.value_low, result.value_high], 'quarantine_sensitivity': result.quarantine_sensitivity},
        'selected_comparable_assessments': assessments,
        'anomaly_reviews': {k: v.model_dump(mode='json') for k, v in config.anomaly_reviews.items()},
        'unresolved_anomaly_ids': sorted(unresolved),
        'strict_reference': {'value': strict.estimated_market_value if strict.evidence else None,
            'tier': strict.tier if strict.evidence else 'NONE', 'eligible_recent': strict.eligible_recent,
            'eligible_historical': strict.eligible_historical, 'selected_sale_ids': sorted(selected),
            'selected_evidence': [e.model_dump(mode='json') for e in strict.evidence],
            'attestations': {s.id: config.sale_verifications[s.id].model_dump(mode='json') for s in verified_sales},
            'review_flags': [f.model_dump(mode='json') for f in strict.review_flags],
            'blocker': None if strict.evidence else 'At least the configured sale and parcel minimums must pass hard filters after independent reviewer attestation; benchmark fallback disabled'},
        'unit_scenario': result.unit_adjustment_scenario,
        'unit_adjustments': result.unit_adjustments,
        'confidence_classification': result.pricing_classification,
        'floor_investigation': {'matched_pairs': _floor_pairs(result.evidence, sales),
            'conclusion': 'Descriptive within-parcel pairs do not control terms, timing, condition or outdoor attributes; no causal rate calibrated'},
        'competitive_evidence': {'eligible': competitors, 'rejection_counts': rejected, 'excluded': rejected_rows,
            'rules': {'max_distance_m': config.special_listing_radius_m if apartment.property_type in {'duplex', 'triplex', 'garden'} else config.asking_gate_radius_m, 'max_area_difference': config.decision_max_area_difference,
                'same_rooms_and_type': True, 'max_floor_difference_for_listings': config.decision_max_floor_difference,
                'max_listing_age_days': config.asking_gate_max_listing_age_days, 'max_capture_age_days': 30,
                'minimum_count': config.decision_min_competitors},
            'warning': 'Observed offers are not completed sales; availability, feature equivalence and concessions remain unverified'},
        'feasible_range': None, 'candidates': [], 'recommended_opening_price': None,
        'fallback_opening_price': result.marketing_strategies.get('balanced', {}).get('suggested_asking_price'),
        'fallback_basis': 'Balanced policy scenario for review; not a competitive optimum or approval',
        'evidence_mode': 'REVIEWER_ATTESTED' if strict.evidence and strict.tier in {'TIER_1', 'TIER_2'} else 'LEGACY_SELECTED_SALES_CONDITIONAL' if not config.decision_require_attestations and result.evidence else 'NO_SALE_EVIDENCE',
        'starting_price_context': starting_price_context,
        'blockers': blockers, 'missing_information': ['Independent primary-document review of comparable sale terms, area, subtype and construction status',
            'Current compatible unit-specific asks and project offers, availability and concessions',
            'Target and comparable parking, storage, outdoor rights, condition and construction specification'],
        'limitations': ['No sale probability, time-to-sale or revenue prediction',
            'Sale evidence may be unverified; human marketing approval is separate',
            'Candidates use declared scenario assumptions; target-only adjustments can double count',
            'Floor and orientation effects are not calibrated']}
    positioning_available = not any(b in blockers for b in ('insufficient_current_compatible_competitor_offers', 'competitor_price_dispersion_excessive'))
    for name in ('quick_sale', 'balanced', 'high_opening'):
        scenario = result.marketing_strategies.get(name, {})
        if (scenario.get('basis') or '').startswith('ASKING_'):
            continue
        price = scenario.get('suggested_asking_price')
        indicative = price is None
        if indicative:
            price = scenario.get('indicative_asking_price')
        if price is None:
            continue
        below = sum(x['observed_price'] < price for x in competitors)/len(competitors) if positioning_available else None
        above = sum(x['observed_price'] > price for x in competitors)/len(competitors) if positioning_available else None
        decision['candidates'].append({'scenario': name, 'price': price, 'indicative': indicative,
            'fraction_strictly_below': below, 'fraction_strictly_above': above,
            'objective_distance': abs(below-target_fraction) if below is not None else None,
            'inside_sale_iqr': None, 'sources': [x['id'] for x in competitors] if positioning_available else []})
    if reference is not None:
        values = [e.adjusted_ppm*apartment.built_area for e in reference.evidence]
        lo, hi = quantile(values, .25), quantile(values, .75)
        decision['feasible_range'] = [round(lo), round(hi)]
        decision['range_basis'] = ('IQR of selected sale PPM times target declared area; not a confidence interval or guaranteed sale price' if not conditional_mode else
            'IQR of legacy-selected provider sales before independent attestation; conditional evidence, not a verified appraisal')
        for candidate in decision['candidates']:
            candidate['inside_sale_iqr'] = lo <= candidate['price'] <= hi
        interior = [c for c in decision['candidates'] if not c['indicative'] and c['inside_sale_iqr']
                    and c['fraction_strictly_below'] is not None and c['fraction_strictly_below'] > 0 and c['fraction_strictly_above'] > 0]
        if not interior:
            blockers.append('no_interior_scenario_within_sale_iqr')
        if not blockers:
            chosen = min(interior, key=lambda c: (c['objective_distance'], abs(c['price']-reference.estimated_market_value), c['price']))
            decision['status'] = 'CONDITIONAL_RECOMMENDATION_REQUIRES_HUMAN_APPROVAL'
            decision['recommended_opening_price'] = chosen['price']
            decision['rationale'] = ('Among scenario prices inside the selected-sale IQR and neither cheapest nor most expensive against eligible offers, '
                'this price minimizes distance to the objective target fraction strictly below; ties favor the market reference then lower price. '
                'This is a transparent positioning rule, not a statistical optimum.')
        else:
            decision['status'] = 'CONDITIONAL_POSITIONING_UNAVAILABLE'
    elif result.adjusted_market_value is not None:
        decision['feasible_range'] = [result.adjusted_value_low, result.adjusted_value_high]
        decision['range_basis'] = 'Indicative benchmark/proxy range plus declared policy; NOT evidence-based sale support'
        decision['status'] = 'INDICATIVE_SCENARIO_ONLY'
        blockers.append('no_evidence_based_sale_range_for_this_property_type')
    decision['blockers'] = blockers
    context = result.competitor_context
    positioning_blockers = [x for x in blockers if x in {'insufficient_current_compatible_competitor_offers', 'competitor_price_dispersion_excessive'}]
    if len(competitors) < config.decision_min_competitors:
        reasons = set(rejected)
        for source, label in [('listing_or_offer_date', 'some_candidates_missing_or_stale_listing_date'),
                              ('built_area', 'some_candidates_have_incompatible_or_missing_features'),
                              ('rooms', 'some_candidates_have_incompatible_or_missing_features'),
                              ('property_type', 'some_candidates_have_incompatible_or_missing_features'),
                              ('floor_unknown_or_different', 'some_candidates_have_incompatible_or_missing_features'),
                              ('duplicate', 'some_records_are_duplicates'),
                              ('same_project_not_independent', 'some_offers_share_a_project')]:
            if source in reasons and label not in positioning_blockers:
                positioning_blockers.append(label)
    context['positioning_available'] = not positioning_blockers
    context['positioning_blockers'] = positioning_blockers
    context['eligible_for_positioning'] = competitors
    prior_conflict = result.market_positioning if 'Benchmark conflicts' in result.market_positioning else ''
    result.market_positioning = (prior_conflict + '; ' if prior_conflict else '') + (
        'ELIGIBLE_FOR_POSITIONING' if context['positioning_available'] else 'POSITIONING_UNAVAILABLE') + (
        f"; unverified market context: {context.get('listing_comparison_count', 0)} listings; eligible: {len(competitors)}; "
        'blockers: ' + (', '.join(positioning_blockers) or 'none'))
    dispersion = context.get('listing_comparison_dispersion')
    if dispersion is not None and dispersion > config.asking_gate_max_price_ratio:
        result.market_positioning += f'; unverified context dispersion {dispersion:.2f}× exceeds {config.asking_gate_max_price_ratio:g}×'
    result.pricing_decision = decision
    return result
