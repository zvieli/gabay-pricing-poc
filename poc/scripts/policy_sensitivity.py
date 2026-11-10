"""Replay the same snapshot with explicit, uncalibrated policy sensitivities."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from gabay_pricing.cli import price
from gabay_pricing.models import PricingConfig, Snapshot


def run(snapshot_path: Path, workbook: Path, policy_path: Path):
    snapshot = Snapshot.model_validate_json(snapshot_path.read_text(encoding='utf-8'))
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    if 'as_of' in policy and policy['as_of'] != str(snapshot.as_of):
        raise ValueError('Policy date conflicts with snapshot')
    variants = {
        'active': {},
        'floor_zero': {'unit_floor_rate': 0},
        'floor_two_percent': {'unit_floor_rate': .02},
        'floor_and_orientation_zero': {'unit_floor_rate': 0, 'unit_orientation_rates': {k: 0 for k in policy.get('unit_orientation_rates', {})}},
        'verified_area_only': {'allow_reported_sale_area': False},
    }
    output = {}
    for name, changes in variants.items():
        config = PricingConfig.model_validate({**policy, **changes, 'as_of': snapshot.as_of})
        bundle = price(snapshot, workbook, config, snapshot_path)
        rows = bundle['results']
        asks = [r['marketing_strategies'].get('balanced', {}).get('suggested_asking_price') for r in rows]
        output[name] = {
            'changes': changes,
            'unit_count': len(rows),
            'tiers': dict(Counter(r['tier'] for r in rows)),
            'balanced_ask_count': sum(x is not None for x in asks),
            'balanced_ask_total': sum(x for x in asks if x is not None),
            'units': {str(r['apartment_id']): {'property_type': r['property_type'], 'tier': r['tier'],
                'estimated_market_value': r.get('estimated_market_value'), 'adjusted_market_value': r.get('adjusted_market_value'),
                'balanced_scenario': (r.get('marketing_strategies') or {}).get('balanced', {}).get('suggested_asking_price'),
                'balanced_indicative': (r.get('marketing_strategies') or {}).get('balanced', {}).get('indicative_asking_price'),
                'approval': (r.get('pricing_decision') or {}).get('approval'),
                'selected_sale_ids': [e['observation_id'] for e in r.get('evidence') or []]} for r in rows},
            'workbook_sha256': bundle['workbook_sha256'],
            'snapshot_sha256': bundle['snapshot_sha256'],
            'snapshot_file_sha256': hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            'policy_file_sha256': hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            'code_sha256': bundle['code_sha256'],
        }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--workbook', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot, args.workbook, args.policy), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
