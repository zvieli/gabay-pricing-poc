"""Experimental hard gates and soft ranking, not a calibrated similarity model."""
from datetime import date
import math


def months_old(sale_date, as_of):
    return (as_of.year-sale_date.year)*12 + as_of.month-sale_date.month + (as_of.day-sale_date.day)/31


def match(apartment, sale, config):
    hard = list(sale.rejections)
    reasons = []
    if sale.duplicate_of:
        hard.append("duplicate")
    if apartment.built_area is None or apartment.rooms is None:
        return None, ["target_missing_area_or_rooms"]
    area = sale.built_area or (sale.reported_area if config.allow_reported_sale_area else None)
    if not area or not sale.rooms or sale.date is None:
        return None, ["missing_comparable_area_rooms_or_date"]
    allowed = {"apartment", "unknown_residential", "unknown"} if apartment.property_type == "apartment" else {apartment.property_type}
    if sale.property_type not in allowed:
        hard.append("incompatible_property_type")
    diff = abs(area-apartment.built_area)/apartment.built_area
    room_diff = abs(sale.rooms-apartment.rooms)
    if diff > config.max_area_difference:
        hard.append("built_or_reported_area_too_different")
    if room_diff > config.max_room_difference:
        hard.append("room_count_too_different")
    age = months_old(sale.date, config.as_of)
    if age < 0 or age > config.history_years*12:
        hard.append("outside_history_window")
    if sale.distance_m is not None and sale.distance_m > config.radius_m:
        hard.append("outside_market_radius")
    if not sale.geo_basis:
        hard.append("unverified_record_geography")
    if hard:
        return None, sorted(set(hard))
    geo_penalty = sale.distance_m/config.radius_m if sale.distance_m is not None else config.matching_neighborhood_penalty if sale.geo_basis == "same_neighborhood" else config.matching_other_geo_penalty
    subtype_penalty = config.matching_unknown_type_penalty if sale.property_type in {"unknown", "unknown_residential"} else 0
    floor_penalty = 0 if sale.floor is None or not apartment.floors else config.matching_floor_penalty*min(config.matching_floor_cap, abs(sale.floor-min(apartment.floors)))
    score = 1/(1+config.matching_area_weight*diff+config.matching_room_weight*room_diff+geo_penalty+age/config.matching_age_months_scale+floor_penalty+subtype_penalty)
    reasons.extend([f"Property compatibility: {sale.property_type} → {apartment.property_type}", f"Area difference {diff:.1%}; room difference {room_diff:g}", f"Geography: {sale.geo_basis}; {'unknown metric distance' if sale.distance_m is None else f'{sale.distance_m:.0f}m source/centroid distance'}", f"Age {age:.1f} months; experimental score {score:.4f}"])
    if sale.floor is None:
        reasons.append("Floor unknown: no price coefficient or fabricated floor")
    if sale.balcony_area is None or sale.garden_area is None:
        reasons.append("Outdoor features unknown: no outdoor similarity claim/premium")
    return (score, reasons, area), []
