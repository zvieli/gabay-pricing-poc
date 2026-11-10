"""Phase 3 same-snapshot comparison; research output stays local and untracked."""
import csv
import json
from collections import Counter
from pathlib import Path

from gabay_pricing.models import PricingConfig
from gabay_pricing.cli import price


def fmt(value):
    return '—' if value is None else f'₪{value:,.0f}'


def create_comparison(snapshot, workbook, snapshot_path, before_path, directory):
    before = json.loads(Path(before_path).read_text())
    config = PricingConfig(as_of=snapshot.as_of)
    current = price(snapshot, workbook, config, snapshot_path)
    if any(before[k] != current[k] for k in ['snapshot_sha256', 'workbook_sha256']):
        raise ValueError('Before/after snapshot or workbook differs')
    floor = price(snapshot, workbook, config.model_copy(update={'unit_floor_rate': .01}), snapshot_path)
    strict_area = price(snapshot, workbook, config.model_copy(update={'allow_reported_sale_area': False}), snapshot_path)
    strict_by_id = {r['apartment_id']: r for r in strict_area['results']}
    old = {x['apartment_id']: x for x in before['results']}
    new = {x['apartment_id']: x for x in current['results']}
    illustrative = {x['apartment_id']: x for x in floor['results']}
    apartments = {x['id']: x for x in current['apartments']}
    if old.keys() != new.keys():
        raise ValueError('Apartment sets differ')
    lines = ['# Phase 3A–3C: before/after comparison', '', '**Research-only. Base values and asking-only strategy scenarios are distinct. No strategy is approved; no time-to-sale predictions. No new paid calls.**', '', f"Same snapshot `{current['snapshot_sha256']}`, workbook `{current['workbook_sha256']}`. Before code `{before['code_sha256']}`, after `{current['code_sha256']}`.", '', '## Every apartment', '', 'Aeq is complete only where all applicable outdoor components are known. The target-only outdoor sensitivity can double count outdoor characteristics embedded in comparison prices; it is not the base valuation. Floor scenario (+1% per floor vs selected-sale weighted-median floor) is illustrative and uncalibrated, never applied to base or strategy.', '', '| ID | Type | Old → new base | Status | Balcony → garden m² (old → new) | Aeq / known partial | Target-only outdoor sensitivity | Illustrative floor delta | Quick / balanced / high asks | Reason |', '|---|---|---|---|---|---|---|---:|---|---|']
    records = []
    for aid in sorted(new, key=int):
        a, b, target, scenario = old[aid], new[aid], apartments[aid], illustrative[aid]['unit_adjustment_scenario']
        old_apartment = next(x for x in before['apartments'] if x['id'] == aid)
        unit = b['unit_adjustment_scenario']
        rates = b['marketing_strategies']
        old_ids = {e['observation_id'] for e in a['evidence']}; new_ids = {e['observation_id'] for e in b['evidence']}
        changed = a['estimated_market_value'] != b['estimated_market_value'] or old_ids != new_ids
        reason = 'Base/selection changed; inspect comparable IDs' if changed else 'Base and selected sales unchanged; new scenarios are separate'
        if target['property_type'] == 'garden':
            reason += '; outdoor header interpreted as inferred garden, balcony unknown'
        if b['status'] == 'UNPRICED' and rates.get('balanced', {}).get('suggested_asking_price'):
            reason += '; asking-only special-property strategies, NOT market value'
        asks = [rates.get(k, {}).get('suggested_asking_price') for k in ['quick_sale','balanced','high_opening']]
        area = unit['equivalent_area'] if unit['equivalent_area'] is not None else f"{unit['known_component_equivalent_area']} partial"
        lines.append(f"| {aid} | {b['property_type']} | {fmt(a['estimated_market_value'])} → {fmt(b['estimated_market_value'])} | {a['status']} → {b['status']} | {old_apartment['balcony_area']}/{old_apartment['garden_area']} → {target['balcony_area']}/{target['garden_area']} | {area} | {fmt(unit['conditional_known_component_only_value'])} | {fmt(scenario['floor_delta'])} | {' / '.join(fmt(x) for x in asks)} | {reason} |")
        records.append({'apartment_id': aid, 'old_base': a['estimated_market_value'], 'new_base': b['estimated_market_value'], 'old_status': a['status'], 'new_status': b['status'], 'old_balcony': old_apartment['balcony_area'], 'old_garden': old_apartment['garden_area'], 'new_balcony': target['balcony_area'], 'new_garden': target['garden_area'], 'equivalent_area': unit['equivalent_area'], 'known_partial_equivalent_area': unit['known_component_equivalent_area'], 'conditional_outdoor_value': unit['conditional_known_component_only_value'], 'illustrative_floor_delta': scenario['floor_delta'], 'strict_area_value': strict_by_id[aid]['estimated_market_value'], 'strict_area_tier': strict_by_id[aid]['tier'], 'quick': asks[0], 'balanced': asks[1], 'high': asks[2], 'removed_comp_ids': ';'.join(sorted(old_ids-new_ids)), 'added_comp_ids': ';'.join(sorted(new_ids-old_ids)), 'reason': reason})
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'phase3_before_after.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0])); writer.writeheader(); writer.writerows(records)
    lines += ['', '## Unit sensitivity and double-counting', '', 'The default target coefficients are 0.25 balcony and 0.20 garden. Formula for the target-only hypothetical scenario: base + base-PPM × (0.25×balcony + 0.20×garden). Comparable sale outdoor areas are unknown, so no adjustment is made to the base and no assumption that comps have no balcony is made. Known-component-only outputs omit unknown areas; they are not complete Aeq. Garden benchmarks already include garden transactions; their target-only deltas can double count and are never applied to a recommendation.', '', 'Floor sensitivity: 1% in this report is illustrative only; default is 0%. The reference is the weighted median of known selected-sale floors, with count and IDs stored in JSON. If there are fewer than two known floor observations, reference is UNKNOWN unless the user explicitly supplies one. No automatic reference floor1. Orientation coefficients default empty; missing/ambiguous direction blocks nonzero orientation scenarios.', '', '## Special-property asking evidence', '', 'Asks are filtered by the same explicit property type, explicit built area, rooms and source point within configurable radius (default 5km), with source/capture references and candidate/exclusion reasons in JSON. The 5km expansion is explicitly broader than the base market and can include less comparable submarkets. Range uses raw asking prices, not scaled or asserted sale values. Candidate price/m² outside 0.5×–2× candidate median is flagged for review, not proven invalid. Two non-flagged ads are required for an exploratory range. Asking-only recommendations additionally require three credible listings and pass distance, area, source-date freshness and dispersion quality gates (or a documented per-apartment human override); smaller or weak samples remain visible without an automated recommendation.', '']
    paired = [(r, strict_by_id[r['apartment_id']]) for r in current['results'] if r['estimated_market_value'] is not None and strict_by_id[r['apartment_id']]['estimated_market_value'] is not None]
    changes = [abs(b['estimated_market_value']/a['estimated_market_value']-1) for a, b in paired]
    from statistics import median
    lines += ['', '## Area-basis sensitivity', '', f"Strict scenario `allow_reported_sale_area=false`: tiers {dict(Counter(r['tier'] for r in strict_area['results']))}; {len(paired)} paired numeric scenarios, median absolute change {median(changes):.1%}, maximum {max(changes):.1%}." if changes else 'Strict scenario produced no paired numeric estimates.', 'This blocks sale records with unknown built-area semantics; review-only Bait benchmark fallback can still have unknown area semantics. It is NOT a validation of built area. Per-apartment scenarios are in the CSV and scenario JSON.', '']
    lines += ['## Filtered special-property listings by apartment', '']
    for aid, r in new.items():
        if r['property_type'] not in {'garden', 'duplex', 'triplex'}:
            continue
        context = r['competitive_context']
        lines.append(f"### Apartment {aid} — {r['status']}, {r['property_type']}")
        lines.append(f"Eligible candidate ads {context.get('count')}, price anomaly flags {context.get('review_count')}, usable {context.get('usable_count')}, raw asking range {context.get('raw_asking_range')}. Strategies use {r['marketing_strategies'].get('balanced',{}).get('basis')}; not an estimated sale value.")
        for listing in context.get('listings', []):
            lines.append(f"- `{listing['id']}` ask {fmt(listing['ask'])}, built {listing['built_area']}m², rooms {listing['rooms']}, {listing['distance_m']:.0f}m ({listing['geo_basis']}), listing date {listing['listing_date'] or 'unknown'}, capture {listing['capture_dates']}, review flag {listing.get('review_flag')}, source {[x['capture_id'] for x in listing['refs']]}; URL {listing['url']}.")
        for warning in context.get('warnings', []):
            lines.append('- Warning: '+warning)
        if not context.get('raw_asking_range'):
            lines.append('- Range and strategic ask withheld: insufficient non-flagged matched listings.')
        elif context.get('quality_gates',{}).get('applicable') and context['quality_gates']['failures']:
            lines.append('- Asking-only scenarios blocked by quality gates: '+', '.join(context['quality_gates']['failures']))
    lines += ['', '## Three configurable strategies', '', 'Default illustrative offsets are quick −3%, balanced 0%, high +5%, applied to the evidence-based base value for ordinary targets, or to a sufficiently supported **asking-only median** for otherwise unpriced special targets after quality gates. These are configurable business scenarios, not calibrated company policy; they do not predict sale time or probability. Strategies are rounded, compared with a selected asking set where available, and always NOT_APPROVED. Unit adjustments are never silently applied; the optional `unit_scenario` basis requires explicit double-count acknowledgement and remains uncalibrated. Existing benchmark/asking-conflict and no-context guards still withhold ordinary strategies.', '', f"Status counts: {dict(Counter(r['status'] for r in current['results']))}; tiers: {dict(Counter(r['tier'] for r in current['results']))}. Base-value changes: {sum(a['old_base'] != a['new_base'] for a in records)}; selection changes: {sum(bool(a['removed_comp_ids'] or a['added_comp_ids']) for a in records)}.", '', '## Remaining limitations', '', '- Garden area is inferred from a column named balcony; the rule may not apply to other workbook formats. `garden_column_overrides` permits per-apartment garden/balcony/unknown correction; original cell values remain in provenance.', '- Garden apartment balcony remains UNKNOWN, not zero. Standard apartment garden is NOT_APPLICABLE by declared property type, not a legal finding.', '- Sale-area definitions, floor reference completeness, listing location precision and active status remain unverified. PPM is not calibrated for outdoor adjustment.', '- Full-ownership, quarantine, CBS and three-tier rules remain unchanged. OVER commercial reuse rights unresolved. All marketing scenarios need human approval.', '', '## Reproduce', '', '```bash', 'cd poc', 'uv run gabay-pricing phase3 --historical-defaults --snapshot data/snapshots/market-live.json --before reports/before-phase3/pricing_results.json', 'uv run pytest', '```', '', 'The local `phase3_scenarios.json` contains all inputs, assumptions, evidence references, selected candidates and full illustrative-floor results; `phase3_before_after.csv` contains one row per apartment.']
    (directory/'PHASE3_COMPARISON.md').write_text('\n'.join(lines)+'\n')
    (directory/'phase3_scenarios.json').write_text(json.dumps({'snapshot_sha256': current['snapshot_sha256'], 'workbook_sha256': current['workbook_sha256'], 'config': current['config'], 'floor_example_config': floor['config'], 'floor_example_results': floor['results'], 'strict_area_config': strict_area['config'], 'strict_area_results': strict_area['results']}, ensure_ascii=False, indent=2))
    return current
