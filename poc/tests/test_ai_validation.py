"""Analyst validation through the public AI boundary with synthetic evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from gabay_pricing.ai_analysis import analyze_bundle, _context, _validate


def synthetic_bundle():
    scenarios = {k: {'suggested_asking_price': 1000000, 'indicative_asking_price': None}
                 for k in ('quick_sale', 'balanced', 'high_opening')}
    pack = {'unit_id': '1', 'unit': {'property_type': 'apartment'},
            'valuation': {'pricing_classification': 'EVIDENCE_SUPPORTED'},
            'decision': {}, 'policy': {}, 'adjustments': {}, 'scenarios': scenarios,
            'selected_sales': [], 'eligible_offers': [], 'excluded_offers': []}
    return {'apartments': [{'id': '1', 'source_rows': [2]}],
            'results': [{'apartment_id': '1', 'property_type': 'apartment', 'rooms': 3,
                         'built_area': 80, 'pricing_decision': {'approval': 'NOT_APPROVED'}}],
            'config': {}, 'evidence_packs': {'1': pack}}


def response(assessment):
    return {'assessment': assessment, 'strategy_comparison': {
        'quick_sale': 'חלופה ראויה לבדיקה', 'balanced': 'חלופה ראויה לבדיקה',
        'high_opening': 'חלופה ראויה לבדיקה'}, 'preferred_scenario': 'abstain',
        'reasoning': 'נדרש מידע נוסף להכרעה', 'evidence_refs': ['VALUE'],
        'missing_information': 'נדרש אימות נתונים', 'next_steps': 'לבדוק ראיות מקוריות'}


@pytest.mark.parametrize('assessment', ['מצד אחד קיימת אי ודאות ומצד שני נדרש אימות',
                                        'כל אחד מהתרחישים דורש בחינה עסקית',
                                        'אחת האפשרויות דורשת בדיקה נוספת'])
def test_qualitative_hebrew_is_not_rejected_as_numeric(assessment):
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'unavailable'}})
        content = response(assessment)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'


@pytest.mark.parametrize('assessment', ['מחיר הדירה הוא 2500000 שקלים'])
def test_quantitative_claims_are_rejected_with_redacted_diagnostics(assessment):
    diagnostics = []
    def api(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(response(assessment))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert result['units']['1']['status'] == 'VALIDATION_FAILED'
    assert result['units']['1']['claim_review']['status'] == 'NOT_RUN'
    assert diagnostics[0]['field'] == 'assessment'
    assert diagnostics[0]['rule'] == 'price_type_mismatch'
    assert '2500000' not in json.dumps(diagnostics)


@pytest.mark.parametrize('classification,suggested', [('EVIDENCE_SUPPORTED', None), ('INDICATIVE_SCENARIO', 1000000)])
def test_indicative_only_scenario_cannot_be_preferred(classification, suggested):
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['valuation']['pricing_classification'] = classification
    bundle['evidence_packs']['1']['scenarios']['balanced'] = {'suggested_asking_price': suggested, 'indicative_asking_price': 1000000}
    def api(request):
        data = response('המידע מצריך בדיקה נוספת')
        data['preferred_scenario'] = 'balanced'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(data)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATION_FAILED'


def test_inline_numbered_actions_are_formatting_not_market_claims():
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        output = response('המידע דורש בדיקה נוספת')
        output['next_steps'] = '1. לבצע ביקורת שטח פיזית. 2. לאסוף עסקאות מתאימות.'
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'


def test_rate_limit_retry_respects_retry_after():
    calls = []
    def api(request):
        body = json.loads(request.content)
        kind = body['response_format']['json_schema']['name']
        calls.append(kind)
        if kind == 'evidence_critic':
            return httpx.Response(503, request=request)
        if calls.count(kind) == 1:
            return httpx.Response(429, request=request, headers={'retry-after': '0'})
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(response('נדרש אימות נוסף של הראיות'))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert calls.count('pricing_analyst') == 2


def test_two_bounded_rate_limits_around_a_corrective_retry_are_honored():
    analyst_attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        analyst_attempts.append(1)
        if len(analyst_attempts) in (1, 3):
            return httpx.Response(429, request=request, headers={'retry-after': '0'})
        text = 'מחיר הנכס הוא 9999999 שקלים' if len(analyst_attempts) == 2 else 'הנתונים דורשים בדיקה נוספת'
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(response(text))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert len(analyst_attempts) == 4


def test_uncited_reference_gets_one_targeted_corrective_retry():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['unit']['built_area'] = 70.8
    attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request, json={'error': {'message': 'offline'}})
        attempts.append(1)
        output = response('הניתוח דורש אימות נוסף')
        output['evidence_refs'] = ['COVERAGE'] if len(attempts) == 1 else ['UNIT']
        output['quantitative_claims'] = [{'claim_type': 'built_area', 'reference': 'UNIT.built_area', 'conclusion': 'reported_value'}]
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert len(attempts) == 2


def test_second_numeric_failure_gets_one_strict_corrective_retry():
    attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        attempts.append(body['messages'][-1]['content'] if len(body['messages']) > 2 else '')
        if len(attempts) < 3:
            return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(response('נפסלו 3 הצעות בבדיקה'))}}]})
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(response('הנתונים דורשים בדיקה נוספת'))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert len(attempts) == 3
    assert 'אסור שתופיע אף ספרה' in attempts[2]


def test_two_consecutive_rate_limits_are_retried_within_bound():
    attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        attempts.append(1)
        if len(attempts) <= 2:
            return httpx.Response(429, request=request, headers={'retry-after': '0'})
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(response('נדרש אימות נוסף של הראיות'))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert len(attempts) == 3


def test_rate_limit_after_corrective_retry_is_honored():
    analyst_attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        analyst_attempts.append(1)
        if len(analyst_attempts) == 1:
            data = response('מחיר הנכס הוא 9999999 שקלים')
        elif len(analyst_attempts) == 2:
            return httpx.Response(429, request=request, headers={'retry-after': '0'})
        else:
            data = response('הנתונים דורשים בדיקה נוספת')
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(data)}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert len(analyst_attempts) == 3


def test_correction_keeps_the_matched_typed_evidence_reference():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['decision']['excluded_offer_count'] = 3
    requests = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        requests.append(body)
        output = response('נפסלו 3 הצעות בבדיקה')
        output['evidence_refs'] = ['COVERAGE']
        if len(requests) > 1:
            output['quantitative_claims'] = [{'claim_type': 'offer_count', 'reference': 'COVERAGE.excluded_offers_total', 'conclusion': 'reported_value'}]
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert 'COVERAGE.excluded_offers_total' in requests[1]['messages'][-1]['content']
    assert result['units']['1']['analysis']['quantitative_claims'][0]['reference'] == 'COVERAGE.excluded_offers_total'


def test_unstructured_numeric_failure_gets_one_bounded_corrective_retry():
    calls = []
    def api(request):
        body = json.loads(request.content)
        kind = body['response_format']['json_schema']['name']
        calls.append(kind)
        if kind == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'offline'}})
        text = 'מחיר הנכס הוא 9999999 שקלים' if calls.count(kind) == 1 else 'נדרש אימות נוסף של הראיות'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(response(text))}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert calls.count('pricing_analyst') == 2
    assert calls.count('evidence_critic') == 2


def test_rate_limit_diagnostics_do_not_store_provider_account_identifiers():
    diagnostics = []
    def api(request):
        return httpx.Response(429, request=request, headers={'retry-after': '0'}, json={'error': {'code': 'rate_limit_exceeded',
            'message': 'organization org_secret_account on output tokens per day (OTPD): Limit 100000, Used 99999, Requested 5'}})
    analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                   transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert 'org_secret_account' not in json.dumps(diagnostics)
    assert diagnostics[0]['code'] == 'rate_limit_exceeded'
    assert diagnostics[0]['limit_kind'] == 'OTPD'
    assert diagnostics[0]['limit'] == 100000


def test_gateway_diagnostics_keep_history_of_earlier_attempts():
    diagnostics = []
    attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        attempts.append(1)
        if len(attempts) == 1:
            data = response('מחיר הנכס הוא 9999999 שקלים')
            return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(data)}}]})
        return httpx.Response(429, request=request, headers={'retry-after': '900'})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert result['units']['1']['status'] == 'API_ERROR'
    assert diagnostics[0]['type'] == 'local_validation'
    assert [x['category'] for x in diagnostics[0]['retry_history']] == ['success']
    assert diagnostics[1]['type'] == 'gateway'
    assert [x['category'] for x in diagnostics[1]['history']] == ['success', 'rate_limit']
    assert [x['category'] for x in result['units']['1']['retry_history']] == ['success', 'rate_limit']


def test_provider_schema_errors_do_not_store_generated_sensitive_text():
    diagnostics = []
    def api(request):
        return httpx.Response(400, request=request, json={'error': {'code': 'json_validate_failed',
            'message': "Error '/assessment' does not validate with /properties/assessment/pattern: secret 1234567"}})
    analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                   transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert '1234567' not in json.dumps(diagnostics)
    assert 'secret' not in json.dumps(diagnostics)
    assert diagnostics[0]['code'] == 'json_validate_failed'
    assert diagnostics[0]['schema_path'] == '/assessment'


def saved_analysis():
    pack = synthetic_bundle()['evidence_packs']['1']
    return _validate(response('המידע דורש בדיקה נוספת'), pack, _context(pack)[1])


def test_reused_unit_keeps_insufficient_evidence_without_new_requests():
    previous = {'1': {'status': 'VALIDATED', 'provider': 'groq', 'model': 'openai/gpt-oss-20b',
        'analysis': saved_analysis(),
        'claim_review': {'status': 'SUCCESS', 'provider': 'groq',
                         'review': {'claims': [{'verdict': 'INSUFFICIENT_EVIDENCE'}]}}}}
    def api(request):
        raise AssertionError('reviewed unit must not trigger a request')
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), previous_units=previous)
    assert result['units']['1']['reused'] is True
    assert result['calls'] == 0


def test_previously_successful_unit_is_not_retried():
    previous = {'1': {'status': 'VALIDATED', 'provider': 'groq', 'model': 'openai/gpt-oss-20b',
        'analysis': saved_analysis(),
        'claim_review': {'status': 'SUCCESS', 'provider': 'groq', 'review': {'claims': [{'verdict': 'SUPPORTED'}]}}}}
    def api(request):
        raise AssertionError('successful unit must not trigger a request')
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), previous_units=previous)
    assert result['units']['1']['reused'] is True
    assert result['calls'] == 0


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / 'data/snapshots/market-live.json').is_file() or not (Path(__file__).resolve().parents[2] / 'DATA/חוברת1.xlsx').is_file(), reason='private snapshot and workbook not supplied')
def test_cli_reports_analyst_and_critic_status(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-m', 'gabay_pricing.cli', 'report', '--snapshot',
                             str(root / 'data/snapshots/market-live.json'), '--reports', str(tmp_path),
                             '--apartments', '1', '--ai', '--ai-max-units', '1'],
                            cwd=root, env={**os.environ, 'GROQ_API_KEY': ''},
                            check=True, capture_output=True, text=True)
    summary = json.loads(result.stdout)
    assert summary['ai_status'] == 'UNAVAILABLE'
    assert summary['ai_validated'] == 0
    assert summary['ai_critic_statuses'] == {'NOT_RUN': 1}
