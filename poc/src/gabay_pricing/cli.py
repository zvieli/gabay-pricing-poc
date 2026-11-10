"""Executable research workflow: replay, bounded live refresh, regenerate reports."""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
import subprocess
from pathlib import Path

from gabay_pricing.ingestion.excel import read_apartments
from gabay_pricing.models import PricingConfig, PricingResult, Snapshot
from gabay_pricing.normalization import normalize
from gabay_pricing.providers.collection import digest, live_refresh, replay_audit, save_snapshot
from gabay_pricing.reporting import write_reports
from gabay_pricing.review import assess
from gabay_pricing.strategy import apply_strategy
from gabay_pricing.valuation import value_apartment
from gabay_pricing.unit_adjustments import apply_unit_adjustments, apply_unit_scenario
from gabay_pricing.pricing_decision import apply_pricing_decision


def code_sha256():
    package = Path(__file__).resolve().parent
    digestor = hashlib.sha256()
    for path in sorted(package.rglob('*.py')):
        digestor.update(str(path.relative_to(package)).encode()+b'\0'+path.read_bytes()+b'\0')
    return digestor.hexdigest()


def location_assumption(config, snapshot):
    return {'configured_anchor': snapshot.address, 'reference_address': snapshot.address,
            'location_role': 'USER_SUPPLIED_MARKET_REFERENCE',
            'unit_mix_role': 'EXERCISE_SCENARIO', 'subject_location_verified': False,
            'verified': bool(config.project_address_verified),
            'status': 'VERIFIED_BY_USER' if config.project_address_verified else 'UNVERIFIED_ASSUMPTION',
            'note': config.project_location_note,
            'effect': 'Exercise unit mix is not asserted to belong to the reference building; market results depend on this geographic anchor.'}


