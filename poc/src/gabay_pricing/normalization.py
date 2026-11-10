"""Observed contracts → typed evidence, retaining source paths and rejection ledgers."""
from datetime import date, datetime
from email.utils import parsedate_to_datetime
import math
import re
import json

from gabay_pricing.geo import canonical, distance, geography, parse_address
from gabay_pricing.models import (AreaBenchmark, GeographicContext, MarketData, MarketListing,
    PriceIndexObservation, ProjectOffering, SaleTransaction, SourceRef)
from gabay_pricing.providers.collection import digest


def num(v):
    try:
        n = float(str(v).replace(",", "")) if v is not None else None
        return n if n is not None and math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def integer(v):
    n = num(v)
    return int(n) if n is not None and n.is_integer() else None


def day(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(v).date()
        except (ValueError, TypeError):
            return None


def kind(v):
    t = str(v or "").lower()
    if "טריפלקס" in t or "triplex" in t:
        return "triplex"
    if "דופלקס" in t or "duplex" in t:
        return "duplex"
    if "גן" in t or "garden" in t or t == "patio":
        return "garden"
    if "פנטהאוז" in t or any(s in t for s in ["penthouse", "roof", "penthouse_app"]):
        return "penthouse"
    if t in {"דירה", "flat", "apartment"}:
        return "apartment"
    return "unknown"


def ref(cap, path, fields=None):
    return SourceRef(capture_id=cap.id, provider=cap.provider, endpoint=cap.endpoint, record_path=path,
                     field_paths={k: (v if v.startswith(("$", "NOT_PRESENT", "DERIVED", "REQUEST")) else "$." + v[5:] if v.startswith("ROOT.") else f"{path}.{v}") for k, v in (fields or {}).items()})


def newest(captures):
    return sorted(captures, key=lambda c: (c.captured_at.isoformat() if c.captured_at else "", c.id), reverse=True)


def resolve_context(snapshot):
    expected = parse_address(snapshot.address)
    for cap in newest(snapshot.captures):
        if cap.provider != "over" or not cap.endpoint.endswith("/nadlan/address"):
            continue
        for i, row in enumerate(cap.payload.get("data") or []):
            identity = row.get("identity") or {}
            for a in identity.get("addresses") or []:
                if canonical(a.get("street")) != canonical(expected["street"]) or str(a.get("house")) != expected["number"]:
                    continue
                city = (identity.get("settlement") or {}).get("name", "")
                if not canonical(city).startswith(canonical(expected["city"])):
                    continue
                if a.get("lat") is None or a.get("lon") is None:
                    continue
                distance(a["lat"], a["lon"], a["lat"], a["lon"])
                block, parcel = str(identity["gush"]), str(identity["helka"])
                ctx = GeographicContext(address=snapshot.address, city=city, city_code=str(identity["settlement"]["code"]), street=expected["street"], number=expected["number"], lat=a["lat"], lon=a["lon"], block=block, parcel=parcel, neighborhood=a.get("neighbourhood"), stat_area=(row.get("stat_area") or {}).get("yishuv_stat"), precision=f"{a.get('point_source', 'unknown')} / parcel match {a.get('match', 'unknown')}", refs=[ref(cap, f"$.data[{i}].identity", {"lat": "addresses[].lat", "lon": "addresses[].lon", "block": "gush", "parcel": "helka", "neighborhood": "addresses[].neighbourhood"})], warnings=["Publisher address precision; not an independent survey", "Parcel-centroid distance is not building-to-building distance"])
                point = identity.get("point") or {}
                if point.get("lat") is not None and point.get("lon") is not None:
                    ctx.parcel_points[f"{block}/{parcel}"] = (point["lat"], point["lon"])
                for lookup in newest(snapshot.captures):
                    if lookup.provider != "over" or not lookup.endpoint.endswith("/nadlan/lookup"):
                        continue
                    q = lookup.payload.get("query") or {}
                    if num(q.get("lat")) is None or num(q.get("lon")) is None or distance(ctx.lat, ctx.lon, float(q["lat"]), float(q["lon"])) > 2:
                        continue
                    for item in lookup.payload.get("data") or []:
                        ident = item.get("identity") or {}
                        p = ident.get("point") or {}
                        if p.get("lat") is not None and p.get("lon") is not None:
                            ctx.parcel_points.setdefault(f"{ident.get('gush')}/{ident.get('helka')}", (p["lat"], p["lon"]))
                    ctx.refs.append(ref(lookup, "$.data[].identity", {"parcel_points": "point"}))
                return ctx
    raise ValueError("No verified address point for this exact requested street/number/city; refusing another location's samples")


def normalize(snapshot, config):
    ctx = resolve_context(snapshot)
    data = MarketData(context=ctx)
    for cap in newest(snapshot.captures):
        p = cap.payload
        try:
            if cap.provider == "over" and ("/deals" in cap.endpoint) and isinstance(p.get("data"), list):
                mirror = "/govmap/" in cap.endpoint
                match = re.search(r"/parcel/(\d+)/(\d+)/deals", cap.endpoint)
                for i, row in enumerate(p["data"]):
                    block = str(row.get("gush") or (match[1] if match else "")) or None
                    parcel = str(row.get("parcel", row.get("helka", match[2] if match else ""))) or None
                    price = num(row.get("deal_amount") if mirror else row.get("amount"))
                    sub = row.get("sub_parcel")
                    sub = str(integer(sub)) if integer(sub) is not None else str(sub) if sub is not None else None
                    sale_date = day(row.get("deal_date") if mirror else row.get("date"))
                    area = num(row.get("asset_area") if mirror else row.get("area_sqm"))
                    sid = str(row.get("deal_id")) if mirror else digest([block, parcel, sub, str(sale_date), price, area, row.get("portion"), row.get("nature"), row.get("rooms")])[:20]
                    a = " / ".join(row.get("addresses") or []) or " ".join(str(row.get(k) or "") for k in ["street", "house_num"]).strip() or None
                    floor = 0 if row.get("floor") == "קרקע" else integer(row.get("floor"))
                    sale = SaleTransaction(id=f"over{'_govmap' if mirror else ''}:{sid}", date=sale_date, price=price, reported_area=area, rooms=num(row.get("rooms")), floor=floor,
                        property_type=kind(row.get("property_type")) if mirror else "unknown_residential" if row.get("nature") == "דירה בבית קומות" else "unknown", nature=row.get("deal_nature") if mirror else row.get("nature"),
                        share=None if mirror else num(row.get("portion_fraction", row.get("portion"))), block=block, parcel=parcel, subparcel=sub, address=a, lat=num(row.get("lat")), lon=num(row.get("lon")), building_year=integer(row.get("year_built")),
                        refs=[ref(cap, f"$.data[{i}]", {"date": "deal_date" if mirror else "date", "price": "deal_amount" if mirror else "amount", "reported_area": "asset_area" if mirror else "area_sqm", "rooms": "rooms", "floor": "floor" if mirror else "NOT_PRESENT", "share": "NOT_PRESENT" if mirror else "portion_fraction|portion", "block": "gush|request.path", "parcel": "parcel|helka|request.path", "subparcel": "sub_parcel", "address": "addresses(parcel crosswalk)|street,house_num", "property_type": "property_type|nature", "building_year": "year_built", "lat": "lat", "lon": "lon"})])
                    # A per-parcel endpoint may omit address fields; provenance still includes
                    # the requested parcel and its independently resolved centroid.
                    apply_geo(sale, ctx, config, city=row.get("settlement"), street=row.get("street") or (ctx.street if any(canonical(x).startswith(canonical(ctx.street)+" ") for x in (row.get("addresses") or [])) else None))
                    data.sales.append(sale)
            elif cap.provider == "apish" and cap.endpoint.endswith("/deals"):
                for i, row in enumerate(p.get("deals") or []):
                    addr = row.get("address") or {}
                    sale = SaleTransaction(id=f"apish:{row.get('id') or digest(row)[:20]}", date=day(row.get("date")), price=num(row.get("price")), reported_area=num(row.get("size_sqm")), rooms=num(row.get("rooms")), floor=integer(row.get("floor")), property_type="unknown", share=num(row.get("share_pct"))/100 if num(row.get("share_pct")) is not None else None,
                        block=str(addr["block"]) if addr.get("block") else None, parcel=str(addr["parcel"]) if addr.get("parcel") else None, address=" ".join(str(addr.get(k) or "") for k in ["street", "house_number"]).strip(), building_year=integer(row.get("building_year")),
                        refs=[ref(cap, f"$.deals[{i}]", {"date": "date", "price": "price", "reported_area": "size_sqm", "rooms": "rooms", "floor": "floor", "share": "share_pct / 100", "block": "address.block", "parcel": "address.parcel", "address": "address.street,house_number", "property_type": "NOT_PRESENT", "subparcel": "NOT_PRESENT; unit not cadastral ID", "building_year": "building_year"})], warnings=["Sale property subtype unknown; not automatically a standard full apartment"])
                    if row.get("properties") not in [None, 1] or row.get("final") is False:
                        sale.rejections.append("multi_property_or_nonfinal_sale")
                    apply_geo(sale, ctx, config, city=addr.get("city"), street=addr.get("street"), neighborhood=addr.get("neighbourhood"))
                    if not row.get("id"):
                        sale.rejections.append("missing_sale_identifier")
                    data.sales.append(sale)
            elif cap.provider in {"reef", "apish"} and (cap.endpoint.endswith("/search") or cap.endpoint.endswith("/listing")):
                body = p.get("data", {}) if cap.provider == "reef" else p
                rows = ([body["listing"]] if body.get("listing") else body.get("listings")) or []
                for i, row in enumerate(rows):
                    data.listings.append(normalize_listing(cap, row, i, ctx, config))
            elif cap.provider == "apish" and cap.endpoint.endswith(("/projects", "/project")):
                projects = p.get("projects") if cap.endpoint.endswith("/projects") else [p]
                for i, project in enumerate(projects or []):
                    collection_key = "units_offered" if project.get("units_offered") else "apartment_types"
                    offerings = list(project.get(collection_key) or [])
                    if num(project.get("price_min")):
                        offerings.append({})
                    if not offerings:
                        offerings = [{}]
                    parent_path = f"$.projects[{i}]" if "projects" in p else "$"
                    for j, unit in enumerate(offerings):
                        lat, lon = num(project.get("latitude")), num(project.get("longitude"))
                        geo, d, rejects, warnings = geography(ctx, lat=lat, lon=lon, city=(project.get("address") or {}).get("city"), radius=config.radius_m)
                        offering = ProjectOffering(id=f"project:{project.get('id')}:{j}", name=project.get("name") or str(project.get("id")), price=num(unit.get("price")) if unit else num(project.get("price_min")), area=num(unit.get("area_sqm", unit.get("size"))), rooms=num(unit.get("rooms")), property_type=kind(unit.get("type")), lat=lat, lon=lon, distance_m=d, price_basis="offered_unit_type" if unit else "project_starting_price_not_unit_quote", date=day(project.get("updated")), refs=[ref(cap, f"{parent_path}.{collection_key}[{j}]" if unit else parent_path, {"price": "price" if unit else "price_min", "area": "area_sqm" if collection_key == "units_offered" else "size", "rooms": "rooms", "lat": f"{parent_path}.latitude", "lon": f"{parent_path}.longitude"})], rejections=rejects, warnings=warnings+["Marketing offer, NOT a transaction; incentives and availability unknown"])
                        if not offering.price or offering.price <= 0:
                            offering.rejections.append("missing_project_price")
                        data.projects.append(offering)
            elif cap.provider == "cbs" and "/data/price" in cap.endpoint:
                for series in p.get("month") or []:
                    rows = series.get("date") or []
                    periods = sorted({date(int(r["year"]), int(r["month"]), 1) for r in rows}, reverse=True)
                    for i, row in enumerate(rows):
                        period = date(int(row["year"]), int(row["month"]), 1)
                        value = num((row.get("currBase") or {}).get("value"))
                        if value is None or value <= 0 or period > config.as_of:
                            continue
                        data.indices.append(PriceIndexObservation(id=f"cbs:{series['code']}:{period}:{cap.id}", series=str(series["code"]), period=period, value=value, base=row["currBase"]["baseDesc"], provisional=period in periods[:3], refs=[ref(cap, f"$.month[code={series['code']}].date[{i}]", {"period": "year,month", "value": "currBase.value", "base": "currBase.baseDesc", "provisional": "DERIVED_latest_three_per_official_methodology", "published_at": "NOT_EXPOSED"})], warnings=["Publication date not exposed; capture date is not release date"]))
            elif cap.provider == "bait":
                period = day((p.get("realestate") or {}).get("window_end"))
                if not period or period > config.as_of:
                    continue
                for prop, buckets in (p.get("realestate") or {}).get("by_home", {}).items():
                    for bucket, cell in buckets.items():
                        limits = bucket.rstrip("+").split("-")
                        lo, hi = float(limits[0]), float(limits[1]) if len(limits)>1 else 99.
                        data.benchmarks.append(AreaBenchmark(id=f"bait:{p['slug']}:{prop}:{bucket}:{cap.id}", geography=p["slug"], scope=p["kind"], city_code=str(p["cbs_locality_code"]), stat_area=p.get("yishuv_stat"), member_areas=p.get("member_areas") or [], property_type=prop, room_min=lo, room_max=hi, median_ppm=num(cell.get("median_ppm")), count=integer(cell.get("n")) or 0, low_data=bool(cell.get("low_data", True)), period_end=period, window_months=integer(cell.get("window_months")) or 36, refs=[ref(cap, f"$.realestate.by_home[{json.dumps(prop)}][{json.dumps(bucket)}]", {"median_ppm": "median_ppm", "count": "n", "low_data": "low_data", "period_end": "ROOT.realestate.window_end", "window_months": "window_months", "geography": "ROOT.slug,member_areas", "upstream": "ROOT.sources.realestate"})]))
        except (ValueError, KeyError, TypeError) as exc:
            data.issues.append({"capture": cap.id, "reason": "incomplete_or_invalid_contract", "error_type": type(exc).__name__})
    deduplicate(data)
    for sale in data.sales:
        if sale.price is None or sale.price <= 0:
            sale.rejections.append("invalid_sale_price")
        area = sale.built_area or sale.reported_area
        if area is None or area < 15 or area > 2000:
            sale.rejections.append("invalid_sale_area")
        if sale.rooms is None or not 0 < sale.rooms <= 20:
            sale.rejections.append("invalid_sale_rooms")
        if sale.date is None or sale.date > config.as_of:
            sale.rejections.append("missing_or_future_sale_date")
        if sale.share is None:
            sale.rejections.append("unknown_ownership_share")
        elif abs(sale.share - 1) > .000001:
            sale.rejections.append("partial_ownership_transfer")
        if sale.nature and "קומבינציה" in sale.nature:
            sale.rejections.append("combination_transaction")
        if sale.building_year is not None:
            if sale.building_year < 1850:
                sale.rejections.append("invalid_building_year")
            elif sale.building_year > config.as_of.year:
                sale.warnings.append("Recorded off-plan/future-building sale, NOT completed construction")
        if sale.floor is None:
            sale.warnings.append("Sale floor unknown; no floor adjustment")
        if sale.built_area is None:
            sale.warnings.append("Reported sale area not verified as built area")
        if sale.property_type in {"unknown", "unknown_residential"}:
            sale.warnings.append("Property subtype unverified; standard-apartment proxy only")
    # Newest captures were traversed first. One index/benchmark vintage per key, never pool vintages.
    data.projects = unique(data.projects, lambda x: (x.id, x.price_basis, x.rooms, x.area, x.price))
    data.indices = unique(data.indices, lambda x: (x.series, x.period))
    data.benchmarks = unique(data.benchmarks, lambda x: (x.geography, x.property_type, x.room_min, x.room_max))
    return data


def apply_geo(obs, ctx, config, **kwargs):
    basis, d, rejects, warnings = geography(ctx, lat=obs.lat, lon=obs.lon, block=getattr(obs, "block", None), parcel=getattr(obs, "parcel", None), radius=config.radius_m, **kwargs)
    obs.geo_basis, obs.distance_m = basis, d
    obs.rejections.extend(rejects)
    obs.warnings.extend(warnings)


def normalize_listing(cap, row, i, ctx, config):
    reef = cap.provider == "reef"
    loc = row.get("location" if reef else "address") or {}
    built = num(row.get("built_size_m2")) if reef else None
    total = num(row.get("size_m2")) if reef else None
    lat, lon = num(loc.get("latitude") if reef else row.get("latitude")), num(loc.get("longitude") if reef else row.get("longitude"))
    listing = MarketListing(id=f"{cap.provider}:{row.get('ad_id' if reef else 'id') or digest(row)[:20]}", price=num(row.get("price")), built_area=built, total_area=total, reported_area=None if reef else num(row.get("area_sqm")), area_semantics="explicit_built" if built else "total_not_built" if reef else "reported_unknown", balcony_area=num(row.get("balcony_sqm")), garden_area=num(row.get("garden_size_m2", row.get("garden_sqm"))), rooms=num(row.get("rooms")), floor=integer(loc.get("floor") if reef else row.get("floor")), property_type=kind(row.get("property_type")), date=day(row.get("updated_at") or row.get("last_updated") or row.get("first_seen")), address=" ".join(str(loc.get(k) or "") for k in (["street", "house_number"] if reef else ["street", "street_number"])).strip(), lat=lat, lon=lon, url=row.get("url"), refs=[ref(cap, ("$.data.listing" if cap.endpoint.endswith("/listing") else f"$.data.listings[{i}]") if reef else f"$.listings[{i}]",  {"price": "price", "built_area": "built_size_m2" if reef else "NOT_PRESENT", "total_area": "size_m2" if reef else "NOT_PRESENT", "reported_area": "area_sqm" if not reef else "NOT_PRESENT", "balcony_area": "balcony_sqm", "garden_area": "garden_size_m2|garden_sqm", "rooms": "rooms", "floor": "location.floor" if reef else "floor", "date": "updated_at|last_updated|first_seen", "property_type": "property_type", "lat": "location.latitude" if reef else "latitude", "lon": "location.longitude" if reef else "longitude", "address": "location.street,house_number" if reef else "address.street,street_number", "url": "url"})])
    apply_geo(listing, ctx, config, city=(loc.get("city") or {}).get("name") if reef and isinstance(loc.get("city"), dict) else loc.get("city"), street=loc.get("street"), neighborhood=(loc.get("neighborhood") or {}).get("name") if reef and isinstance(loc.get("neighborhood"), dict) else loc.get("neighbourhood"), precision=loc.get("precision"))
    if row.get("price_not_published") or row.get("price_placeholder") or not listing.price or listing.price < 100000:
        listing.rejections.append("missing_placeholder_or_invalid_asking_price")
    if built is not None and (built <= 0 or total is not None and built > total):
        listing.rejections.append("inconsistent_built_total_area")
    if not row.get('ad_id' if reef else 'id'):
        listing.rejections.append("missing_advertisement_identifier")
    if built is None:
        listing.warnings.append("Listing built area unknown; total/reported area not silently substituted")
    if listing.date is None:
        listing.warnings.append("Listing publication/update date unknown")
    return listing


def unique(items, key):
    seen = set()
    result = []
    for x in items:
        k = key(x)
        if k not in seen:
            seen.add(k)
            result.append(x)
    return result


def fill_missing(primary, secondary):
    fields = []
    for key in primary.__class__.model_fields:
        if key not in {"duplicate_of", "id", "refs"} and getattr(primary, key) is None and getattr(secondary, key) is not None:
            setattr(primary, key, getattr(secondary, key))
            fields.append(key)
    for r in secondary.refs:
        primary.refs.append(r.model_copy(update={"field_paths": {k: v for k, v in r.field_paths.items() if k in fields}}))


def deduplicate(data):
    sales = sorted(data.sales, key=lambda s: (s.share == 1, s.subparcel not in {None, "0"}), reverse=True)
    by_id, strong, core = {}, {}, {}
    for s in sales:
        k = (s.block, s.parcel, s.subparcel, s.date, s.price, s.reported_area, s.rooms, s.share)
        base = (s.block, s.parcel, s.date, s.price, s.reported_area, s.rooms)
        known = s.block and s.parcel and s.subparcel not in {None, "0"}
        primary = by_id.get(s.id) or (strong.get(k) if known else None)
        # Mirror with missing share can enrich an exact cadastral/subparcel/amount/date match.
        if not primary and known:
            primary = next((x for x in core.get(base, []) if x.subparcel == s.subparcel and (x.share == s.share or s.share is None)), None)
        if primary:
            s.duplicate_of = primary.id
            s.rejections.append("duplicate_observation")
            fill_missing(primary, s)
            data.duplicates.append({"id": s.id, "duplicate_of": primary.id, "reason": "same_source_ID_or_exact_cadastral_sale"})
        else:
            if s.subparcel is None and core.get(base) and s.block and s.parcel:
                s.rejections.append("potential_cross_source_duplicate_not_independent")
                data.duplicates.append({"id": s.id, "candidate_duplicates": [x.id for x in core[base]], "reason": "Unknown subparcel; retained but excluded from independent evidence"})
            by_id[s.id] = s
            if known:
                strong[k] = s
            core.setdefault(base, []).append(s)
    listings = {}
    cross = {}
    for listing in data.listings:
        primary = listings.get(listing.id)
        # Strong cross-portal address/type/floor/explicit built area/rooms/price equality only.
        k = (canonical(listing.address), listing.property_type, listing.floor, listing.built_area, listing.rooms, listing.price)
        if not primary and listing.built_area and listing.floor is not None and re.search(r"\d", listing.address or "") and listing.geo_basis == "source_point":
            primary = cross.get(k)
            cross[k] = primary or listing
        if primary:
            listing.duplicate_of = primary.id
            listing.rejections.append("duplicate_advertisement")
            fill_missing(primary, listing)
            data.duplicates.append({"id": listing.id, "duplicate_of": primary.id, "reason": "same_ad_ID_or_strong_cross_portal_signature"})
        else:
            listings[listing.id] = listing
