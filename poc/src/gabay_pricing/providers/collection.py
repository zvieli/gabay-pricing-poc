"""Audited contracts only; local snapshots and bounded public/paid requests."""
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

import httpx
from dotenv import dotenv_values

from gabay_pricing.geo import canonical, parse_address
from gabay_pricing.models import Capture, Snapshot


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def sanitize(data):
    if isinstance(data, dict):
        return {k: sanitize(v) for k, v in data.items() if not re.search(r"phone|email|seller|broker|agency|sale_agents|developers|contact|description|raw$|image|photo|logo|title|tags|municipal_head|architect|constructor|authorization|token|cookie", k, re.I)}
    if isinstance(data, list):
        return [sanitize(v) for v in data]
    return data


def capture(provider, endpoint, request, payload, mode, at=None, path=None):
    payload = sanitize(payload)
    sha = digest(payload)
    data = payload.get("meta") or payload
    return Capture(id=digest([provider, endpoint, request, sha, mode])[:20], provider=provider, endpoint=endpoint, request=request,
                   payload=payload, mode=mode, captured_at=at, fetched_at=data.get("fetched_at"), original_path=path, sha256=sha)


def replay_audit(root: Path, address: str, as_of: date) -> Snapshot:
    audit = root / "docs/data-source-audit"
    snap = Snapshot(address=address, as_of=as_of)
    for folder in [audit, audit / "critical-sales-access"]:
        ledger_path = folder / "REQUEST_LEDGER.json"
        if not ledger_path.exists():
            continue
        for row in json.loads(ledger_path.read_text()):
            provider = row["provider"]
            name = row.get("name", row.get("sample"))
            if provider not in {"over", "apish", "reef", "cbs", "bait"} or row.get("status") != 200:
                continue
            path = folder / "RAW_SAMPLES" / provider / f"{name}.json"
            if not path.exists():
                continue
            if provider == "over" and name not in {"address", "nearby_lookup", "helsinki_recent", "helsinki_page2", "parcel_deals", "nearby_parcel_recent", "govmap_mirror_recent"}:
                continue
            if provider == "bait" and name not in {"kikar_hamedina", "tel_aviv"}:
                continue
            if provider == "cbs" and name not in {"tel_aviv_index", "apartment_index"}:
                continue
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict) or payload.get("_non_json"):
                continue
            request = row.get("params") or row.get("request_json") or {}
            if not request and "?" in row["url"]:
                from urllib.parse import parse_qsl, urlsplit
                request = dict(parse_qsl(urlsplit(row["url"]).query))
            snap.captures.append(capture(provider, row["url"].split("?")[0], request, payload, "AUDIT_REPLAY", row.get("at"), str(path.relative_to(root))))
    if not snap.captures:
        raise ValueError("No verified local audit captures; supply an existing evidence snapshot")
    return snap


def save_snapshot(snapshot: Snapshot, path: Path):
    for cap in snapshot.captures:
        cap.payload = sanitize(cap.payload)
        cap.sha256 = digest(cap.payload)
        cap.id = digest([cap.provider, cap.endpoint, cap.request, cap.sha256, cap.mode])[:20]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(snapshot.model_dump_json(indent=2))
    temporary.replace(path)


