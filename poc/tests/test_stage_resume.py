import copy
import json
import sys

import httpx
import pytest

from gabay_pricing.ai_analysis import _context, _validate, analyze_bundle
from gabay_pricing.llm_gateway import LLMGateway
from test_ai_validation import response, synthetic_bundle
from test_persistence import real_bundle, SNAPSHOT, WORKBOOK


def saved_unit():
    pack = synthetic_bundle()['evidence_packs']['1']
    analysis = _validate(response('המידע דורש בדיקה נוספת'), pack, _context(pack)[1])
    return {'status': 'VALIDATED', 'provider': 'gemini', 'model': 'gemini-3.1-flash-lite',
            'analysis': analysis, 'claim_review': {'status': 'API_ERROR'}, 'retry_history': []}


def critic_answer(request):
    body = json.loads(request.content)
    assert body['response_format']['json_schema']['name'] == 'evidence_critic'
    claims = json.loads(body['messages'][1]['content'])['claims']
    output = {'claims': [{'claim_id': c['id'], 'verdict': 'INSUFFICIENT_EVIDENCE', 'reason': 'insufficient',
                          'supporting_fact_ids': [], 'contradicting_fact_ids': [], 'suggested_correction': ''} for c in claims],
              'quality': {k: 'reviewed' for k in ('unit_specificity', 'strategy_comparison', 'preference_grounding',
                                                   'abstention', 'actions_usefulness')}}
    model = body['model']
    return httpx.Response(200, request=request, json={'id': 'x', 'object': 'chat.completion', 'created': 1,
        'model': model, 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': json.dumps(output)}}]})


def run_resume(handler, prior, path=None):
    gateway = LLMGateway(credentials={'groq': 'x', 'gemini': 'x'}, allowed={'groq', 'gemini'},
                         transport=httpx.MockTransport(handler), max_requests=4, max_wait=0)
    result = analyze_bundle(synthetic_bundle(), api_key='x', gateway=gateway, max_units=1,
                            previous_units={'1': prior}, durable_path=path)
    return result, gateway


def test_resume_analyst_calls_only_critic_with_fallback(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.url.host, json.loads(request.content)['response_format']['json_schema']['name']))
        if 'groq' in request.url.host:
            return httpx.Response(429, request=request, headers={'retry-after': '3600'},
                                  json={'error': {'code': 'rate_limit_exceeded', 'message': 'TPD limit'}})
        return critic_answer(request)
    prior = saved_unit()
    path = tmp_path / 'durable.json'
    result, gateway = run_resume(handler, prior, path)
    unit = result['units']['1']
    assert calls == [('api.groq.com', 'evidence_critic'), ('generativelanguage.googleapis.com', 'evidence_critic')]
    assert gateway.requests == 2 and unit['reused'] and unit['analysis'] == prior['analysis']
    assert (unit['provider'], unit['model']) == ('gemini', 'gemini-3.1-flash-lite')
    review = unit['claim_review']
    assert (review['status'], review['provider'], review['model']) == ('SUCCESS', 'gemini', 'gemini-3.1-flash-lite')
    assert all(c['verdict'] == 'INSUFFICIENT_EVIDENCE' for c in review['review']['claims'])
    assert json.loads(path.read_text())['ai_analysis']['units'] == result['units']


def test_critic_quota_preserves_analyst_durably(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(429, request=request, headers={'retry-after': '3600'},
                              json={'error': {'code': 'rate_limit_exceeded', 'message': 'TPD limit'}})
    prior = saved_unit()
    path = tmp_path / 'durable.json'
    result, gateway = run_resume(handler, prior, path)
    assert len(calls) == gateway.requests == 2
    assert result['stopped_reason'] == 'quota_exhausted'
    unit = json.loads(path.read_text())['ai_analysis']['units']['1']
    assert unit['status'] == 'VALIDATED' and unit['analysis'] == prior['analysis']
    assert unit['claim_review']['error_category'] == 'quota_exhausted'


def test_successful_review_requires_no_calls():
    prior = saved_unit()
    prior['claim_review'] = {'status': 'SUCCESS', 'provider': 'gemini', 'review': {'claims': [{'verdict': 'SUPPORTED'}]}}
    result, gateway = run_resume(lambda r: pytest.fail('unexpected API call'), prior)
    assert gateway.requests == 0 and result['units']['1']['reused']


def test_tampered_saved_analysis_never_reaches_critic():
    prior = saved_unit()
    prior['analysis']['assessment'] = 'מחיר 999999999 שקלים'
    seen = []
    def handler(request):
        seen.append(json.loads(request.content)['response_format']['json_schema']['name'])
        return httpx.Response(400, request=request)
    result, _ = run_resume(handler, prior)
    assert seen == ['pricing_analyst'] and result['units']['1']['status'] != 'VALIDATED'


@pytest.mark.parametrize('changed', ['results', 'evidence_packs'])
def test_cli_never_reuses_changed_result_or_pack(tmp_path, monkeypatch, changed):
    import gabay_pricing.cli as cli
    import gabay_pricing.ai_analysis as analysis
    bundle = real_bundle(tmp_path)
    prior = copy.deepcopy(bundle)
    aid = str(prior['results'][0]['apartment_id'])
    placeholder = str(prior['results'][1]['apartment_id'])
    prior['ai_analysis'] = {'contract_version': analysis.AI_CONTRACT_VERSION, 'prompt_version': 'business-analyst-v1',
                            'model': analysis.MODEL, 'units': {aid: saved_unit(), placeholder: {'reused': True}}}
    if changed == 'results':
        prior['results'][0]['estimated_market_value'] = -1
    else:
        prior['evidence_packs'][aid]['valuation']['estimated_market_value'] = -1
    path = tmp_path / 'prior.json'
    path.write_text(json.dumps(prior), encoding='utf-8')
    captured = {}
    def fake_analysis(bundle, **kwargs):
        captured.update(kwargs['previous_units'])
        return {'status': 'FAILED', 'units': {}, 'calls': 0}
    monkeypatch.setattr(analysis, 'analyze_bundle', fake_analysis)
    monkeypatch.setattr(cli, 'write_reports', lambda *args: None)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    monkeypatch.setattr(sys, 'argv', ['gabay-pricing', 'report', '--snapshot', str(SNAPSHOT), '--workbook', str(WORKBOOK),
                                     '--reports', str(tmp_path / 'out'), '--ai', '--resume-ai', str(path)])
    cli.main()
    assert not captured.get(aid)
    assert placeholder not in captured
