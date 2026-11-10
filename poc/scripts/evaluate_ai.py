"""Run optional Groq critic on saved results; never runs the pricing pipeline."""
import argparse
from collections import Counter
import copy
import hashlib
import json
import re
from pathlib import Path

from dotenv import dotenv_values

from gabay_pricing.ai_evaluation import collect_facts, evaluate
from gabay_pricing.ai_analysis import _context, _validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saved', type=Path, required=True)
    parser.add_argument('--cases', type=Path, default=Path('tests/fixtures/ai_evaluation_cases.json'))
    parser.add_argument('--unit', default='5')
    parser.add_argument('--case-ids', required=True, help='Comma-separated evaluation case IDs, at most four')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true', help='Compare historical regex and deterministic guard only; no LLM call')
    args = parser.parse_args()
    selected = set(args.case_ids.split(','))
    if not 1 <= len(selected) <= (20 if args.offline else 4):
        parser.error('Choose one to four cases for a live call')
    bundle = json.loads(args.saved.read_text(encoding='utf-8'))
    pack = bundle['evidence_packs'][args.unit]
    analysis = bundle['ai_analysis']['units'][args.unit]['analysis']
    facts = collect_facts(pack, analysis)
    cases = [x for x in json.loads(args.cases.read_text(encoding='utf-8')) if x['unit_id'] == args.unit and x['id'] in selected]
    if {x['id'] for x in cases} != selected:
        parser.error('Unknown case ID or case from a different unit')
    for case in cases:
        match = next((k for k, v in facts.items() if v['record'] == case['fact_record'] and v['path'] == case['fact_path']), None)
        if not match:
            parser.error('Case fact absent from saved evidence pack: ' + case['id'])
        case['references'] = [match]
    def clean(v, action=False):
        if isinstance(v, list):
            return [clean(x, action) for x in v]
        if isinstance(v, dict):
            out = {k: clean(x, action or k == 'actions') for k, x in v.items() if k not in {'supporting_facts', 'limitations', 'evidence_ids', 'verification', 'kind'}}
            if 'text' in out and 'references' in out:
                out.setdefault('limitation_refs', [])
                out.setdefault('uncertainty', '')
                out.pop('quantitative_refs', None)
            return out
        return v
    def legacy_regex(text):
        # Historical guard, replayed only in this evaluation, not in pricing.
        numeric = r'\d|[₪$€%]|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million)\b|(?<![\w])(?:אחד|אחת|שניים|שתיים|שלושה|שלוש|ארבעה|ארבע|חמישה|חמש|מיליון|אלף)(?![\w])'
        construction = (re.search(r'\b(?:all|every)\b', text, re.I) and re.search(r'off.plan|future.building', text, re.I)
                        and (not pack['selected_sales'] or any(not any('off-plan' in w.lower() or 'future-building' in w.lower() for w in s['limitations']) for s in pack['selected_sales'])))
        competitors = re.search(r'\bno (?:current )?compatible (?:sale )?(?:offers|listings|listing data)', text, re.I) and pack['decision'].get('eligible_offer_count', 0)
        return bool(re.search(numeric, text, re.I) or construction or competitors
                    or re.search(r'\b(?:approved price|price is approved|has been approved)\b|מחיר מאושר|המחיר אושר', text, re.I)
                    or re.search(r'\bno\b.{0,100}\b(?:sales?|transactions?|listings?)\b.{0,100}\b(?:in the (?:same )?area|on the market)\b', text, re.I))
    _, refs = _context(pack)
    for case in cases:
        candidate = clean(copy.deepcopy(analysis))
        if 'assessment' not in candidate:
            case['deterministic'] = 'NOT_COMPARABLE_LEGACY_SCHEMA'
        else:
            candidate['assessment'] = case['text']
            candidate['evidence_refs'] = [case['fact_record']]
            try:
                _validate(candidate, pack, refs)
                case['deterministic'] = 'ACCEPTED_UNVERIFIED'
            except ValueError as exc:
                case['deterministic'] = 'NOT_COMPARABLE_SAVED_CONTEXT' if str(exc) == 'Unknown evidence reference' else 'REJECTED'
                case['deterministic_reason'] = str(exc)[:200]
        case['legacy_regex'] = 'REJECTED' if legacy_regex(case['text']) else 'ACCEPTED_UNVERIFIED'
    if args.offline:
        result = {'status': 'OFFLINE_ONLY'}
    else:
        key = dotenv_values(Path(__file__).resolve().parents[2] / '.env').get('GROQ_API_KEY')
        result = evaluate(pack, analysis, cases, api_key=key)
    result['cases'] = cases
    result['saved_path'] = str(args.saved)
    result['saved_sha256'] = hashlib.sha256(args.saved.read_bytes()).hexdigest()
    result['cases_sha256'] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    if result['status'] == 'SUCCESS':
        expected = {x['id']: x['expected'] for x in cases}
        result['comparison'] = dict(Counter(f"{expected[x['claim_id']]} -> {x['verdict']}" for x in result['review']['claims']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'http_status': result.get('http_status'), 'comparison': result.get('comparison'), 'usage': result.get('usage')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