class Collector:
    def __init__(self, snapshot: Snapshot, path: Path, keys: dict, transport=None):
        self.snapshot, self.path, self.keys = snapshot, path, keys
        self.client = httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=False, transport=transport, headers={"User-Agent": "GabayResearchPOC/0.1 (read-only)", "Accept": "application/json"})

    def close(self):
        self.client.close()

    def fetch(self, provider, endpoint, params=None, method="GET"):
        params = params or {}
        request_id = digest([provider, endpoint, method, params])
        # A resumed run reuses its successful live call; it cannot silently spend another budget.
        for c in reversed(self.snapshot.captures):
            if c.mode != "AUDIT_REPLAY" and c.provider == provider and c.endpoint == endpoint and c.request == params:
                return c.payload
        limits = {"reef": 10, "apish": 10, "over": 20, "cbs": 3, "bait": 5}
        key_name = {"reef": "REEF_API_KEY", "apish": "APISH_API_KEY"}.get(provider)
        if key_name and not self.keys.get(key_name):
            self.snapshot.failures.append({"provider": provider, "endpoint": endpoint, "reason": "credential_missing"})
            save_snapshot(self.snapshot, self.path)
            return None
        for attempt in range(2):
            if sum(r["provider"] == provider for r in self.snapshot.requests) >= limits[provider]:
                self.snapshot.failures.append({"provider": provider, "endpoint": endpoint, "reason": "request_budget_exhausted"})
                save_snapshot(self.snapshot, self.path)
                return None
            entry = {"provider": provider, "endpoint": endpoint, "request": params, "method": method, "request_id": request_id, "attempt": attempt + 1, "at": datetime.now(timezone.utc).isoformat(), "status": None, "credits": None}
            self.snapshot.requests.append(entry)
            save_snapshot(self.snapshot, self.path)
            headers = {"x-api-key": self.keys[key_name]} if key_name else {}
            try:
                response = self.client.request(method, endpoint, headers=headers, params=params if method == "GET" else None, json=params if method == "POST" else None)
                entry["status"] = response.status_code
                entry["headers"] = {k: v for k, v in response.headers.items() if k in {"date", "cache-control", "x-credits-used", "x-credits-remaining", "retry-after"}}
                if response.status_code >= 500 and attempt == 0:
                    save_snapshot(self.snapshot, self.path)
                    time.sleep(.5)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("ok") is False or payload.get("success") is False:
                    raise ValueError("invalid_or_failed_response_envelope")
                entry["credits"] = payload.get("credits_used", (payload.get("meta") or {}).get("charged_credits"))
                c = capture(provider, endpoint, params, payload, "PROVIDER_CACHE" if payload.get("cached") or (payload.get("meta") or {}).get("cache_hit") else "LIVE", entry["at"])
                self.snapshot.captures.append(c)
                save_snapshot(self.snapshot, self.path)
                return c.payload
            except (httpx.HTTPError, ValueError) as exc:
                # Never persist exception messages: HTTP errors can contain credential-bearing URLs.
                entry["error_type"] = type(exc).__name__
                self.snapshot.failures.append({"provider": provider, "endpoint": endpoint, "status": entry["status"], "reason": type(exc).__name__})
                save_snapshot(self.snapshot, self.path)
                if isinstance(exc, httpx.TransportError) and attempt == 0:
                    time.sleep(.5)
                    continue
                return None
        return None


