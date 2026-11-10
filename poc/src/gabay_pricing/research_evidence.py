"""Offline, display-only research; never passed to valuation or decision engines."""
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from datetime import date
from urllib.parse import urlparse

from gabay_pricing.valuation import weighted_median



def _read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _number(value):
    if not value:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Research numeric value must be finite')
    return number


def research_matches(apartment, research, config, address):
    """Illustrative candidates only; unknown attributes are disclosed, never decision-qualified."""
    target_area = apartment.get('built_area')
    if not target_area or not apartment.get('rooms'):
        return []
    # Resolve transitive declared URL aliases before eligibility, so input order cannot duplicate a property.
    parent = {}
    def root(url):
        parent.setdefault(url, url)
        if parent[url] != url:
            parent[url] = root(parent[url])
        return parent[url]
    for offer in research['offers']:
        first, other = offer['source_url'], offer.get('possible_duplicate_url')
        if other:
            a, b = root(first), root(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    seen, matches = set(), []
    for offer in sorted(research['offers'], key=lambda x: x['id']):
        if not offer['price'] or not offer['area'] or offer['price'] <= 0 or offer['area'] <= 0:
            continue
        if offer['rooms'] != apartment['rooms'] or abs(offer['area']-target_area)/target_area > config['research_max_area_difference']:
            continue
        if offer['property_type'] not in ('unknown', apartment['property_type']):
            continue
        if offer['city'] and offer['city'].replace(' ', '').replace('-', '') not in address.replace(' ', '').replace('-', ''):
            continue
        if offer.get('distance_m') is not None and (offer['distance_m'] < 0 or offer['distance_m'] > config['research_max_distance_m']):
            continue
        floors = apartment.get('floors') or []
        if floors and offer['floor'] is not None and abs(min(floors)-offer['floor']) > config['research_max_floor_difference']:
            continue
        source_date = offer.get('updated_at') or offer.get('published_at')
        if source_date:
            age = (date.fromisoformat(config['as_of']) - date.fromisoformat(source_date)).days
            if age < 0 or age > config['research_max_listing_age_days']:
                continue
        canonical = root(offer['source_url'])
        if canonical in seen:
            continue
        seen.add(canonical)
        matches.append(offer)
    return matches


def load_research(directory: Path, bundle):
    files = [directory / name for name in ('GABAY_24_SALES_EVIDENCE.csv',
             'GABAY_COMPETITIVE_OFFERS_EVIDENCE.csv', 'GABAY_MARKET_RESEARCH_FOLLOWUP_HE.md')]
    checks_path = directory / 'GABAY_RESEARCH_SOURCE_CHECKS.json'
    checks = json.loads(checks_path.read_text(encoding='utf-8')) if checks_path.exists() else None
    prices_path = directory / 'GABAY_RESEARCH_PRICE_CONFLICTS.csv'
    price_rows = _read(prices_path) if prices_path.exists() else []
    external_prices = {}
    for row in price_rows:
        value = _number(row['external_price'])
        if not row['sale_id'] or not value or value <= 0 or row['sale_id'] in external_prices:
            raise ValueError('Invalid or duplicate research external sale price')
        external_prices[row['sale_id']] = row
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files + ([checks_path] if checks else []) + ([prices_path] if prices_path.exists() else [])}
    sales, offers = [], []
    sale_map = {s['id']: s for s in (bundle.get('market') or {}).get('sales', []) if not s.get('duplicate_of')}
    for row in _read(files[0]):
        url = row['מקור_חיצוני'] or None
        price_row = external_prices.get(row['מזהה_מקור'])
        external_price = _number(price_row['external_price']) if price_row else None
        model_price = _number(row['מחיר_במודל_₪'])
        if row.get('מחיר_עסקה') == 'סותר' and external_price is None:
            raise ValueError('Conflicting sale requires structured external price: ' + row['מזהה_מקור'])
        conflict = external_price is not None and model_price is not None and external_price != model_price
        sales.append({'id': row['מזהה_מקור'], 'source_url': url,
            'source_type': 'SECONDARY_SALE_HISTORY' if url else 'MODEL_SNAPSHOT_ONLY',
            'published_at': None, 'updated_at': None, 'collected_at': row['מועד_בדיקה'] or None,
            'transaction_date': row['תאריך_במודל'], 'model_price': model_price,
            'external_price': external_price, 'external_price_source': price_row['source_url'] if price_row else None,
            'area': _number(row['שטח_מדווח_מ״ר']), 'rooms': _number(row['חדרים_במודל']),
            'floor': _number(row['קומה_במודל']), 'location': row['כתובת_במודל'],
            'verification': 'EXTERNAL_PRICE_CONFLICT_UNRESOLVED' if conflict else row['סטטוס_התאמת_זהות_תאריך_מחיר_שטח_חדרים'],
            'research_eligible': row['סטטוס_התאמת_זהות_תאריך_מחיר_שטח_חדרים'] == 'תואם במקור משני', 'decision_price_eligible': False,
            'limitations': row['הערת_ביקורת'],
            'snapshot_refs': sale_map.get(row['מזהה_מקור'], {}).get('refs', []),
            'snapshot_record_present': row['מזהה_מקור'] in sale_map,
            'original_record': row})
    unknown_ids = set(external_prices) - {s['id'] for s in sales}
    if unknown_ids:
        raise ValueError('External research prices reference unknown sale IDs: ' + ', '.join(sorted(unknown_ids)))
    for row in _read(files[1]):
        url = row['url']
        if urlparse(url).scheme not in {'https', 'http'} or not urlparse(url).netloc:
            raise ValueError('Research listing requires an http(s) source URL')
        published = row['date'] if row['date_kind'].startswith('פורסם') else None
        updated = row['date'] if row['date_kind'] == 'עודכן' else None
        offers.append({'id': url, 'source_url': url, 'source_type': 'SECONDARY_ASKING_LISTING',
            'published_at': published, 'updated_at': updated, 'collected_at': row['check_date'],
            'price': _number(row['price']), 'area': _number(row['area']), 'rooms': _number(row['rooms']),
            'floor': _number(row['floor']), 'location': row['location'], 'name': row['name'],
            'group': row['type'], 'property_type': row.get('property_type') or 'unknown', 'city': row.get('city') or None,
            'distance_m': _number(row.get('distance_m')), 'geo_basis': row.get('geo_basis') or 'UNVERIFIED',
            'area_note': row['area_note'], 'balcony': _number(row['balcony']),
            'verification': 'RESEARCH_REPORTED_NOT_INDEPENDENTLY_VERIFIED',
            'additional_source_check': next((c for c in (checks or {}).get('checks', []) if c['url'] == url), None),
            'research_eligible': row['status'] == 'מועמד מחקר בלבד', 'decision_price_eligible': False,
            'limitations': row['qual'], 'availability': row['status'], 'possible_duplicate_url': row['other'] or None,
            'original_record': row})
    sensitivity = []
    conflicted_ids = {s['id'] for s in sales if s['verification'] == 'EXTERNAL_PRICE_CONFLICT_UNRESOLVED'}
    for result in bundle['results']:
      for sale_id in sorted(conflicted_ids & {e['observation_id'] for e in result.get('evidence', [])}):
        kept = [e for e in result['evidence'] if e['observation_id'] != sale_id]
        parcels = {(sale_map[e['observation_id']]['block'], sale_map[e['observation_id']]['parcel'])
                   for e in kept if e['observation_id'] in sale_map and sale_map[e['observation_id']]['block'] and sale_map[e['observation_id']]['parcel']}
        cfg = bundle['config']
        enough = len(kept) >= cfg['min_comps'] and len(parcels) >= cfg['min_parcels']
        vals = [e['adjusted_ppm'] for e in kept]
        ppm = (median(vals) if cfg['method'] == 'median' else weighted_median(vals, [e['score'] for e in kept])) if enough else None
        value = round(ppm * result['built_area']) if ppm is not None else None
        sensitivity.append({'sale_id': sale_id, 'apartment_id': result['apartment_id'], 'baseline_value': result['estimated_market_value'],
                            'without_sale_value': value, 'delta': value-result['estimated_market_value'] if value is not None else None,
                            'remaining_count': len(kept), 'remaining_parcels': len(parcels), 'backfill': False})
    return {'kind': 'ILLUSTRATIVE_COMPARISON', 'source_hashes': hashes,
            'disclaimer': 'Research only; no current availability or redistribution rights verified; not decision eligible.',
            'sales': sales, 'offers': offers, 'source_checks': checks, 'conflict_sensitivity': sensitivity}
