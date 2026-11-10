"""Three transparent valuation tiers; scenario ranges are NOT confidence intervals."""
from datetime import date
import math
from statistics import median

from gabay_pricing.matching.engine import match, months_old
from gabay_pricing.models import PricingEvidence, PricingResult, ReviewFlag


def weighted_median(values, weights):
    if not values or len(values) != len(weights) or any(w <= 0 for w in weights):
        raise ValueError("Nonempty equally sized values/positive weights required")
    ordered = sorted(zip(values, weights))
    halfway = sum(weights)/2
    total = 0
    for value, weight in ordered:
        total += weight
        if total >= halfway:
            return value
    return ordered[-1][0]


def quantile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No values")
    position = (len(ordered)-1)*fraction
    left, right = math.floor(position), math.ceil(position)
    return ordered[left] + (ordered[right]-ordered[left])*(position-left)


def index_factor(sale_date, indices, as_of, series="60400"):
    rows = [i for i in indices if i.series == series and i.period <= as_of]
    if not rows:
        raise ValueError("No applicable published residential index")
    reference = max(rows, key=lambda i: i.period)
    period = date(sale_date.year, sale_date.month, 1)
    if period > reference.period:
        raise ValueError("Sale after latest index: no extrapolation permitted")
    source = next((i for i in rows if i.period == period), None)
    if source is None:
        raise ValueError("Sale-month index missing; no interpolation/extrapolation")
    if source.base != reference.base or source.series != reference.series:
        raise ValueError("Index base/series mismatch")
    if source.value <= 0 or reference.value <= 0:
        raise ValueError("Invalid index level")
    return reference.value/source.value, source, reference


def enough(evidence, sales_by_id, config):
    parcels = {(sales_by_id[e.observation_id].block, sales_by_id[e.observation_id].parcel) for e in evidence if sales_by_id[e.observation_id].block and sales_by_id[e.observation_id].parcel}
    return len(evidence) >= config.min_comps and len(parcels) >= config.min_parcels


def select_comparables(candidates, sales_by_id, config):
    selected, parcels = [], set()
    for e in candidates:
        s = sales_by_id[e.observation_id]
        key = (s.block, s.parcel)
        if s.block and s.parcel and key not in parcels:
            selected.append(e)
            parcels.add(key)
            if len(parcels) >= config.min_parcels:
                break
    for e in candidates:
        if len(selected) >= config.max_comps:
            break
        if e not in selected:
            selected.append(e)
    return sorted(selected, key=lambda e: (-e.score, e.observation_id))


def quarantine_flags(selected, config):
    """A relative signal prompts review, not a claim that a sale is invalid."""
    if len(selected) < 3:
        return []
    midpoint = median(e.adjusted_ppm for e in selected)
    return [ReviewFlag(observation_id=e.observation_id, classification="UNVERIFIED_ANOMALY", reason=f"Adjusted PPM outside {config.review_low_ratio:g}–{config.review_high_ratio:g}× selected median; source record has not been independently verified", refs=e.refs, raw_ppm=e.raw_ppm, peer_median_ppm=midpoint) for e in selected if e.adjusted_ppm < midpoint*config.review_low_ratio or e.adjusted_ppm > midpoint*config.review_high_ratio]


