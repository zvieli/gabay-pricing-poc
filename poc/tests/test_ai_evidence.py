"""Exercise real pricing and AI processing with a controlled external API boundary."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from gabay_pricing.ai_analysis import analyze_bundle

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT / 'data/snapshots/market-live.json').is_file() or not (ROOT.parent / 'DATA/חוברת1.xlsx').is_file(), reason='private snapshot and workbook not supplied')
def test_ai_claims_are_checked_against_its_evidence_pack(tmp_path):
    subprocess.run([sys.executable, '-m', 'gabay_pricing.cli', 'report', '--snapshot',
                    str(ROOT / 'data/snapshots/market-live.json'), '--apartments', '5',
                    '--reports', str(tmp_path)], cwd=ROOT, check=True, capture_output=True)
    bundle = json.loads((tmp_path / 'pricing_results.json').read_text())

    def external_service(request):
        body = json.loads(request.content)
        if 'claims' in body['messages'][1]['content']:
            context = json.loads(body['messages'][1]['content'])
            assert all(isinstance(v, list) and len(v) == 4 for v in context['facts'].values())
            assert set(context['facts']) == {ref for claim in context['claims'] for ref in claim['references']}
            claims = [{'claim_id': c['id'], 'verdict': 'CONTRADICTED' if i == 0 else 'INSUFFICIENT_EVIDENCE',
                       'reason': 'requires review', 'supporting_fact_ids': [],
                       'contradicting_fact_ids': c['references'][:1] if i == 0 else [], 'suggested_correction': ''}
                      for i, c in enumerate(context['claims'])]
            result = {'claims': claims, 'quality': {'unit_specificity': '', 'strategy_comparison': '',
                      'preference_grounding': '', 'abstention': '', 'actions_usefulness': ''}}
        else:
            refs = body['response_format']['json_schema']['schema']['properties']['evidence_refs']['items']['enum']
            result = {'assessment': 'הערכה דורשת בדיקה מעמיקה',
                      'strategy_comparison': {'quick_sale': 'חלופה דורשת בדיקה', 'balanced': 'חלופה דורשת בדיקה',
                                              'high_opening': 'חלופה דורשת בדיקה'},
                      'preferred_scenario': 'abstain', 'reasoning': 'נדרש מידע נוסף להכרעה',
                      'evidence_refs': list(dict.fromkeys([refs[0], 'VALUE'])), 'missing_information': 'נדרש אימות נתונים', 'next_steps': 'לבדוק ראיות מקוריות',
                      'quantitative_claims': [{'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value', 'conclusion': 'reported_value'}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})

    original_results = copy.deepcopy(bundle['results'])
    progress = []
    ai = analyze_bundle(bundle, api_key='test-only', max_units=1, transport=httpx.MockTransport(external_service),
                        progress=lambda completed, total: progress.append((completed, total)))
    assert progress == [(1, 1)]
    result = ai['units']['5']
    assert result['status'] == 'VALIDATED'
    assert result['claim_review']['status'] == 'SUCCESS'
    assert any(c['verdict'] == 'CONTRADICTED' for c in result['claim_review']['review']['claims'])
    assert bundle['results'] == original_results
    assert bundle['results'][0]['pricing_decision']['approval'] == 'NOT_APPROVED'
    from gabay_pricing.reporting import write_reports
    bundle['ai_analysis'] = ai
    write_reports(bundle, tmp_path / 'reviewed')
    report = (tmp_path / 'reviewed/MARKETING_DASHBOARD.html').read_text()
    assert 'טענות שסותרות את הראיות או דורשות בדיקה' in report
    assert 'סטטוס בודק הטענות: SUCCESS' in report
    assert 'CONTRADICTED' in report
    assert result['analysis']['assessment'] not in report
    assert ai['status'] == 'PARTIAL'

    def critic_unavailable(request):
        if 'claims' in json.loads(request.content)['messages'][1]['content']:
            return httpx.Response(503, json={'error': {'message': 'unavailable'}})
        return external_service(request)
    failed_review = analyze_bundle(bundle, api_key='test-only', max_units=1,
                                   transport=httpx.MockTransport(critic_unavailable))
    assert failed_review['units']['5']['claim_review']['status'] == 'API_ERROR'
    assert failed_review['status'] == 'PARTIAL'
    bundle['ai_analysis'] = failed_review
    write_reports(bundle, tmp_path / 'critic-unavailable')
    report = (tmp_path / 'critic-unavailable/MARKETING_DASHBOARD.html').read_text()
    assert 'כל הטענות דורשות אימות אנושי' in report
    assert 'סטטוס בודק הטענות: API_ERROR' in report

    def unsupported_number(request):
        original = external_service(request).json()
        content = json.loads(original['choices'][0]['message']['content'])
        content['assessment'] = 'נמצאו 10 נכסים באזור'
        original['choices'][0]['message']['content'] = json.dumps(content)
        return httpx.Response(200, json=original)
    unsafe = analyze_bundle(bundle, api_key='test-only', max_units=1,
                            transport=httpx.MockTransport(unsupported_number))
    assert unsafe['units']['5']['status'] == 'VALIDATION_FAILED'
    bundle['ai_analysis'] = unsafe
    write_reports(bundle, tmp_path / 'validation-failed')
    report = (tmp_path / 'validation-failed/MARKETING_DASHBOARD.html').read_text()
    assert 'Numeric claims must come from the pricing engine' in report
    assert 'סטטוס בודק הטענות: NOT_RUN' in report
