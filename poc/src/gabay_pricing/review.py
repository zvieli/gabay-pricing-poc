"""Separate evidence assessment and explicitly hypothetical differentiation scenarios."""
from gabay_pricing.models import PricingResult
from gabay_pricing.valuation import apply_benchmark


def assess(result, apartment, market, config):
    states = apartment.attribute_states or {'built_area': 'OBSERVED' if apartment.built_area is not None else 'MISSING', 'rooms': 'OBSERVED' if apartment.rooms is not None else 'MISSING', 'floor': 'OBSERVED' if apartment.floors else 'MISSING', 'directions': 'OBSERVED' if apartment.directions else 'MISSING', 'balcony_area': 'OBSERVED' if apartment.balcony_area is not None else 'UNKNOWN', 'garden_area': 'OBSERVED' if apartment.garden_area is not None else ('UNKNOWN' if apartment.property_type == 'garden' else 'NOT_APPLICABLE')}
    missing = [name for name, state in states.items() if state in {'MISSING', 'UNKNOWN'}]
    inferred = [name for name, state in states.items() if state == 'INFERRED']
    result.feature_completeness = "INCOMPLETE" if missing else "RECORDED_NOT_VERIFIED"
    result.evidence_quality = "INSUFFICIENT" if result.tier == "NONE" else "BENCHMARK_ONLY" if result.tier == "TIER_3" else "LIMITED_WITH_ANOMALIES" if result.review_flags else "LIMITED_UNVERIFIED" if any(e.warnings for e in result.evidence) else "COMPARABLES_WITHOUT_RECORDED_WARNINGS"
    result.methodology_strength = {"NONE": "NO_DEFENSIBLE_METHOD", "TIER_1": "EXPERIMENTAL_RECENT_COMPARABLES", "TIER_2": "EXPERIMENTAL_CBS_ADJUSTED", "TIER_3": "EXPERIMENTAL_AGGREGATE_PROXY"}[result.tier]
    result.marketing_approval = "NOT_APPROVED_HUMAN_REVIEW_REQUIRED"
    result.classification_reasons.extend(["Unknown/missing target attributes: " + (", ".join(missing) if missing else "none recorded; measurements unverified"), "Inferred target attributes needing confirmation: " + (', '.join(inferred) or 'none'), "Human marketing approval is not granted by a numerical estimate or suggested ask"])
    if result.status == "UNPRICED":
        result.classification_reasons.append("Insufficient eligible comparable/benchmark evidence")
    elif result.tier == "TIER_3":
        result.classification_reasons.append("Benchmark-only preliminary estimate; no comparable-level evidence")
    scenario = {"label": "USER_ASSUMPTION_ONLY_NOT_CALIBRATED", "enabled": False, "value": None, "components": [], "warnings": []}
    enabled = bool(config.scenario_floor_rate or config.scenario_orientation_rates or config.scenario_balcony_ils_per_sqm or config.scenario_garden_ils_per_sqm)
    scenario["enabled"] = enabled
    if enabled and result.estimated_market_value is not None:
        rate = 0
        if config.scenario_floor_rate:
            if not apartment.floors:
                scenario["warnings"].append("Floor is unknown; requested scenario cannot be calculated")
            else:
                floor = min(apartment.floors)
                rate += (floor-config.scenario_floor_reference)*config.scenario_floor_rate
                scenario["components"].append(f"Floor {floor}, reference {config.scenario_floor_reference}, uncalibrated rate {config.scenario_floor_rate:+.2%}/floor")
        if config.scenario_orientation_rates:
            matches = [config.scenario_orientation_rates[d] for d in apartment.directions if d in config.scenario_orientation_rates]
            if len(apartment.directions) != 1 or len(matches) != 1:
                scenario["warnings"].append("Orientation missing, ambiguous, or not configured; requested scenario cannot be calculated")
            else:
                rate += matches[0]
                scenario["components"].append(f"Orientation {apartment.directions[0]}: uncalibrated rate {matches[0]:+.2%}")
        outdoor = 0
        for field, coefficient in [("balcony_area", config.scenario_balcony_ils_per_sqm), ("garden_area", config.scenario_garden_ils_per_sqm)]:
            if coefficient:
                value = getattr(apartment, field)
                if value is None:
                    scenario["warnings"].append(f"{field} unknown; requested scenario cannot be calculated")
                else:
                    outdoor += value*coefficient
                    scenario["components"].append(f"{field}: {value:g} m² × {coefficient:g} ILS/m² (uncalibrated marginal scenario)")
        if 1+rate <= 0:
            scenario["warnings"].append("Combined rate produces a nonpositive multiplier; scenario withheld")
        if not scenario["warnings"]:
            scenario["value"] = round(result.estimated_market_value*(1+rate)+outdoor)
        scenario["warnings"].append("Scenario may double count attributes already present in comparable prices; never use as calibrated market value or approved ask")
    elif enabled:
        scenario["warnings"].append("No base valuation; differentiation scenario withheld")
    result.differentiation_scenario = scenario
    if apartment.property_type == "garden" and result.status == "UNPRICED":
        # Separate counterfactual, never convert an ordinary-flat benchmark into a garden valuation.
        proxy_apartment = apartment.model_copy(update={"property_type": "apartment"})
        proxy = PricingResult(apartment_id=apartment.id, property_type="apartment", built_area=apartment.built_area, rooms=apartment.rooms, status="UNPRICED", tier="NONE", valuation_reference=str(config.as_of))
        apply_benchmark(proxy, proxy_apartment, market, config)
        result.special_property_scenario = {"label": "STANDARD_APARTMENT_PROXY_ONLY_NOT_GARDEN_PROPERTY_VALUE", "value": proxy.estimated_market_value, "range": [proxy.value_low, proxy.value_high] if proxy.estimated_market_value is not None else None, "benchmark_id": proxy.benchmark_id, "refs": [r.model_dump(mode="json") for r in proxy.benchmark_refs], "warnings": proxy.warnings + ["Scenario only; garden area and premium unknown; not a garden-property estimate or marketing ask"]}
    if apartment.property_type in {"duplex", "triplex"} and result.status == "UNPRICED":
        proxy = PricingResult(apartment_id=apartment.id, property_type=apartment.property_type, built_area=apartment.built_area, rooms=apartment.rooms, status="UNPRICED", tier="NONE", valuation_reference=str(config.as_of))
        apply_benchmark(proxy, apartment, market, config, special_scenario=True)
        result.special_property_scenario = {"label": "STANDARD_APARTMENT_PROXY_ONLY_NOT_SPECIAL_PROPERTY_VALUE", "value": proxy.estimated_market_value, "range": [proxy.value_low, proxy.value_high] if proxy.estimated_market_value is not None else None, "benchmark_id": proxy.benchmark_id, "refs": [r.model_dump(mode="json") for r in proxy.benchmark_refs], "warnings": proxy.warnings + ["Scenario only; not an estimate of this special property, no marketing ask"]}
    return result
