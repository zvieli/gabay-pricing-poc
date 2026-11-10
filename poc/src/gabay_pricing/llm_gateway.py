"""Bounded OpenAI-compatible routing; no pricing or evidence decisions."""
from dataclasses import dataclass
import os
import time
import re
import json

import httpx
from openai import OpenAI, APIError, APIConnectionError, APIStatusError, APITimeoutError


# Only these model/schema combinations passed both real non-sensitive probes.
PROFILES = {
    ('groq', 'openai/gpt-oss-20b'): ('https://api.groq.com/openai/v1', 'max_completion_tokens', True),
    ('groq', 'openai/gpt-oss-120b'): ('https://api.groq.com/openai/v1', 'max_completion_tokens', True),
    ('gemini', 'gemini-3.1-flash-lite'): ('https://generativelanguage.googleapis.com/v1beta/openai/', 'max_tokens', False),
}
# Configured but excluded until both schemas pass live probes; free NVIDIA prohibits confidential input.
UNVERIFIED_PROFILES = {('kilo', 'nvidia/nemotron-3.5-lightning:free'): ('https://ai-gateway.kilo.ai/api/v1', 'max_tokens', False)}


@dataclass
class GatewayResult:
    content: str
    provider: str
    model: str
    usage: dict
    latency_ms: int
    history: list


def gemini_schema(schema):
    """Inline $ref/$defs and drop claim enums; Gemini rejects them even though local validation enforces both."""
    defs = schema.get('$defs') or {}
    def build(node):
        if isinstance(node, dict):
            if '$ref' in node:
                name = node['$ref'].rsplit('/', 1)[-1]
                if name in defs:
                    merged = {k: v for k, v in node.items() if k != '$ref'}
                    return build({**defs[name], **merged})
            return {k: build(v) for k, v in node.items() if k != '$defs'}
        if isinstance(node, list):
            return [build(v) for v in node]
        return node
    result = build(schema)
    items = ((result.get('properties') or {}).get('quantitative_claims') or {}).get('items') or {}
    properties = items.get('properties') or {}
    reference = properties.get('reference')
    if isinstance(reference, dict) and isinstance(reference.get('enum'), list):
        codes = {f'R{i}': ref for i, ref in enumerate(reference['enum'])}
        reference['enum'] = list(codes)
        reference['description'] = 'Reference codes: ' + json.dumps(codes, ensure_ascii=False)
    # The conclusion is a fixed marker; Gemini ignores its enum, so omit the field and validate it when present.
    properties.pop('conclusion', None)
    if 'required' in items:
        items['required'] = [name for name in items['required'] if name != 'conclusion']
    return result


class GatewayError(Exception):
    def __init__(self, category, *, status=None, history=None):
        super().__init__(category)
        self.category, self.status, self.history = category, status, history or []


