"""Quantitative claims are rendered only from authorized, typed evidence."""
import json
import httpx
import pytest

from gabay_pricing.ai_analysis import analyze_bundle
from test_ai_validation import synthetic_bundle, response


def run_claim(claim, *, critic_verdict='SUPPORTED', wrong_citation=False, wrong_citation_first=False, rate_limit_critic=False,
              assessment='מצד אחד קיימת אי ודאות ומצד שני נדרש אימות'):
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['valuation']['estimated_market_value'] = 1234567
    diagnostics = []
    critic_attempts = []
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            critic_attempts.append(1)
            if rate_limit_critic and len(critic_attempts) == 1:
                return httpx.Response(429, request=request, headers={'retry-after': '0'})
            payload = json.loads(body['messages'][1]['content'])
            claims = []
            for c in payload['claims']:
                refs = c['references']
                use_wrong = wrong_citation or (wrong_citation_first and len(critic_attempts) == 1)
                supported = ([next((k for k in payload['facts'] if k not in refs), 'NF999999')] if use_wrong else refs[:1])
                claims.append({'claim_id': c['id'], 'verdict': critic_verdict,
                    'reason': 'בדיקה', 'supporting_fact_ids': supported if critic_verdict == 'SUPPORTED' else [],
                    'contradicting_fact_ids': refs[:1] if critic_verdict == 'CONTRADICTED' else [],
                    'suggested_correction': ''})
            review = {'claims': claims, 'quality': {'unit_specificity': '', 'strategy_comparison': '',
                'preference_grounding': '', 'abstention': '', 'actions_usefulness': ''}}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(review)}}]})
        output = response(assessment)
        output['quantitative_claims'] = [claim]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), diagnostics=diagnostics)
    return result['units']['1'], diagnostics


def test_analyst_schema_excludes_unavailable_values():
    observed = {}
    def api(request):
        body = json.loads(request.content)
        schema = body['response_format']['json_schema']['schema']
        observed['allowed'] = schema['$defs']['QuantitativeClaim']['properties']['reference'].get('enum', [])
        return httpx.Response(503, json={'error': {'message': 'offline'}})
    analyze_bundle(synthetic_bundle(), api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert 'VALUE.estimated_market_value' not in observed['allowed']
    assert 'COVERAGE.selected_sales_total' in observed['allowed']


def test_garden_area_is_available_as_unverified_apartment_evidence():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['unit']['garden_area'] = 100.0
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'offline'}})
        output = response('שטח הגינה המדווח הוא 100 מ״ר')
        output['evidence_refs'] = ['UNIT']
        output['quantitative_claims'] = [{'claim_type': 'outdoor_area', 'reference': 'UNIT.garden_area', 'conclusion': 'reported_value'}]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert 'דורש אימות' in result['units']['1']['analysis']['quantitative_claims'][0]['rendered']


def test_area_reference_cannot_authorize_the_same_number_as_a_price():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['unit']['built_area'] = 70.8
    def api(request):
        output = response('מחיר הדירה הוא 70.8 ₪')
        output['evidence_refs'] = ['UNIT']
        output['quantitative_claims'] = [{'claim_type': 'built_area', 'reference': 'UNIT.built_area', 'conclusion': 'reported_value'}]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATION_FAILED'


def test_unicode_minus_preserves_signed_policy_percentage():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['scenarios']['quick_sale']['adjustment_fraction'] = -0.03
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'offline'}})
        output = response('הניתוח דורש בדיקה נוספת')
        output['strategy_comparison']['quick_sale'] = 'התאמה של ‑3% לפי המדיניות'
        output['evidence_refs'] = ['SC_QUICK_SALE']
        output['quantitative_claims'] = [{'claim_type': 'scenario_adjustment', 'reference': 'SC_QUICK_SALE.adjustment_fraction', 'conclusion': 'reported_value'}]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert '-3%' in result['units']['1']['analysis']['strategy_comparison']['quick_sale']


def test_policy_fraction_renders_as_referenced_percentage():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['scenarios']['high_opening']['adjustment_fraction'] = 0.05
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'offline'}})
        output = response('הניתוח דורש בדיקה נוספת')
        output['strategy_comparison']['high_opening'] = 'התאמת המדיניות היא 5%'
        output['evidence_refs'] = ['SC_HIGH_OPENING']
        output['quantitative_claims'] = [{'claim_type': 'scenario_adjustment', 'reference': 'SC_HIGH_OPENING.adjustment_fraction', 'conclusion': 'reported_value'}]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert '5%' in result['units']['1']['analysis']['strategy_comparison']['high_opening']


def test_grounded_numeric_prose_uses_typed_authoritative_value():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['unit']['built_area'] = 70.8
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, json={'error': {'message': 'offline'}})
        output = response('שטח הדירה הוא 70.8 מ״ר')
        output['evidence_refs'] = ['UNIT']
        output['quantitative_claims'] = [{'claim_type': 'built_area', 'reference': 'UNIT.built_area', 'conclusion': 'reported_value'}]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'
    assert '70.8' in result['units']['1']['analysis']['assessment']


