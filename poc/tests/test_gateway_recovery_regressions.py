import copy
import json

import httpx
import pytest

from gabay_pricing.ai_analysis import analyze_bundle
from gabay_pricing.llm_gateway import GatewayError, LLMGateway
from gabay_pricing.reporting import carry_over_previous_ai
from test_persistence import make_bundle, ai_meta, QuotaGateway
from test_ai_validation import synthetic_bundle, response


def test_daily_quota_then_bad_request_is_not_route_quota():
    def handler(request):
        if 'googleapis' in str(request.url):
            return httpx.Response(429, request=request, json={'error': {'message': 'retry in 8h'}})
        return httpx.Response(400, request=request, json={'error': {'message': 'bad request'}})
    g = LLMGateway(credentials={'gemini': 'x', 'groq': 'x'}, allowed={'gemini', 'groq'},
                   routes={'analyst': [('gemini', 'gemini-3.1-flash-lite'), ('groq', 'openai/gpt-oss-20b')]},
                   transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError) as error:
        g.complete('analyst', messages=[], schema={'type': 'object'}, max_tokens=10)
    assert error.value.category == 'request_error'
    assert [h['category'] for h in error.value.history] == ['quota_exhausted', 'request_error']


@pytest.mark.parametrize('changed', ['results', 'evidence_packs'])
def test_carry_over_rejects_changed_calculation_or_evidence(tmp_path, changed):
    bundle = make_bundle()
    prior = copy.deepcopy(bundle)
    prior['ai_analysis'] = ai_meta()
    (tmp_path / 'pricing_results.json').write_text(json.dumps(prior), encoding='utf-8')
    if changed == 'results':
        bundle['results'][0]['estimated_market_value'] = 123
    else:
        bundle['evidence_packs']['1']['valuation']['estimated_market_value'] = 123
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'units': {}}
    carry_over_previous_ai(bundle, tmp_path)
    assert bundle['ai_analysis']['status'] == 'NOT_RUN'


def test_failed_checkpoint_stops_before_next_call_and_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / 'durable.json'
    path.write_text('{"previous": true}', encoding='utf-8')
    g = QuotaGateway()
    diagnostics = []
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('gabay_pricing.ai_analysis.os.replace', fail)
    with pytest.raises(OSError, match='checkpoint write failed'):
        analyze_bundle(make_bundle(), api_key='unused', gateway=g, max_units=3,
                       durable_path=path, diagnostics=diagnostics)
    assert g.requests == 1
    assert json.loads(path.read_text()) == {'previous': True}
    assert diagnostics[-1]['type'] == 'checkpoint_write_failed'


def test_missing_groq_key_falls_back_to_authorized_gemini():
    def handler(request):
        return httpx.Response(200, request=request, json={'id': 'x', 'object': 'chat.completion',
            'created': 1, 'model': 'gemini-3.1-flash-lite', 'choices': [{'index': 0,
            'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}]})
    g = LLMGateway(credentials={'gemini': 'x'}, allowed={'gemini', 'groq'},
                   routes={'analyst': [('groq', 'openai/gpt-oss-20b'), ('gemini', 'gemini-3.1-flash-lite')]},
                   transport=httpx.MockTransport(handler))
    result = g.complete('analyst', messages=[], schema={'type': 'object'}, max_tokens=10)
    assert result.provider == 'gemini'
    assert result.history[0]['category'] == 'authentication'


@pytest.mark.parametrize('corrective_retry', [False, True])
def test_critic_falls_back_after_analyst_success(corrective_retry):
    calls = []
    analyst = response('המידע דורש בדיקה נוספת')

    def handler(request):
        body = json.loads(request.content)
        role = body['response_format']['json_schema']['name']
        provider = 'groq' if 'groq' in request.url.host else 'gemini'
        calls.append((provider, role))
        if provider == 'groq' and role == 'evidence_critic' and (not corrective_retry or calls.count((provider, role)) > 1):
            return httpx.Response(429, request=request, headers={'retry-after': '3600'},
                                  json={'error': {'code': 'rate_limit_exceeded', 'message': 'TPD limit'}})
        if role == 'pricing_analyst':
            output = analyst
        else:
            claims = json.loads(body['messages'][1]['content'])['claims']
            output = {'claims': [{'claim_id': c['id'], 'verdict': 'INSUFFICIENT_EVIDENCE',
                       'reason': 'insufficient', 'supporting_fact_ids': [], 'contradicting_fact_ids': [],
                       'suggested_correction': ''} for c in claims],
                      'quality': {k: 'reviewed' for k in ('unit_specificity', 'strategy_comparison',
                                  'preference_grounding', 'abstention', 'actions_usefulness')}}
            if provider == 'groq':
                output['claims'][0]['supporting_fact_ids'] = ['NOT_ALLOWED']
        model = 'openai/gpt-oss-20b' if provider == 'groq' else 'gemini-3.1-flash-lite'
        return httpx.Response(200, request=request, json={'id': 'x', 'object': 'chat.completion',
            'created': 1, 'model': model, 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': json.dumps(output)}}]})

    g = LLMGateway(credentials={'groq': 'x', 'gemini': 'x'}, allowed={'groq', 'gemini'},
                   transport=httpx.MockTransport(handler), max_requests=5)
    result = analyze_bundle(synthetic_bundle(), api_key='x', gateway=g, max_units=1)
    unit = result['units']['1']
    assert calls == [('groq', 'pricing_analyst')] + [('groq', 'evidence_critic')] * (2 if corrective_retry else 1) + [('gemini', 'evidence_critic')]
    assert unit['status'] == 'VALIDATED' and unit['analysis']['assessment'] == analyst['assessment']
    assert (unit['provider'], unit['model']) == ('groq', 'openai/gpt-oss-20b')
    review = unit['claim_review']
    assert (review['status'], review['provider'], review['model']) == ('SUCCESS', 'gemini', 'gemini-3.1-flash-lite')
    assert review['review']['claims'] and all(c['verdict'] == 'INSUFFICIENT_EVIDENCE' for c in review['review']['claims'])
    assert [h['category'] for h in review['retry_history']] == (['success', 'validation_failed'] if corrective_retry else []) + ['quota_exhausted', 'success']


def test_both_daily_breakers_prevent_further_requests():
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(429, request=request, headers={'retry-after': '3600'},
                              json={'error': {'code': 'rate_limit_exceeded', 'message': 'TPD limit'}})
    g = LLMGateway(credentials={'groq': 'x', 'gemini': 'x'}, allowed={'groq', 'gemini'},
                   transport=httpx.MockTransport(handler), max_requests=5, clock=lambda: 1000)
    for _ in range(2):
        with pytest.raises(GatewayError) as error:
            g.complete('critic', messages=[], schema={'type': 'object'}, max_tokens=10)
        assert error.value.category == 'quota_exhausted'
    assert calls == ['api.groq.com', 'generativelanguage.googleapis.com']