def value_apartment(apartment, market, config):
    result = PricingResult(apartment_id=apartment.id, property_type=apartment.property_type, built_area=apartment.built_area, rooms=apartment.rooms, status="UNPRICED", tier="NONE", valuation_reference=str(config.as_of), warnings=list(apartment.warnings))
    if not apartment.built_area or not apartment.rooms:
        result.explanation.append("Missing target area/rooms: no numeric estimate fabricated")
        return result
    recent, older = [], []
    exclusions = {}
    series = "60400" if any(i.series == "60400" for i in market.indices) else "40010"
    by_id = {s.id: s for s in reversed(market.sales) if not s.duplicate_of}
    for sale in by_id.values():
        ranked, rejected = match(apartment, sale, config)
        if ranked is None:
            for reason in rejected:
                exclusions[reason] = exclusions.get(reason, 0)+1
            continue
        score, reasons, area = ranked
        e = PricingEvidence(observation_id=sale.id, refs=sale.refs, score=score, reasons=reasons, warnings=list(sale.warnings), original_price=sale.price, area_basis=area, area_basis_semantics='explicit_built' if sale.built_area is not None else sale.area_semantics, raw_ppm=sale.price/area, adjusted_ppm=sale.price/area, sale_date=sale.date, distance_m=sale.distance_m, geo_basis=sale.geo_basis)
        if months_old(sale.date, config.as_of) <= config.recent_months:
            recent.append(e)
        else:
            try:
                factor, source, reference = index_factor(sale.date, market.indices, config.as_of, series)
            except ValueError as exc:
                reason = str(exc)
                exclusions[reason] = exclusions.get(reason, 0)+1
                continue
            e.time_factor = factor
            e.adjusted_ppm = e.raw_ppm*factor
            e.index_sale_id, e.index_reference_id = source.id, reference.id
            e.reasons.append(f"Historical time adjustment {factor:.6f} = index {reference.period}/{source.period}, base {reference.base}")
            e.warnings.append("CBS transaction-month to overlapping-period label assignment is experimental")
            if source.provisional or reference.provisional:
                e.warnings.append("CBS factor uses provisional observation(s)")
            older.append(e)
    recent.sort(key=lambda e: (-e.score, e.observation_id))
    older.sort(key=lambda e: (-e.score, e.observation_id))
    result.eligible_recent, result.eligible_historical = len(recent), len(older)
    if enough(recent, by_id, config):
        selected, tier = select_comparables(recent, by_id, config), "TIER_1"
    elif enough(recent+older, by_id, config):
        selected = select_comparables(sorted(recent+older, key=lambda e: (-e.score, e.observation_id)), by_id, config)
        tier = "TIER_2"
    else:
        selected, tier = [], "NONE"
    # Selection must still meet independence after truncation by score.
    if selected and not enough(selected, by_id, config):
        selected = []
    if selected:
        flags = quarantine_flags(selected, config)
        flagged_ids = {flag.observation_id for flag in flags}
        clean = [e for e in selected if e.observation_id not in flagged_ids]
        sensitivity = {"policy": config.quarantine_policy, "flagged_ids": sorted(flagged_ids), "threshold": f"selected adjusted PPM < {config.review_low_ratio:g}× or > {config.review_high_ratio:g}× selected median; review signal only", "baseline_selected_ids": [e.observation_id for e in selected], "clean_selected_ids": [e.observation_id for e in clean], "baseline_value": None, "without_flagged_value": None, "without_flagged_range": None, "without_flagged_usable": False}
        def estimate(items):
            values = [e.adjusted_ppm for e in items]
            return round((median(values) if config.method == "median" else weighted_median(values, [e.score for e in items])) * apartment.built_area)
        sensitivity["baseline_value"] = estimate(selected)
        if enough(clean, by_id, config):
            sensitivity["without_flagged_usable"] = True
            sensitivity["without_flagged_value"] = estimate(clean)
            clean_values = [e.adjusted_ppm for e in clean]
            clean_value = sensitivity["without_flagged_value"]
            sensitivity["without_flagged_range"] = [round(min(quantile(clean_values, .25)*apartment.built_area, clean_value*(1-config.min_range_fraction))), round(max(quantile(clean_values, .75)*apartment.built_area, clean_value*(1+config.min_range_fraction)))]
        result.review_flags = flags
        result.quarantine_sensitivity = sensitivity
        if flags:
            result.warnings.append("Baseline selected set has unverified price anomalies; see quarantine holdout sensitivity")
        if config.quarantine_policy == "exclude_flagged" and flags:
            if sensitivity["without_flagged_usable"]:
                selected = clean
                result.warnings.append("Review-flagged observations excluded by explicit scenario; not proven invalid")
            else:
                result.warnings.append("Excluding flagged observations left insufficient comparable evidence")
                selected = []
        if not selected:
            apply_benchmark(result, apartment, market, config)
            result.explanation.append("Quarantine exclusion scenario did not meet sale evidence minimums")
            return result
        values = [e.adjusted_ppm for e in selected]
        weights = [e.score for e in selected]
        result.median_estimate = round(median(values)*apartment.built_area)
        result.weighted_median_estimate = round(weighted_median(values, weights)*apartment.built_area)
        chosen = result.median_estimate if config.method == "median" else result.weighted_median_estimate
        result.estimated_market_value = chosen
        result.implied_ppm = chosen/apartment.built_area
        result.dispersion_iqr_ppm = quantile(values, .75)-quantile(values, .25)
        result.value_low = round(min(quantile(values, .25)*apartment.built_area, chosen*(1-config.min_range_fraction)))
        result.value_high = round(max(quantile(values, .75)*apartment.built_area, chosen*(1+config.min_range_fraction)))
        parcel_weights = {}
        for e in selected:
            sale = by_id[e.observation_id]
            group = (sale.block, sale.parcel) if sale.block and sale.parcel else ("unknown", "unknown")
            parcel_weights[group] = parcel_weights.get(group, 0)+e.score
        result.effective_count = round(sum(weights)**2/sum(w*w for w in parcel_weights.values()), 3)
        result.independent_parcels = len({(by_id[e.observation_id].block, by_id[e.observation_id].parcel) for e in selected})
        result.median_age_days = median([(config.as_of-e.sale_date).days for e in selected])
        result.median_age_basis = "median selected transaction age"
        result.evidence, result.tier = selected, tier
        if len(selected) > result.independent_parcels:
            result.warnings.append("Multiple selected transactions share a parcel; results are not independent observations")
        limitations = []
        if any(e.warnings for e in selected):
            limitations.append("Comparable evidence has unverified subtype, area, geography, floor or other warnings")
        if apartment.property_type != "apartment":
            limitations.append("Atypical property needs manual review")
        if apartment.balcony_area is None or apartment.balcony_area or apartment.garden_area:
            limitations.append("Outdoor area is unknown or has no calibrated marginal adjustment")
        if result.review_flags:
            limitations.append("Baseline selection contains unverified anomalous transactions; review or exclusion sensitivity required")
        result.status = "REVIEW_REQUIRED" if limitations else "PRICED"
        result.evidence_strength = "LIMITED" if result.status == "REVIEW_REQUIRED" else "MODERATE_COMPARABLE_SUPPORT"
        result.classification_reasons.extend(limitations or ["Experimental comparable gates met without recorded evidence warnings"])
        result.warnings.extend(sorted({w for e in selected for w in e.warnings}))
        midpoint = median(values)
        if any(v < midpoint*config.review_low_ratio or v > midpoint*config.review_high_ratio for v in values):
            result.warnings.append(f"Comparable PPM outlier outside {config.review_low_ratio:g}×–{config.review_high_ratio:g}× selected median; retained in robust estimate and range, investigate non-market transfer")
        result.warnings.extend(["No calibrated floor, condition, new-build or outdoor coefficients", "Robust PPM scaling is an experimental comparable-area assumption", "Range has a configurable minimum width; NOT a statistical confidence interval", "Recent nominal sales are not automatically time-adjusted to today"])
        if tier == "TIER_2":
            ids = {e.index_reference_id for e in selected if e.index_reference_id}
            periods = sorted({str(i.period) for i in market.indices if i.id in ids})
            result.valuation_reference = "CBS " + ",".join(periods) + "; unadjusted recent evidence retained at original dates"
            result.warnings.append("Mixed dated recent evidence and historical CBS reference; no extrapolation to current month")
        result.explanation.extend([f"{tier}: {len(selected)} selected / {len(recent)} eligible recent and {len(older)} index-adjustable historical; {result.independent_parcels} distinct parcels", f"{config.method} comparable PPM × declared target built area {apartment.built_area:g}; selection reserves minimum distinct parcels before filling by score; no hidden premiums", f"Median alternative {result.median_estimate:,.0f} ILS; weighted-median alternative {result.weighted_median_estimate:,.0f} ILS", "Effective count is Kish weight count grouped by parcel; distinct parcels are not guaranteed statistically independent"])
        if config.balcony_equivalent_fraction or config.garden_equivalent_fraction:
            # Scenario adjustments must not be silently mixed into empirically unsupported sale adjustments.
            result.warnings.append("Outdoor scenario coefficients apply only to benchmark fallback, not transaction tiers")
    else:
        apply_benchmark(result, apartment, market, config)
    result.explanation.append("Hard-exclusion counts: " + str(dict(sorted(exclusions.items()))))
    if result.status == "UNPRICED":
        result.explanation.append("Insufficient compatible independent sale evidence or credible segment; asking listings are NOT sale values")
    return result


