"""Marketing policy scenarios are separate from executed-sale valuation."""
from datetime import date
from statistics import median


def build_competitor_context(apartment, market, config):
    """Nearby project offers are context; starting prices are never target-equivalent quotes."""
    rows = []
    for project in market.projects:
        if project.rejections or not project.price or project.price <= 0:
            continue
        if project.distance_m is None or project.distance_m > config.competitor_context_radius_m:
            continue
        rows.append({'id': project.id, 'name': project.name, 'distance_m': round(project.distance_m, 1),
            'property_type': project.property_type, 'rooms': project.rooms, 'area': project.area,
            'asking_price': project.price, 'price_per_sqm': round(project.price / project.area) if project.area else None,
            'price_basis': project.price_basis,
            'is_unit_quote': project.price_basis == 'offered_unit_type' and project.area is not None,
            'date': str(project.date) if project.date else None,
            'refs': [r.model_dump(mode='json') for r in project.refs],
            'limitations': ['Marketing offer, not a completed sale', 'Availability and incentives unverified'] +
                ([] if project.area else ['Starting price only: no unit area, so no price per m² and no target-equivalent price'])})
    rows.sort(key=lambda x: (x['distance_m'], x['asking_price']))
    return {'radius_m': config.competitor_context_radius_m, 'count': len(rows),
            'priced_unit_offers': sum(x['is_unit_quote'] for x in rows),
            'starting_price_only': sum(not x['is_unit_quote'] for x in rows),
            'projects': rows, 'listing_comparisons': [], 'listing_comparison_count': 0,
            'listing_comparison_dispersion': None, 'positioning_available': False,
            'warnings': ['Project offers are marketing context, NOT completed transactions or equivalent target quotes',
                         'Starting prices without a unit area are displayed without price per m² and are excluded from valuation and positioning']}


