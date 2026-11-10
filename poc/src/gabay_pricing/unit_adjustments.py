"""Unit-level policy adjustments, explicitly separated from market evidence."""
from statistics import median

from gabay_pricing.valuation import weighted_median


def apply_unit_scenario(result, apartment, market, config):
    scenario = {'label': 'UNCALIBRATED_TARGET_ONLY_SENSITIVITY_NOT_MARKET_VALUE', 'base_value': result.estimated_market_value,
                'coefficients': {'balcony': config.unit_balcony_equivalent, 'garden': config.unit_garden_equivalent, 'floor_rate': config.unit_floor_rate, 'orientation_rates': config.unit_orientation_rates},
                'built_area': apartment.built_area, 'balcony_area': apartment.balcony_area, 'garden_area': apartment.garden_area,
                'equivalent_area': None, 'known_component_equivalent_area': None, 'conditional_known_component_only_delta': None, 'conditional_known_component_only_value': None, 'outdoor_delta': None, 'floor_delta': None, 'orientation_delta': None,
                'value': None, 'floor_reference': None, 'floor_reference_basis': None, 'floor_reference_sale_ids': [], 'orientation_basis': 'USER_ASSUMED_RATE' if config.unit_orientation_rates else 'NOT_CONFIGURED_NO_ADJUSTMENT', 'floor_rate_basis': 'USER_ASSUMED_RATE' if config.unit_floor_rate else 'NOT_CONFIGURED_NO_ADJUSTMENT', 'warnings': [],
                'assumption': f'Aeq=built+{config.unit_balcony_equivalent:g}*balcony+{config.unit_garden_equivalent:g}*garden; coefficients are business assumptions, not calibrated.'}
    if apartment.built_area is None:
        scenario['warnings'].append('Built area missing')
        result.unit_adjustment_scenario = scenario
        return result
    states = apartment.attribute_states
    balcony = apartment.balcony_area
    garden = apartment.garden_area
    # N/A is a classification convention, not evidence that the building has no garden.
    garden_na = states.get('garden_area') == 'NOT_APPLICABLE'
    known = apartment.built_area + config.unit_balcony_equivalent*(balcony if balcony is not None else 0) + config.unit_garden_equivalent*(garden if garden is not None else 0)
    scenario['known_component_equivalent_area'] = round(known, 4)
    if balcony is not None and (garden is not None or garden_na):
        scenario['equivalent_area'] = round(known, 4)
    else:
        scenario['warnings'].append('Unknown outdoor component: shown known-component equivalent area is partial, not complete')
    if garden_na:
        scenario['warnings'].append('Garden N/A is a property-type convention, not physical verification')
    sales = {s.id: s for s in market.sales if not s.duplicate_of}
    available = [(sales[e.observation_id].floor, e.score, e.observation_id) for e in result.evidence if e.observation_id in sales and sales[e.observation_id].floor is not None]
    if config.unit_reference_floor is not None:
        scenario['floor_reference'] = config.unit_reference_floor
        scenario['floor_reference_basis'] = 'USER_ASSUMED_REFERENCE_NOT_EMPIRICAL'
    elif len(available) >= 2:
        scenario['floor_reference'] = weighted_median([x[0] for x in available], [x[1] for x in available])
        scenario['floor_reference_basis'] = 'WEIGHTED_MEDIAN_OF_KNOWN_SELECTED_SALE_FLOORS_NOT_CAUSAL'
        scenario['floor_reference_sale_ids'] = [x[2] for x in available]
    else:
        scenario['floor_reference_basis'] = 'UNKNOWN_NO_ASSUMED_FLOOR_1'
    scenario['floor_observed_count'] = len(available)
    scenario['floor_selected_count'] = len(result.evidence)
    if result.estimated_market_value is not None:
        value = result.estimated_market_value
        ppm = result.implied_ppm
        scenario['conditional_known_component_only_delta'] = round((known - apartment.built_area)*ppm)
        scenario['conditional_known_component_only_value'] = value + scenario['conditional_known_component_only_delta']
        if scenario['equivalent_area'] is not None:
            scenario['outdoor_delta'] = scenario['conditional_known_component_only_delta']
        else:
            scenario['warnings'].append('Complete outdoor monetary delta withheld: conditional known-component-only value omits unknown areas and is not a valuation')
        if config.unit_floor_rate and (not apartment.floors or scenario['floor_reference'] is None):
            scenario['warnings'].append('Floor scenario withheld: target floor or comparable reference unknown')
        else:
            scenario['floor_delta'] = round(value*config.unit_floor_rate*(min(apartment.floors)-scenario['floor_reference'])) if config.unit_floor_rate else 0
        if config.unit_orientation_rates:
            if len(apartment.directions) != 1 or apartment.directions[0] not in config.unit_orientation_rates:
                scenario['warnings'].append('Orientation scenario withheld: direction missing, ambiguous or not configured')
            else:
                scenario['orientation_delta'] = round(value*config.unit_orientation_rates[apartment.directions[0]])
        else:
            scenario['orientation_delta'] = 0
        if all(scenario[x] is not None for x in ['outdoor_delta', 'floor_delta', 'orientation_delta']):
            scenario['value'] = value + scenario['outdoor_delta'] + scenario['floor_delta'] + scenario['orientation_delta']
        scenario['warnings'].append('TARGET-ONLY outdoor scenario may double-count outdoor attributes already embedded in transaction or benchmark prices; comparable outdoor areas are unknown. Never add to base value or asking price without explicit human validation.')
        if result.tier == 'TIER_3' and apartment.property_type == 'garden':
            scenario['warnings'].append('Garden benchmark already derives from garden transactions; extra garden delta is especially likely to double count')
    else:
        scenario['warnings'].append('No sale-based base value: no adjusted valuation generated')
    result.unit_adjustment_scenario = scenario
    return result


