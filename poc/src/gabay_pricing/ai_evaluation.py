"""Optional, offline-triggered LLM quality review; never a pricing or approval gate."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gabay_pricing.ai_analysis import MODEL, _context, _fact_catalog
from gabay_pricing.llm_gateway import GatewayError, LLMGateway


class ClaimReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claim_id: str
    verdict: Literal['SUPPORTED', 'CONTRADICTED', 'INSUFFICIENT_EVIDENCE']
    reason: str = Field(min_length=1, max_length=500)
    supporting_fact_ids: list[str] = Field(max_length=64)
    contradicting_fact_ids: list[str] = Field(max_length=64)
    suggested_correction: str = Field(max_length=500)


class QualityReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unit_specificity: str
    strategy_comparison: str
    preference_grounding: str
    abstention: str
    actions_usefulness: str


class CriticResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claims: list[ClaimReview]
    quality: QualityReview


def collect_facts(pack, analysis):
    """Preserve the analyst's original IDs; new IDs cannot collide with them."""
    cited = {}
    def walk(v):
        if isinstance(v, dict):
            for f in v.get('supporting_facts', []) + v.get('limitations', []):
                cited[f['id']] = {k: f.get(k) for k in ('record', 'path', 'value', 'source_id', 'is_limitation')}
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(analysis)
    context, refs = _context(pack)
    catalog = _fact_catalog(context, refs)
    # Include potentially conflicting facts independently of the analyst's citations.
    for key, fact in catalog.items():
        cited['N' + key] = {k: fact.get(k) for k in ('record', 'path', 'value', 'source_id', 'is_limitation')}
    for i, sale in enumerate(pack.get('selected_sales', [])[3:], 4):
        cited[f'EXTRA_SALE_{i}'] = {'record': 'SALE', 'path': 'selected_sale', 'value': {k: sale['facts'].get(k) for k in ('area_basis', 'area_basis_semantics', 'verified_built_area', 'property_type')}, 'source_id': sale['id']}
    return cited