def apply_benchmark(result, apartment, market, config, *, special_scenario=False):
    prop = apartment.property_type
    if prop in {"duplex", "triplex"}:
        if not special_scenario:
            result.warnings.append("No explicit duplex/triplex transaction or benchmark segment; standard-apartment proxy disabled")
            return
        prop = "apartment"
        result.warnings.append("EXPERIMENTAL special-property standard-apartment benchmark proxy enabled")
    candidates = []
    for benchmark in market.benchmarks:
        if benchmark.city_code != market.context.city_code or benchmark.property_type != prop or not benchmark.room_min <= apartment.rooms <= benchmark.room_max:
            continue
        if benchmark.median_ppm is None or benchmark.median_ppm <= 0 or benchmark.count < config.min_benchmark_count:
            continue
        if benchmark.scope != "locality" and market.context.stat_area not in benchmark.member_areas and benchmark.stat_area != market.context.stat_area:
            continue
        rank = {"area": 0, "neighborhood": 1, "locality": 2}.get(benchmark.scope, 3)
        candidates.append((rank, benchmark.low_data, -benchmark.count, benchmark))
    if not candidates:
        result.warnings.append("No matching area benchmark with adequate sample and verified geographic membership")
        return
    benchmark = min(candidates, key=lambda x: x[:3])[3]
    if config.balcony_equivalent_fraction and apartment.balcony_area is None or config.garden_equivalent_fraction and apartment.garden_area is None:
        result.warnings.append("Requested outdoor-area scenario has unknown target area; refusing fabricated zero")
        return
    equivalent = apartment.built_area + config.balcony_equivalent_fraction*(apartment.balcony_area or 0) + config.garden_equivalent_fraction*(apartment.garden_area or 0)
    result.estimated_market_value = round(benchmark.median_ppm*equivalent)
    result.implied_ppm = result.estimated_market_value/apartment.built_area
    width = max(config.min_range_fraction, config.benchmark_low_data_range_fraction if benchmark.low_data or prop != "apartment" else config.benchmark_range_fraction)
    result.value_low = round(result.estimated_market_value*(1-width))
    result.value_high = round(result.estimated_market_value*(1+width))
    result.tier, result.status, result.evidence_strength = "TIER_3", "REVIEW_REQUIRED", "WEAK_BENCHMARK_ONLY"
    result.benchmark_id, result.benchmark_refs = benchmark.id, benchmark.refs
    result.median_age_days = (config.as_of-benchmark.period_end).days
    result.median_age_basis = "age since benchmark window end; median individual transaction age unavailable"
    result.valuation_reference = f"Benchmark period ends {benchmark.period_end}; {benchmark.window_months} months; NOT updated to today"
    result.warnings.extend(["Derived Tax Authority statistic, not independent comparable transactions", "Benchmark area semantics not independently verified; declared built-area proxy is experimental", "Segment median already embodies heterogeneous outdoor areas; no marginal outdoor premium is calibrated", f"Benchmark n={benchmark.count}, low_data={benchmark.low_data}, geography={benchmark.scope}", "Preliminary benchmark scenario; range NOT a statistical confidence interval"])
    result.explanation.extend([f"TIER_3: {benchmark.id}; {benchmark.median_ppm:,.2f} ILS/m² × equivalent area {equivalent:g}", f"Area basis = built {apartment.built_area:g} + balcony × {config.balcony_equivalent_fraction} + garden × {config.garden_equivalent_fraction}; neutral default omits marginal outdoor premium"])