def _market_floor_evidence(result, market, config):
    """Descriptive same-parcel pairs are evidence of association, not a causal rate."""
    sales = {s.id: s for s in market.sales if not s.duplicate_of and not s.rejections}
    rows = [sales[e.observation_id] for e in result.evidence if e.observation_id in sales]
    per_floor = []
    for i, lower in enumerate(rows):
        for upper in rows[i + 1:]:
            if (lower.block and lower.parcel and (lower.block, lower.parcel) == (upper.block, upper.parcel)
                    and lower.floor is not None and upper.floor is not None and lower.floor != upper.floor
                    and lower.rooms == upper.rooms and lower.reported_area and upper.reported_area
                    and abs(lower.reported_area - upper.reported_area) <= .01 * max(lower.reported_area, upper.reported_area)
                    and lower.price and upper.price and lower.price > 0 and upper.price > 0):
                lo, hi = sorted([lower, upper], key=lambda x: x.floor)
                per_floor.append((hi.price / lo.price) ** (1 / (hi.floor - lo.floor)) - 1)
    if len(per_floor) < config.unit_min_market_floor_pairs:
        return None, {'pairs': len(per_floor), 'required': config.unit_min_market_floor_pairs,
                      'basis': 'INSUFFICIENT_MATCHED_PAIRS_USING_DECLARED_POLICY'}
    return median(per_floor), {'pairs': len(per_floor), 'required': config.unit_min_market_floor_pairs,
                               'basis': 'MARKET_DERIVED_DESCRIPTIVE_SAME_PARCEL_PAIRS_NOT_CAUSAL',
                               'per_floor_rates': [round(x, 6) for x in per_floor]}