def evaluate(pack, analysis, cases, *, api_key, model=MODEL, timeout=60, transport=None, gateway=None, _reference_retry=False):
    """Assess claims once. A critic verdict is an opinion, not an evidence gate."""
    if not api_key and gateway is None:
        return {'status': 'UNAVAILABLE'}
    if gateway is None:
        gateway = LLMGateway(credentials={'groq': api_key}, allowed={'groq'},
                             routes={role: [('groq', model)] for role in ('analyst', 'critic')}, timeout=timeout, transport=transport)
    facts = collect_facts(pack, analysis)
    case_ids = [c['id'] for c in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError('Duplicate claim ID')
    if any(not set(c.get('references', [])) <= facts.keys() for c in cases):
        raise ValueError('Unknown analyst fact ID')
    # Expected labels belong to the evaluator, not the critic's prompt.
    claims = [{k: c[k] for k in ('id', 'text', 'references') if k in c} for c in cases]
    # Only claim-referenced facts leave the process; source identifiers remain local.
    used_refs = {ref for claim in claims for ref in claim.get('references', [])}
    compact_facts = {k: [v.get('record'), v.get('path'), v.get('value'), v.get('is_limitation')] for k, v in facts.items() if k in used_refs}
    body = {'model': model, 'temperature': 0, 'max_completion_tokens': 2500,
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'evidence_critic', 'strict': True, 'schema': CriticResponse.model_json_schema()}},
            'messages': [{'role': 'system', 'content': 'אתה מבקר עצמאי. הערך כל טענה מול העובדות; ציטוט קיים לבדו אינו מוכיח שהטקסט נתמך. '
                'כל עובדה בפורמט [רשומה, שדה, ערך, האם מגבלה]. בדוק את משמעות כל טענה בפועל מול העובדות המופיעות ב-references שלה בלבד; מזהה קיים אינו הוכחת תמיכה. '
                'הבחן בין עסקת מכר למחיר ביקוש, בין מדגם נבחר לכל השוק, ובין שטח מדווח למאומת. '
                'לכל טענה החזר verdict, סיבה קצרה של משפט יחיד עד מאה תווים, עד שלושה מזהי עובדות בכל רשימה ותיקון קצר. '
                'אין לחזור על עובדות או על ניסוח הטענה בסיבה; אם אין צורך בתיקון החזר מחרוזת ריקה. '
                'כאשר verdict הוא CONTRADICTED חובה להחזיר לפחות מזהה אחד ב-contradicting_fact_ids; כאשר הוא SUPPORTED חובה לפחות מזהה אחד ב-supporting_fact_ids. '
                'החזר INSUFFICIENT_EVIDENCE כשהמידע אינו מכריע. הערך גם ספציפיות, השוואת חלופות, ביסוס העדפה, הימנעות ופעולות. '
                'תוצאתך היא חוות דעת לא מאומתת, לא אישור מחיר. השתמש רק במזהי facts הנתונים.'},
                {'role': 'user', 'content': json.dumps({'unit_id': pack['unit_id'], 'facts': compact_facts, 'claims': claims, 'preferred_scenario': analysis.get('preferred_scenario')}, ensure_ascii=False)}]}
    if _reference_retry:
        body['messages'].append({'role': 'user', 'content': 'בתשובתך הקודמת הופיע מזהה שאינו ברשימת references של אותה טענה. החזר מחדש JSON מלא; השתמש לכל טענה רק במזהים מתוך references שלה, או קבע INSUFFICIENT_EVIDENCE עם רשימות ריקות.'})
    response = None
    try:
        response = gateway.complete('critic', messages=body['messages'], schema=CriticResponse.model_json_schema(), max_tokens=2500)
        parsed = CriticResponse.model_validate(json.loads(response.content))
        ids = [c.claim_id for c in parsed.claims]
        if len(ids) != len(case_ids) or set(ids) != set(case_ids):
            raise ValueError('Critic omitted or duplicated claims')
        case_refs = {c['id']: set(c.get('references', [])) for c in cases}
        for c in parsed.claims:
            if not set(c.supporting_fact_ids + c.contradicting_fact_ids) <= case_refs[c.claim_id]:
                raise ValueError('Critic cited fact outside claim references')
            if c.verdict == 'CONTRADICTED' and not c.contradicting_fact_ids:
                raise ValueError('Contradiction without cited fact')
            if c.verdict == 'SUPPORTED' and not c.supporting_fact_ids:
                raise ValueError('Support without cited fact')
        return {'status': 'SUCCESS', 'model': response.model, 'provider': response.provider, 'latency_ms': response.latency_ms,
                'retry_history': response.history, 'review': parsed.model_dump(), 'facts': facts,
                'usage': {k: v for k, v in response.usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                'notice': 'LLM critic opinion only; neither verified truth nor pricing approval'}
    except GatewayError as exc:
        return {'status': 'API_ERROR', 'http_status': exc.status, 'error_category': exc.category, 'retry_history': exc.history}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        if type(exc) is ValueError and str(exc) == 'Critic cited fact outside claim references' and not _reference_retry:
            retried = evaluate(pack, analysis, cases, api_key=api_key, model=model, timeout=timeout, transport=transport, gateway=gateway, _reference_retry=True)
            retried['retry_history'] = response.history + [{'provider': response.provider, 'model': response.model, 'category': 'validation_failed'}] + retried.get('retry_history', [])
            return retried
        safe = {'Critic omitted or duplicated claims', 'Critic cited fact outside claim references', 'Contradiction without cited fact', 'Support without cited fact'}
        reason = str(exc) if type(exc) is ValueError and str(exc) in safe else type(exc).__name__
        result = {'status': 'INVALID_RESPONSE', 'error': reason}
        if response is not None:
            result.update(provider=response.provider, model=response.model, latency_ms=response.latency_ms,
                          usage=response.usage, retry_history=response.history)
        return result