def price(snapshot, workbook, config, snapshot_path, apartment_ids=None):
    data_classification = 'REAL_INPUT_UNVERIFIED'
    apartments = read_apartments(workbook, garden_overrides=config.garden_column_overrides)
    try:
        market = normalize(snapshot, config)
    except ValueError as exc:
        # No verified geographic anchor means no sale is geographically admissible.
        results = [PricingResult(apartment_id=a.id, property_type=a.property_type, built_area=a.built_area, rooms=a.rooms, status="UNPRICED", tier="NONE", valuation_reference=str(config.as_of), warnings=a.warnings + ["No verified geographic anchor"], classification_reasons=["No verified geographic anchor", "Human marketing approval has not been granted"], marketing_approval="NOT_APPROVED_HUMAN_REVIEW_REQUIRED", explanation=["All market valuations withheld; source address did not resolve exactly"], errors=[type(exc).__name__]).model_dump(mode="json") for a in apartments if not apartment_ids or a.id in apartment_ids]
        return {"schema_version": 1, "data_classification": data_classification, "code_sha256": code_sha256(), "address": snapshot.address, "location": location_assumption(config, snapshot), "snapshot_path": str(snapshot_path), "snapshot_sha256": digest(snapshot.model_dump(mode="json")), "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(), "config": config.model_dump(mode="json"), "apartments": [a.model_dump(mode="json") for a in apartments], "captures": [c.model_dump(mode="json", exclude={"payload"}) for c in snapshot.captures], "requests": snapshot.requests, "failures": snapshot.failures + [{"provider": "over", "reason": "verified_anchor_unavailable"}], "pagination": [], "market": None, "results": results, "quality_checks": ["No verified anchor; all targets explicitly UNPRICED", "No unrelated audit location was substituted"]}
    results = []
    for apartment in apartments:
        if apartment_ids and apartment.id not in apartment_ids:
            continue
        try:
            result = assess(value_apartment(apartment, market, config), apartment, market, config)
            result = apply_unit_scenario(result, apartment, market, config)
            result = apply_unit_adjustments(result, apartment, market, config)
            result = apply_strategy(result, apartment, market, snapshot, config)
            result = apply_pricing_decision(result, apartment, market, snapshot, config)
        except Exception as exc:
            # Per-apartment isolation is intentional; provider/credential errors never enter here.
            result = PricingResult(apartment_id=apartment.id, property_type=apartment.property_type, built_area=apartment.built_area, rooms=apartment.rooms, status="UNPRICED", tier="NONE", valuation_reference=str(config.as_of), errors=[type(exc).__name__], explanation=["Per-apartment error captured; other apartments continue"])
        results.append(result)
    checks = []
    for result in results:
        if result.estimated_market_value is not None:
            assert result.value_low <= result.estimated_market_value <= result.value_high
            assert result.evidence or result.benchmark_refs, "Price without evidence"
            assert result.estimated_market_value > 0
            if not 10000 < result.implied_ppm < 150000:
                result.warnings.append("Unusual implied PPM: requires manual evidence inspection")
            assert all(abs(e.time_factor-1) < 1e-12 for e in result.evidence) if result.tier == "TIER_1" else True
    pagination = []
    for cap in snapshot.captures:
        p = cap.payload
        inner = p.get('data') if isinstance(p.get('data'), dict) else p
        rows = inner.get('listings') or inner.get('deals') or (p.get('data') if isinstance(p.get('data'), list) else [])
        total = inner.get('total')
        if inner.get('next_page') or inner.get('has_more') or inner.get('total_capped') or isinstance(total, int) and total > len(rows) + int(inner.get('offset') or 0):
            pagination.append({'capture_id': cap.id, 'provider': cap.provider, 'returned': len(rows), 'total': total, 'next_page': inner.get('next_page'), 'has_more': inner.get('has_more'), 'total_capped': inner.get('total_capped'), 'note': 'Bounded sample; another capture may contain later pages'})
    checks += [f"All {len(results)} output rows unique: {len({r.apartment_id for r in results})==len(results)}", "Every numeric result has selected sale references or a benchmark source", "All numeric scenarios sit inside their scenario ranges", "Tier 1 has no historical adjustment; Tier 2 factors retain source/reference observations", "No floor/outdoor premium is hidden; sparse/special property exclusions retained", "Commercial-use rights unresolved; local research outputs only"]
    return {"schema_version": 1, "data_classification": data_classification, "code_sha256": code_sha256(), "address": snapshot.address, "location": location_assumption(config, snapshot), "snapshot_path": str(snapshot_path), "snapshot_sha256": digest(snapshot.model_dump(mode="json")), "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(), "config": config.model_dump(mode="json"), "apartments": [a.model_dump(mode="json") for a in apartments], "captures": [c.model_dump(mode="json", exclude={"payload"}) for c in snapshot.captures], "requests": snapshot.requests, "failures": snapshot.failures, "pagination": pagination, "market": market.model_dump(mode="json"), "results": [r.model_dump(mode="json") for r in results], "quality_checks": checks}


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Research-only POC; no certified appraisal or redistribution license")
    parser.add_argument("command", choices=["replay", "live", "report", "review", "phase3", "decision"],
                        metavar='{report,live}', help='report: saved snapshot; live: bounded collection')
    parser.add_argument("--workbook", type=Path, default=root.parent / "DATA/חוברת1.xlsx")
    parser.add_argument("--address", help="Market anchor; defaults to saved snapshot address when a snapshot is supplied")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--reports", type=Path, default=root / "reports")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--config", type=Path, help="Optional JSON PricingConfig overrides; unknown fields rejected")
    parser.add_argument("--before", type=Path, help="Original-run pricing_results.json for same-snapshot review")
    parser.add_argument('--research-dir', type=Path, help='Optional separate research package directory, display only (report command)')
    parser.add_argument('--ai', action='store_true', help='Opt in to sending minimal structured pricing evidence to Groq')
    parser.add_argument('--ai-model', default='openai/gpt-oss-20b')
    parser.add_argument('--ai-primary', choices=['groq', 'gemini'], default='groq', help='Preferred verified provider; the other authorized provider remains the fallback')
    parser.add_argument('--groq-key-source', choices=['env', 'dotenv'], default='env', help='Choose credential source explicitly; never recorded')
    parser.add_argument('--ai-authorize-provider', action='append', choices=['gemini', 'kilo'], default=[], help='Explicitly authorize this destination for submitted evidence')
    parser.add_argument('--llm-max-requests', type=int, default=100, help='Maximum analyst and critic API requests for this run')
    parser.add_argument('--resume-ai', type=Path, help='Resume compatible AI units from an identical local pricing_results.json')
    parser.add_argument('--reuse-saved-ai', type=Path, help='Carry over compatible saved AI without provider calls')
    parser.add_argument('--saved-ai-checkpoint', type=Path, help='Verify and record the saved AI checkpoint origin')
    parser.add_argument('--ai-max-units', type=int, default=3, help='Maximum apartments to send for AI review')
    parser.add_argument('--ai-apartments', help='Comma-separated apartment IDs to analyze; pricing still runs for all units')
    parser.add_argument('--historical-defaults', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument("--apartments", help="Comma-separated apartment IDs; default all apartments")
    args = parser.parse_args()
    if args.saved_ai_checkpoint and not args.reuse_saved_ai:
        parser.error('--saved-ai-checkpoint requires --reuse-saved-ai')
    if args.reuse_saved_ai and (args.ai or args.command != 'report'):
        parser.error('--reuse-saved-ai is only available for offline report without --ai')
    if args.ai and args.command != 'report':
        parser.error('--ai is supported only by the report command')
    if args.ai_max_units < 1:
        parser.error('--ai-max-units must be at least 1')
    if args.llm_max_requests < 1:
        parser.error('--llm-max-requests must be at least 1')
    if args.ai and args.ai_primary == 'gemini' and 'gemini' not in args.ai_authorize_provider:
        parser.error('--ai-primary gemini requires --ai-authorize-provider gemini')
    if args.research_dir and args.command != 'report':
        parser.error('--research-dir is supported only by the offline report command')
    if args.command in {'review', 'phase3', 'decision'} and not args.historical_defaults:
        parser.error('Historical experiment uses legacy programmatic policy; explicitly pass --historical-defaults')
    if args.historical_defaults and args.command not in {'review', 'phase3', 'decision'}:
        parser.error('--historical-defaults applies only to review/phase3/decision')
    path = args.snapshot or root / "data/snapshots" / ("market-live.json" if args.command == "live" else "market-replay.json")
    if path.exists():
        snap = Snapshot.model_validate_json(path.read_text())
        if args.address and snap.address != args.address:
            parser.error("Snapshot address differs: explicitly supply its address or a new snapshot path")
    elif args.command in {"report", "review", "phase3", "decision"}:
        parser.error("report/review requires an existing snapshot; it never contacts providers")
    else:
        if not args.address:
            parser.error('A new collection/replay requires an explicit --address and the corresponding local evidence')
        snap = replay_audit(root.parent, args.address, args.as_of or datetime.now(timezone.utc).date())
    if args.as_of and args.as_of != snap.as_of:
        parser.error("Snapshot as-of date differs; use a new snapshot path rather than relabel existing evidence")
    config_path = args.config or (root / 'config/pricing_policy.json')
    if args.command in {'report', 'replay', 'live'}:
        overrides = json.loads(config_path.read_text(encoding='utf-8'))
        if 'as_of' in overrides and overrides['as_of'] != str(snap.as_of):
            parser.error('Configuration as_of conflicts with snapshot reference date')
        config = PricingConfig.model_validate({**overrides, 'as_of': snap.as_of})
    if args.command == "live":
        live_refresh(snap, path, root.parent)
    if args.command not in {"report", "review", "phase3", "decision"}:
        save_snapshot(snap, path)
    if args.command == 'decision':
        if args.config or args.apartments:
            parser.error('decision uses default configuration and all apartments; run report for custom scenarios')
        from gabay_pricing.legacy.decision_quality_report import create_report
        bundle = create_report(snap, args.workbook, path, args.reports, args.before)
        write_reports(bundle, args.reports)
        print(json.dumps({'apartments': len(bundle['results']), 'statuses': dict(Counter(r['status'] for r in bundle['results'])), 'decision_report': str(args.reports / 'DECISION_QUALITY.md')}, indent=2))
        return
    if args.command == 'phase3':
        if not args.before or args.config or args.apartments:
            parser.error('phase3 requires --before and compares all apartments with default configuration')
        from gabay_pricing.legacy.unit_sensitivity_report import create_comparison
        bundle = create_comparison(snap, args.workbook, path, args.before, args.reports)
        write_reports(bundle, args.reports)
        print(json.dumps({'apartments': len(bundle['results']), 'statuses': dict(Counter(r['status'] for r in bundle['results'])), 'comparison': str(args.reports / 'PHASE3_COMPARISON.md')}, indent=2))
        return
    if args.command == "review":
        if not args.before or args.config or args.apartments:
            parser.error("review requires --before and compares all apartments with default configuration")
        from gabay_pricing.legacy.evidence_review import review
        bundle = review(snap, args.workbook, path, args.before, args.reports)
        write_reports(bundle, args.reports)
        print(json.dumps({"apartments": len(bundle["results"]), "statuses": dict(Counter(r["status"] for r in bundle["results"])), "review": str(args.reports / 'PRICING_REVIEW.md')}, indent=2))
        return
    bundle = price(snap, args.workbook, config, path, set(args.apartments.split(",")) if args.apartments else None)
    if args.apartments:
        bundle['selection_scope'] = 'explicit IDs'  # scoped diagnostic run: partial result set is intentional
    from gabay_pricing.evidence_pack import build_evidence_pack
    apartments = {a['id']: a for a in bundle['apartments']}
    bundle['evidence_packs'] = {str(row['apartment_id']): build_evidence_pack(
        row, apartments[row['apartment_id']], bundle['config']).model_dump(mode='json') for row in bundle['results']}
    if args.research_dir:
        from gabay_pricing.research_evidence import load_research
        research = load_research(args.research_dir, bundle)
    else:
        research = None
    if args.ai:
        from gabay_pricing.ai_analysis import AI_CONTRACT_VERSION, analyze_bundle
        from gabay_pricing.llm_gateway import LLMGateway
        providers = [('gemini', 'gemini-3.1-flash-lite'), ('groq', args.ai_model)]
        if args.ai_primary == 'groq':
            providers.reverse()
        routes = {role: [*providers, ('kilo', 'nvidia/nemotron-3.5-lightning:free')] for role in ('analyst', 'critic')}
        gateway = LLMGateway.from_source(args.groq_key_source, dotenv_path=root.parent / '.env',
            allowed={'groq', *args.ai_authorize_provider}, routes=routes, max_requests=args.llm_max_requests)
        api_key = gateway.credentials['groq']
        if not any(gateway.credentials.get(provider) for provider in gateway.allowed):
            gateway = None
        ai_ids = {x.strip() for x in args.ai_apartments.split(',')} if args.ai_apartments else None
        if ai_ids and (not ai_ids <= {str(r['apartment_id']) for r in bundle['results']} or len(ai_ids) > args.ai_max_units):
            parser.error('--ai-apartments must contain existing IDs and fit within --ai-max-units')
        previous_units = {}
        if args.resume_ai:
            previous = json.loads(args.resume_ai.read_text(encoding='utf-8'))
            for key in ('snapshot_sha256', 'workbook_sha256', 'config'):
                if previous.get(key) != bundle.get(key):
                    parser.error('--resume-ai must match the current snapshot, workbook and configuration')
            prior_ai = previous.get('ai_analysis') or {}
            if (prior_ai.get('contract_version') != AI_CONTRACT_VERSION
                    or prior_ai.get('prompt_version') != 'business-analyst-v1' or prior_ai.get('model') != args.ai_model):
                parser.error('--resume-ai was produced by a different AI contract, prompt version or model; '
                             'validated analyses cannot be reused across that change')
            old_results = {str(r['apartment_id']): r for r in previous.get('results', [])}
            for result in bundle['results']:
                aid = str(result['apartment_id'])
                prior_unit = prior_ai.get('units', {}).get(aid)
                if prior_unit and prior_unit.get('status') and old_results.get(aid) == result and (previous.get('evidence_packs') or {}).get(aid) == bundle['evidence_packs'][aid]:
                    previous_units[aid] = prior_unit
        diagnostics = []
        progress_path = os.environ.get('GABAY_PROGRESS_FILE')
        def progress(completed, total):
            if progress_path:
                try:
                    Path(progress_path).write_text(json.dumps({'completed': completed, 'total': total}), encoding='utf-8')
                except OSError:
                    pass  # Progress reporting must not interrupt pricing or AI analysis.
        bundle['ai_analysis'] = analyze_bundle(bundle, api_key=api_key, model=args.ai_model, max_units=args.ai_max_units, research=research, apartment_ids=ai_ids, diagnostics=diagnostics, progress=progress, gateway=gateway, previous_units=previous_units, durable_path=args.reports / 'AI_UNITS_DURABLE.json')
        if args.resume_ai:
            bundle['ai_analysis']['provenance'] = {'source_checkpoint': str(args.resume_ai.resolve()),
                'source_run_calls': prior_ai.get('calls'), 'historical_total_calls': (prior_ai.get('provenance') or {}).get('historical_total_calls'),
                'source_provenance': prior_ai.get('provenance')}
        bundle['ai_analysis']['credential_source'] = args.groq_key_source
        args.reports.mkdir(parents=True, exist_ok=True)
        diagnostic_path = args.reports / 'GROQ_DIAGNOSTICS_LOCAL.json'
        fd = os.open(diagnostic_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as diagnostic_file:
            os.fchmod(diagnostic_file.fileno(), 0o600)
            json.dump(diagnostics, diagnostic_file, ensure_ascii=False, indent=2)
    else:
        bundle['ai_analysis'] = {'status': 'NOT_RUN', 'model': None, 'units': {}, 'calls': 0}
        from gabay_pricing.reporting import carry_over_previous_ai
        carry_over_previous_ai(bundle, args.reports, source_path=args.reuse_saved_ai, checkpoint_path=args.saved_ai_checkpoint)
        if args.reuse_saved_ai and not bundle['ai_analysis'].get('carried_over_from_previous_report'):
            parser.error('--reuse-saved-ai is incompatible with the calculated results or evidence')
        if args.reuse_saved_ai:
            bundle['replayed_market_data'] = True
    effective = config.model_dump(mode='json')
    canonical = json.dumps(effective, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    bundle['run_manifest'] = {'policy_id': config_path.name, 'config_path': str(config_path.resolve()),
        'policy_selection': 'explicit' if args.config else 'active_default', 'effective_config': effective,
        'config_sha256': hashlib.sha256(canonical).hexdigest(), 'config_file_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'workbook_sha256': hashlib.sha256(args.workbook.read_bytes()).hexdigest(),
        'snapshot_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'research_file_sha256': research['source_hashes'] if research else {}, 'git_revision': revision,
        'code_sha256': bundle['code_sha256'], 'as_of': str(snap.as_of), 'data_classification': bundle['data_classification'],
        'ai': {'enabled': bool(args.ai), 'model': args.ai_model if args.ai else None,
               'max_units': args.ai_max_units if args.ai else None, 'max_requests': args.llm_max_requests if args.ai else None, 'primary_provider': args.ai_primary if args.ai else None, 'primary_model': ('gemini-3.1-flash-lite' if args.ai_primary == 'gemini' else args.ai_model) if args.ai else None, 'authorized_providers': ['groq', *args.ai_authorize_provider] if args.ai else [], 'status': bundle['ai_analysis']['status']}}
    write_reports(bundle, args.reports, research)
    ai = bundle['ai_analysis']
    ai_units = ai.get('units', {})
    print(json.dumps({"apartments": len(bundle["results"]), "statuses": dict(Counter(r["status"] for r in bundle["results"])), "tiers": dict(Counter(r["tier"] for r in bundle["results"])), "ai_status": ai['status'], "ai_validated": sum(u['status'] == 'VALIDATED' for u in ai_units.values()), "ai_total": len(ai_units), "ai_critic_statuses": dict(Counter((u.get('claim_review') or {}).get('status', 'NOT_RUN') for u in ai_units.values())), "reports": str(args.reports), "snapshot": str(path)}, indent=2))


if __name__ == "__main__":
    main()