def _apply_legacy_strategy(result, apartment, market, snapshot, config):
    result.strategy_assumptions = [
        f"Uncalibrated neutral marketing scenario: value × (1 + {config.marketing_adjustment:.3f})",
        f"Round to nearest {config.marketing_rounding:,} ILS; not a company pricing policy",
        "Asks/project offers are competition, never executed-sale inputs or an automatic market-value premium",
    ]
    if result.estimated_market_value is None:
        return result
    captures = {c.id: c for c in snapshot.captures}
    asks = []
    selected_listings = {}
    for listing in sorted(market.listings, key=lambda x: (x.id, x.model_dump_json())):
        if listing.id in selected_listings:
            continue
        if listing.rejections or listing.duplicate_of or listing.property_type != apartment.property_type:
            continue
        if not listing.built_area or not listing.price or listing.rooms is None:
            continue
        if abs(listing.built_area-apartment.built_area)/apartment.built_area > config.max_area_difference or abs(listing.rooms-apartment.rooms) > config.max_room_difference:
            continue
        observed = [captures[r.capture_id].captured_at.date() for r in listing.refs if r.capture_id in captures and captures[r.capture_id].captured_at]
        if not observed or not 0 <= (config.as_of-max(observed)).days <= 30:
            continue
        if listing.date and listing.date > config.as_of:
            continue
        asks.append(listing.price/listing.built_area*apartment.built_area)
        selected_listings[listing.id] = listing
        result.listing_refs.append(listing.id)
    offered = []
    project_ids = set()
    for project in market.projects:
        if project.rejections or not project.price:
            continue
        # Starting prices without unit area/rooms are context only, not comparable target quotes.
        if project.area and project.rooms and project.property_type == apartment.property_type and abs(project.area-apartment.built_area)/apartment.built_area <= config.max_area_difference and abs(project.rooms-apartment.rooms) <= config.max_room_difference:
            project_key = project.id.split(':')[1]
            if project_key in project_ids:
                continue
            project_ids.add(project_key)
            offered.append(project.price/project.area*apartment.built_area)
            result.project_refs.append(project.id)
    listing_comparisons = [{'id': listing.id, 'ask': listing.price,
        'target_equivalent': round(listing.price/listing.built_area*apartment.built_area),
        'built_area': listing.built_area, 'rooms': listing.rooms, 'floor': listing.floor,
        'distance_m': listing.distance_m, 'date': str(listing.date) if listing.date else None}
        for listing in selected_listings.values()]
    dispersion = None
    if len(listing_comparisons) >= 2:
        values = [x['target_equivalent'] for x in listing_comparisons]
        dispersion = max(values)/min(values) if min(values) > 0 else None
    result.competitor_context['listing_comparisons'] = listing_comparisons
    result.competitor_context['listing_comparison_count'] = len(listing_comparisons)
    result.competitor_context['listing_comparison_dispersion'] = dispersion
    result.competitor_context['positioning_available'] = False
    result.competitor_context['listing_comparison_kind'] = 'MARKET_CONTEXT_UNVERIFIED_NOT_ELIGIBLE_FOR_POSITIONING'
    if asks or offered:
        raw = result.estimated_market_value*(1+config.marketing_adjustment)
        result.suggested_asking_price = int((raw+config.marketing_rounding/2)//config.marketing_rounding)*config.marketing_rounding
        competition = asks+offered
        if result.tier == "TIER_3" and abs(result.estimated_market_value-median(competition))/median(competition) > config.benchmark_ask_conflict_fraction:
            conflict_msg = f"Benchmark conflicts with selected target-equivalent asking median {median(competition):,.0f} ILS; {len(asks)} asks / {len(offered)} priced offers"
            result.suggested_asking_price = None
            result.market_positioning = conflict_msg + '; POSITIONING_UNAVAILABLE'
            result.warnings.append("Benchmark/asking disagreement: recommendation withheld; asking context is unverified")
            result.strategy_assumptions.append(f"Uncalibrated safety gate: benchmark vs asking median disagreement > {config.benchmark_ask_conflict_fraction:.0%} requires human review; recommendation withheld")
            return result
        result.market_positioning = f"{len(asks)} observed explicit-built-area asks; {len(offered)} priced unit offers; target-equivalent ask median {median(competition):,.0f} ILS; POSITIONING_UNAVAILABLE (freshness and independence unverified)"
        result.warnings.append("Competitive context is asking evidence, with unverified availability and uncalibrated area scaling")
        if result.status == "REVIEW_REQUIRED":
            result.strategy_assumptions.append("Suggested ask inherits REVIEW_REQUIRED preliminary valuation, not an approved launch price")
    else:
        result.market_positioning = "No sufficiently comparable fresh observed ask with explicit built area or priced project unit"
        result.explanation.append("Marketing recommendation withheld: no defensible comparable asking context")
    if dispersion is not None and dispersion > config.asking_gate_max_price_ratio:
        result.market_positioning += f"; unverified context dispersion {dispersion:.2f}× exceeds {config.asking_gate_max_price_ratio:g}×"
    return result


def apply_strategy(result, apartment, market, snapshot, config):
    """Three labelled user-assumption strategies and separately observed asking context."""
    result.competitor_context = build_competitor_context(apartment, market, config)
    result = _apply_legacy_strategy(result, apartment, market, snapshot, config)
    captures = {c.id: c for c in snapshot.captures}
    candidates, rejected = [], []
    is_special = apartment.property_type in {'duplex', 'triplex', 'garden'}
    if is_special and apartment.built_area and apartment.rooms:
        selected_special_ids = set()
        for listing in sorted(market.listings, key=lambda x: (x.id, x.model_dump_json())):
            if listing.property_type != apartment.property_type or listing.duplicate_of:
                continue
            failures = [r for r in listing.rejections if r != 'outside_market_radius']
            if listing.geo_basis not in {'source_point', 'coarse_source_point'} or listing.distance_m is None or listing.distance_m > config.special_listing_radius_m:
                failures.append('outside_special_radius_or_unverified_point')
            if not listing.built_area or not listing.price or not listing.rooms:
                failures.append('missing_explicit_built_area_price_or_rooms')
            elif abs(listing.built_area-apartment.built_area)/apartment.built_area > config.max_area_difference or abs(listing.rooms-apartment.rooms) > config.max_room_difference:
                failures.append('incompatible_area_or_room_count')
            seen = [captures[r.capture_id].captured_at.date() for r in listing.refs if r.capture_id in captures and captures[r.capture_id].captured_at]
            if not seen or not 0 <= (config.as_of-max(seen)).days <= 30 or listing.date and listing.date > config.as_of:
                failures.append('unavailable_fresh_capture')
            row = {'id': listing.id, 'ask': listing.price, 'built_area': listing.built_area, 'rooms': listing.rooms, 'floor': listing.floor,
                   'distance_m': listing.distance_m, 'geo_basis': listing.geo_basis, 'listing_date': str(listing.date) if listing.date else None,
                   'capture_dates': [str(x) for x in seen], 'url': listing.url, 'refs': [r.model_dump(mode='json') for r in listing.refs],
                   'warnings': listing.warnings, 'reasons': failures}
            if failures:
                rejected.append(row)
            elif listing.id in selected_special_ids:
                row['reasons'] = ['duplicate']
                rejected.append(row)
            else:
                selected_special_ids.add(listing.id)
                candidates.append(row)
    candidates.sort(key=lambda x: (x['distance_m'], x['id']))
    # When there are at least two matching ads, review extreme ask-PPM observations;
    # no claim of invalidity and no optimistic replacement by area-incompatible ads.
    if len(candidates) >= 2:
        midpoint = median(x['ask']/x['built_area'] for x in candidates)
        for row in candidates:
            row['review_flag'] = row['ask']/row['built_area'] < midpoint*config.review_low_ratio or row['ask']/row['built_area'] > midpoint*config.review_high_ratio
    else:
        for row in candidates:
            row['review_flag'] = False
    clean = [r for r in candidates if not r['review_flag']]
    has_range = len(clean) >= config.special_listing_min_count
    raw_range = [min(x['ask'] for x in clean), max(x['ask'] for x in clean)] if has_range else None
    # Advertised capture time is not evidence that the listing is still active.
    for row in clean:
        issues = []
        if row['geo_basis'] != 'source_point' or row['distance_m'] > config.asking_gate_radius_m:
            issues.append('geographic_precision_or_distance')
        if abs(row['built_area']-apartment.built_area)/apartment.built_area > config.asking_gate_max_area_difference:
            issues.append('built_area_compatibility')
        if not row['listing_date'] or not 0 <= (config.as_of - date.fromisoformat(row['listing_date'])).days <= config.asking_gate_max_listing_age_days:
            issues.append('listing_freshness_uncertain')
        row['quality_gate_failures'] = issues
    credible = [row for row in clean if not row['quality_gate_failures']]
    # Inspect the entire pre-quarantine shortlist: dropping a flagged extreme must
    # never hide severe dispersion when deciding whether an ask is actionable.
    observed_range = [min(x['ask'] for x in candidates), max(x['ask'] for x in candidates)] if len(candidates) >= 2 else None
    dispersion = observed_range[1]/observed_range[0] if observed_range else None
    gate_failures = []
    if len(credible) < config.asking_gate_min_credible:
        gate_failures.append('insufficient_credible_listings')
        for issue in ['geographic_precision_or_distance', 'built_area_compatibility', 'listing_freshness_uncertain']:
            if any(issue in row['quality_gate_failures'] for row in clean):
                gate_failures.append(issue)
    if dispersion is not None and dispersion > config.asking_gate_max_price_ratio:
        gate_failures.append('excessive_price_dispersion')
    # Hard type/area/price filters ran above; unlike judgement gates they cannot be overridden.
    gates = {'applicable': is_special and result.estimated_market_value is None, 'passed': not gate_failures and has_range, 'failures': gate_failures,
             'credible_count': len(credible), 'minimum_credible': config.asking_gate_min_credible,
             'max_radius_m': config.asking_gate_radius_m, 'max_area_difference': config.asking_gate_max_area_difference,
             'max_listing_age_days': config.asking_gate_max_listing_age_days,
             'price_range_ratio': dispersion, 'max_price_ratio': config.asking_gate_max_price_ratio,
             'type_check': 'EXACT_TYPE_REQUIRED', 'freshness_check': 'PUBLISHED_OR_UPDATED_DATE_REQUIRED; CAPTURE_TIME_NOT_ENOUGH',
             'override': None}
    override = config.asking_overrides.get(apartment.id)
    if override:
        accepted = set(override.accept_gate_failures)
        gates['override'] = {'reviewer': override.reviewer, 'reason': override.reason,
            'accepted_failures': sorted(accepted), 'actual_failures': gate_failures,
            'applied': bool(gate_failures and has_range and set(gate_failures) <= accepted),
            'effect': 'Scenario only; NOT human approval; hard listing filters and minimum two ads cannot be bypassed'}
    range_warnings = ['Very wide observed asking spread; shortlist is heterogeneous and unsuitable for an unattended decision'] if dispersion is not None and dispersion > config.asking_gate_max_price_ratio else []
    result.competitive_context = {'kind': 'SPECIAL_PROPERTY_ASKING_ONLY' if is_special else 'STANDARD_MARKET_CONTEXT',
        'radius_m': config.special_listing_radius_m if is_special else config.radius_m, 'count': len(candidates), 'review_count': len(candidates)-len(clean),
        'usable_count': len(clean), 'min_count': config.special_listing_min_count, 'raw_asking_range': raw_range,
        'raw_asking_median': median(x['ask'] for x in clean) if has_range else None,
        'observed_range_before_review': observed_range,
        'quality_gates': gates, 'price_dispersion_ratio': dispersion,
        'listings': candidates, 'excluded': rejected,
        'warnings': ['Asking prices, NOT completed sales or verified market value; area/room filters and wide geography are experimental',
                     'Review flags are price-per-built-area anomalies, not proof an asking price is invalid',
                     'Range withheld unless at least two non-flagged comparable ads; capture time does not establish active availability'] + range_warnings if is_special else ['See listing_refs and market_positioning; asking prices are not sales']}
    base = result.estimated_market_value
    basis = 'EVIDENCE_BASE_VALUE_UNADJUSTED' if base is not None else None
    indicative_only = result.pricing_classification == 'INDICATIVE_SCENARIO'
    if config.unit_adjustment_mode == 'policy' and result.adjusted_market_value is not None:
        base = result.adjusted_market_value
        basis = ('INDICATIVE_SPECIAL_PROPERTY_PROXY_PLUS_DECLARED_POLICY' if result.estimated_market_value is None
                 else 'EVIDENCE_BASE_PLUS_DECLARED_UNIT_POLICY')
    scenario_withheld = False
    if config.unit_adjustment_mode == 'off' and base is not None and config.marketing_valuation_basis == 'unit_scenario':
        adjusted = result.unit_adjustment_scenario.get('value')
        if not config.acknowledge_target_only_double_counting or adjusted is None or adjusted <= 0:
            scenario_withheld = True
        else:
            base = adjusted
            basis = 'EXPLICIT_TARGET_ONLY_UNIT_SCENARIO_NOT_EVIDENCE_VALUE'
    asking_only = base is None and is_special
    if asking_only and candidates:
        basis = 'ASKING_LISTINGS_ONLY_INSUFFICIENT_FOR_MEDIAN' if not has_range else 'ASKING_PRICE_MEDIAN_ONLY_NOT_SALE_VALUE'
    override_applied = bool(gates['override'] and gates['override']['applied'])
    asking_gate_blocked = asking_only and not (gates['passed'] or override_applied)
    if asking_only and has_range:
        # Override uses the exploratory shortlist; a passed gate uses only credible ads.
        pool = clean if override_applied or asking_gate_blocked else credible
        base = median(x['ask'] for x in pool)
        basis = 'ASKING_PRICE_MEDIAN_ONLY_NOT_SALE_VALUE'
    blocked = asking_gate_blocked or scenario_withheld or result.tier == 'TIER_3' and 'Benchmark conflicts' in result.market_positioning or indicative_only
    if result.estimated_market_value is not None and result.suggested_asking_price is None:
        blocked = True
    for name, adjustment in [('quick_sale', config.marketing_fast_adjustment), ('balanced', config.marketing_balanced_adjustment), ('high_opening', config.marketing_high_adjustment)]:
        raw = base*(1+adjustment) if base is not None and not blocked else None
        proposed = int((raw+config.marketing_rounding/2)//config.marketing_rounding)*config.marketing_rounding if raw is not None else None
        indicative_raw = base*(1+adjustment) if base is not None and blocked else None
        indicative = int((indicative_raw+config.marketing_rounding/2)//config.marketing_rounding)*config.marketing_rounding if indicative_raw is not None else None
        if is_special:
            comparisons = [x['ask'] for x in (clean if override_applied or not asking_only or asking_gate_blocked else credible)]
        else:
            comparisons = [x['target_equivalent'] for x in result.competitor_context['listing_comparisons']]
            comparisons += [x.price/x.area*apartment.built_area for x in market.projects if x.id in result.project_refs and x.price and x.area]
        position = None  # Exploratory context has not passed the decision engine's freshness and independence gates.
        result.marketing_strategies[name] = {'label': 'UNCALIBRATED_BUSINESS_SCENARIO_REQUIRES_HUMAN_APPROVAL', 'basis': basis,
            'basis_amount': base, 'adjustment_fraction': adjustment, 'suggested_asking_price': proposed,
            'indicative_asking_price': indicative, 'is_indicative': indicative is not None,
            'confidence_classification': result.pricing_classification,
            'comparison_fraction_below': position, 'comparison_count': len(comparisons), 'price_dispersion_ratio': dispersion if is_special else (max(comparisons)/min(comparisons) if comparisons and min(comparisons) > 0 else None), 'comparison_basis': ('raw exploratory shortlist, NOT all gate-credible' if is_special and asking_gate_blocked else 'raw matched asking prices' if is_special else 'target-area-scaled asking prices; experimental'),
            'formula': f'basis × (1 + {adjustment:g}), rounded to {config.marketing_rounding:,} ILS',
            'approval': 'NOT_APPROVED', 'blocked_reason': ('indicative special-property proxy: not an evidence-based asking price' if indicative_only else 'asking-only quality gates: ' + ', '.join(gate_failures or ['insufficient_listings']) if asking_gate_blocked else 'unit scenario requires explicit double-count acknowledgement and complete usable inputs' if scenario_withheld else 'benchmark/asking conflict or insufficient competitive context' if blocked else None), 'quality_gate_override': gates['override'] if asking_only else None, 'warnings': ['No sale-time prediction; adjustment is an illustrative configurable scenario, not company policy',
             ('Unit sensitivity used only by explicit user selection and double-count acknowledgement; still uncalibrated' if basis == 'EXPLICIT_TARGET_ONLY_UNIT_SCENARIO_NOT_EVIDENCE_VALUE' else 'Unit sensitivity is excluded or withheld; applying target-only outdoor coefficients could double count'),
             'Source is ASKING PRICES ONLY, not a transaction-based valuation' if basis and basis.startswith('ASKING_') else 'Base estimate inherits all evidence and quality limitations'] + range_warnings}
    if config.unit_adjustment_mode == 'policy':
        result.suggested_asking_price = result.marketing_strategies['balanced']['suggested_asking_price']
    return result
