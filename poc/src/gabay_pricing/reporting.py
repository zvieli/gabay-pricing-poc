"""Local-only deterministic JSON/CSV/Markdown/HTML. Never redistribute raw snapshots."""
from collections import Counter
import csv
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import os
import shutil
from pathlib import Path
from statistics import median

from gabay_pricing.ai_analysis import AI_CONTRACT_VERSION
from gabay_pricing.marketing_report import write_marketing_reports
from gabay_pricing.csv_safety import SafeWriter


def money(value):
    return "—" if value is None else f"₪{value:,.0f}"


def experiment_summary(bundle, directory):
    rows = []
    for label, folder in [('Median vs weighted median', 'median'), ('Strict verified-built-area gate', 'strict-area'), ('Tier 2 sensitivity: recent window 1 month', 'one-month-recent')]:
        path = directory/'experiments'/folder/'pricing_results.json'
        if not path.exists():
            continue
        other = json.loads(path.read_text())
        # Never compare artifacts from a different evidence snapshot or workbook.
        if other['snapshot_sha256'] != bundle['snapshot_sha256'] or other['workbook_sha256'] != bundle['workbook_sha256'] or other.get('code_sha256') != bundle.get('code_sha256'):
            continue
        originals = {r['apartment_id']: r for r in bundle['results']}
        pairs = [(r, originals[r['apartment_id']]) for r in other['results'] if r['estimated_market_value'] is not None and originals[r['apartment_id']]['estimated_market_value']]
        changes = [abs(r['estimated_market_value']/b['estimated_market_value']-1) for r, b in pairs]
        factors = [e['time_factor'] for r in other['results'] for e in r['evidence']]
        rows.append(f"- **{label}:** statuses {dict(Counter(r['status'] for r in other['results']))}; tiers {dict(Counter(r['tier'] for r in other['results']))}; median absolute change {median(changes):.1%}, maximum {max(changes):.1%} across {len(changes)} paired numeric scenarios." + (f" Applied factor range {min(factors):.5f}–{max(factors):.5f}." if factors and folder == 'one-month-recent' else ''))
    if not rows:
        return []
    return ['\n## Same-snapshot methodology experiments\n', 'These are sensitivity experiments, not model validation, a calibrated backtest or competing ground truth. One-month recent is an intentionally artificial scenario to exercise the historical CBS path; it is NOT the default decision policy.\n', *rows, 'The strict-area scenario can still produce review-only Bait benchmarks because those medians themselves use unverified area semantics; it is not a verified-built-area valuation.\n']


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def carry_over_previous_ai(bundle, directory: Path, source_path=None, checkpoint_path=None):
    """A re-run without --ai must not destroy a previous AI report for the identical inputs."""
    existing = Path(source_path) if source_path else Path(directory) / 'pricing_results.json'
    if not existing.exists():
        return bundle
    try:
        prior = json.loads(existing.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return bundle
    keys = ('snapshot_sha256', 'workbook_sha256', 'config', 'results', 'evidence_packs')
    prior_ai = prior.get('ai_analysis') or {}
    if checkpoint_path:
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding='utf-8'))
            checkpoint_ai = checkpoint.get('ai_analysis') or {}
            if (any(checkpoint.get(k) != prior.get(k) for k in keys)
                    or any(checkpoint_ai.get(k) != prior_ai.get(k) for k in ('model', 'prompt_version', 'contract_version'))
                    or any(checkpoint_ai.get('units', {}).get(aid) != unit for aid, unit in prior_ai.get('units', {}).items())):
                return bundle
        except (OSError, ValueError, TypeError, AttributeError):
            return bundle
    if prior_ai.get('units') and prior_ai.get('contract_version', 1) == AI_CONTRACT_VERSION \
            and prior_ai.get('prompt_version') == 'business-analyst-v1' \
            and prior.get('results') and prior.get('evidence_packs') \
            and set(prior_ai['units']) <= {str(r['apartment_id']) for r in prior['results']} \
            and set(prior_ai['units']) <= set(prior['evidence_packs']) \
            and all(prior.get(k) == bundle.get(k) for k in keys):
        units = {aid: {**unit, 'analyst_reused': unit.get('status') == 'VALIDATED',
                       'critic_reused': (unit.get('claim_review') or {}).get('status') == 'SUCCESS'}
                 for aid, unit in prior_ai['units'].items()}
        bundle['ai_analysis'] = {**prior_ai, 'units': units, 'calls': 0,
            'carried_over_from_previous_report': True,
            'provenance': {'source_report': str(existing.resolve()),
                           'source_report_calls': prior_ai.get('calls'),
                           'source_checkpoint': str(Path(checkpoint_path).resolve()) if checkpoint_path else (prior_ai.get('provenance') or {}).get('source_checkpoint'),
                           'source_primary': (prior.get('run_manifest') or {}).get('ai', {}).get('primary_provider'),
                           'source_primary_model': (prior.get('run_manifest') or {}).get('ai', {}).get('primary_model'),
                           'historical_total_calls': (prior_ai.get('provenance') or {}).get('historical_total_calls'),
                           'source_provenance': prior_ai.get('provenance')}}
    return bundle


