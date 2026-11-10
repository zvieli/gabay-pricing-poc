"""Persistence contract: results are never destroyed by retries, resumes or re-reports."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gabay_pricing.ai_analysis import AI_CONTRACT_VERSION, MODEL, analyze_bundle, _context, _validate
from gabay_pricing.llm_gateway import GatewayError
from gabay_pricing.reporting import carry_over_previous_ai, write_reports

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'data/snapshots/market-live.json'
WORKBOOK = ROOT.parent / 'DATA/חוברת1.xlsx'


class QuotaGateway:
    """Stands in for a quota-exhausted provider; never touches the network."""

    def __init__(self, allowed=('gemini',)):
        self.allowed = set(allowed)
        self.requests = 0

    def complete(self, role, **kwargs):
        self.requests += 1
        raise GatewayError('rate_limit', status=429)


def make_pack(i):
    return {
        'decision': {'selected_sale_count': 0, 'eligible_offer_count': 0, 'excluded_offer_count': 0,
                     'status': 'BLOCKED_REQUIRES_REVIEW', 'approval': 'NOT_APPROVED', 'blockers': [],
                     'missing_information': []},
        'selected_sales': [], 'eligible_offers': [], 'excluded_offers': [],
        'unit': {'id': i, 'property_type': 'דירה', 'rooms': 3, 'built_area': 70, 'balcony_area': 0, 'garden_area': 0},
        'valuation': {'estimated_market_value': None, 'tier': 'NONE', 'status': 'UNPRICED', 'review_flags': []},
        'policy': {'unit_floor_rate': 0.01, 'unit_orientation_rates': {}, 'calibration': 'experimental'},
        'adjustments': {'components': []},
        'scenarios': {k: {} for k in ('quick_sale', 'balanced', 'high_opening')},
    }


def make_bundle(ids=(1, 2, 3)):
    apartments = [{'id': i, 'property_type': 'דירה', 'rooms': 3, 'built_area': 70, 'source_rows': []} for i in ids]
    results = [{'apartment_id': i, 'property_type': 'דירה', 'rooms': 3, 'built_area': 70,
                'review_flags': [], 'pricing_decision': {}, 'marketing_strategies': {},
                'warnings': [], 'status': 'REVIEW_REQUIRED', 'tier': 'TIER_1'} for i in ids]
    packs = {str(i): make_pack(i) for i in ids}
    return {'apartments': apartments, 'results': results, 'evidence_packs': packs,
            'config': {'as_of': '2026-11-09'}, 'snapshot_sha256': 's' * 64,
            'workbook_sha256': 'w' * 64, 'code_sha256': 'c' * 64,
            'snapshot_path': 'unused.json'}


import httpx


def answer(request):
    return httpx.Response(200, request=request, json={'id': 'x', 'object': 'chat.completion',
        'created': 1, 'model': 'test-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': '{"ok":true}'}}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})


def gateway(handler, **kwargs):
    from gabay_pricing.llm_gateway import LLMGateway
    return LLMGateway(credentials={'groq': 'test-groq', 'gemini': 'test-gemini'},
                      allowed={'groq', 'gemini'}, max_requests=kwargs.pop('max_requests', 5),
                      transport=httpx.MockTransport(handler), **kwargs)


def validated_unit(provider='gemini', model='gemini-3.1-flash-lite'):
    pack = make_pack(1)
    analysis = _validate({'assessment': 'assessment requires review', 'strategy_comparison': {
        'quick_sale': 'requires review', 'balanced': 'requires review', 'high_opening': 'requires review'},
        'preferred_scenario': 'abstain', 'reasoning': 'requires further review', 'evidence_refs': ['VALUE'],
        'missing_information': 'requires review', 'next_steps': 'requires review'}, pack, _context(pack)[1])
    return {'status': 'VALIDATED', 'provider': provider, 'model': model,
            'analysis': analysis, 'usage': {'total_tokens': 1},
            'claim_review': {'status': 'SUCCESS', 'provider': provider,
                             'review': {'claims': [{'verdict': 'SUPPORTED'}]}}}


def priors(ids=(1, 2)):
    return {str(i): validated_unit() for i in ids}


def real_bundle(tmp_path):
    """A genuine deterministic bundle produced by the actual CLI entry point."""
    if not SNAPSHOT.is_file() or not WORKBOOK.is_file():
        pytest.skip('private snapshot and workbook not supplied')
    run_dir = tmp_path / 'run1'
    subprocess.run([sys.executable, '-m', 'gabay_pricing.cli', 'report', '--snapshot', str(SNAPSHOT),
                    '--workbook', str(WORKBOOK), '--reports', str(run_dir)],
                   cwd=ROOT, check=True, capture_output=True, timeout=600)
    return json.loads((run_dir / 'pricing_results.json').read_text(encoding='utf-8'))


def test_retry_of_failed_unit_keeps_all_validated_units():
    bundle = make_bundle()
    gateway = QuotaGateway()
    result = analyze_bundle(bundle, api_key='unused', gateway=gateway, max_units=39,
                            apartment_ids={'3'}, previous_units=priors())
    assert set(result['units']) == {'1', '2', '3'}
    assert result['units']['1']['status'] == 'VALIDATED' and result['units']['1'].get('reused') is True
    assert result['units']['2']['status'] == 'VALIDATED'
    assert result['units']['3']['status'] == 'API_ERROR'
    assert result['status'] == 'PARTIAL'


def test_quota_failure_does_not_erase_validated_analyses():
    bundle = make_bundle()
    gateway = QuotaGateway()
    result = analyze_bundle(bundle, api_key='unused', gateway=gateway, max_units=39, previous_units=priors())
    assert result['units']['1']['status'] == 'VALIDATED'
    assert result['units']['2']['status'] == 'VALIDATED'
    assert result['units']['3']['status'] == 'API_ERROR'
    assert result['units']['1']['analysis'] == validated_unit()['analysis']


def test_resume_is_idempotent_and_makes_no_calls():
    bundle = make_bundle()
    gateway = QuotaGateway()
    result = analyze_bundle(bundle, api_key='unused', gateway=gateway, max_units=39,
                            previous_units=priors((1, 2, 3)))
    assert gateway.requests == 0
    assert set(result['units']) == {'1', '2', '3'}
    assert all(u['status'] == 'VALIDATED' and u.get('reused') is True for u in result['units'].values())
    assert result['status'] == 'SUCCESS'


def test_credential_missing_run_keeps_validated_units_visible():
    bundle = make_bundle()
    result = analyze_bundle(bundle, api_key=None, gateway=None, max_units=39, previous_units=priors())
    assert result['status'] == 'UNAVAILABLE'
    assert result['units']['1']['status'] == 'VALIDATED'
    assert result['units']['3']['status'] == 'CREDENTIAL_MISSING'


def test_durable_sidecar_written_after_each_unit(tmp_path):
    bundle = make_bundle()
    durable = tmp_path / 'AI_UNITS_DURABLE.json'
    gateway = QuotaGateway()
    analyze_bundle(bundle, api_key='unused', gateway=gateway, max_units=39, durable_path=durable)
    saved = json.loads(durable.read_text(encoding='utf-8'))
    assert set(saved['ai_analysis']['units']) == {'1', '2', '3'}
    for key in ('snapshot_sha256', 'workbook_sha256', 'code_sha256', 'config', 'results', 'evidence_packs'):
        assert key in saved
    assert saved['snapshot_sha256'] == bundle['snapshot_sha256']


def test_durable_sidecar_is_resume_compatible(tmp_path):
    bundle = make_bundle()
    durable = tmp_path / 'AI_UNITS_DURABLE.json'
    gateway = QuotaGateway()
    analyze_bundle(bundle, api_key='unused', gateway=gateway, max_units=39, durable_path=durable)
    saved = json.loads(durable.read_text(encoding='utf-8'))
    ai = saved['ai_analysis']
    assert ai['prompt_version'] == 'business-analyst-v1'
    assert ai['contract_version'] == AI_CONTRACT_VERSION
    assert ai['model'] == MODEL


def test_per_provider_tpm_budgets_are_independent(monkeypatch):
    now = [1000.0]
    slept = []
    monkeypatch.setenv('GABAY_LLM_TPM_BUDGET_GROQ', '10')
    g = gateway(answer, tpm_budget=None, clock=lambda: now[0],
                sleep=lambda s: slept.append(s) or now.__setitem__(0, now[0] + s))
    schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    for _ in range(2):
        g.complete('analyst', messages=[{'role': 'user', 'content': 't'}], schema=schema, max_tokens=20,
                   pinned=('groq', 'openai/gpt-oss-20b'))
    groq_sleeps = len(slept)
    for _ in range(2):
        g.complete('critic', messages=[{'role': 'user', 'content': 't'}], schema=schema, max_tokens=20,
                   pinned=('gemini', 'gemini-3.1-flash-lite'))
    assert g.tpm_budgets['groq'] == 10 and g.tpm_budgets['gemini'] == 0
    assert len(slept) == groq_sleeps  # gemini has no budget: gemini traffic never sleeps
    assert g._windows['gemini'] == []  # unbudgeted providers are not paced or tracked


def test_daily_quota_429_opens_circuit_breaker():
    state = {'n': 0}
    def tpd(request):
        state['n'] += 1
        return httpx.Response(429, request=request, headers={'retry-after': '1800'},
                              json={'error': {'code': 'rate_limit_exceeded',
                                              'message': 'Rate limit reached (TPD): Limit 200000, Used 199861, Requested 4304'}})
    g = gateway(tpd, routes={'analyst': [('groq', 'openai/gpt-oss-20b')]}, clock=lambda: 1000.0)
    with pytest.raises(GatewayError) as err:
        g.complete('analyst', messages=[{'role': 'user', 'content': 't'}],
                   schema={'type': 'object', 'properties': {}, 'additionalProperties': False}, max_tokens=20)
    assert err.value.category == 'quota_exhausted'
    entry = [h for h in err.value.history if h.get('category') == 'quota_exhausted'][0]
    assert entry['limit_kind'] == 'TPD' and entry['used'] == 199861
    assert g.cooldowns['groq'] >= 2800  # circuit breaker: no retries until the daily window resets
    before = state['n']
    with pytest.raises(GatewayError) as err2:
        g.complete('analyst', messages=[{'role': 'user', 'content': 't'}],
                   schema={'type': 'object', 'properties': {}, 'additionalProperties': False}, max_tokens=20)
    assert state['n'] == before  # zero further HTTP requests while the breaker is open
    assert err2.value.category == 'quota_exhausted'


def test_gemini_429_body_retry_delay_overrides_header():
    def gemini_rpd(request):
        return httpx.Response(429, request=request, headers={'retry-after': '60'},
                              json={'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED',
                                              'message': 'You exceeded your current quota. Generate requests per day. retry in 8h42m33s.'}})
    g = gateway(gemini_rpd, routes={'analyst': [('gemini', 'gemini-3.1-flash-lite')]}, clock=lambda: 1000.0)
    with pytest.raises(GatewayError) as err:
        g.complete('analyst', messages=[{'role': 'user', 'content': 't'}],
                   schema={'type': 'object', 'properties': {}, 'additionalProperties': False}, max_tokens=20)
    assert err.value.category == 'quota_exhausted'
    entry = [h for h in err.value.history if h.get('category') == 'quota_exhausted'][0]
    assert entry['body_retry_delay'] == 8 * 3600 + 42 * 60 + 33
    assert g.cooldowns['gemini'] >= 8 * 3600  # the 60s header must not cause repeated futile requests


def test_quota_exhaustion_stops_run_preserving_completed_units(tmp_path):
    bundle = make_bundle()
    durable = tmp_path / 'AI_UNITS_DURABLE.json'

    class QuotaWall:
        requests = 0
        def complete(self, role, **kwargs):
            self.requests += 1
            raise GatewayError('quota_exhausted', status=429)

    gw = QuotaWall()
    result = analyze_bundle(bundle, api_key='unused', gateway=gw, max_units=39, durable_path=durable)
    assert set(result['units']) == {'1'}  # run stopped after the first failure
    assert result['units']['1']['status'] == 'API_ERROR'
    assert result['stopped_reason'] == 'quota_exhausted'
    saved = json.loads(durable.read_text(encoding='utf-8'))
    assert set(saved['ai_analysis']['units']) == {'1'}


def test_durable_sidecar_survives_midway_crash(tmp_path):
    bundle = make_bundle((1, 2))
    durable = tmp_path / 'AI_UNITS_DURABLE.json'

    class ExplodingGateway(QuotaGateway):
        def complete(self, role, **kwargs):
            self.requests += 1
            if self.requests == 1:
                raise GatewayError('rate_limit', status=429)  # unit 1 fails but is recorded
            raise KeyboardInterrupt  # simulate the process dying mid-run

    with pytest.raises(KeyboardInterrupt):
        analyze_bundle(bundle, api_key='unused', gateway=ExplodingGateway(), max_units=39, durable_path=durable)
    saved = json.loads(durable.read_text(encoding='utf-8'))
    assert set(saved['ai_analysis']['units']) == {'1'}


def ai_meta(**over):
    meta = {'status': 'PARTIAL', 'model': 'm', 'calls': 9, 'prompt_version': 'business-analyst-v1',
            'contract_version': 1, 'units': {'1': validated_unit()}}
    meta.update(over)
    return meta


def test_no_ai_report_preserves_existing_ai_with_matching_hashes(tmp_path):
    bundle = make_bundle()
    existing = {'snapshot_sha256': bundle['snapshot_sha256'], 'workbook_sha256': bundle['workbook_sha256'],
                'code_sha256': 'old-code-hash', 'config': bundle['config'],
                'results': bundle['results'], 'evidence_packs': bundle['evidence_packs'],
                'ai_analysis': ai_meta()}
    path = tmp_path / 'pricing_results.json'
    path.write_text(json.dumps(existing, ensure_ascii=False), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'model': None, 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path)
    assert bundle['ai_analysis']['status'] == 'PARTIAL'
    assert bundle['ai_analysis']['units']['1']['status'] == 'VALIDATED'
    assert bundle['ai_analysis'].get('carried_over_from_previous_report') is True


def test_carry_over_reports_current_and_historical_call_provenance(tmp_path):
    bundle = make_bundle()
    prior = {**bundle, 'ai_analysis': ai_meta(calls=0, provenance={'source_checkpoint': '/private/checkpoint.json'})}
    path = tmp_path / 'saved.json'
    path.write_text(json.dumps(prior), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path, source_path=path)
    ai = bundle['ai_analysis']
    assert ai['calls'] == 0 and ai['provenance']['historical_total_calls'] is None
    assert ai['provenance']['source_checkpoint'] == '/private/checkpoint.json'
    assert ai['provenance']['source_report'] == str(path.resolve())
    assert ai['units']['1']['analyst_reused'] and ai['units']['1']['critic_reused']


def test_saved_ai_checkpoint_must_match_before_recording_origin(tmp_path):
    bundle = make_bundle()
    prior = {**bundle, 'ai_analysis': ai_meta()}
    source = tmp_path / 'source.json'
    checkpoint = tmp_path / 'checkpoint.json'
    source.write_text(json.dumps(prior), encoding='utf-8')
    checkpoint.write_text(json.dumps(prior), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path, source_path=source, checkpoint_path=checkpoint)
    assert bundle['ai_analysis']['provenance']['source_checkpoint'] == str(checkpoint)
    prior['ai_analysis']['units']['1']['analysis']['assessment'] = 'tampered'
    checkpoint.write_text(json.dumps(prior), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path, source_path=source, checkpoint_path=checkpoint)
    assert bundle['ai_analysis']['status'] == 'NOT_RUN'


def test_no_ai_report_carries_over_legacy_results_without_contract_version(tmp_path):
    """Pre-contract files (before the persistence fix) carry the same AI semantics; recovery must work."""
    bundle = make_bundle()
    existing = {'snapshot_sha256': bundle['snapshot_sha256'], 'workbook_sha256': bundle['workbook_sha256'],
                'code_sha256': 'old-code-hash', 'config': bundle['config'],
                'results': bundle['results'], 'evidence_packs': bundle['evidence_packs'],
                'ai_analysis': {'status': 'PARTIAL', 'model': 'openai/gpt-oss-20b', 'calls': 121,
                                'prompt_version': 'business-analyst-v1', 'units': {'1': validated_unit()}}}
    (tmp_path / 'pricing_results.json').write_text(json.dumps(existing, ensure_ascii=False), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'model': None, 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path)
    assert bundle['ai_analysis']['units']['1']['status'] == 'VALIDATED'


def test_no_ai_report_refuses_ai_from_a_different_contract_version(tmp_path):
    bundle = make_bundle()
    existing = {'snapshot_sha256': bundle['snapshot_sha256'], 'workbook_sha256': bundle['workbook_sha256'],
                'code_sha256': bundle['code_sha256'], 'config': bundle['config'],
                'results': bundle['results'], 'evidence_packs': bundle['evidence_packs'],
                'ai_analysis': ai_meta(contract_version=999)}
    (tmp_path / 'pricing_results.json').write_text(json.dumps(existing, ensure_ascii=False), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'model': None, 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path)
    assert bundle['ai_analysis']['status'] == 'NOT_RUN' and bundle['ai_analysis']['units'] == {}


def test_no_ai_report_does_not_carry_ai_from_a_different_run(tmp_path):
    bundle = make_bundle()
    existing = {'snapshot_sha256': 'different' * 8, 'workbook_sha256': bundle['workbook_sha256'],
                'code_sha256': bundle['code_sha256'], 'config': bundle['config'],
                'results': bundle['results'], 'evidence_packs': bundle['evidence_packs'],
                'ai_analysis': ai_meta()}
    (tmp_path / 'pricing_results.json').write_text(json.dumps(existing, ensure_ascii=False), encoding='utf-8')
    bundle['ai_analysis'] = {'status': 'NOT_RUN', 'model': None, 'units': {}, 'calls': 0}
    carry_over_previous_ai(bundle, tmp_path)
    assert bundle['ai_analysis']['status'] == 'NOT_RUN' and bundle['ai_analysis']['units'] == {}


def test_write_reports_backs_up_existing_results_before_overwrite(tmp_path):
    bundle = real_bundle(tmp_path)
    target = tmp_path / 'run2'
    target.mkdir()
    (target / 'pricing_results.json').write_text('{"marker": "previous-run"}', encoding='utf-8')
    write_reports(bundle, target)
    backups = list(target.glob('pricing_results.backup-*.json'))
    assert backups and json.loads(backups[0].read_text(encoding='utf-8'))['marker'] == 'previous-run'
    assert json.loads((target / 'pricing_results.json').read_text(encoding='utf-8'))['results']


def test_write_reports_refuses_incomplete_result_set(tmp_path):
    bundle = real_bundle(tmp_path)
    bundle['results'] = bundle['results'][:-1]  # one apartment missing
    target = tmp_path / 'run3'
    target.mkdir()
    with pytest.raises(ValueError):
        write_reports(bundle, target)
    assert not (target / 'pricing_results.json').exists()