def apply_unit_adjustments(result, apartment, market, config):
    """Apply an explicit, auditable unit policy while preserving the market baseline."""
    baseline = result.estimated_market_value
    scenario = result.special_property_scenario or {}
    indicative_base = scenario.get('value') if baseline is None else None
    indicative_range = scenario.get('range') if baseline is None else None
    classification = ('EVIDENCE_LIMITED_BENCHMARK' if baseline is not None and result.tier == 'TIER_3'
                      else 'EVIDENCE_SUPPORTED' if baseline is not None else 'INDICATIVE_SCENARIO' if indicative_base else 'INSUFFICIENT_EVIDENCE')
    payload = {'mode': config.unit_adjustment_mode,
               'baseline_value': baseline, 'baseline_range': [result.value_low, result.value_high],
               'baseline_ppm': result.implied_ppm, 'indicative_proxy_value': indicative_base,
               'classification': classification, 'components': [], 'applied_total': 0,
               'adjusted_value': baseline, 'adjusted_range': [result.value_low, result.value_high],
               'policy_label': 'DECLARED_COMPANY_POLICY_ASSUMPTION_NOT_EMPIRICALLY_CALIBRATED',
               'warnings': [], 'market_floor_evidence': None}
    if config.unit_adjustment_mode == 'off':
        payload['note'] = 'Unit adjustment policy disabled: baseline market value unchanged, no attribute premium applied'
        result.unit_adjustments = payload
        result.pricing_classification = classification
        result.adjusted_market_value = baseline
        result.adjusted_value_low, result.adjusted_value_high = result.value_low, result.value_high
        return result
    if baseline is None and not indicative_base:
        payload['note'] = 'No evidence-based or indicative base value: unit adjustments withheld rather than fabricated'
        result.unit_adjustments = payload
        result.pricing_classification = 'INSUFFICIENT_EVIDENCE'
        return result
    base = baseline if baseline is not None else indicative_base
    base_kind = 'EVIDENCE_BASE' if baseline is not None else 'INDICATIVE_PROXY_BASE'
    ppm = base / apartment.built_area if apartment.built_area else None
    total = 0
    def add(attribute, amount, source, basis, applied, observed=None, reference=None, note='', amount_basis='ILS'):
        nonlocal total
        entry = {'attribute': attribute, 'observed': observed, 'reference': reference, 'amount_ils': amount if applied else None,
                 'sensitivity_amount_ils': None if applied else amount, 'source': source, 'applied': applied,
                 'amount_basis': amount_basis, 'note': note}
        if applied:
            total += amount
        payload['components'].append(entry)
    # Floor: only a descriptive market rate with enough matched pairs overrides the declared policy.
    rate, evidence = _market_floor_evidence(result, market, config)
    payload['market_floor_evidence'] = evidence
    target_floor = min(apartment.floors) if apartment.floors else None
    reference = config.unit_reference_floor
    if reference is None:
        reference = (result.unit_adjustment_scenario or {}).get('floor_reference')
    if rate is None:
        rate = config.unit_floor_rate
        source = 'DECLARED_COMPANY_POLICY_ASSUMPTION'
    else:
        source = evidence['basis']
    if not rate or target_floor is None or reference is None:
        add('floor', None, source, 'NOT_APPLIED', False, observed=target_floor, reference=reference,
            note='Floor, reference or explicit rate missing; no premium assumed' if target_floor is not None else 'Target floor unknown; no floor premium assumed')
    else:
        add('floor', round(base * rate * (target_floor - reference)), source, f'{rate:+.4%} per floor vs reference {reference:g}', True,
            observed=target_floor, reference=reference,
            note='Declared policy, not an empirical causal coefficient' if source == 'DECLARED_COMPANY_POLICY_ASSUMPTION' else 'Descriptive same-parcel association; not causal')
    # Orientation: only a single recorded direction can be priced; missing is not zero and receives no premium.
    orientation = config.unit_orientation_rates.get(apartment.directions[0]) if len(apartment.directions) == 1 else None
    if orientation is None:
        add('orientation', None, 'DECLARED_COMPANY_POLICY_ASSUMPTION', 'NOT_APPLIED', False,
            observed=list(apartment.directions),
            note='Direction missing, ambiguous or not configured; no premium assumed')
    else:
        add('orientation', round(base * orientation), 'DECLARED_COMPANY_POLICY_ASSUMPTION', f'{orientation:+.2%}', True,
            observed=apartment.directions[0], note='Declared policy assumption; no orientation data exists in comparable sales')
    # Outdoor: shown as sensitivity only. Comparable outdoor areas are unknown, so adding it could double count.
    for attribute, area, equivalent in [('balcony', apartment.balcony_area, config.unit_balcony_equivalent),
                                        ('garden', apartment.garden_area, config.unit_garden_equivalent)]:
        if area is None:
            add(attribute, None, 'DECLARED_COMPANY_POLICY_ASSUMPTION', 'NOT_APPLIED', False, observed=None,
                note='Area unknown: no zero substitution and no premium assumed')
        elif config.unit_apply_outdoor_to_base and ppm:
            add(attribute, round(area * equivalent * ppm), 'DECLARED_COMPANY_POLICY_ASSUMPTION', f'{equivalent:.0%} of area × baseline PPM', True,
                observed=area, note='Explicit policy; comparable outdoor areas unavailable')
        else:
            add(attribute, round(area * equivalent * ppm) if ppm else None, 'DECLARED_COMPANY_POLICY_ASSUMPTION',
                f'{equivalent:.0%} equivalent area (sensitivity only)', False, observed=area,
                note='Not added to base: comparable outdoor areas are unknown and the benchmark may already include outdoor value; this avoids double counting')
    if apartment.property_type in {'duplex', 'triplex'} and baseline is None and indicative_base:
        premium = config.unit_duplex_premium if apartment.property_type == 'duplex' else config.unit_triplex_premium
        if premium:
            add('property_type', round(indicative_base * premium), 'DECLARED_COMPANY_POLICY_ASSUMPTION',
                f'{premium:+.0%} special-property proxy premium', True, observed=apartment.property_type,
                note='Indicative only; applied to a standard-apartment benchmark proxy, not to a special-property sale')
    adjusted = round(base + total)
    if baseline is not None:
        payload['adjusted_value'] = adjusted
        payload['adjusted_range'] = [round((result.value_low or baseline) + total), round((result.value_high or baseline) + total)]
    else:
        lo, hi = (indicative_range or [adjusted, adjusted])
        payload['adjusted_value'] = adjusted
        payload['adjusted_range'] = [round(lo + total), round(hi + total)]
        payload['warnings'].append('Indicative scenario only: based on a standard-apartment benchmark proxy, not evidence of this special property')
    payload['applied_total'] = total
    payload['applied_adjustments'] = [c['attribute'] for c in payload['components'] if c['applied']]
    payload['formula'] = 'baseline or indicative proxy + sum of individually rounded applied components; outdoor is sensitivity only unless explicitly applied'
    if payload['applied_adjustments']:
        payload['classification'] = classification
    else:
        payload['warnings'].append('No unit attribute adjustment could be applied: prices remain differentiated only by market evidence and area')
    result.unit_adjustments = payload
    result.adjusted_market_value = payload['adjusted_value']
    result.adjusted_value_low, result.adjusted_value_high = payload['adjusted_range']
    result.pricing_classification = classification
    if classification == 'INDICATIVE_SCENARIO':
        result.pricing_classification = 'INDICATIVE_SCENARIO'
    return result