def write_reports(bundle, directory: Path, research=None):
    directory.mkdir(parents=True, exist_ok=True)
    apartments = bundle.get('apartments') or []
    results = bundle.get('results') or []
    if apartments and len(results) != len(apartments) and bundle.get('selection_scope') != 'explicit IDs':
        raise ValueError(f'incomplete result set: {len(results)} of {len(apartments)} apartments; report not published')
    existing = directory / 'pricing_results.json'
    if existing.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(existing, directory / f'pricing_results.backup-{stamp}.json')
    _atomic_write(existing, json.dumps(bundle, ensure_ascii=False, indent=2))
    manifest = bundle.get('run_manifest')
    if manifest is None:
        config = bundle['config']
        config_bytes = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        snapshot_path = Path(bundle['snapshot_path'])
        manifest = {'policy_id': 'PROGRAMMATIC_CONFIG', 'config_path': None, 'policy_selection': 'programmatic',
                    'effective_config': config, 'config_sha256': hashlib.sha256(config_bytes).hexdigest(),
                    'workbook_sha256': bundle['workbook_sha256'],
                    'snapshot_file_sha256': hashlib.sha256(snapshot_path.read_bytes()).hexdigest() if snapshot_path.is_file() else None,
                    'research_file_sha256': research['source_hashes'] if research else {},
                    'git_revision': None, 'code_sha256': bundle.get('code_sha256'), 'as_of': config['as_of']}
    (directory/'RUN_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if research is not None:
        (directory/'RESEARCH_EVIDENCE.json').write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding='utf-8')
    ai = bundle.get('ai_analysis') or {'status': 'NOT_RUN', 'model': None, 'units': {}}
    provenance = ai.get('provenance') or {}
    primary_info = (manifest or {}).get('ai', {})
    primary = primary_info.get('primary_provider') or provenance.get('source_primary') or 'לא צוין'
    primary_model = primary_info.get('primary_model') or provenance.get('source_primary_model') or ('gemini-3.1-flash-lite' if primary == 'gemini' else ai.get('model')) or 'לא הופעל'
    ai_lines = ['# ניתוח AI — נפרד מהתמחור', '', f"סטטוס: {ai['status']}; ספק ראשי מוגדר: {primary}; מודל ראשי מוגדר: {primary_model}. המחירים והאישורים נקבעים במנוע הדטרמיניסטי בלבד.", '']
    if provenance:
        ai_lines.append(f"קריאות בהרצה זו: {ai.get('calls', 0)}; סך קריאות היסטוריות: {provenance.get('historical_total_calls') if provenance.get('historical_total_calls') is not None else 'לא ידוע'}; מקור AI: {provenance.get('source_report') or 'לא ידוע'}; מחסום מקור: {provenance.get('source_checkpoint') or 'לא תועד'}.")
    for aid, item in ai.get('units', {}).items():
        ai_lines.append(f"## דירה {aid} — {item['status']}")
        review = item.get('claim_review') or {}
        ai_lines.append(f"- Analyst בפועל: {item.get('provider') or 'לא זמין'} / {item.get('model') or 'לא זמין'} (שימוש חוזר: {item.get('analyst_reused', False)}); Critic: {review.get('status', 'NOT_RUN')} — {review.get('provider') or 'לא זמין'} / {review.get('model') or 'לא זמין'} (שימוש חוזר: {item.get('critic_reused', False)})")
        for stage, history in [('Analyst', item.get('retry_history')), ('Critic', review.get('retry_history'))]:
            if history:
                ai_lines.append(f"- {stage} היסטוריית ספקים: " + ' → '.join(f"{h.get('provider', '?')}/{h.get('model', '?')}: {h.get('category', '?')}" for h in history))
        if review.get('status') == 'SUCCESS':
            ai_lines.append('- הכרעות Critic: ' + json.dumps([{ 'claim_id': c['claim_id'], 'verdict': c['verdict']} for c in review['review']['claims']], ensure_ascii=False))
        if item.get('analysis'):
            ai_lines.append('פרשנות AI לא מאומתת; מזהי ראיות אינם הוכחת נכונות הטקסט. אין כאן אישור מחיר.')
            if review.get('status') == 'SUCCESS' and all(c['verdict'] == 'SUPPORTED' for c in review['review']['claims']):
                ai_lines.append('```json\n' + json.dumps(item['analysis'], ensure_ascii=False, indent=2) + '\n```')
            else:
                ai_lines.append('הפרשנות הוסתרה: בדיקת הטענות נכשלה או לא תמכה בכל הטענות.')
        ai_lines.append(f"- זמן: {item.get('latency_ms')} ms; שימוש בטוקנים: {item.get('usage', {})}")
    (directory/'AI_ANALYSIS_HE.md').write_text('\n'.join(ai_lines)+'\n', encoding='utf-8')
    fields = ["apartment_id", "property_type", "rooms", "built_area", "status", "tier", "estimated_market_value", "value_low", "value_high", "evidence_strength", "evidence_quality", "feature_completeness", "methodology_strength", "marketing_approval", "eligible_recent", "eligible_historical", "effective_count", "independent_parcels", "median_age_days", "median_age_basis", "benchmark_id", "valuation_reference"]
    with (directory/"pricing_results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields+["unit_scenario_value", "outdoor_delta", "floor_delta", "orientation_delta", "floor_reference", "floor_reference_basis", "equivalent_area", "known_component_equivalent_area", "special_asking_range", "asking_dispersion_ratio", "asking_gate_failures", "quick_ask", "balanced_ask", "high_ask", "quick_indicative", "balanced_indicative", "high_indicative", "recommended_opening_price", "decision_status", "human_approval", "marketing_blocked_reason", "comparable_refs", "listing_refs", "warnings", "data_classification"])
        writer = SafeWriter(writer)
        writer.writeheader()
        for row in bundle["results"]:
            writer.writerow({**{k: row[k] for k in fields}, "data_classification": bundle.get('data_classification', 'REAL_INPUT_UNVERIFIED'), "unit_scenario_value": row['unit_adjustment_scenario'].get('value'), "outdoor_delta": row['unit_adjustment_scenario'].get('outdoor_delta'), "floor_delta": row['unit_adjustment_scenario'].get('floor_delta'), "orientation_delta": row['unit_adjustment_scenario'].get('orientation_delta'), "floor_reference": row['unit_adjustment_scenario'].get('floor_reference'), "floor_reference_basis": row['unit_adjustment_scenario'].get('floor_reference_basis'), "equivalent_area": row['unit_adjustment_scenario'].get('equivalent_area'), "known_component_equivalent_area": row['unit_adjustment_scenario'].get('known_component_equivalent_area'), "special_asking_range": row['competitive_context'].get('raw_asking_range'), "asking_dispersion_ratio": row['competitive_context'].get('price_dispersion_ratio'), "asking_gate_failures": ';'.join(row['competitive_context'].get('quality_gates',{}).get('failures',[])) if row['competitive_context'].get('quality_gates',{}).get('applicable') else '', "marketing_blocked_reason": row['marketing_strategies'].get('balanced',{}).get('blocked_reason'), "quick_ask": row['marketing_strategies'].get('quick_sale',{}).get('suggested_asking_price'), "balanced_ask": row['marketing_strategies'].get('balanced',{}).get('suggested_asking_price'), "high_ask": row['marketing_strategies'].get('high_opening',{}).get('suggested_asking_price'), "quick_indicative": row['marketing_strategies'].get('quick_sale',{}).get('indicative_asking_price'), "balanced_indicative": row['marketing_strategies'].get('balanced',{}).get('indicative_asking_price'), "high_indicative": row['marketing_strategies'].get('high_opening',{}).get('indicative_asking_price'), "recommended_opening_price": row.get('pricing_decision',{}).get('recommended_opening_price'), "decision_status": row.get('pricing_decision',{}).get('status'), "human_approval": row.get('pricing_decision',{}).get('approval'), "comparable_refs": ";".join(e["observation_id"] for e in row["evidence"]), "listing_refs": ";".join(row["listing_refs"]), "warnings": "; ".join(row["warnings"])})
    results = bundle["results"]
    numeric = [r["estimated_market_value"] for r in results if r["estimated_market_value"] is not None]
    summary = ["# Pricing POC — actual executed results\n", "**Research-only marketing decision support; not a certified appraisal. Raw/derived property evidence is local-only.**\n", f"Address: {bundle['address']}; as of {bundle['config']['as_of']}. Snapshot SHA256: `{bundle['snapshot_sha256']}`. Workbook SHA256: `{bundle['workbook_sha256']}`. Code SHA256: `{bundle['code_sha256']}`.\n", f"Results: {len(results)}. Statuses: {dict(Counter(r['status'] for r in results))}. Tiers: {dict(Counter(r['tier'] for r in results))}.", f"Numeric scenarios: {len(numeric)}; observed output range {money(min(numeric)) if numeric else 'none'}–{money(max(numeric)) if numeric else 'none'}. REVIEW_REQUIRED values are not supported launch/appraisal decisions.\n", "## Method comparison\n", "Median and weighted-median outputs are reported side by side per transaction estimate. No ground-truth holdout valuation is available; a plausible number or agreement is NOT accuracy validation. No tuning to produce attractive prices.\n", "## All apartments\n", "| Apartment | Type | Rooms | Built m² | Status | Tier | Market estimate | Estimate range | Balanced asking scenario (not recommendation) | Eligible recent/old |", "|---|---|---:|---:|---|---|---:|---|---:|---|"]
    for r in results:
        summary.append(f"| {r['apartment_id']} | {r['property_type']} | {r['rooms']} | {r['built_area']} | {r['status']} | {r['tier']} | {money(r['estimated_market_value'])} | {money(r['value_low'])}–{money(r['value_high'])} | {money(r['suggested_asking_price'])} | {r['eligible_recent']}/{r['eligible_historical']} |")
    summary += ['\n## Decision-gated recommendations and separate human approval\n', '| Apartment | Recommended opening price | Decision status | Human approval |', '|---|---:|---|---|']
    for r in results:
        d = r.get('pricing_decision') or {}
        summary.append(f"| {r['apartment_id']} | {money(d.get('recommended_opening_price'))} | {d.get('status')} | {d.get('approval')} |")
    summary += ["\n## Representative apartments and traceable evidence\n"]
    for r in results:
        aid = r['apartment_id']
        summary += [f"\n### Apartment {aid}: {r['property_type']}\n", f"{r['status']}, {r['tier']}; value {money(r['estimated_market_value'])}, balanced asking scenario {money(r['suggested_asking_price'])} (not recommendation). Strength: {r['evidence_strength']}; effective count {r['effective_count']}; age {r['median_age_days']} days ({r['median_age_basis']}).\n", *["- "+x for x in r["explanation"]], "\n| Sale ID | Date | Original price | Area basis | Raw PPM | Adjusted PPM | Factor | Weight | Geography | Capture IDs |", "|---|---|---:|---:|---:|---:|---:|---:|---|---|"]
        for e in r["evidence"]:
            summary.append(f"| {e['observation_id']} | {e['sale_date']} | {money(e['original_price'])} | {e['area_basis']} | {e['raw_ppm']:.0f} | {e['adjusted_ppm']:.0f} | {e['time_factor']:.5f} | {e['score']:.4f} | {e['geo_basis']} / {e['distance_m']}m | {', '.join(x['capture_id'] for x in e['refs'])} |")
        if r["benchmark_id"]:
            summary.append(f"\nBenchmark: `{r['benchmark_id']}`; capture IDs: {', '.join(x['capture_id'] for x in r['benchmark_refs'])}.")
        summary.extend([f"\nClassification: evidence={r['evidence_quality']}; features={r['feature_completeness']}; method={r['methodology_strength']}; approval={r['marketing_approval']}.", *['- '+x for x in r['classification_reasons']], f"\nQuarantine sensitivity: {r['quarantine_sensitivity']}", f"\nDifferentiation scenario: {r['differentiation_scenario']}", f"\nSpecial-property proxy (scenario only): {r['special_property_scenario']}", "\nWarnings:", *["- "+x for x in r["warnings"]], "\nMarketing: "+r["market_positioning"], *["- "+x for x in r["strategy_assumptions"]]])
    missing = {key: sum(a.get(key) is None or a.get(key) == [] for a in bundle['apartments']) for key in ['rooms', 'built_area', 'balcony_area', 'garden_area', 'floors', 'directions']}
    summary += ["\n## Missing input attributes\n", str(missing), "Garden area is inferred from the balcony-labelled column for garden apartments, with per-apartment override; standard-apartment garden is NOT_APPLICABLE by type, not physical proof. Unknown areas are not zero. Workbook summaries were verified, not double-counted.\n"]
    summary += ['\n## Unit and marketing layers: all apartments\n', '| ID | Equivalent area / known partial | Outdoor / floor / orientation delta | Combined hypothetical value | Floor reference | Special ask range / dispersion | Quick / balanced / high asking scenarios (not recommendations) | Blocked reason / approval |', '|---|---|---|---:|---|---|---|---|']
    for r in results:
        u, c, m = r['unit_adjustment_scenario'], r['competitive_context'], r['marketing_strategies']
        asks = [money(m.get(x, {}).get('suggested_asking_price')) if m.get(x, {}).get('suggested_asking_price') is not None else money(m.get(x, {}).get('indicative_asking_price')) + ' (indicative)' for x in ('quick_sale', 'balanced', 'high_opening')]
        deltas = ' / '.join(money(u.get(x)) for x in ('outdoor_delta','floor_delta','orientation_delta'))
        blocked = m.get('balanced',{}).get('blocked_reason') or 'none'
        summary.append(f"| {r['apartment_id']} | {u.get('equivalent_area')} / {u.get('known_component_equivalent_area')} | {deltas} | {money(u.get('value'))} | {u.get('floor_reference')} ({u.get('floor_reference_basis')}) | {c.get('raw_asking_range')} / {c.get('price_dispersion_ratio')} | {' / '.join(asks)} | {blocked} / {r['marketing_approval']} |")
    summary += ['\nUnit outputs are assumption-only; unknown components are not assumed zero in a complete equivalent area. Target-only values can double count existing outdoor value. Asking-only strategies for special properties are not sale valuations; see full JSON for individual listing references and exclusions.\n']
    summary += experiment_summary(bundle, directory)
    for r in results:
        if r['estimated_market_value'] is not None and r['value_low'] and r['value_high']/r['value_low'] > 2:
            summary.append(f"- Apartment {r['apartment_id']}: unusually wide scenario range ({r['value_high']/r['value_low']:.2f}×); inspect anomalous transfers, do not treat as precision.")
        if any('outlier exceeds' in w for w in r['warnings']):
            summary.append(f"- Apartment {r['apartment_id']}: selected PPM outlier flagged; retained transparently, not silently discarded.")
    summary += ["\n## Execution inspection\n", *["- "+x for x in bundle["quality_checks"]], "\nMachine-readable JSON includes every apartment, selected/excluded observations, source capture registry, workbook row references, all experimental configuration, benchmark/index IDs and source field paths. Snapshot payloads are in the ignored local snapshot file, not committed. No raw evidence is redistributed.\n"]
    (directory/"PRICING_ANALYSIS.md").write_text("\n".join(summary))
    market = bundle["market"]
    if market is None:
        market = {"sales": [], "listings": [], "projects": [], "benchmarks": [], "indices": [], "duplicates": []}
    coverage = ["# Source coverage\n", f"Snapshot file: `{bundle['snapshot_path']}`; capture modes: {dict(Counter(c['mode'] for c in bundle['captures']))}. AUDIT_REPLAY is not a new fetch; PROVIDER_CACHE retains provider-fetched time separately.\n", "## Normalized observations (before matching)\n", f"Sales: {len(market['sales'])}; eligible after strict record checks: {sum(not s['rejections'] and not s['duplicate_of'] for s in market['sales'])}.", f"Listings: {len(market['listings'])}; geo/price eligible: {sum(not s['rejections'] and not s['duplicate_of'] for s in market['listings'])}.", f"Project offerings: {len(market['projects'])}; priced and geographically eligible: {sum(not p['rejections'] for p in market['projects'])}.", f"Benchmarks: {len(market['benchmarks'])}; index observations: {len(market['indices'])}.\n", "Sale rejection reasons: "+str(dict(Counter(x for s in market["sales"] for x in s["rejections"]))), "Listing rejection reasons: "+str(dict(Counter(x for s in market["listings"] for x in s["rejections"]))), f"Duplicate associations: {len(market['duplicates'])}; retained in JSON with reasons. Sources sharing Tax Authority records/derived Bait statistics are NOT independent corroboration.\n", "## Requests and actual reported credits\n", "| Provider | Attempts | Reported credits |", "|---|---:|---:|"]
    for p, count in sorted(Counter(x["provider"] for x in bundle["requests"]).items()):
        coverage.append(f"| {p} | {count} | {sum((x.get('credits') or 0) for x in bundle['requests'] if x['provider']==p)} |")
    coverage += ["\nFailures/budget stops (no raw error messages or credentials):", "```json", json.dumps(bundle["failures"], indent=2, ensure_ascii=False), "```", "\n## Data reference versus collection time\n", "| Provider | Mode | Captured | Source fetched | Original sample / endpoint |", "|---|---|---|---|---|"]
    for c in bundle["captures"]:
        coverage.append(f"| {c['provider']} | {c['mode']} | {c['captured_at']} | {c['fetched_at']} | {c.get('original_path') or c['endpoint']} |")
    coverage += ["\n## Actual normalized reference dates\n"]
    for provider in ['over', 'over_govmap', 'apish']:
        dated = [s['date'] for s in market['sales'] if s['id'].startswith(provider+':') and s['date']]
        if dated:
            coverage.append(f"- {provider}: {min(dated)} to {max(dated)} in collected/replayed sample, not full database coverage.")
    if market['indices']:
        coverage.append('- CBS latest monthly label: '+max(i['period'] for i in market['indices'])+'; release date unknown; latest three provisional per methodology.')
    for c in bundle['captures']:
        # Bounded pagination is deliberate; an incomplete collection is not a market census.
        capture = next((x for x in bundle.get('pagination', []) if x['capture_id']==c['id']), None)
        if capture:
            coverage.append('- Bounded/incomplete page: '+str(capture))
    (directory/"SOURCE_COVERAGE.md").write_text("\n".join(coverage))
    limitations = """# Assumptions and limitations

- Research-only OVER access: commercial reuse/redistribution rights remain unresolved. No private endpoints, purchases, scraping actors, authentication bypass or paid calls in tests.
- Workbook defines hypothetical target properties, not an inventory to verify at the anchor. All floor rows are summed, declared summary rows are checked and excluded, repeated room counts are not summed.
- The workbook's apartment-area column is a declared built-area proxy, not legal measurement. Garden rows have values under a balcony header; by user rule they are inferred garden area, with provenance and configurable correction. Actual balcony area stays unknown. No missing feature is silently set to zero.
- Main OVER sale area and Madlan area are reported areas with unverified built-only semantics. Allowing these against target built areas is an explicit experimental scenario and reduces support to REVIEW_REQUIRED. Disable allow_reported_sale_area for a stricter run.
- Full ownership must be explicitly 100%; fractional/unknown shares, combination/multiple-property transfers and invalid records are excluded. No partial sale is divided by share to fabricate a full-property sale.
- Similarity weights, thresholds, minimum counts/parcels and scenario ranges are experimental, not calibrated or probabilistic. Ranking weight = 1 / (1 + 3×relative area difference + room difference + geography penalty + age_months/24 + 0.03×capped floor difference + unknown-type penalty0.35). Missing outdoor fields never imply a match. Effective count groups weights by parcel; distinct parcels still need not be statistically independent. Property incompatibility is a hard gate, so small standard apartments cannot price a duplex/triplex merely by proximity. Special benchmark proxies are off by default.
- No floor, orientation, condition, elevator, parking, new-build or duplex/triplex premium is empirically estimated. Unit scenario equivalents default to 0.25 balcony and 0.20 garden as uncalibrated business assumptions. Floor reference comes from selected sale floors or an explicit user assumption, never silently floor1. Target-only outputs can double count; they never update base value or strategy. Optional floor/orientation/outdoor differentiation scenarios are separate from the base value and explicitly user-assumed, not calibrated. Outdoor marginal coefficients default to zero; this omits an unsupported premium, not a claim outdoor space has no value. Benchmark segment medians already mix outdoor characteristics.
- Recent-tier values use nominal comparable PPM. Historical-tier values use a same-series/same-base CBS ratio once, at the latest observed period, and may blend raw recent evidence at original dates. No extrapolation to today. Calendar transaction month → CBS overlapping-period month label is explicitly experimental. Missing publication dates remain unknown; capture dates are not substituted. Latest three index estimates are treated as provisional.
- Ranges are scenario/sensitivity ranges with a minimum width, not confidence intervals. An apparently precise median does not imply appraisal accuracy.
- Parcel-centroid and source-point distances are not certified building-to-building distances. Same-street/neighborhood matches without coordinates are lower-resolution evidence, explicitly labelled. Query matched-address metadata is never used to certify a returned sale's location.
- Same-subparcel/date/price/area matches are deduplicated across sources. Distinct subparcels remain distinct even at identical price. Ambiguous cross-source repeats without subparcel are retained but excluded from independent evidence. Ads merge only on source ID or a strong complete property signature.
- Tax Authority-derived transactions, OVER's GovMap mirror and Bait statistics share upstream dependencies. Counts are not independent confirmations. Source revisions, address crosswalk collisions and selection bias remain possible.
- Quarantine flags are relative review signals, not proof of invalidity. Default retains flagged observations; without-flagged sensitivity is shown independently and is not an approved correction.
- Asking-only recommendations require at least3 credible same-type listings, within1.5km at source-point precision, within20% built area, with source publication/update date within90days, and pre-review max/min asking ratio ≤2. These are configurable uncalibrated safety gates, not validated market rules. Capture time is not publication freshness; severe dispersion or uncertain freshness blocks recommendations while preserving the exploratory range. Per-apartment override requires reviewer, reason and explicit acceptance of every failed gate; it remains a scenario, not approval.
- The benchmark/asking conflict gate (default35%, configurable) withholds a preliminary marketing recommendation rather than moving the estimated value to fit asks; this is an uncalibrated research safety rule, not company policy.
- Asking listings and starting project prices are NOT completed sales. Strategy is a neutral value-aligned default plus optional explicitly uncalibrated adjustment; never automatically cheapest. No ask is recommended without comparable asking/quoted-unit context. Observed recent ads do not guarantee current availability.
- Unpriced atypical or sparse targets are intentional, not filled with invented values. No ML model, LLM explanations or validation claims from tiny data.

## Exact configuration

"""
    (directory/"ASSUMPTIONS_AND_LIMITATIONS.md").write_text(limitations+"```json\n"+json.dumps(bundle["config"], indent=2)+"\n```\n")
    html_rows = []
    for r in results:
        details = escape(json.dumps({"explanation": r["explanation"], "evidence": r["evidence"], "benchmark": r["benchmark_id"], "warnings": r["warnings"]}, ensure_ascii=False, indent=2))
        html_rows.append("<tr>"+"".join(f"<td>{escape(str(x))}</td>" for x in [r['apartment_id'],r['property_type'],r['status'],r['tier'],money(r['estimated_market_value']),money(r['value_low'])+' – '+money(r['value_high']),money(r['suggested_asking_price']),money((r.get('pricing_decision') or {}).get('recommended_opening_price')),(r.get('pricing_decision') or {}).get('approval')])+f"<td><details><summary>Evidence</summary><pre>{details}</pre></details></td></tr>")
    (directory/"pricing_report.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Gabay research POC</title><style>body{font:16px system-ui;margin:2rem;line-height:1.5}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.5rem;text-align:left}pre{white-space:pre-wrap;max-width:65rem}th{background:#eee}</style><h1>Gabay pricing research POC</h1><p>Not an appraisal. REVIEW_REQUIRED values are preliminary scenarios; UNPRICED means insufficient evidence. All asking figures are unapproved scenarios, not decision-gated recommendations. Decision-gated recommendations and human approval are separate in JSON and CSV. No commercial redistribution permission established.</p><p>'+escape(bundle['address'])+'</p><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in ['Apartment','Type','Status','Tier','Market estimate','Estimate range','Balanced asking scenario (not recommendation)','Decision-gated opening recommendation','Human approval','Trace'])+'</tr></thead><tbody>'+''.join(html_rows)+'</tbody></table></html>')
    write_marketing_reports(bundle, directory, research)