def test_existing_market_value_requires_a_typed_reference():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['valuation']['estimated_market_value'] = 1234567
    def api(request):
        output = response('הניתוח דורש בדיקה נוספת')
        output['evidence_refs'] = ['VALUE']
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    diagnostics = []
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert result['units']['1']['status'] == 'VALIDATION_FAILED'
    assert diagnostics[0]['rule'] == 'missing_required_market_value'


def test_short_reference_code_maps_to_authorized_reference():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'R0'})
    assert unit['status'] == 'VALIDATED'
    assert unit['analysis']['quantitative_claims'][0]['reference'] == 'VALUE.estimated_market_value'
    assert unit['analysis']['quantitative_claims'][0]['value'] == 1234567


def test_engine_value_is_rendered_from_typed_reference():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'})
    assert unit['status'] == 'VALIDATED'
    assert unit['claim_review']['status'] == 'SUCCESS'
    assert unit['analysis']['quantitative_claims'][0]['value'] == 1234567
    assert '1234567' in unit['analysis']['quantitative_claims'][0]['rendered']


@pytest.mark.parametrize('claim', [
    {'claim_type': 'market_value', 'reference': 'VALUE.missing', 'conclusion': 'reported_value'},
    {'claim_type': 'built_area', 'reference': 'VALUE.estimated_market_value', 'conclusion': 'reported_value'},
    {'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value', 'conclusion': 'above_market'},
    {'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value', 'conclusion': 'reported_value', 'value': 9999999},
])
def test_invented_values_and_misleading_claim_types_are_rejected(claim):
    unit, diagnostics = run_claim(claim)
    assert unit['status'] == 'VALIDATION_FAILED'
    assert diagnostics[0]['type'] == 'local_validation'
    assert diagnostics[0]['field'].startswith('quantitative_claims.')
    assert diagnostics[0]['rule']
    assert '9999999' not in json.dumps(diagnostics)


def test_analyst_can_reference_more_than_eight_authorized_facts():
    bundle = synthetic_bundle()
    bundle['evidence_packs']['1']['valuation']['estimated_market_value'] = 1234567
    def api(request):
        body = json.loads(request.content)
        if body['response_format']['json_schema']['name'] == 'evidence_critic':
            return httpx.Response(503, request=request)
        output = response('הניתוח דורש בדיקה נוספת')
        output['evidence_refs'] = ['VALUE']
        output['quantitative_claims'] = [{'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value', 'conclusion': 'reported_value'}] * 9
        return httpx.Response(200, request=request, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(bundle, api_key='test', max_units=1, transport=httpx.MockTransport(api))
    assert result['units']['1']['status'] == 'VALIDATED'


def test_critic_schema_can_represent_more_than_ten_referenced_facts():
    from gabay_pricing.ai_evaluation import ClaimReview
    item = {'claim_id': 'assessment', 'verdict': 'SUPPORTED', 'reason': 'נתמך',
            'supporting_fact_ids': [f'F{i}' for i in range(11)],
            'contradicting_fact_ids': [], 'suggested_correction': ''}
    assert len(ClaimReview.model_validate(item).supporting_fact_ids) == 11


def test_critic_retries_a_bounded_rate_limit():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'}, rate_limit_critic=True)
    assert unit['claim_review']['status'] == 'SUCCESS'


def test_critic_receives_actual_conclusions_and_referenced_facts():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'}, critic_verdict='CONTRADICTED')
    assert unit['claim_review']['status'] == 'SUCCESS'
    assert unit['claim_review']['review']['claims'][0]['verdict'] == 'CONTRADICTED'
    assert unit['analysis']['verification'] == 'AI_INTERPRETATION_NOT_VERIFIED'


def test_spelled_out_unsupported_claim_is_flagged_by_critic():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'}, critic_verdict='CONTRADICTED',
                         assessment='מחיר הנכס הוא חמישה מיליון שקלים')
    verdicts = {c['claim_id']: c['verdict'] for c in unit['claim_review']['review']['claims']}
    assert verdicts['assessment'] == 'CONTRADICTED'


def test_unknown_evidence_reference_has_safe_diagnostics():
    diagnostics = []
    def api(request):
        output = response('הערכת שוק דורשת בדיקה')
        output['evidence_refs'] = ['SECRET-NOT-A-REFERENCE']
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    result = analyze_bundle(synthetic_bundle(), api_key='test', max_units=1,
                            transport=httpx.MockTransport(api), diagnostics=diagnostics)
    assert result['units']['1']['status'] == 'VALIDATION_FAILED'
    assert diagnostics[0]['field'] == 'evidence_refs'
    assert diagnostics[0]['reference'] == '[UNKNOWN]'
    assert 'SECRET-NOT-A-REFERENCE' not in json.dumps(diagnostics)


def test_critic_can_correct_an_unreferenced_fact_once():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'}, wrong_citation_first=True)
    assert unit['claim_review']['status'] == 'SUCCESS'


def test_critic_cannot_support_a_claim_using_an_unreferenced_fact():
    unit, _ = run_claim({'claim_type': 'market_value', 'reference': 'VALUE.estimated_market_value',
                          'conclusion': 'reported_value'}, wrong_citation=True)
    assert unit['claim_review']['status'] == 'INVALID_RESPONSE'
