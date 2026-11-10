"""Reproducible quality-gate review of the existing snapshot, never a data fetch."""
from collections import Counter
import json
from statistics import median

from gabay_pricing.cli import price
from gabay_pricing.models import PricingConfig


def money(x):
    return '—' if x is None else f'₪{x:,.0f}'


def create_report(snapshot, workbook, snapshot_path, directory, before_path=None):
    config = PricingConfig(as_of=snapshot.as_of)
    current = price(snapshot, workbook, config, snapshot_path)
    strict = price(snapshot, workbook, config.model_copy(update={'allow_reported_sale_area': False}), snapshot_path)
    floor = price(snapshot, workbook, config.model_copy(update={'unit_floor_rate': .01}), snapshot_path)
    if before_path:
        before = json.loads(before_path.read_text())
        if any(before[k] != current[k] for k in ['snapshot_sha256','workbook_sha256']):
            raise ValueError('Previous report snapshot or workbook differs')
    else:
        before = None
    rows = {r['apartment_id']: r for r in current['results']}
    strict_rows = {r['apartment_id']: r for r in strict['results']}
    floor_rows = {r['apartment_id']: r for r in floor['results']}
    lines = ['# Pricing decision quality gates', '', '**Research-only decision support; not an appraisal, sale-time prediction, or approved marketing price. No new paid calls.**', '', f"Snapshot `{current['snapshot_sha256']}`; workbook `{current['workbook_sha256']}`; code `{current['code_sha256']}`; as-of {snapshot.as_of}. Archived capture dates are not evidence of a listing's current availability.", '', '## All 39 results', '', f"Status: {dict(Counter(r['status'] for r in current['results']))}; tiers: {dict(Counter(r['tier'] for r in current['results']))}. Asking-only recommendations blocked by quality gates: {sum(r['status']=='UNPRICED' and r['competitive_context'].get('quality_gates',{}).get('failures') != [] for r in current['results'])}. Status UNPRICED remains independent of asking-only context."]
    if before:
        previous = {r['apartment_id']:r for r in before['results']}
        if previous.keys() != rows.keys():
            raise ValueError('Previous apartment set differs')
        changes = sum(previous[k]['estimated_market_value'] != rows[k]['estimated_market_value'] for k in rows)
        asks = [k for k in rows if previous[k].get('marketing_strategies',{}).get('balanced',{}).get('suggested_asking_price') is not None and rows[k].get('marketing_strategies',{}).get('balanced',{}).get('suggested_asking_price') is None]
        lines += ['', f'Compared with saved Phase 3 result: {changes} base-value changes; previously available balanced scenarios now blocked: {asks}. The new gates change recommendation eligibility, not the evidence-based valuation.']
    lines += ['', '## Representative decision details', '', 'Outdoor, floor and orientation deltas are independent scenario components; their sum is not automatically added to base. Aeq uses 0.25 balcony / 0.20 garden, uncalibrated. Inferred garden area and unknown balcony area are explicitly distinguished. No uncalibrated unit adjustment is silently used in asking strategies.', '']
    for aid in rows:
        r = rows[aid]; u = r['unit_adjustment_scenario']; ctx = r['competitive_context']; gates = ctx.get('quality_gates',{}); f = floor_rows[aid]['unit_adjustment_scenario']; area = strict_rows[aid]
        lines += [f'### Apartment {aid} — {r["property_type"]}', '', f"- **Base market value:** {money(r['estimated_market_value'])}; {r['tier']} / {r['status']}; evidence {r['evidence_quality']}; human marketing approval {r['marketing_approval']}.", f"- **Area-basis sensitivity:** strict sale-area gate gives {money(area['estimated_market_value'])}, {area['tier']} / {area['status']}; Bait fallback itself may have unknown area semantics. Default selected sales use unknown built-area semantics where recorded; no empirical calibration is implied.", f"- **Outdoor:** complete Aeq {u.get('equivalent_area')}; known-component Aeq {u.get('known_component_equivalent_area')}; complete outdoor delta {money(u.get('outdoor_delta'))}; partial-known-component delta {money(u.get('conditional_known_component_only_delta'))}. A missing component is not silently zero; outdoor amounts can double count comparable or benchmark amenities.", f"- **Floor:** default delta {money(u.get('floor_delta'))}; explicit illustrative +1%/floor delta {money(f.get('floor_delta'))}, reference {f.get('floor_reference')} ({f.get('floor_reference_basis')}); known selected floors {f.get('floor_observed_count')}/{f.get('floor_selected_count')}. This is an assumption, not a measured floor effect.", f"- **Orientation:** default {money(u.get('orientation_delta'))}, {u.get('orientation_basis')}; no orientation premium unless explicitly configured.", f"- **Combined hypothetical unit value:** default {money(u.get('value'))}; illustrative +1% floor {money(f.get('value'))}. Both are target-only sensitivities, not the evidence-based value or approved ask.", f"- **Asking-only context:** exploratory raw range {ctx.get('raw_asking_range')}; pre-review range {ctx.get('observed_range_before_review')}; observations {ctx.get('count')}, credible {gates.get('credible_count')}, raw max/min dispersion {ctx.get('price_dispersion_ratio')}. This is asking evidence, not completed sales.", f"- **Asking-only quality gates:** {'APPLIED' if gates.get('applicable') else 'NOT APPLIED (sale-based or benchmark basis)'}; observed failures {gates.get('failures',[])}; minimum {gates.get('minimum_credible')}, radius {gates.get('max_radius_m')}m, max area difference {gates.get('max_area_difference')}, max raw-price ratio {gates.get('max_price_ratio')}, max listing age {gates.get('max_listing_age_days')} days; missing source publication/update dates fail freshness even with a recent capture."]
        for name, strategy in r['marketing_strategies'].items():
            lines.append(f"- **{name}:** {money(strategy['suggested_asking_price'])}; basis {strategy['basis']}, adjustment {strategy['adjustment_fraction']:+.1%}; position {strategy['comparison_fraction_below']} across {strategy['comparison_count']} observations; dispersion {strategy['price_dispersion_ratio']}; blocked: {strategy['blocked_reason'] or 'no automated gate block'}; approval {strategy['approval']}.")
        if gates.get('override'):
            lines.append(f"- Override audit: {gates['override']}; this is NOT approval.")
        lines.append('- Uncertainty: '+ '; '.join((r['warnings']+u.get('warnings',[]))[:8]))
        lines.append('')
    lines += ['## Area semantics across the run', '']
    pairs = [(r,strict_rows[r['apartment_id']]) for r in current['results'] if r['estimated_market_value'] and strict_rows[r['apartment_id']]['estimated_market_value']]
    changes = [abs(b['estimated_market_value']/a['estimated_market_value']-1) for a,b in pairs]
    lines.append(f"Strict-area experiment: tiers {dict(Counter(r['tier'] for r in strict['results']))}; {len(pairs)} paired estimates; median absolute change {median(changes):.1%}, maximum {max(changes):.1%}. Strict mode excludes sale records with unverified built-area semantics, but any remaining benchmark estimate also has unverified area semantics and remains REVIEW_REQUIRED." if changes else 'No comparable numeric strict-area results.')
    lines += ['', '## Override and limitations', '', 'An asking-only quality override is opt-in per apartment via `asking_overrides`, with a nonblank reviewer, written reason and an explicit list accepting every failed gate. The complete record is included in output and snapshot-independent configuration. Overrides cannot bypass the hard candidate type/price/area validity filters, the minimum two exploratory ads, or change UNPRICED; they create a marked scenario only, never approval. No override was used in the default report.', '', 'Gate thresholds are conservative configurable research safeguards, not empirically validated cutoffs. Reported sale areas, geographic precision, outlier handling, actual listing availability and OVER redistribution rights require further validation. Three marketing offsets remain illustrative and configurable; none predicts sale time.', '', '## Reproduce offline', '', '```bash', 'cd poc', 'uv run gabay-pricing decision --historical-defaults --snapshot data/snapshots/market-live.json --before reports/before-phase35/pricing_results.json', 'uv run pytest', '```', '', 'Detailed source references, all 39 results, gate decisions and per-listing failures are in local pricing_results.json; strict/floor runs and configurations are in decision_quality_scenarios.json. These files are intentionally not committed.']
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'DECISION_QUALITY.md').write_text('\n'.join(lines)+'\n')
    (directory/'decision_quality_scenarios.json').write_text(json.dumps({'snapshot_sha256': current['snapshot_sha256'], 'workbook_sha256': current['workbook_sha256'], 'code_sha256': current['code_sha256'], 'strict_config': strict['config'], 'strict_results': strict['results'], 'floor_config': floor['config'], 'floor_results': floor['results']},ensure_ascii=False,indent=2))
    return current