class LLMGateway:
    def __init__(self, *, credentials, allowed, routes=None, max_requests=100, timeout=45, transport=None,
                 tpm_budget=None, max_wait=None, clock=time.monotonic, sleep=time.sleep):
        self.credentials = dict(credentials)
        self.allowed = set(allowed)
        self.routes = routes or {role: [('groq', 'openai/gpt-oss-20b'), ('gemini', 'gemini-3.1-flash-lite'), ('kilo', 'nvidia/nemotron-3.5-lightning:free')] for role in ('analyst', 'critic')}
        if max_requests < 1:
            raise ValueError('max_requests must be positive')
        self.max_requests, self.requests, self.timeout = max_requests, 0, timeout
        self.transport = transport
        self.cooldowns = {}
        self.clients = {}
        # Optional token-aware pacing (tokens per rolling 60s window); 0/disabled by default.
        # Per-provider budgets reflect each provider's actual limits; the global budget is the default.
        global_budget = int(os.environ['GABAY_LLM_TPM_BUDGET']) if tpm_budget is None and os.environ.get('GABAY_LLM_TPM_BUDGET') else (tpm_budget or 0)
        self.tpm_budgets = {}
        for provider in ('groq', 'gemini', 'kilo'):
            specific = os.environ.get(f'GABAY_LLM_TPM_BUDGET_{provider.upper()}')
            self.tpm_budgets[provider] = int(specific) if specific else global_budget
        # Longest single wait for a provider Retry-After window; default preserves the previous 60s cap.
        self.max_wait = int(os.environ['GABAY_LLM_MAX_WAIT']) if max_wait is None and os.environ.get('GABAY_LLM_MAX_WAIT') else (60 if max_wait is None else max_wait)
        self._clock, self._sleep = clock, sleep
        self._windows = {provider: [] for provider in self.tpm_budgets}  # (timestamp, total_tokens) per provider
        self.daily_quota = set()  # providers under an open daily-quota circuit breaker

    def _pace(self, provider):
        budget = self.tpm_budgets.get(provider, 0)
        if not budget:
            return
        window = self._windows.setdefault(provider, [])
        for _ in range(20):
            now = self._clock()
            window[:] = [(t, n) for t, n in window if now - t < 60]
            used = sum(n for _, n in window)
            # Conservative estimate: the next request can be as large as the largest recent one; never assume the average.
            estimate = max(4000, max((n for _, n in window), default=0))
            if used + estimate <= budget or not window:
                return
            wait = 60 - (now - window[0][0])
            if wait <= 0:
                return
            self._sleep(min(wait, self.max_wait))

    @classmethod
    def from_source(cls, source, *, dotenv_path, **kwargs):
        if source == 'dotenv':
            from dotenv import dotenv_values
            values = dotenv_values(dotenv_path)
        elif source == 'env':
            values = os.environ
        else:
            raise ValueError('Unknown credential source')
        names = {'groq': 'GROQ_API_KEY', 'gemini': 'GEMINI_API_KEY', 'kilo': 'KILO_API_KEY'}
        return cls(credentials={provider: values.get(name) for provider, name in names.items()}, **kwargs)

    def close(self):
        for client in self.clients.values():
            client.close()

    def _client(self, provider, model):
        profile = PROFILES[(provider, model)]
        if provider not in self.clients:
            opts = {'api_key': self.credentials[provider], 'base_url': profile[0], 'timeout': self.timeout, 'max_retries': 0}
            if self.transport is not None:
                opts['http_client'] = httpx.Client(transport=self.transport, timeout=self.timeout)
            self.clients[provider] = OpenAI(**opts)
        return self.clients[provider]

    def complete(self, role, *, messages, schema, max_tokens, pinned=None, _retries=0):
        if role not in self.routes:
            raise ValueError('Unknown LLM role')
        route = [pinned] if pinned is not None else self.routes[role]
        if pinned is not None and (pinned[0] not in self.allowed or pinned not in PROFILES):
            raise GatewayError('no_authorized_verified_provider')
        eligible = [(p, m) for p, m in route if p in self.allowed]
        if eligible and eligible[0] not in PROFILES:
            raise GatewayError('unverified_model', history=[{'provider': eligible[0][0], 'model': eligible[0][1], 'category': 'unverified_model'}])
        candidates = [(p, m) for p, m in eligible if (p, m) in PROFILES]
        excluded = [{'provider': p, 'model': m, 'category': 'unverified_model'} for p, m in eligible if (p, m) not in PROFILES]
        history = []
        if not candidates:
            raise GatewayError('no_authorized_verified_provider')
        for provider, model in candidates:
            remaining = self.cooldowns.get(provider, 0) - self._clock()
            if remaining > 0:
                history.append({'provider': provider, 'model': model, 'category': 'cooldown', 'retry_after': round(remaining, 2)})
                continue
            if not self.credentials.get(provider):
                history.append({'provider': provider, 'model': model, 'category': 'authentication'})
                last = GatewayError('authentication', history=history)
                continue
            if self.requests >= self.max_requests:
                raise GatewayError('budget_exhausted', history=history)
            profile = PROFILES[(provider, model)]
            self._pace(provider)
            request_schema = gemini_schema(schema) if provider == 'gemini' else schema
            args = {'model': model, 'messages': messages, profile[1]: max_tokens,
                    'response_format': {'type': 'json_schema', 'json_schema': {'name': 'pricing_analyst' if role == 'analyst' else 'evidence_critic', 'strict': True, 'schema': request_schema}}}
            if profile[2]:
                args['extra_body'] = {'reasoning_effort': 'low'}
            started = time.monotonic()
            self.requests += 1
            try:
                response = self._client(provider, model).chat.completions.create(**args)
                content = response.choices[0].message.content
                if not content:
                    raise GatewayError('invalid_response', history=history)
                latency = round((time.monotonic() - started) * 1000)
                usage = response.usage.model_dump(exclude_none=True) if response.usage else {}
                if self.tpm_budgets.get(provider):
                    self._windows.setdefault(provider, []).append((self._clock(), int(usage.get('total_tokens', 0) or 0)))
                history.append({'provider': provider, 'model': model, 'category': 'success', 'latency_ms': latency,
                                'usage': {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens') if k in usage}})
                return GatewayResult(content, provider, response.model or model, usage, latency, history)
            except (APIConnectionError, APITimeoutError) as exc:
                history.append({'provider': provider, 'model': model, 'category': 'connection', 'latency_ms': round((time.monotonic()-started)*1000)})
                last = GatewayError('connection', history=history)
            except APIStatusError as exc:
                status = exc.status_code
                category = 'rate_limit' if status == 429 else 'transient' if status >= 500 else 'authentication' if status in (401, 403) else 'request_error'
                entry = {'provider': provider, 'model': model, 'category': category, 'http_status': status, 'latency_ms': round((time.monotonic()-started)*1000)}
                try:
                    error = exc.response.json().get('error') or {}
                    code, message = error.get('code'), error.get('message') or ''
                    if code in {'invalid_api_key', 'rate_limit_exceeded', 'json_validate_failed', 'model_not_found', 'output_parse_failed', 'insufficient_quota'}:
                        entry['code'] = code
                    if code == 'rate_limit_exceeded':
                        kind = re.search(r'\b(OTPM|OTPD|TPM|TPD|RPM|RPD)\b', message)
                        amounts = re.search(r'Limit\s+(\d+),\s*Used\s+(\d+),\s*Requested\s+(\d+)', message)
                        if kind: entry['limit_kind'] = kind.group(1)
                        if amounts: entry.update(limit=int(amounts.group(1)), used=int(amounts.group(2)), requested=int(amounts.group(3)))
                    if code == 'json_validate_failed':
                        match = re.search(r"'(/(?:assessment|reasoning|missing_information|next_steps|claims|quality))'", message)
                        if match: entry['schema_path'] = match.group(1)
                    # Some providers state the real reset in the body while the header only carries a short
                    # value (Gemini RPD: header 60s, body 'retry in 8h42m33s'); the body wins when longer.
                    body_delay = re.search(r'retry in ((?:\d+h)?(?:\d+m)?(?:\d+(?:\.\d+)?s)?)', message, re.I)
                    if body_delay:
                        text = body_delay.group(1)
                        seconds = 0.0
                        for amount, unit in re.findall(r'(\d+(?:\.\d+)?)([hms])', text):
                            seconds += float(amount) * {'h': 3600, 'm': 60, 's': 1}[unit]
                        if seconds:
                            entry['body_retry_delay'] = int(seconds)
                    if code == 'rate_limit_exceeded' and not entry.get('limit_kind'):
                        lowered = message.lower()
                        if 'per day' in lowered or 'per-day' in lowered:
                            entry['limit_kind'] = 'RPD'
                        elif 'per minute' in lowered:
                            entry['limit_kind'] = 'RPM'
                except (ValueError, TypeError, AttributeError):
                    pass
                if status == 413 and entry.get('code') == 'rate_limit_exceeded':
                    category = entry['category'] = 'rate_limit'
                if category == 'rate_limit':
                    body_delay = entry.get('body_retry_delay')
                    try:
                        delay = max(0, float(exc.response.headers.get('retry-after', '60')))
                    except ValueError:
                        delay = 60
                    if body_delay and body_delay > delay:
                        delay = body_delay
                    # Daily-quota exhaustion (TPD/RPD or a daily-scale body delay) opens a circuit breaker:
                    # no further requests to that provider until the recorded reset, and the run stops cleanly.
                    if entry.get('limit_kind') in {'TPD', 'RPD'} or (body_delay or 0) > 300:
                        entry['category'] = category = 'quota_exhausted'
                        self.daily_quota.add(provider)
                    self.cooldowns[provider] = self._clock() + delay
                    entry['retry_after'] = delay
                history.append(entry)
                last = GatewayError(category, status=status, history=history)
                if entry.get('code') == 'json_validate_failed' and _retries < 1:
                    # The provider rejected its own generated output, not our request; retry once on the same provider.
                    try:
                        result = self.complete(role, messages=messages, schema=schema, max_tokens=max_tokens,
                                               pinned=(provider, model), _retries=_retries + 1)
                        result.history = history + result.history
                        return result
                    except GatewayError as exc:
                        raise GatewayError(exc.category, status=exc.status, history=history + exc.history) from None
                if category not in {'rate_limit', 'transient', 'quota_exhausted'}:
                    raise last from None
            except APIError:
                history.append({'provider': provider, 'model': model, 'category': 'invalid_response', 'latency_ms': round((time.monotonic()-started)*1000)})
                raise GatewayError('invalid_response', history=history) from None
        history.extend(excluded)
        if 'last' not in locals() and candidates and _retries < 1:
            # Every candidate was skipped for cooldown; wait out a bounded Retry-After instead of failing every remaining unit.
            delay = max(0, self.cooldowns.get(candidates[0][0], 0) - self._clock())
            if candidates[0][0] not in self.daily_quota and 0 < delay <= self.max_wait:
                self._sleep(delay)
                self.cooldowns.pop(candidates[0][0], None)
                try:
                    result = self.complete(role, messages=messages, schema=schema, max_tokens=max_tokens, pinned=pinned, _retries=_retries + 1)
                    result.history = history + result.history
                    return result
                except GatewayError as exc:
                    raise GatewayError(exc.category, status=exc.status, history=history + exc.history) from None
        retry_limit = 2 if 'last' in locals() and last.category == 'rate_limit' else 1
        if 'last' in locals() and _retries < retry_limit and len(candidates) == 1 and last.category in {'rate_limit', 'transient', 'connection'}:
            delay = max(0, self.cooldowns.get(candidates[0][0], 0) - self._clock())
            if delay <= self.max_wait:
                if delay:
                    self._sleep(delay)
                self.cooldowns.pop(candidates[0][0], None)
                try:
                    result = self.complete(role, messages=messages, schema=schema, max_tokens=max_tokens, pinned=pinned, _retries=_retries + 1)
                    result.history = history + result.history
                    return result
                except GatewayError as exc:
                    raise GatewayError(exc.category, status=exc.status, history=history + exc.history) from None
        broken = {p for p, _ in candidates if p in self.daily_quota and self.cooldowns.get(p, 0) > self._clock()}
        if candidates and broken >= {p for p, _ in candidates}:
            # Every authorized candidate hit a daily-quota wall; the caller must stop the run cleanly.
            raise GatewayError('quota_exhausted', status=getattr(last, 'status', None) if 'last' in locals() else 429, history=history)
        if 'last' in locals() and last.category != 'quota_exhausted':
            raise last
        other = next((h for h in reversed(history) if h.get('category') not in {'quota_exhausted', 'unverified_model'} and h.get('provider') not in broken), None)
        raise GatewayError(other['category'] if other else 'cooldown', status=other.get('http_status') if other else None, history=history)
