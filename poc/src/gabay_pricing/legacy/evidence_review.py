"""Same-snapshot before/after and scenario review; no requests or source writes."""
from collections import Counter
import csv
import json
from pathlib import Path
from statistics import median

from gabay_pricing.cli import price
from gabay_pricing.models import PricingConfig
from gabay_pricing.normalization import normalize


def render(value):
    return "—" if value is None else f"₪{value:,.0f}"


def review(snapshot, workbook, snapshot_path, before_path, directory):
    before = json.loads(Path(before_path).read_text())
    config = PricingConfig(as_of=snapshot.as_of)
    current = price(snapshot, workbook, config, snapshot_path)
    if any(before[k] != current[k] for k in ["snapshot_sha256", "workbook_sha256"]):
        raise ValueError("Before and after snapshot/workbook hashes differ; comparison refused")
    old = {r["apartment_id"]: r for r in before["results"]}
    new = {r["apartment_id"]: r for r in current["results"]}
    if old.keys() != new.keys():
        raise ValueError("Before and after apartment IDs differ")
    exclude = price(snapshot, workbook, config.model_copy(update={"quarantine_policy": "exclude_flagged"}), snapshot_path)
    wide = price(snapshot, workbook, config.model_copy(update={"radius_m": 5000}), snapshot_path)
    illustrative = price(snapshot, workbook, config.model_copy(update={"scenario_floor_rate": .01, "scenario_floor_reference": 1}), snapshot_path)
    wide_market = normalize(snapshot, config.model_copy(update={"radius_m": 5000}))
    flags = {}
    for result in current["results"]:
        for flag in result["review_flags"]:
            flags.setdefault(flag["observation_id"], {"flag": flag, "apartments": []})["apartments"].append(result["apartment_id"])
    text = ["# Pricing evidence hardening review", "", "Research-only decision support; not an appraisal. No new provider calls. Review flags are unverified anomalies, not proof of invalidity. Baseline values retain them; the exclusion scenario is a sensitivity, not a corrected market price.", "", f"Snapshot: `{current['snapshot_sha256']}`; workbook: `{current['workbook_sha256']}`; old code: `{before['code_sha256']}`; new code: `{current['code_sha256']}`. Same snapshot and workbook validated before comparison.", "", "## Before/after: all apartments", "", "| ID | Type | Old value | New value | Delta | Old/new status | Old/new evidence quality | Comparable IDs removed / added | Reason |", "|---|---|---:|---:|---:|---|---|---|---|"]
    comparison = []
    for aid in sorted(new, key=int):
        a, b = old[aid], new[aid]
        old_ids = {e["observation_id"] for e in a["evidence"]}
        new_ids = {e["observation_id"] for e in b["evidence"]}
        removed, added = sorted(old_ids-new_ids), sorted(new_ids-old_ids)
        delta = None if a["estimated_market_value"] is None or b["estimated_market_value"] is None else b["estimated_market_value"]-a["estimated_market_value"]
        reason = "No change to base valuation or selected comparables; assessment/review labels added"
        if delta or removed or added or a["status"] != b["status"]:
            reason = "Selection/status or value changed; inspect IDs and flag policy"
        old_quality = a.get("evidence_quality", "NOT_SEPARATELY_RECORDED; legacy strength="+a["evidence_strength"])
        text.append(f"| {aid} | {b['property_type']} | {render(a['estimated_market_value'])} | {render(b['estimated_market_value'])} | {render(delta)} | {a['status']} → {b['status']} | {old_quality} → {b['evidence_quality']} | {', '.join(removed) or '—'} / {', '.join(added) or '—'} | {reason} |")
        comparison.append({"apartment_id": aid, "old_value": a["estimated_market_value"], "new_value": b["estimated_market_value"], "delta": delta, "old_status": a["status"], "new_status": b["status"], "old_evidence_quality": old_quality, "new_evidence_quality": b["evidence_quality"], "old_strength": a["evidence_strength"], "new_strength": b["evidence_strength"], "removed": ";".join(removed), "added": ";".join(added), "reason": reason})
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/"pricing_before_after.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(comparison[0])); writer.writeheader(); writer.writerows(comparison)
    text += ["", "## Suspicious selected sale audit", "", "Flag rule: adjusted price/m² below half or above twice the selected-set median, only with at least three comparables. Threshold is a review trigger, not a trained outlier model or evidence of fraud/invalidity. Three categories: PROVEN_INVALID = source-proven violation of full-sale requirements; UNVERIFIED_ANOMALY = review only; LEGITIMATE_UNUSUAL requires independent verification. No flagged selected sale is proven invalid or independently proven legitimate on these captures.", ""]
    sales = {s["id"]: s for s in current["market"]["sales"] if not s["duplicate_of"]}
    captures = {c.id: c for c in snapshot.captures}
    for ident, info in flags.items():
        s, f = sales[ident], info["flag"]
        text += [f"### {ident}", f"**{f['classification']}**; {s['date']}, {render(s['price'])}, {s['reported_area']}m², {s['rooms']} rooms, floor {s['floor']}, parcel {s['block']}/{s['parcel']}; share {s['share']}. Raw {f['raw_ppm']:,.0f} ILS/m² vs selected median {f['peer_median_ppm']:,.0f}. Used by apartments {', '.join(info['apartments'])}."]
        for source in f["refs"]:
            cap = captures[source["capture_id"]]
            text.append(f"- Captured {cap.captured_at}, {cap.mode}, `{cap.endpoint}`, record `{source['record_path']}`, capture `{cap.id}`; fields: `{source['field_paths']}`. Request matched Helsinki24, but actual record address is {s['address']}; source geography is neighborhood-level, no measured distance.")
        peers = [x for x in sales.values() if x["id"] != ident and x["block"] == s["block"] and x["parcel"] == s["parcel"] and x["price"] and x["reported_area"] and not x["rejections"]]
        text.append("- Other non-rejected saved sales on same parcel: " + ("; ".join(f"{p['date']} {render(p['price'])}/{p['reported_area']}m² ({p['id']})" for p in peers[:5]) or "none in saved snapshot") + ". Different units/dates do not prove the flagged transaction invalid.")
        text.append("- Source reports full share/final, but original official record, arm's-length terms, identity, unit rights, and area semantics are not independently verified. No automatic invalidity conclusion.")
    text += ["", "**PROVEN_INVALID for this POC's full-ownership apartment-comparable eligibility**, not a claim the transaction itself is legally invalid: the source explicitly reports the following disqualifying fields. These are separate from the unverified low-price flags:"]
    for reason in ['partial_ownership_transfer', 'combination_transaction']:
        example = next((s for s in current['market']['sales'] if reason in s['rejections'] and not s['duplicate_of']), None)
        if example:
            src = example['refs'][0]
            text.append(f"- {example['id']}: {reason}; date {example['date']}, share {example['share']}, nature {example['nature']}, area {example['reported_area']}; capture `{src['capture_id']}` record `{src['record_path']}`. Excluded by explicit source field, not by low price.")
    text += ["", "**LEGITIMATE_UNUSUAL:** none independently verified; do not infer legitimacy from a non-rejected flag. Strict record rejections and their source references remain in normalized JSON.", "", "## Quarantine sensitivity, selected-set holdout", "", "Quarantine does not remove records by default. The alternative excludes only flagged observations from the existing selected set, without backfilling, and only returns a value if the same comparable-count and parcel minimums still hold. This is a diagnostic range/estimate, not a reason to delete a real sale.", "", "| Apartment | Flagged count | With flagged | Without flagged | Without-flagged range | Usable minimums |", "|---|---:|---:|---:|---|---|"]
    for aid in sorted(new, key=int):
        r = new[aid]; q = r["quarantine_sensitivity"]
        if q.get("flagged_ids"):
            bounds = q["without_flagged_range"]
            text.append(f"| {aid} | {len(q['flagged_ids'])} | {render(q['baseline_value'])} | {render(q['without_flagged_value'])} | {render(bounds[0])+'–'+render(bounds[1]) if bounds else 'insufficient'} | {q['without_flagged_usable']} |")
    eligible_sales = [s for s in current['market']['sales'] if not s['rejections'] and not s['duplicate_of']]
    sale_floors = sum(s['floor'] is not None for s in eligible_sales)
    sale_outdoors = sum(s['balcony_area'] is not None or s['garden_area'] is not None for s in eligible_sales)
    text += [f"\nObserved attribute coverage: {sale_floors}/{len(eligible_sales)} eligible sales have a floor and {sale_outdoors}/{len(eligible_sales)} have outdoor-area data; transaction orientation is not in the normalized source contract. A shared median can persist despite different floor scores.\n"]
    text += ["", "## Four separate dimensions", "", f"Old statuses: {dict(Counter(r['status'] for r in old.values()))}; new statuses: {dict(Counter(r['status'] for r in new.values()))}. Evidence quality: {dict(Counter(r['evidence_quality'] for r in new.values()))}. Feature completeness: {dict(Counter(r['feature_completeness'] for r in new.values()))}. Methodology: {dict(Counter(r['methodology_strength'] for r in new.values()))}. Marketing approval: {dict(Counter(r['marketing_approval'] for r in new.values()))}.", "", "Every default Tier 1 result retains REVIEW_REQUIRED because selected sales carry explicit warnings about area semantics, subtype, geography, floor or other provenance, and the workbook's balcony has no empirically justified marginal coefficient. Ranking is experimental. Human approval is separately NOT_APPROVED; a suggested asking scenario is not approval. Missing garden area is recorded as unknown rather than asserted absent.", "", "## Floor, orientation and outdoor differentiation", "", "Floor affects the existing experimental matching score, not an empirical price premium. Orientation is present for some targets, but not reliably on captured sales; balcony/garden dimensions are also mostly missing or have incompatible semantics. There is no justified coefficient to estimate from this snapshot. Identical inputs/comparable sets can yield identical prices; differing floor scores can change selection or weights, not establish a causal floor premium.", "", "The default differentiation scenario is disabled. Optional floor rate, reference floor, orientation rate mapping and balcony/garden ILS/m² are explicit uncalibrated user assumptions; scenarios are separate fields and never change base market value, status, or suggested ask. Unknown inputs withhold the requested scenario. A nonzero outdoor scenario may double-count attributes embodied in source prices.", "", "Illustration only: +1% per floor above floor1 (no orientation/outdoor assumptions), not a recommendation or fitted coefficient:", "", "| Apartment | Floor(s) | Orientation | Balcony m² | Base | Illustrative scenario |", "|---|---|---|---:|---:|---:|"]
    apartments = {a["id"]: a for a in current["apartments"]}
    scenarios = {r["apartment_id"]: r for r in illustrative["results"]}
    for aid in new:
        a, r = apartments[aid], scenarios[aid]
        text.append(f"| {aid} | {a['floors']} | {', '.join(a['directions']) or 'unknown'} | {a['balcony_area']} | {render(r['estimated_market_value'])} | {render(r['differentiation_scenario'].get('value'))} |")
    text += ["", "## Special-property coverage and geographically broader test", "", "The broader-radius experiment reuses the identical snapshot, radius 5km rather than 1.5km. No API calls or new market records; records rejected by original radius are reconsidered only if their returned coordinates support the wider gate. It is a sensitivity, not proof of adequate coverage.", "", f"Base statuses {dict(Counter(r['status'] for r in current['results']))}; 5km statuses {dict(Counter(r['status'] for r in wide['results']))}."]
    for typ in ["garden", "duplex", "triplex"]:
        typed_sales = [s for s in wide_market.sales if s.property_type == typ and not s.duplicate_of and not s.rejections]
        typed_asks = [s for s in wide_market.listings if s.property_type == typ and not s.duplicate_of and not s.rejections]
        text.append(f"- {typ}: {len(typed_sales)} eligible typed sales, {len(typed_asks)} eligible typed asks within broader saved sample. Asking prices are not completed-sale evidence.")
    text += ["", "| Apartment | Type | Base status | 5km status | Standard-apartment proxy ONLY | Proxy source |", "|---|---|---|---|---:|---|"]
    wide_by_id = {r['apartment_id']: r for r in wide['results']}
    for aid in sorted(new, key=int):
        r = new[aid]
        if r['status'] != 'UNPRICED':
            continue
        proxy = r['special_property_scenario']
        text.append(f"| {aid} | {r['property_type']} | {r['status']} | {wide_by_id[aid]['status']} | {render(proxy.get('value'))} | {proxy.get('benchmark_id') or 'unavailable'} |")
    text += ["", "These proxies deliberately remain separate from `estimated_market_value` and never generate a marketing price. They are ordinary-flat benchmarks with no validated duplex/triplex/garden conversion and may be sparse or geographically broad; they cannot establish special-property value.", "", "## Reproduce", "", "```bash", "cd poc", "uv run gabay-pricing review --historical-defaults --snapshot data/snapshots/market-live.json --before reports/before-phase2/pricing_results.json", "uv run pytest", "```", "", "The review command also writes `pricing_review_scenarios.json` containing full configurations and results for exclusion, wider radius and illustrative floor scenarios. It never contacts providers. `pricing_before_after.csv` contains all result comparisons. These files and the snapshot are local-only and ignored by Git.", "", "## Unresolved decisions", "", "- Obtain official transaction verification (including identity, rights, arm's-length terms, share, reporting and true area) for all flagged records before proving invalidity or accepting legitimacy.", "- Select and independently validate floor/orientation/outdoor effects using genuinely comparable, properly measured properties; none is calibrated here.", "- Establish independent, typed duplex/triplex/garden sales and geographic/temporal coverage; asking prices are not sales.", "- Validate index timing, source sample selection, floor/range modeling and sensitivity thresholds against holdout outcomes.", "- Obtain OVER and upstream commercial-use/redistribution permissions. Human marketing approval remains required."]
    (directory/"PRICING_REVIEW.md").write_text("\n".join(text)+"\n")
    (directory/"pricing_review_scenarios.json").write_text(json.dumps({"snapshot_sha256": current["snapshot_sha256"], "workbook_sha256": current["workbook_sha256"], "before_code_sha256": before["code_sha256"], "after_code_sha256": current["code_sha256"], "baseline": {"config": current["config"], "results": current["results"]}, "quarantine_excluded": {"config": exclude["config"], "results": exclude["results"]}, "radius_5000": {"config": wide["config"], "results": wide["results"]}, "illustrative_floor": {"config": illustrative["config"], "results": illustrative["results"]}}, ensure_ascii=False, indent=2))
    return current