def live_refresh(snapshot: Snapshot, path: Path, root: Path):
    import os
    address = parse_address(snapshot.address)
    file_keys = dotenv_values(root / ".env")
    keys = {k: os.environ.get(k) or file_keys.get(k) for k in ["REEF_API_KEY", "APISH_API_KEY"]}
    c = Collector(snapshot, path, keys)
    try:
        c.fetch("over", "https://www.over.org.il/api/nadlan/address", {**address, "geometry": "false"})
        # If both the live lookup and audited fallback are unavailable, still collect
        # CBS; the CLI will emit 39 UNPRICED rows rather than inventing an anchor.
        from gabay_pricing.normalization import resolve_context
        try:
            ctx = resolve_context(snapshot)
        except ValueError:
            c.fetch("cbs", "https://api.cbs.gov.il/index/data/price", {"id": 40010, "format": "json", "download": "false", "pagesize": 1000, "coef": "true"})
            return snapshot
        nearby = c.fetch("over", "https://www.over.org.il/api/nadlan/lookup", {"lat": ctx.lat, "lon": ctx.lon, "radius_m": 1000, "limit": 60, "fields": "identity,point,streets,addresses,deals,match"})
        year = max(1998, snapshot.as_of.year - 10)
        for offset in [0, 100]:
            d = c.fetch("over", "https://www.over.org.il/api/deals/search", {"settlement": ctx.city, "street": ctx.street, "nature": "דירה בבית קומות", "date_from": f"{year}-01-01", "date_to": str(snapshot.as_of), "sort": "date_desc", "limit": 100, "offset": offset})
            if not d or len(d.get("data", [])) < 100:
                break
        parcels = [(ctx.block, ctx.parcel)]
        for row in (nearby or {}).get("data", []):
            identity = row.get("identity") or {}
            if row.get("deals") and identity.get("gush") and identity.get("helka") is not None:
                pair = (str(identity["gush"]), str(identity["helka"]))
                if pair not in parcels:
                    parcels.append(pair)
        for block, parcel in parcels[:7]:
            c.fetch("over", f"https://www.over.org.il/api/nadlan/parcel/{block}/{parcel}/deals", {"limit": 40, "offset": 0})
        c.fetch("over", "https://www.over.org.il/api/deals/govmap/search", {"settlement": ctx.city, "street": ctx.street, "date_from": f"{year}-01-01", "limit": 50})
        location = f"{ctx.street} {ctx.number} {ctx.city}"
        for page in [1, 2]:
            d = c.fetch("apish", "https://api.api.sh/madlan/v1/deals", {"location": location, "from": f"{snapshot.as_of.year-2}-{snapshot.as_of.month:02d}-01", "to": str(snapshot.as_of), "limit": 100, "page": page, "raw": "0"})
            if not d or not d.get("next_page"):
                break
        c.fetch("apish", "https://api.api.sh/madlan/v1/deals", {"location": location, "from": f"{year}-01-01", "to": f"{snapshot.as_of.year-2}-{snapshot.as_of.month:02d}-01", "limit": 100, "raw": "0"})
        listing_count = 0
        for filters in [{"min_rooms": 3, "max_rooms": 3, "property_type": "flat"}, {"min_rooms": 5, "max_rooms": 5, "property_type": "flat"}, {"property_type": "duplex,triplex,penthouse,gardenApartment"}]:
            listed = c.fetch("apish", "https://api.api.sh/madlan/v1/search", {"location": location, "deal": "buy", "limit": 30, "raw": "0", **filters})
            listing_count += len((listed or {}).get("listings") or [])
        if not listing_count:
            # Targets are hypothetical: an empty building search must not end market collection.
            c.fetch("apish", "https://api.api.sh/madlan/v1/search", {"location": f"{ctx.street} {ctx.city}", "deal": "buy", "limit": 30, "raw": "0"})
        projects = c.fetch("apish", "https://api.api.sh/madlan/v1/projects", {"location": location, "raw": "0"})
        if projects and projects.get("projects"):
            c.fetch("apish", "https://api.api.sh/madlan/v1/project", {"id": projects["projects"][0]["id"], "raw": "0"})
        c.fetch("reef", "https://api.reefapi.com/yad2/v1/locations", {"query": f"{ctx.street} {ctx.city}"}, "POST")
        categories = [None, "apartment", "garden_apartment", "duplex", "triplex", "rooftop_penthouse"]
        returned = []
        for category in categories:
            params = {"deal_type": "sale", "location": f"{ctx.street} {ctx.city}", "page": 1, "include_pii": False}
            if category:
                params["property_type"] = [category]
            d = c.fetch("reef", "https://api.reefapi.com/yad2/v1/real_estate/search", params, "POST")
            returned.extend((d or {}).get("data", {}).get("listings", []))
        seen = set()
        for row in returned:
            if row.get("ad_id") in seen:
                continue
            if len(seen) >= 3:
                break
            seen.add(row.get("ad_id"))
            c.fetch("reef", "https://api.reefapi.com/yad2/v1/listing", {"ad_id": row.get("ad_id"), "vertical": "real_estate", "include_pii": False}, "POST")
        c.fetch("cbs", "https://api.cbs.gov.il/index/data/price", {"id": 60400 if ctx.city_code == '5000' else 40010, "format": "json", "download": "false", "startPeriod": "01-2017", "endPeriod": f"{snapshot.as_of.month:02d}-{snapshot.as_of.year}", "pagesize": 1000, "coef": "true"})
        # Only use place slugs whose saved source actually covers this resolved locality/area.
        bait_slugs = set()
        for cap in snapshot.captures:
            if cap.provider != "bait":
                continue
            p = cap.payload
            if str(p.get("cbs_locality_code")) == ctx.city_code and (p.get("kind") == "locality" or ctx.stat_area in p.get("member_areas", [])):
                bait_slugs.add(p["slug"])
        for slug in sorted(bait_slugs):
            c.fetch("bait", f"https://www.bait-israeli.com/api/v1/places/{slug}.json")
    finally:
        c.close()
        save_snapshot(snapshot, path)
    return snapshot
