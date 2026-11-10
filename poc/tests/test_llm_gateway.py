"""Gateway policy at the external-provider seam; not live acceptance."""
import json
import httpx
import pytest

from gabay_pricing.llm_gateway import LLMGateway, GatewayError


def answer(request):
    return httpx.Response(200, request=request, json={'id': 'x', 'object': 'chat.completion',
        'created': 1, 'model': 'test-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': '{"ok":true}'}}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})


def gateway(handler, **kwargs):
    return LLMGateway(credentials={'groq': 'test-groq', 'gemini': 'test-gemini'},
        allowed={'groq', 'gemini'}, max_requests=kwargs.pop('max_requests', 5),
        transport=httpx.MockTransport(handler), **kwargs)


def test_rate_limit_routes_only_to_verified_authorized_fallback():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if 'groq' in request.url.host:
            return httpx.Response(429, request=request, headers={'retry-after': '60'}, json={'error': {'message':'private'}})
        return answer(request)
    g = gateway(handler)
    result = g.complete('analyst', messages=[{'role':'user','content':'test'}],
                        schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    assert result.provider == 'gemini'
    assert hosts == ['api.groq.com', 'generativelanguage.googleapis.com']
    assert result.usage['total_tokens'] == 5
    assert result.history[0]['category'] == 'rate_limit'
    g.complete('critic', messages=[{'role':'user','content':'test'}], schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    assert hosts[-1] == 'generativelanguage.googleapis.com'


def test_pinned_validation_retry_never_switches_providers():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(429, request=request, headers={'retry-after':'120'})
    g = gateway(handler)
    with pytest.raises(GatewayError):
        g.complete('analyst', messages=[{'role':'user','content':'test'}], schema={'type':'object'}, max_tokens=20,
                   pinned=('groq', 'openai/gpt-oss-20b'))
    assert hosts == ['api.groq.com']


def test_provider_413_token_rate_limit_fails_over():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if 'groq' in request.url.host:
            return httpx.Response(413, request=request, headers={'retry-after': '60'}, json={'error': {'code':'rate_limit_exceeded'}})
        return answer(request)
    result = gateway(handler).complete('critic', messages=[{'role':'user','content':'test'}],
                                       schema={'type':'object'}, max_tokens=20)
    assert result.provider == 'gemini'
    assert result.history[0]['category'] == 'rate_limit'


def test_credentials_and_schema_errors_never_fail_over():
    hosts=[]
    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(401,request=request,json={'error':{'message':'sensitive'}})
    with pytest.raises(GatewayError) as err:
        gateway(handler).complete('analyst',messages=[{'role':'user','content':'test'}],schema={'type':'object'},max_tokens=20)
    assert err.value.category == 'authentication'
    assert hosts == ['api.groq.com']
    assert 'sensitive' not in str(err.value)


def test_unauthorized_fallback_is_never_contacted():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(429, request=request, headers={'retry-after': '120'})
    g = LLMGateway(credentials={'groq': 'test', 'gemini': 'test'}, allowed={'groq'},
                   max_requests=2, transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError):
        g.complete('analyst', messages=[{'role':'user','content':'test'}], schema={'type':'object'}, max_tokens=20)
    assert hosts == ['api.groq.com']


def test_unsupported_primary_never_silently_routes_around_it():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        return answer(request)
    g = gateway(handler, routes={'analyst': [('groq', 'unverified'), ('gemini', 'gemini-3.1-flash-lite')]})
    with pytest.raises(GatewayError) as err:
        g.complete('analyst', messages=[{'role':'user','content':'test'}], schema={'type':'object'}, max_tokens=20)
    assert err.value.category == 'unverified_model'
    assert hosts == []


def test_generated_schema_failure_retries_same_provider_once():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if len(hosts) == 1:
            return httpx.Response(400, request=request,
                                  json={'error': {'code': 'json_validate_failed', 'message': 'private'}})
        return answer(request)
    result = gateway(handler).complete('analyst', messages=[{'role': 'user', 'content': 'test'}],
                                       schema={'type': 'object'}, max_tokens=20)
    assert result.provider == 'groq'
    assert hosts == ['api.groq.com', 'api.groq.com']
    assert 'private' not in json.dumps(result.history)


def test_plain_bad_request_never_retries_or_fails_over():
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(400, request=request, json={'error': {'code': 'invalid_request_error'}})
    with pytest.raises(GatewayError) as err:
        gateway(handler).complete('analyst', messages=[{'role': 'user', 'content': 'test'}],
                                  schema={'type': 'object'}, max_tokens=20)
    assert err.value.category == 'request_error'
    assert hosts == ['api.groq.com']


def test_gemini_schema_is_inlined_and_drops_typed_claim_enums():
    seen = {}
    def handler(request):
        seen[request.url.host] = json.loads(request.content)['response_format']['json_schema']['schema']
        return answer(request)
    schema = {'type': 'object', '$defs': {'Claim': {'type': 'object', 'properties': {
        'reference': {'type': 'string', 'enum': ['UNIT.rooms', 'VALUE.estimated_market_value']},
        'conclusion': {'type': 'string', 'enum': ['reported_value']}}, 'required': ['reference']}},
        'properties': {'quantitative_claims': {'type': 'array', 'items': {'$ref': '#/$defs/Claim'}},
                       'assessment': {'type': 'string'}}, 'required': ['assessment']}
    gateway(handler, routes={'analyst': [('gemini', 'gemini-3.1-flash-lite')]}).complete(
        'analyst', messages=[{'role': 'user', 'content': 'test'}], schema=schema, max_tokens=20)
    sent = seen['generativelanguage.googleapis.com']
    assert '$defs' not in sent and '$ref' not in json.dumps(sent)
    claim = sent['properties']['quantitative_claims']['items']['properties']
    assert claim['reference']['enum'] == ['R0', 'R1']
    assert 'UNIT.rooms' in claim['reference']['description'] and 'VALUE.estimated_market_value' in claim['reference']['description']
    assert 'conclusion' not in claim and 'conclusion' not in sent['properties']['quantitative_claims']['items']['required']
    assert schema['$defs']['Claim']['properties']['reference']['enum'] == ['UNIT.rooms', 'VALUE.estimated_market_value']
    gateway(handler, routes={'analyst': [('groq', 'openai/gpt-oss-20b')]}).complete(
        'analyst', messages=[{'role': 'user', 'content': 'test'}], schema=schema, max_tokens=20)
    assert '$defs' in seen['api.groq.com']


def test_budget_stops_requests():
    g = gateway(answer,max_requests=1)
    g.complete('analyst', messages=[{'role':'user','content':'test'}],schema={'type':'object'},max_tokens=20)
    with pytest.raises(GatewayError) as err:
        g.complete('critic', messages=[{'role':'user','content':'test'}],schema={'type':'object'},max_tokens=20)
    assert err.value.category == 'budget_exhausted'


def test_token_pacing_sleeps_to_stay_under_budget():
    now = [1000.0]
    slept = []
    def handler(request):
        return answer(request)
    g = gateway(handler, tpm_budget=10, clock=lambda: now[0], sleep=slept.append)
    for _ in range(3):
        g.complete('analyst', messages=[{'role':'user','content':'test'}],
                   schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    # three requests of 5 tokens each exceed 10/min, so sleeps must have happened
    assert slept and all(0 < s <= 60 for s in slept)
    assert g.requests == 3


def test_token_pacing_disabled_by_default():
    slept = []
    g = gateway(answer, sleep=slept.append)
    g.complete('analyst', messages=[{'role':'user','content':'test'}],
               schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    assert slept == [] and g.requests == 1


def test_long_retry_after_wait_respects_configured_cap():
    slept = []
    state = {'n': 0}
    def flaky(request):
        state['n'] += 1
        if state['n'] == 1:
            return httpx.Response(429, request=request, headers={'retry-after': '300'}, json={'error': {'message':'rate limit exceeded'}})
        return answer(request)
    g = gateway(flaky, routes={'analyst': [('groq', 'openai/gpt-oss-20b')]}, max_wait=600, sleep=slept.append)
    result = g.complete('analyst', messages=[{'role':'user','content':'test'}],
                        schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    assert result.provider == 'groq' and any(299 <= s <= 300 for s in slept)
    state2 = {'n': 0}
    def always_limited(request):
        state2['n'] += 1
        return httpx.Response(429, request=request, headers={'retry-after': '300'}, json={'error': {'message':'rate limit exceeded'}})
    g2 = gateway(always_limited, routes={'analyst': [('groq', 'openai/gpt-oss-20b')]}, sleep=slept.append)
    before = len(slept)
    with pytest.raises(GatewayError) as err:
        g2.complete('analyst', messages=[{'role':'user','content':'test'}],
                    schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    assert err.value.category == 'rate_limit' and len(slept) == before


def test_pacing_uses_conservative_estimate_not_average():
    now = [1000.0]
    slept = []
    def handler(request):
        return httpx.Response(200, request=request, json={'id':'x','object':'chat.completion','created':1,
            'model':'m','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"ok":true}'}}],
            'usage': {'prompt_tokens': 3571, 'completion_tokens': 0, 'total_tokens': 3571}})
    g = gateway(handler, tpm_budget=7500, clock=lambda: now[0], sleep=lambda s: slept.append(s) or now.__setitem__(0, now[0]+s))
    g.complete('analyst', messages=[{'role':'user','content':'t'}], schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    before = len(slept)
    g.complete('critic', messages=[{'role':'user','content':'t'}], schema={'type':'object','properties':{},'additionalProperties':False}, max_tokens=20)
    # second request must wait: 3571 used + 4000 floor estimate > 7500
    assert len(slept) > before and 50 <= slept[-1] <= 60


def test_cooldown_wait_retries_within_cap_instead_of_failing():
    now = [1000.0]
    slept = []
    state = {'n': 0}
    def handler(request):
        state['n'] += 1
        if state['n'] == 1:
            return httpx.Response(429, request=request, headers={'retry-after': '1200'}, json={'error': {'message':'rate limit exceeded'}})
        return answer(request)
    g = gateway(handler, routes={'analyst': [('groq', 'openai/gpt-oss-20b')], 'critic': [('groq', 'openai/gpt-oss-20b')]},
                max_wait=600, clock=lambda: now[0], sleep=lambda s: slept.append(s) or now.__setitem__(0, now[0]+s))
    schema = {'type':'object','properties':{},'additionalProperties':False}
    # Retry-After 1200 exceeds the 600s cap: the request fails honestly and sets a cooldown.
    with pytest.raises(GatewayError) as err:
        g.complete('analyst', messages=[{'role':'user','content':'t'}], schema=schema, max_tokens=20)
    assert err.value.category == 'rate_limit'
    # Later, once the remaining window fits under the cap, the next call waits it out instead of failing every unit.
    now[0] += 700
    result = g.complete('critic', messages=[{'role':'user','content':'t'}], schema=schema, max_tokens=20)
    assert result.provider == 'groq' and any(499 <= s <= 501 for s in slept)


def test_critic_failure_records_gateway_history():
    import gabay_pricing.ai_analysis as ai_analysis
    src = open('src/gabay_pricing/ai_analysis.py', encoding='utf-8').read()
    assert "except GatewayError as exc" in src and "'status': 'FAILED', 'error': exc.category" in src
