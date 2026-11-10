"""Conservative geography checks on returned records, never query metadata."""
import math
import re

from gabay_pricing.models import GeographicContext


def canonical(value: str | None) -> str:
    value = re.sub(r"[^\w\s]", " ", value or "")
    return " ".join(w for w in value.split() if w not in {"שכונת", "שכונה", "סביבת"})


def parse_address(address: str) -> dict:
    parts = address.split(",", 1)
    match = re.fullmatch(r"\s*(.+?)\s+(\d+[א-תA-Za-z]?)\s*", parts[0])
    if len(parts) != 2 or not match or not parts[1].strip():
        raise ValueError("Address must be 'street number, city'; ambiguous inputs require explicit correction")
    return {"street": match[1], "number": match[2], "city": parts[1].strip()}


def distance(lat1, lon1, lat2, lon2):
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in [lat1, lon1, lat2, lon2]):
        raise ValueError("Invalid geographic coordinate")
    if not all(29 <= v <= 34 for v in [lat1, lat2]) or not all(33 <= v <= 36.5 for v in [lon1, lon2]):
        raise ValueError("Coordinates are not Israeli WGS84 degrees")
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 2*6371008.8*math.asin(min(1, math.sqrt(h)))


def geography(ctx: GeographicContext, *, lat=None, lon=None, block=None, parcel=None, city=None, street=None, neighborhood=None, radius=1500, precision=None):
    """Return basis, metric distance and rejection/warning lists."""
    if city and not (canonical(city) == canonical(ctx.city) or canonical(city).startswith(canonical(ctx.city)) or canonical(ctx.city).startswith(canonical(city))):
        return None, None, ["wrong_city"], []
    if lat is not None and lon is not None:
        try:
            d = distance(ctx.lat, ctx.lon, lat, lon)
        except ValueError:
            return None, None, ["invalid_coordinate_system"], []
        if d > radius:
            return "source_point", d, ["outside_market_radius"], []
        if precision in {"neighborhood", "neighbourhood", "city"}:
            return "coarse_source_point", d, [], ["Coarse listing point is not exact building distance"]
        return "source_point", d, [], ["Source point precision not independently surveyed"]
    key = f"{block}/{parcel}"
    if key in ctx.parcel_points:
        p = ctx.parcel_points[key]
        d = distance(ctx.lat, ctx.lon, *p)
        return "parcel_centroid", d, ["outside_market_radius"] if d > radius else [], ["Distance to parcel centroid, NOT building-to-building distance"]
    if city and street and canonical(street) == canonical(ctx.street):
        return "same_street", None, [], ["Actual returned street matches; metric distance unavailable"]
    if city and neighborhood and canonical(neighborhood) == canonical(ctx.neighborhood):
        return "same_neighborhood", None, [], ["Actual returned neighborhood matches; metric distance unavailable"]
    return None, None, ["unverified_record_geography"], []
