"""Evidence-grounded Groq interpretation, isolated from deterministic pricing.

Text is interpretation, not verified fact. Numeric facts are rendered from the pack,
never from model prose; citations establish traceability, not textual entailment.
"""
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from typing import Literal
import json
import math
import os
import re
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gabay_pricing.evidence_pack import build_evidence_pack
from gabay_pricing.llm_gateway import GatewayError, LLMGateway

MODEL = 'openai/gpt-oss-20b'
# Bump only when the analyst/critic prompts, schema or validation semantics change.
# Resume compatibility is keyed on this plus prompt_version and model — NOT on the whole-package
# code hash, so unrelated hardening (gateway, reporting) never invalidates valid analyses.
AI_CONTRACT_VERSION = 1
STRATEGIES = ('quick_sale', 'balanced', 'high_opening')
QUANTITATIVE_REFERENCES = {
    'market_value': {'VALUE.estimated_market_value': 'אומדן שווי שוק'},
    'built_area': {'UNIT.built_area': 'שטח בנוי מדווח (דורש אימות)'},
    'room_count': {'UNIT.rooms': 'מספר חדרים מדווח'},
    'outdoor_area': {'UNIT.garden_area': 'שטח גינה מדווח ומסווג בהנחה (דורש אימות)', 'UNIT.balcony_area': 'שטח מרפסת מדווח (דורש אימות)'},
    'sale_count': {'COVERAGE.selected_sales_total': 'עסקאות נבחרות'},
    'offer_count': {'COVERAGE.eligible_offers_total': 'הצעות כשירות שנבדקו', 'COVERAGE.excluded_offers_total': 'הצעות שנפסלו', 'COVERAGE.eligible_offers_in_pack': 'הצעות כשירות בחבילת הראיות'},
    'scenario_price': {f'SC_{key.upper()}.suggested_asking_price': f'תרחיש {key}' for key in STRATEGIES},
    'scenario_adjustment': {f'SC_{key.upper()}.adjustment_fraction': f'שיעור התאמת תרחיש {key} (הנחת מדיניות לא מכוילת)' for key in STRATEGIES},
}


def _numeric(context, reference):
    alias, _, field = reference.partition('.')
    value = context.get(alias, {}).get(field)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _available_references(context):
    return {kind: {ref: label for ref, label in choices.items() if _numeric(context, ref)}
            for kind, choices in QUANTITATIVE_REFERENCES.items()}


def _reference_codes(context):
    return {f'R{i}': ref for i, ref in enumerate(
        ref for choices in _available_references(context).values() for ref in choices)}


class EvidenceReferenceError(ValueError):
    def __init__(self, field, reference, message='Invalid or unauthorized quantitative reference'):
        super().__init__(message)
        self.field = field
        allowed = {r for choices in QUANTITATIVE_REFERENCES.values() for r in choices}
        self.reference = reference if reference in allowed else '[UNKNOWN]'


class NumericClaimError(ValueError):
    def __init__(self, field, match, rule='unstructured_digit', reference=None, evidence_matches=None):
        super().__init__('Numeric claims must come from the pricing engine')
        self.field, self.match, self.rule, self.reference = field, match, rule, reference
        self.evidence_matches = evidence_matches or []


class QuantitativeClaim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claim_type: Literal['market_value', 'built_area', 'outdoor_area', 'room_count', 'sale_count', 'offer_count', 'scenario_price', 'scenario_adjustment']
    reference: str
    conclusion: Literal['reported_value']


class Comparison(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quick_sale: str = Field(min_length=5, max_length=1200)
    balanced: str = Field(min_length=5, max_length=1200)
    high_opening: str = Field(min_length=5, max_length=1200)


class Response(BaseModel):
    model_config = ConfigDict(extra='forbid')
    assessment: str = Field(min_length=10, max_length=2000)
    strategy_comparison: Comparison
    preferred_scenario: Literal['quick_sale', 'balanced', 'high_opening', 'abstain']
    reasoning: str = Field(min_length=10, max_length=1500)
    evidence_refs: list[str] = Field(min_length=1, max_length=32)
    missing_information: str = Field(min_length=5, max_length=1200)
    next_steps: str = Field(min_length=5, max_length=1200)
    quantitative_claims: list[QuantitativeClaim] = Field(default_factory=list, max_length=16)


def _context(pack):
    """Send bounded structured evidence under aliases, without source URLs/addresses."""
    selected_count = pack['decision'].get('selected_sale_count', len(pack['selected_sales']))
    facts = {'COVERAGE': {'selected_sales_total': selected_count, 'selected_sales_in_pack': len(pack['selected_sales']),
                          'selected_sales_sent': min(3, len(pack['selected_sales'])),
                          'selected_sales_not_included_in_context': max(0, selected_count - 3),
                          'eligible_offers_in_pack': len(pack['eligible_offers']),
                          'eligible_offers_total': pack['decision'].get('eligible_offer_count'),
                          'excluded_offers_total': pack['decision'].get('excluded_offer_count'),
                          'note': 'Bounded selected evidence, not a census or proof of absence in the market. SALE = provider-reported sale transaction, not necessarily a completed building; OFFER = asking price, EXCLUDED = ineligible asking offer. Construction status and area may be unverified; policy is not calibrated.'},
             'UNIT': {k: v for k, v in pack['unit'].items() if k not in ('provenance', 'floor_areas')},
             'VALUE': {k: v for k, v in pack['valuation'].items() if k != 'review_flags'},
             'DECISION': pack['decision'], 'POLICY': pack['policy'],
             'ADJUSTMENTS': {'components': pack['adjustments'].get('components', [])}}
    # Provenance remains in the local pack; only concise facts are sent externally.
    refs = {k: k for k in facts}
    for key in STRATEGIES:
        alias = 'SC_' + key.upper()
        scenario = pack['scenarios'].get(key) or {}
        facts[alias] = {k: scenario.get(k) for k in ('basis', 'suggested_asking_price', 'indicative_asking_price', 'adjustment_fraction', 'is_indicative', 'blocked_reason', 'approval')}
        refs[alias] = 'scenario:' + key
    for prefix, items in [('SALE', pack['selected_sales']), ('OFFER', pack['eligible_offers']), ('EXCLUDED', pack['excluded_offers'])]:
        for i, item in enumerate(items[:3], 1):
            alias = f'{prefix}_{i}'
            facts[alias] = {k: v for k, v in item.items() if k not in ('id', 'source_refs')}
            if 'limitations' in facts[alias]:
                facts[alias]['limitations'] = list(dict.fromkeys(facts[alias]['limitations']))[:4]
            if prefix == 'SALE':
                facts[alias]['facts'] = {k: v for k, v in facts[alias]['facts'].items() if k not in ('attestation', 'verified_built_area_basis')}
            refs[alias] = item['id']
    if pack.get('benchmark'):
        facts['BENCHMARK'] = {k: v for k, v in pack['benchmark'].items() if k not in ('id', 'source_refs')}
        refs['BENCHMARK'] = pack['benchmark']['id']
    if pack.get('special_proxy'):
        facts['PROXY'] = {k: v for k, v in pack['special_proxy'].items() if k not in ('refs', 'benchmark_id')}
        refs['PROXY'] = pack['special_proxy'].get('benchmark_id') or 'special_property_scenario'
    return facts, refs


def _fact_catalog(context, refs):
    """Atomic values are copied locally; a reference to a record alone is not a fact."""
    catalog = {}
    selected = {
        'UNIT': {'property_type', 'rooms', 'built_area', 'garden_area', 'balcony_area', 'attribute_states', 'warnings'},
        'VALUE': {'estimated_market_value', 'adjusted_market_value', 'tier', 'status', 'pricing_classification', 'evidence_quality'},
        'DECISION': {'status', 'approval', 'blockers', 'missing_information', 'eligible_offer_count'},
        'POLICY': {'unit_floor_rate', 'unit_orientation_rates', 'unit_triplex_premium', 'unit_duplex_premium', 'calibration'},
        'ADJUSTMENTS': set(),
    }
    def visit(alias, value, path):
        if path and alias in selected and path[0] not in selected[alias]:
            return
        if alias.startswith('SALE_') and len(path) >= 2 and path[0] == 'facts' and path[1] not in {'original_price', 'area_basis', 'area_basis_semantics', 'verified_built_area', 'property_type', 'score'}:
            return
        if alias.startswith('SC_') and path and path[0] not in {'suggested_asking_price', 'indicative_asking_price', 'adjustment_fraction', 'is_indicative'}:
            return
        if alias == 'UNIT' and len(path) >= 2 and path[0] == 'attribute_states' and path[1] not in {'built_area', 'garden_area'}:
            return
        if alias.startswith('EXCLUDED_') and alias != 'EXCLUDED_1':
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(alias, child, path + [key])
        elif isinstance(value, list):
            for i, child in enumerate(value[:2]):
                visit(alias, child, path + [str(i)])
        elif value is None or isinstance(value, (str, bool, int, float)):
            if isinstance(value, str) and len(value) > 300:
                return
            fact_id = 'F' + str(len(catalog) + 1)
            field = '.'.join(path)
            is_limitation = (field.startswith(('warnings', 'limitations', 'reasons', 'blockers', 'missing_information', 'calibration'))
                             or field.startswith('attribute_states.') and value in {'UNKNOWN', 'MISSING', 'INFERRED'}
                             or field in {'evidence_quality', 'pricing_classification', 'area_basis_semantics', 'facts.area_basis_semantics'} and value not in (None, 'EVIDENCE_SUPPORTED', 'explicit_built')
                             or field == 'is_indicative' and value is True)
            offer_record = context.get(alias) or {}
            offer_kind = offer_record.get('facts', {}).get('kind') or offer_record.get('kind')
            evidence_type = ('SALE_TRANSACTION' if alias.startswith('SALE_') else ('PROJECT_OFFER' if offer_kind == 'project' else 'ASKING_OFFER') if alias.startswith(('OFFER_', 'EXCLUDED_')) else
                             'BENCHMARK' if alias in {'BENCHMARK', 'PROXY'} else
                             'POLICY_ASSUMPTION' if alias in {'POLICY', 'SC_QUICK_SALE', 'SC_BALANCED', 'SC_HIGH_OPENING'} else
                             'DECISION_BLOCKER' if alias == 'DECISION' else 'UNIT_OR_VALUATION')
            state = ('NOT_ELIGIBLE' if alias.startswith('EXCLUDED_') else 'NOT_INCLUDED_IN_CONTEXT' if field == 'selected_sales_not_included_in_context' and value else
                     'NOT_VERIFIED' if field in {'facts.verified_built_area', 'facts.area_basis_semantics'} and value in (None, 'reported_unknown') else
                     'NOT_AVAILABLE' if value is None else 'AVAILABLE')
            catalog[fact_id] = {'record': alias, 'path': field, 'value': value, 'source_id': refs[alias], 'is_limitation': is_limitation, 'evidence_type': evidence_type, 'state': state}
    for alias, value in context.items():
        visit(alias, value, [])
    return catalog


def _validate(data, pack, refs):
    context, _ = _context(pack)
    if isinstance(data, dict):
        codes = _reference_codes(context)
        for claim in data.get('quantitative_claims') or []:
            if isinstance(claim, dict):
                # Gemini sends short reference codes; any explicit value it sends is still validated strictly.
                claim.setdefault('conclusion', 'reported_value')
                if claim.get('reference') in codes:
                    claim['reference'] = codes[claim['reference']]
    parsed = Response.model_validate(data)
    unknown_refs = set(parsed.evidence_refs) - set(refs)
    if unknown_refs:
        raise EvidenceReferenceError('evidence_refs', next(iter(unknown_refs)), 'Unknown evidence reference')
    result = parsed.model_dump()
    rendered_claims = []
    for i, claim in enumerate(parsed.quantitative_claims):
        labels = QUANTITATIVE_REFERENCES[claim.claim_type]
        alias, _, field = claim.reference.partition('.')
        value = context.get(alias, {}).get(field)
        if claim.reference not in labels or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise EvidenceReferenceError(f'quantitative_claims.{i}.reference', claim.reference)
        if alias not in parsed.evidence_refs:
            raise EvidenceReferenceError(f'quantitative_claims.{i}.reference', claim.reference, 'Reference alias must be cited in evidence_refs')
        suffix = ' מ״ר' if claim.claim_type in {'built_area', 'outdoor_area'} else ' ₪' if claim.claim_type in {'market_value', 'scenario_price'} else ''
        rendered_claims.append({**claim.model_dump(), 'value': value, 'rendered': f'{labels[claim.reference]}: {value}{suffix}'})
    result['quantitative_claims'] = rendered_claims
    market_value = context.get('VALUE', {}).get('estimated_market_value')
    if isinstance(market_value, (int, float)) and not isinstance(market_value, bool) and math.isfinite(market_value) and not any(c['reference'] == 'VALUE.estimated_market_value' for c in rendered_claims):
        raise EvidenceReferenceError('quantitative_claims', 'VALUE.estimated_market_value', 'Required market value reference missing')
    prose = {'assessment': parsed.assessment, **{'strategy_comparison.' + k: v for k, v in parsed.strategy_comparison.model_dump().items()},
             'reasoning': parsed.reasoning, 'missing_information': parsed.missing_information, 'next_steps': parsed.next_steps}
    numeric_values = {Decimal(str(c['value'])): str(c['value']) for c in rendered_claims}
    percentages = {Decimal(str(c['value'])) * 100 for c in rendered_claims if c['claim_type'] == 'scenario_adjustment'}
    prices = {Decimal(str(c['value'])) for c in rendered_claims if c['claim_type'] in {'market_value', 'scenario_price'}}
    for field, text in prose.items():
        # A typed, authorized claim must supply each numeric value; the engine renders it.
        def render_number(match):
            token = match.group()
            try:
                value = Decimal(token.replace(',', '').replace('−', '-').replace('‑', '-').replace('–', '-'))
            except InvalidOperation:
                raise NumericClaimError(field, '[DIGITS]') from None
            after = match.string[match.end():].lstrip()
            before = match.string[:match.start()].rstrip()
            if after.startswith(('%', '٪')):
                if value not in percentages:
                    matching = [ref for ref in QUANTITATIVE_REFERENCES['scenario_adjustment']
                                if (candidate := context.get(ref.split('.')[0], {}).get(ref.split('.')[1])) is not None
                                and abs(Decimal(str(candidate)) * 100) == abs(value)]
                    raise NumericClaimError(field, '[DIGITS]', 'percentage_type_mismatch', matching[0] if len(matching) == 1 else None, matching[:8])
                return format(value.normalize(), 'f')
            if (after.startswith(('₪', 'ש״ח', 'ש"ח', 'שקל')) or before.endswith('₪')) and value not in prices:
                raise NumericClaimError(field, '[DIGITS]', 'price_type_mismatch')
            if value not in numeric_values:
                candidates = []
                for choices in QUANTITATIVE_REFERENCES.values():
                    for ref in choices:
                        alias, _, name = ref.partition('.')
                        actual = context.get(alias, {}).get(name)
                        if isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and Decimal(str(actual)) == value:
                            candidates.append(ref)
                catalog = _fact_catalog(context, refs)
                matches = [f"{fact['record']}.{fact['path']}" for fact in catalog.values()
                           if isinstance(fact['value'], (int, float)) and not isinstance(fact['value'], bool)
                           and math.isfinite(fact['value']) and Decimal(str(fact['value'])) == value]
                raise NumericClaimError(field, '[DIGITS]', 'missing_typed_claim' if candidates else 'unmatched_numeric', candidates[0] if len(candidates) == 1 else None, matches[:8])
            return numeric_values[value]
        markers = {m.span('number') for m in re.finditer(r'(?m)(?:^|(?<=[.!?])\s+)(?P<number>\d+)[.)]\s+', text)}
        if any(c.isdecimal() and not c.isascii() for c in text):
            raise NumericClaimError(field, '[DIGITS]')
        rendered = re.sub(r'[-+−‑–]?\d[\d,]*(?:\.\d+)?',
                          lambda m: m.group() if m.span() in markers else render_number(m), text)
        if field.startswith('strategy_comparison.'):
            result['strategy_comparison'][field.split('.', 1)[1]] = rendered
        else:
            result[field] = rendered
    if parsed.preferred_scenario != 'abstain':
        scenario = pack['scenarios'].get(parsed.preferred_scenario) or {}
        if scenario.get('suggested_asking_price') is None or pack['valuation'].get('pricing_classification') == 'INDICATIVE_SCENARIO':
            raise ValueError('Cannot prefer unavailable or indicative-only scenario')
        if 'SC_' + parsed.preferred_scenario.upper() not in parsed.evidence_refs:
            raise ValueError('Preferred scenario lacks evidence reference')
    result['evidence_ids'] = [refs[r] for r in parsed.evidence_refs]
    result['verification'] = 'AI_INTERPRETATION_NOT_VERIFIED'
    return result


def _review_analysis(pack, validated, context, refs, *, api_key, model, timeout, transport, gateway):
    from gabay_pricing.ai_evaluation import evaluate
    catalog = _fact_catalog(context, refs)
    referenced = ['N' + k for k, v in catalog.items() if v['record'] in validated['evidence_refs']]
    cases = []
    for i, claim in enumerate(validated['quantitative_claims']):
        alias, _, field = claim['reference'].partition('.')
        fact_ids = ['N' + k for k, v in catalog.items() if v['record'] == alias and v['path'] == field]
        cases.append({'id': 'quantity_' + str(i), 'text': claim['rendered'], 'references': fact_ids})
    statements = {'assessment': validated['assessment'], 'reasoning': validated['reasoning'],
                  'missing_information': validated['missing_information'], 'next_steps': validated['next_steps'],
                  **{'strategy_' + k: v for k, v in validated['strategy_comparison'].items()}}
    cases.extend({'id': k, 'text': v, 'references': referenced} for k, v in statements.items())
    if validated['preferred_scenario'] != 'abstain':
        cases.append({'id': 'preference', 'text': validated['preferred_scenario'] + ': ' + validated['reasoning'], 'references': referenced})
    try:
        return evaluate(pack, validated, cases, api_key=api_key, model=model, timeout=timeout, transport=transport, gateway=gateway)
    except GatewayError as exc:
        return {'status': 'FAILED', 'error': exc.category, 'http_status': exc.status, 'retry_history': exc.history}
    except Exception:
        return {'status': 'FAILED'}


def _revalidate_saved_analysis(saved, pack, refs):
    raw = {k: v for k, v in saved.items() if k not in {'evidence_ids', 'verification'}}
    raw['quantitative_claims'] = [{k: v for k, v in claim.items() if k not in {'value', 'rendered'}}
                                  for claim in raw['quantitative_claims']]
    validated = _validate(raw, pack, refs)
    if validated != saved:
        raise ValueError('Saved analysis does not match validated output')
    return validated


def analyze_bundle(bundle, *, api_key, model=MODEL, max_units=3, timeout=45, research=None, transport=None, apartment_ids=None, diagnostics=None, progress=None, gateway=None, previous_units=None, durable_path=None):
    """No model output can modify the bundle, its prices, gates, or approval."""
    apartments = {str(a['id']): a for a in bundle['apartments']}
    ordered = sorted(bundle['results'], key=lambda r: (-(r['property_type'] in {'garden', 'duplex', 'triplex'}),
                     -len((r.get('pricing_decision') or {}).get('blockers', [])), -len(r.get('review_flags') or []),
                     r['property_type'], r.get('rooms') or 0, r.get('built_area') or 0,
                     tuple(apartments[str(r['apartment_id'])].get('source_rows') or [])))
    if apartment_ids is not None:
        rows = [r for r in ordered if str(r['apartment_id']) in set(apartment_ids)][:max_units]
    else:
        representatives, seen = [], set()
        for row in ordered:
            if row['property_type'] not in seen:
                representatives.append(row)
                seen.add(row['property_type'])
        rows = (representatives + [r for r in ordered if r not in representatives])[:max_units]
    if not api_key and gateway is None:
        if progress:
            progress(len(rows), len(rows))
        missing = {str(row['apartment_id']): {'status': 'CREDENTIAL_MISSING'} for row in rows
                   if str(row['apartment_id']) not in (previous_units or {})}
        # Keep previously validated units visible even when this run cannot analyze anything.
        return {'status': 'UNAVAILABLE', 'model': model,
                'units': {**dict(previous_units or {}), **missing}, 'calls': 0}
    # previous_units are contract-vetted by the caller (hash + evidence-pack match); carried-over
    # units are marked so reports can distinguish reused analyses from fresh ones.
    units = {k: {**v, 'reused': True} for k, v in (previous_units or {}).items()}
    calls = 0
    stopped = None

    def dump_durable():
        """Persist the full resume-compatible state after every unit so a crash or quota loss
        can never destroy completed analyses."""
        if not durable_path:
            return
        try:
            payload = {'snapshot_sha256': bundle.get('snapshot_sha256'),
                       'workbook_sha256': bundle.get('workbook_sha256'),
                       'code_sha256': bundle.get('code_sha256'), 'config': bundle.get('config'),
                       'results': bundle.get('results'), 'evidence_packs': bundle.get('evidence_packs'),
                       'ai_analysis': {'status': 'IN_PROGRESS', 'model': model, 'units': units,
                                       'calls': gateway.requests - initial_requests,
                                       'prompt_version': 'business-analyst-v1',
                                       'contract_version': AI_CONTRACT_VERSION}}
            path = Path(durable_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + '.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, path)
        except OSError as exc:
            if diagnostics is not None:
                diagnostics.append({'type': 'checkpoint_write_failed', 'error': type(exc).__name__})
            raise OSError(f'AI checkpoint write failed: {durable_path}') from exc
    if gateway is None:
        routes = {role: [('groq', model)] for role in ('analyst', 'critic')}
        gateway = LLMGateway(credentials={'groq': api_key}, allowed={'groq'}, routes=routes, timeout=timeout, transport=transport)
    initial_requests = gateway.requests
    with nullcontext(gateway) as client:
        for row in rows:
            aid = str(row['apartment_id'])
            prior = (previous_units or {}).get(aid) or {}
            review = prior.get('claim_review') or {}
            pack = (bundle.get('evidence_packs') or {}).get(aid) or build_evidence_pack(row, apartments[aid], bundle['config']).model_dump(mode='json')
            context, refs = _context(pack)
            if prior.get('status') == 'VALIDATED' and prior.get('provider') in gateway.allowed and prior.get('model'):
                try:
                    saved = _revalidate_saved_analysis(prior['analysis'], pack, refs)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
                else:
                    if (review.get('status') == 'SUCCESS' and review.get('provider') in gateway.allowed
                            and (review.get('review') or {}).get('claims')):
                        units[aid] = {**prior, 'reused': True, 'analyst_reused': True, 'critic_reused': True}
                        if progress:
                            progress(len(units), len(rows))
                        dump_durable()
                        continue
                    claim_review = _review_analysis(pack, saved, context, refs, api_key=api_key, model=model,
                                                    timeout=timeout, transport=transport, gateway=client)
                    units[aid] = {**prior, 'reused': True, 'analyst_reused': True, 'critic_reused': False, 'claim_review': claim_review}
                    if claim_review.get('error_category') == 'quota_exhausted':
                        stopped = 'quota_exhausted'
                    if progress:
                        progress(len(units), len(rows))
                    dump_durable()
                    if stopped:
                        break
                    continue
            schema = Response.model_json_schema()
            available_quantitative = _available_references(context)
            allowed_refs = [ref for choices in available_quantitative.values() for ref in choices]
            schema['$defs']['QuantitativeClaim']['properties']['reference']['enum'] = allowed_refs
            schema['$defs']['QuantitativeClaim']['properties']['claim_type']['enum'] = [kind for kind, choices in available_quantitative.items() if choices]
            schema['properties']['evidence_refs']['items']['enum'] = list(refs)
            schema['properties']['quantitative_claims'].pop('default', None)
            schema['required'].append('quantitative_claims')
            body = {'model': model, 'temperature': 0, 'max_completion_tokens': 2800,
                    'response_format': {'type': 'json_schema', 'json_schema': {'name': 'pricing_analyst', 'strict': True, 'schema': schema}},
                    'messages': [{'role': 'system', 'content': ('אתה יועץ תמחור למנהל שיווק. נתח את הדירה הספציפית בעברית, תוך שיקול דעת מקורי ולא תיאור כללי. '
                       'הסבר מה תומך באומדן ומה מחליש אותו, כולל השוואות בעייתיות, חסמים והנחות מדיניות. '
                       'השווה את שלוש האסטרטגיות: בעד ונגד כל אחת בנסיבות הדירה, ללא תחזיות ביקוש או זמני מכירה. '
                       'כשאין מחיר תרחיש זמין, הסבר את מגבלותיו ללא ציון אחוזי מדיניות: אחוז לבדו אינו מחיר או הוכחת כדאיות. '
                       'בחר כיוון מותנה לבדיקה אנושית רק אם הראיות מאפשרות להעדיפו; אחרת abstain. הפרד בין העדפה לאישור מחיר. '
                       'הצע פעולת מחקר אחת או שתיים בעלות הערך הגבוה ביותר ומה עשוי לשנות את הבחירה. '
                       'עסקאות מכר אינן מחירי ביקוש; מדגם נבחר אינו כלל השוק; חסר אינו מאומת ופרוקסי אינו שווי מאומת. '
                       'אין להמציא נתונים, מחירים או מקורות. ציין ב-evidence_refs רק מפתחות ראשיים מהקלט שתומכים בניתוח. '
                       'כתוב ניתוח בעברית. לכל ערך מספרי בטקסט חובה לצרף quantitative_claims מתאים; '
                       'השתמש אך ורק בערכים התואמים לשדות המובנים מהקלט. מזהים זמינים לפי סוג טענה: ' + json.dumps(available_quantitative, ensure_ascii=False) + '. '
                       'כל מקור שנבחר חייב להופיע גם ב-evidence_refs. אם אומדן שווי VALUE.estimated_market_value זמין, חובה לכלול לו טענת market_value ואת VALUE ב-evidence_refs; אחרת אל תמציא שווי. '
                       'אל תטען דבר על מרחק או מפרט שאינו מתועד. החזר רק JSON לפי הסכמה.')},
                       {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]}
            if model in {'openai/gpt-oss-20b', 'openai/gpt-oss-120b'}:
                body['reasoning_effort'] = 'low'
            started = time.monotonic()
            corrections = 0
            attempt_history = []
            pinned = None
            for attempt in range(3):
                response = None
                try:
                    response = client.complete('analyst', messages=body['messages'], schema=schema, max_tokens=2800, pinned=pinned)
                    usage = response.usage
                    attempt_history.extend(response.history)
                    validated = _validate(json.loads(response.content), pack, refs)
                    claim_review = _review_analysis(pack, validated, context, refs, api_key=api_key, model=model,
                                                    timeout=timeout, transport=transport, gateway=client)
                    units[aid] = {'status': 'VALIDATED', 'analysis': validated, 'claim_review': claim_review,
                                  'provider': response.provider, 'model': response.model, 'retry_history': attempt_history,
                                  'latency_ms': round((time.monotonic()-started)*1000),
                                  'usage': {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens') if k in usage}}
                    break
                except GatewayError as exc:
                    if diagnostics is not None:
                        diagnostics.append({**(exc.history[-1] if exc.history else {}), 'unit_id': aid, 'attempt': attempt + 1, 'type': 'gateway', 'category': exc.category, 'history': attempt_history + exc.history})
                    units[aid] = {'status': 'API_ERROR', 'error': exc.category, 'http_status': exc.status,
                                  'retry_history': attempt_history + exc.history, 'claim_review': {'status': 'NOT_RUN'},
                                  'latency_ms': round((time.monotonic()-started)*1000)}
                    if exc.category == 'quota_exhausted':
                        # Hard daily-quota wall on every authorized provider: stop the run cleanly instead
                        # of failing the remaining units repeatedly. Completed units stay durable; a later
                        # authorized run resumes the missing ones.
                        stopped = 'quota_exhausted'
                    break
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    reason = str(exc) if isinstance(exc, (NumericClaimError, EvidenceReferenceError)) or type(exc) is ValueError and str(exc) in {'Unknown evidence reference', 'Cannot prefer unavailable or indicative-only scenario', 'Preferred scenario lacks evidence reference'} else type(exc).__name__
                    if diagnostics is not None:
                        entry = {'unit_id': aid, 'attempt': attempt + 1, 'status': 200, 'type': 'local_validation', 'message': reason}
                        if response is not None:
                            entry.update(provider=response.provider, model=response.model, usage=response.usage,
                                         latency_ms=response.latency_ms, retry_history=attempt_history)
                        if isinstance(exc, NumericClaimError):
                            entry.update(field=exc.field, rule=exc.rule, reference=exc.reference, evidence_matches=exc.evidence_matches)
                        elif isinstance(exc, EvidenceReferenceError):
                            entry.update(field=exc.field, rule='missing_required_market_value' if str(exc) == 'Required market value reference missing' else 'invalid_or_unauthorized_reference', reference=exc.reference)
                        elif isinstance(exc, ValidationError):
                            first = exc.errors(include_input=False)[0]
                            allowed = {'assessment', 'strategy_comparison', 'quick_sale', 'balanced', 'high_opening', 'preferred_scenario', 'reasoning', 'evidence_refs', 'missing_information', 'next_steps', 'quantitative_claims', 'claim_type', 'reference', 'conclusion'}
                            field = '.'.join(str(x) if isinstance(x, int) or x in allowed else '[UNKNOWN]' for x in first['loc'])
                            entry.update(field=field, rule='schema_' + first['type'], reference='[REDACTED]')
                        diagnostics.append(entry)
                    correctable = isinstance(exc, (NumericClaimError, EvidenceReferenceError))
                    if correctable and corrections < 2:
                        corrections += 1
                        pinned = (response.provider, response.history[-1]['model'])
                        kind = next((k for k, choices in available_quantitative.items() if exc.reference in choices), None)
                        if corrections == 1 and isinstance(exc, EvidenceReferenceError):
                            codes = _reference_codes(context)
                            alias = exc.reference.partition('.')[0] if exc.reference != '[UNKNOWN]' else ''
                            correction = ('הטענה המספרית נפסלה (' + str(exc) + '). השתמש רק בקודי ההפניה המותרים: '
                                + json.dumps(codes, ensure_ascii=False) + '. '
                                + ('הוסף את ' + alias + ' ל-evidence_refs. ' if alias else 'הוסף ל-evidence_refs את שם הרשומה של הקוד שבחרת. ')
                                + ('אם VALUE.estimated_market_value זמין, חובה טענת market_value עם הקוד המתאים ו-VALUE ב-evidence_refs.'
                                   if available_quantitative['market_value'] else 'אם אין שווי זמין, החזר quantitative_claims כרשימה ריקה.'))
                        elif corrections == 1 and kind and exc.rule == 'missing_typed_claim':
                            correction = ('המספר שנכשל תואם לשדה מקור מורשה. החזר JSON מלא וציין ב-quantitative_claims '
                                + json.dumps({'claim_type': kind, 'reference': exc.reference, 'conclusion': 'reported_value'}, ensure_ascii=False)
                                + '; כלול את כינוי המקור ב-evidence_refs. כל מספר אחר חייב גם הוא בטענה תואמת; אל תמציא נתונים.')
                        else:
                            required = ('אם שווי השוק זמין, כלול ב-quantitative_claims את VALUE.estimated_market_value מסוג market_value וב-evidence_refs את VALUE. '
                                        if available_quantitative['market_value'] else 'החזר quantitative_claims כרשימה ריקה. ')
                            correction = ('החזר מחדש JSON מלא כאפשרות חלופית שמרנית: ניתוח איכותני בלבד, ללא מספרים, אחוזים, '
                                'מספרי דירות, מזהי ראיות, שמות אתרים ממוספרים או מספרי רשימות בטקסט החופשי. '
                                'אסור שתופיע אף ספרה (0-9) בכל שדה טקסט, כולל שלבים, טווחים וכמויות. '
                                + required + 'המחירים והנתונים יוצגו על ידי המנוע בנפרד. אל תמציא נתונים ואל תסיק מחיר כאשר אין ראיות מספיקות.')
                        body['messages'].append({'role': 'user', 'content': 'התשובה הקודמת לא עברה בדיקה בשדה ' + exc.field + '. ' + correction})
                        continue
                    units[aid] = {'status': 'VALIDATION_FAILED', 'error': reason, 'claim_review': {'status': 'NOT_RUN'}, 'retry_history': attempt_history, 'latency_ms': round((time.monotonic()-started)*1000)}
                    break
            if progress:
                progress(len(units), len(rows))
            dump_durable()
            if stopped:
                break
    calls = gateway.requests - initial_requests
    statuses = Counter(x['status'] for x in units.values())
    all_reviewed = all((u.get('claim_review') or {}).get('status') == 'SUCCESS' and all(c['verdict'] == 'SUPPORTED' for c in u['claim_review']['review']['claims']) for u in units.values())
    status = 'SUCCESS' if units and len(units) == statuses['VALIDATED'] and all_reviewed else 'PARTIAL' if statuses['VALIDATED'] else 'FAILED'
    return {'status': status, 'model': model, 'units': units, 'calls': calls, 'prompt_version': 'business-analyst-v1',
            'stopped_reason': stopped,
            'contract_version': AI_CONTRACT_VERSION,
            'selection': 'explicit IDs' if apartment_ids is not None else 'one per property type, prioritized by blockers and review flags'}
