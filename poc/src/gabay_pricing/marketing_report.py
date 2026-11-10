"""Marketing-facing Hebrew deliverables generated from real pipeline results.

Every figure is either traceable to a source record, computed from the configured
policy, or explicitly labelled as an indicative scenario. This is not an appraisal.
"""
import csv
import json
from collections import Counter
from gabay_pricing.csv_safety import SafeWriter
from html import escape
from pathlib import Path
from statistics import median


def _money(value):
    return '—' if value is None else f'₪{value:,.0f}'


def _pct(value):
    return '—' if value is None else f'{value:.1%}'


def _text(value):
    return '—' if value is None or value == '' else str(value)


_HE_TYPES = {'apartment': 'דירה', 'garden': 'דירת גן', 'duplex': 'דופלקס', 'triplex': 'טריפלקס',
             'penthouse': 'פנטהאוז', 'roof': 'דירת גג', 'unknown': 'לא ידוע'}


def _type(value):
    return _HE_TYPES.get(str(value), _text(value))


def _component(row, attribute):
    for item in (row.get('unit_adjustments') or {}).get('components', []):
        if item.get('attribute') == attribute:
            return item
    return {}


def _ask(row, key):
    entry = (row.get('marketing_strategies') or {}).get(key, {})
    return entry.get('suggested_asking_price'), entry.get('indicative_asking_price')


def _asks_text(row):
    parts = []
    for label, key in [('מהיר', 'quick_sale'), ('מאוזן', 'balanced'), ('גבוה', 'high_opening')]:
        supported, indicative = _ask(row, key)
        if supported is not None:
            parts.append(f'{label}: {_money(supported)}')
        elif indicative is not None:
            parts.append(f'{label}: {_money(indicative)} (אינדיקטיבי)')
        else:
            parts.append(f'{label}: —')
    return ' | '.join(parts)


def _kinds(row):
    return ' / '.join(str(x) for x in [row.get('property_type'), row.get('rooms'), row.get('built_area')])


def _unit_rows(bundle):
    rows = []
    for row in bundle['results']:
        unit = row.get('unit_adjustments') or {}
        floor = _component(row, 'floor')
        orientation = _component(row, 'orientation')
        balcony = _component(row, 'balcony')
        garden = _component(row, 'garden')
        scenarios = [_ask(row, key) for key in ('quick_sale', 'balanced', 'high_opening')]
        confidence = row.get('pricing_classification') or unit.get('classification') or 'INSUFFICIENT_EVIDENCE'
        status = ('תרחיש מחיר לבדיקה' if any(supported is not None for supported, _ in scenarios) else
                  'תרחיש אינדיקטיבי' if any(indicative is not None for _, indicative in scenarios) or row.get('adjusted_market_value') is not None else 'ללא מחיר')
        ppms = [e['adjusted_ppm'] for e in (row.get('evidence') or []) if e.get('adjusted_ppm')]
        rows.append({
            'id': row['apartment_id'], 'type': row['property_type'], 'rooms': row['rooms'],
            'evidence_count': len(row.get('evidence') or []), 'ppm_median': round(median(ppms)) if ppms else None,
            'floor': ', '.join(str(x) for x in (next((a['floors'] for a in bundle['apartments'] if a['id'] == row['apartment_id']), []))) or '—',
            'built': row['built_area'], 'outdoor': (next((a for a in bundle['apartments'] if a['id'] == row['apartment_id']), {}) or {}),
            'base': row.get('estimated_market_value'), 'base_range': [row.get('value_low'), row.get('value_high')],
            'floor_amount': floor.get('amount_ils'), 'orientation_amount': orientation.get('amount_ils'),
            'balcony_sensitivity': balcony.get('sensitivity_amount_ils'), 'garden_sensitivity': garden.get('sensitivity_amount_ils'),
            'adjusted': row.get('adjusted_market_value'),
            'adjusted_range': [row.get('adjusted_value_low'), row.get('adjusted_value_high')],
            'asks': _asks_text(row), 'ask_values': {key: _ask(row, key) for key in ['quick_sale', 'balanced', 'high_opening']},
            'confidence': confidence, 'status': status,
            'positioning': row.get('market_positioning'), 'warnings': row.get('warnings') or [],
            'decision': row.get('pricing_decision') or {}, 'unit': unit,
        })
    return rows


def _revenue(bundle):
    scenarios = []
    for key, label in [('quick_sale', 'מכירה מהירה'), ('balanced', 'מאוזן'), ('high_opening', 'פתיחה גבוהה')]:
        supported = []
        indicative = []
        for row in bundle['results']:
            suggested, indicative_price = _ask(row, key)
            if suggested is not None:
                supported.append(suggested)
            elif indicative_price is not None:
                indicative.append(indicative_price)
        scenarios.append({
            'key': key, 'label': label, 'supported_count': len(supported),
            'supported_total': sum(supported), 'supported_average': round(sum(supported) / len(supported)) if supported else None,
            'indicative_count': len(indicative), 'indicative_total': sum(indicative),
            'coverage': len(supported) / len(bundle['results']) if bundle['results'] else 0,
        })
    return scenarios


def _competitors(bundle):
    market = bundle.get('market') or {}
    rows = []
    for project in market.get('projects', []):
        if project.get('rejections') or not project.get('price'):
            continue
        area = project.get('area')
        rows.append({
            'name': project.get('name'), 'distance_m': project.get('distance_m'),
            'type': project.get('property_type'), 'rooms': project.get('rooms'), 'area': area,
            'price': project.get('price'), 'ppm': round(project['price'] / area) if area else None,
            'basis': project.get('price_basis'), 'date': project.get('date'),
            'limitations': '; '.join(project.get('warnings') or []) or
                ('מחיר פתיחה בלבד: אין שטח יחידה, לכן אין ₪/מ"ר ואין מחיר שווה-ערך ליחידה' if not area else 'הצעה שיווקית, לא עסקה'),
        })
    rows.sort(key=lambda x: (x['distance_m'] if x['distance_m'] is not None else 10**9, x['price'] or 0))
    return rows


def _research_html(bundle, research):
    if not research:
        return '<div class="card"><h2>מחקר שוק</h2><p>לא נטענה חבילת ראיות מחקר; השוואה מחקרית אינה זמינה.</p></div>'
    from gabay_pricing.research_evidence import research_matches
    sections = []
    apartments = {a['id']: a for a in bundle['apartments']}
    for unit in bundle['results']:
        apartment = apartments[unit['apartment_id']]
        offers = research_matches(apartment, research, bundle['config'], bundle['address'])
        scenario = unit.get('marketing_strategies', {}).get('balanced', {})
        price = scenario.get('suggested_asking_price') or scenario.get('indicative_asking_price')
        rows = []
        for offer in offers:
            norm = round(offer['price'] / offer['area'] * apartment['built_area'])
            dates = f"פורסם: {offer['published_at'] or 'לא ידוע'}; עודכן: {offer['updated_at'] or 'לא ידוע'}; נאסף: {offer['collected_at']}"
            check = offer.get('additional_source_check') or {}
            if check.get('observed_published_at') and check['observed_published_at'] != offer['published_at']:
                dates += f"; בדף בבדיקה נוספת: {check['observed_published_at']} — פער תאריך לבדיקה"
            link = '<a href="' + escape(offer['source_url'], quote=True) + '" rel="noopener noreferrer">' + escape(offer['name']) + '</a>'
            mirror = offer.get('possible_duplicate_url') or ''
            if mirror.startswith(('https://', 'http://')):
                link += '<br><a href="' + escape(mirror, quote=True) + '">פרסום נוסף — ייתכן אותו נכס</a>'
            limitations = f"{offer['limitations']}; סטטוס: {offer['availability']}; סוג: {offer['property_type']}; דיוק גאוגרפי: {offer['geo_basis']}; מרחק: {offer['distance_m'] if offer['distance_m'] is not None else 'לא ידוע'}"
            cells = [offer['area'], _money(offer['price']), _money(offer['price']/offer['area']), _money(norm), dates, limitations]
            rows.append('<tr><td>' + link + '</td>' + ''.join('<td>' + escape(str(c)) + '</td>' for c in cells) + '</tr>')
        body = '<table><thead><tr><th>מודעה ומקור</th><th>שטח</th><th>מחיר ביקוש</th><th>₪/מ״ר</th><th>נרמול ליעד</th><th>תאריכים</th><th>מגבלות</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' if rows else '<p>לא נמצאו ראיות מחקר העומדות בתנאי ההתאמה לתצוגה.</p>'
        comparison = f'<p>התרחיש מעל {sum(o["price"] < price for o in offers)} מתוך {len(offers)} פרסומים במחיר גולמי; אלו אינם בהכרח נכסים עצמאיים או זמינים ואינם מיצוב שוק.</p>' if price is not None and offers else ''
        sections.append(f'<details><summary>דירה {escape(str(unit["apartment_id"]))} — {escape(str(apartment["built_area"]))} מ״ר, {escape(str(apartment["rooms"]))} חדרים; תרחיש מאוזן {_money(price)}</summary>{comparison}{body}</details>')
    conflicts = []
    for sale in research['sales']:
        if sale['verification'] != 'EXTERNAL_PRICE_CONFLICT_UNRESOLVED':
            continue
        sensitivities = '; '.join(f"דירה {x['apartment_id']}: {_money(x['baseline_value'])} → {_money(x['without_sale_value'])}" for x in research['conflict_sensitivity'] if x['sale_id'] == sale['id']) or 'אין שימוש בעסקה בדירות שנבחרו'
        url = sale.get('external_price_source') or sale.get('source_url') or ''
        link = f'<a href="{escape(url, quote=True)}">מקור חיצוני</a>' if url.startswith(('https://', 'http://')) else 'מקור לא זמין'
        conflicts.append(f'<div class="warn">{escape(sale["id"])} — EXTERNAL_PRICE_CONFLICT_UNRESOLVED: {_money(sale["model_price"])} לעומת {_money(sale["external_price"])}; {link}; {escape(sale["limitations"])}. רגישות ללא החלפת עסקאות: {escape(sensitivities)}</div>')
    return '<div class="card" id="research-cases"><h2>מחקר שוק מתועד — ILLUSTRATIVE_COMPARISON</h2><p>מחירי בקשה ולא עסקאות; זמינות, סוג נכס, מיקום מדויק ועצמאות אינם בהכרח מאומתים. המחקר אינו משפיע על מחיר או שערי החלטה. נרמול = מחיר × שטח יעד / שטח מודעה; אינו מתקן מפרט, קומה או מיקום. אין להסיק שווי שוק או מיצוב מאושר.</p>' + ''.join(conflicts) + ''.join(sections) + '</div>'


def _ai_html(bundle):
    ai = bundle.get('ai_analysis') or {'status': 'NOT_RUN', 'units': {}}
    rows = []
    for unit in bundle['results']:
        item = ai.get('units', {}).get(unit['apartment_id'])
        if not item:
            detail = 'NOT_ANALYZED — לא בוצע ניתוח לדירה זו; מחירון ומצב האישור אינם תלויים ב־AI.'
        elif item['status'] != 'VALIDATED':
            detail = 'הניתוח לא זמין: ' + item['status'] + '; סיבה: ' + str(item.get('error') or item.get('http_status') or 'לא נמסרה') + '. סטטוס בודק הטענות: NOT_RUN. המחירון לא השתנה.'
        else:
            analysis = item['analysis']
            from gabay_pricing.ai_analysis import _context
            pack = (bundle.get('evidence_packs') or {}).get(str(unit['apartment_id']))
            context, refs = _context(pack) if pack else ({}, {})
            review = item.get('claim_review') or {}
            supported = review.get('status') == 'SUCCESS' and all(c['verdict'] == 'SUPPORTED' for c in review['review']['claims'])
            if not supported:
                detail = '<p>הפרשנות הוסתרה: בדיקת הטענות נכשלה או מצאה טענות שאינן נתמכות. נדרש אימות אנושי.</p>'
            else:
                detail = '<p><b>הערכת מצב — פרשנות AI לא מאומתת:</b> ' + escape(analysis['assessment']) + '</p>'
            if supported:
                detail += '<b>השוואת אסטרטגיות (מחירים מהמנוע בלבד):</b><ul>'
                for label, key in [('מהיר', 'quick_sale'), ('מאוזן', 'balanced'), ('גבוה', 'high_opening')]:
                    suggested, indicative = _ask(unit, key)
                    detail += '<li>' + label + ' — ' + escape(_money(suggested if suggested is not None else indicative)) + (' (אינדיקטיבי)' if suggested is None and indicative is not None else '') + ': ' + escape(analysis['strategy_comparison'][key]) + '</li>'
                detail += '</ul><p><b>כיוון לבחינה אנושית בלבד:</b> ' + escape(analysis['preferred_scenario']) + ' — ' + escape(analysis['reasoning']) + '</p>'
                detail += '<p><b>מידע חסר:</b> ' + escape(analysis['missing_information']) + '</p><p><b>פעולות המשך:</b> ' + escape(analysis['next_steps']) + '</p>'
                detail += ''.join('<p>' + escape(c['rendered']) + '</p>' for c in analysis.get('quantitative_claims', []))
            detail += '<p>סטטוס אנליסט: VALIDATED; ספק/מודל אנליסט: ' + escape(str(item.get('provider') or 'לא זמין') + ' / ' + str(item.get('model') or 'לא זמין')) + '; שימוש חוזר באנליסט: ' + str(bool(item.get('analyst_reused'))) + '; סטטוס בודק הטענות: ' + escape(str(review.get('status', 'NOT_RUN'))) + '; ספק/מודל בודק: ' + escape(str(review.get('provider') or 'לא זמין') + ' / ' + str(review.get('model') or 'לא זמין')) + '; שימוש חוזר בבודק: ' + str(bool(item.get('critic_reused'))) + '.</p>'
            for stage, history in [('Analyst', item.get('retry_history')), ('Critic', review.get('retry_history'))]:
                if history:
                    detail += '<p>' + stage + ' היסטוריית ספקים: ' + escape(' → '.join(f"{h.get('provider', '?')}/{h.get('model', '?')}: {h.get('category', '?')}" for h in history)) + '</p>'
            if review.get('status') == 'SUCCESS':
                detail += '<p>כל הכרעות הבודק: ' + escape('; '.join(f"{c['claim_id']}: {c['verdict']}" for c in review['review']['claims'])) + '</p>'
            detail += '<p><b>טענות שסותרות את הראיות או דורשות בדיקה:</b> '
            if review.get('status') == 'SUCCESS':
                flagged = [c for c in review['review']['claims'] if c['verdict'] != 'SUPPORTED']
                detail += escape('; '.join(f"{c['claim_id']}: {c['verdict']}" for c in flagged) or 'לא סומנו טענות')
            else:
                detail += 'בדיקת הטענות אינה זמינה; כל הטענות דורשות אימות אנושי'
            detail += '. הבדיקה היא חוות דעת של מודל נוסף, לא הוכחה לאמיתות הטענות.</p>'
            detail += '<details><summary>ראיות מרכזיות ונתונים מקוריים (לא אימות חיצוני)</summary><ul>'
            for alias in analysis['evidence_refs']:
                detail += '<li><code dir="ltr">' + escape(alias + ' / ' + str(refs.get(alias, ''))) + '</code><pre dir="auto">' + escape(json.dumps(context.get(alias), ensure_ascii=False, indent=2)) + '</pre></li>'
            detail += '</ul></details>'
        status = 'ניתוח זמין' if item and item['status'] == 'VALIDATED' else 'NOT_ANALYZED' if not item else 'לא זמין'
        scenario = (unit.get('marketing_strategies') or {}).get('balanced') or {}
        price = scenario.get('suggested_asking_price')
        indicative = scenario.get('indicative_asking_price')
        basis = 'סיכום מנוע: אומדן ' + _money(unit.get('estimated_market_value')) + '; מאוזן ' + _money(price if price is not None else indicative) + (' (אינדיקטיבי)' if price is None and indicative is not None else '') + '; אישור ' + str((unit.get('pricing_decision') or {}).get('approval') or 'לא ידוע') + '.'
        rows.append('<details><summary>דירה ' + escape(str(unit['apartment_id'])) + ' — ' + status + '</summary><p>' + escape(basis) + '</p>' + (detail if item and item['status'] == 'VALIDATED' else '<p>' + escape(detail) + '</p>') + '</details>')
    completed = sum(x.get('status') == 'VALIDATED' for x in ai.get('units', {}).values())
    from collections import Counter
    used = Counter((x.get('provider'), x.get('model')) for x in ai.get('units', {}).values() if x.get('status') == 'VALIDATED')
    models = '; '.join(f"{p or '?'} / {m or '?'} ×{n}" for (p, m), n in sorted(used.items())) or 'לא הופעל'
    provenance = ai.get('provenance') or {}
    provenance_html = ('<p>ניתוח AI שמור משוחזר; קריאות בהרצה זו: ' + str(ai.get('calls', 0)) +
        '; סך קריאות היסטוריות: ' + escape(str(provenance.get('historical_total_calls') if provenance.get('historical_total_calls') is not None else 'לא ידוע')) +
        '; מקור דוח שמור: ' + escape(str(provenance.get('source_report') or 'לא ידוע')) +
        '; מחסום מקור: ' + escape(str(provenance.get('source_checkpoint') or 'לא תועד')) + '.</p>') if provenance else ''
    return '<div class="card"><h2>ניתוח AI — נפרד מהתמחור</h2>' + provenance_html + '<p>סטטוס הריצה: ' + escape(str(ai['status'])) + '; נותחו ' + str(completed) + ' מתוך ' + str(len(bundle['results'])) + ' דירות; ספק/מודל בפועל: ' + escape(models) + '. הטקסט הוא פרשנות AI לא מאומתת, לא עובדה שנבדקה; קישור לראיה אינו מוכיח את נכונות הפרשנות. המחירים מהמנוע בלבד; אין שינוי בשערים או באישור. נדרשת בדיקה אנושית.</p>' + ''.join(rows) + '</div>'


def _html(bundle, units, revenue, competitors, research=None):
    location = bundle.get('location') or {}
    verified = location.get('verified')
    replay = '<div class="warn"><b>הדגמה לא מקוונת: נתוני השוק משוחזרים מ-snapshot שמור; ניתוח ה-AI נשמר בהרצה קודמת ואינו ניתוח חדש.</b></div>' if bundle.get('replayed_market_data') else ''
    banner = replay + ('<div class="warn">תמהיל התרגיל נבחן בתרחיש מיקום ' + escape(str(location.get('configured_anchor') or bundle.get('address'))) +
              ' כעוגן שוק. שיוך התמהיל לבניין לא אומת ואין טענה שהוא משויך אליו.</div>')
    supported = sum(1 for r in units if r['confidence'] == 'EVIDENCE_SUPPORTED' or r['confidence'] == 'EVIDENCE_LIMITED_BENCHMARK')
    units_html = []
    for r in units:
        warnings = escape('; '.join(r['warnings'][:6])) or '—'
        units_html.append('<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in [
            r['id'], _type(r['type']), r['floor'], r['rooms'], r['built'], r['evidence_count'], _money(r['ppm_median']),
            _money(r['base']), _money(r['adjusted']), f"{_money(r['adjusted_range'][0])}–{_money(r['adjusted_range'][1])}",
            r['asks'], r['status'], _text(r['decision'].get('approval')), _text(r['decision'].get('status')), warnings]) + '</tr>')
    adjust_html = []
    for r in units:
        u = r['unit']
        adjust_html.append('<tr>' + ''.join(f'<td>{escape(_text(x))}</td>' for x in [
            r['id'], _money(r['floor_amount']), _money(r['orientation_amount']),
            _money(r['balcony_sensitivity']), _money(r['garden_sensitivity']),
            _money(u.get('applied_total')), u.get('policy_label', ''), '; '.join(u.get('warnings') or []) or '—']) + '</tr>')
    comp_html = []
    for c in competitors:
        comp_html.append('<tr>' + ''.join(f'<td>{escape(_text(x))}</td>' for x in [
            c['name'], f"{c['distance_m']:.0f}" if c['distance_m'] is not None else '—', _type(c['type']), c['rooms'], c['area'],
            _money(c['price']), _money(c['ppm']), c['basis'], c['date'] or '—', c['limitations']]) + '</tr>')
    revenue_html = []
    for s in revenue:
        revenue_html.append(f"<tr><td>{escape(s['label'])}</td><td>{s['supported_count']}</td><td>{_money(s['supported_total'])}</td>"
                            f"<td>{_money(s['supported_average'])}</td><td>{s['indicative_count']}</td><td>{_money(s['indicative_total'])}</td>"
                            f"<td>{_pct(s['coverage'])}</td></tr>")
    details = []
    sale_usage = Counter(sale_id for row in bundle['results'] for sale_id in {e['observation_id'] for e in row.get('evidence') or []})
    for r, raw in zip(units, bundle['results']):
        u, d, a = r['unit'], r['decision'], r['outdoor']
        base = u.get('baseline_value') if u.get('baseline_value') is not None else u.get('indicative_proxy_value')
        components = []
        for c in u.get('components', []):
            amount = c.get('amount_ils') if c.get('applied') else c.get('sensitivity_amount_ils')
            ratio = amount / base if amount is not None and base else None
            components.append(f"{c.get('attribute')}: {_money(amount)} ({_pct(ratio)}), " + ('הוחל' if c.get('applied') else 'רגישות בלבד / לא הוחל') + f"; {c.get('note') or ''}; {'; '.join(c.get('warnings') or [])}")
        present_components = {c.get('attribute') for c in u.get('components', [])}
        missing_components = [name for name in ('floor', 'orientation', 'balcony', 'garden') if name not in present_components]
        sales = []
        for e in raw.get('evidence') or []:
            sources = ', '.join(f"{ref.get('provider')} / {ref.get('endpoint')} / {ref.get('capture_id')}" for ref in e.get('refs') or []) or 'לא ידוע'
            sales.append(f"{e.get('observation_id')}: {e.get('sale_date')}, {_money(e.get('original_price'))}, שטח {e.get('area_basis')} ({e.get('area_basis_semantics')}), מרחק {e.get('distance_m')}, מקור העסקה: {sources}, אזהרות: {'; '.join(e.get('warnings') or [])}")
        competitive = d.get('competitive_evidence') or {}
        rejection_counts = competitive.get('rejection_counts') or {}
        top_rejections = sorted(rejection_counts.items(), key=lambda x: (-x[1], x[0]))[:4]
        assessments = d.get('selected_comparable_assessments') or []
        future_year = sum(bool(s.get('construction_year') and str(s.get('date') or '')[:4].isdigit() and s['construction_year'] > int(str(s['date'])[:4])) for s in assessments)
        attested_off_plan = sum((s.get('reviewer_attestation') or {}).get('construction_status') == 'off_plan' for s in assessments)
        text = (f"דירה {r['id']} — {_type(r['type'])}, {r['rooms']} חדרים, {r['built']} מ״ר, קומה {r['floor']}, כיוון {a.get('directions')}, מרפסת {a.get('balcony_area')}, גינה {a.get('garden_area')}. "
                f"מקור: {raw.get('tier')}, סיווג {r['confidence']}; בסיס {_money(base)} {'(פרוקסי בלבד)' if u.get('baseline_value') is None else ''}; "
                f"התאמות: {'; '.join(components)}; מותאם {_money(r['adjusted'])}; טווח {_money(r['adjusted_range'][0])}–{_money(r['adjusted_range'][1])}; {r['asks']}. "
                f"מיצוב: {r['positioning']}; השוואות כשירות: {len((d.get('competitive_evidence') or {}).get('eligible') or [])}; "
                f"המלצת פתיחה {_money(d.get('recommended_opening_price'))}; אישור {d.get('approval')}; חסמים: {', '.join(d.get('blockers') or [])}. "
                f"עסקאות נבחרות ({len(sales)}): {' | '.join(sales) if sales else 'אין עסקאות נבחרות; ראו מקור benchmark/פרוקסי ב־JSON'}. "
                'שטח מדווח אינו שטח בנוי מאומת; סוג הנכס ותנאי העסקאות לא אומתו; המקדמים אינם מכוילים.')
        fallback = {'TIER_1': 'עסקאות דומות ועדכניות; לא נדרש מסלול חלופי',
                    'TIER_2': 'עסקאות ישנות יותר עם התאמת מדד למ״ס',
                    'TIER_3': 'מדד אזורי מצרפי בהיעדר מספיק עסקאות דומות; אומדן מוגבל',
                    'NONE': 'אין בסיס מספיק; לא נקבע אומדן שוק'}.get(raw.get('tier'), 'לא ידוע')
        text += (f' מה נבדק: עסקאות דומות, הצעות שוק ופרויקטים מתחרים לפי הנתונים הזמינים. '
                 f'עסקאות עדכניות כשירות: {raw.get("eligible_recent", 0)}; '
                 f'עסקאות היסטוריות כשירות: {raw.get("eligible_historical", 0)}. '
                 f'מסלול חלופי: {fallback}. מקור ייחוס: {raw.get("benchmark_id") or "עסקאות נבחרות או לא זמין"}; '
                 f'תקופת ייחוס: {raw.get("valuation_reference") or "לא ידועה"}. '
                 f'פירוט המגבלות והחסמים: {"; ".join(raw.get("warnings") or []) or "לא נרשמו אזהרות נוספות"}. '
                 f'מגבלות ההתאמות: {"; ".join(u.get("warnings") or []) or "לא נרשמו אזהרות נוספות"}. '
                 f'רכיבי התאמה חסרים: {", ".join(missing_components) or "אין"}.')
        if raw.get('evidence'):
            reused = sum(sale_usage[e['observation_id']] > 1 for e in raw['evidence'])
            text += (f' עצמאות הראיות: {raw.get("independent_parcels")} חלקות מובחנות ומשקל אפקטיבי {raw.get("effective_count")}; '
                     f'{reused} מהעסקאות הנבחרות משמשות גם דירות אחרות בהרצה. אין לסכום שימושים כאישוש עצמאי; גם חלקות שונות אינן מבטיחות עצמאות.')
        if assessments:
            text += (f' מצב בנייה בעסקאות נבחרות: {attested_off_plan} עם אימות אנושי למכירה לפני השלמת בנייה (off-plan); '
                     f'{future_year} עם שנת בנייה מאוחרת לשנת העסקה, סימן לבדיקה בלבד ולא הוכחה למכירה לפני השלמה. '
                     'בהיעדר אימות מצב הבנייה לא ידוע; לא בוצעה התאמה כלכלית בין שלבי בנייה.')
        if top_rejections:
            rejection_labels = {'property_type': 'סוג נכס שונה או לא ידוע', 'built_area': 'שטח חסר או לא תואם',
                'listing_or_offer_date': 'תאריך הצעה חסר או לא עדכני', 'rooms': 'מספר חדרים לא תואם',
                'price': 'מחיר חסר או לא תקין', 'distance': 'מרחק חסר או גדול מדי',
                'point_precision': 'מיקום לא מדויק', 'capture_date': 'מועד איסוף חסר או ישן',
                'floor_unknown_or_different': 'קומה לא ידועה או לא תואמת',
                'starting_price_not_unit_offer': 'מחיר פתיחה ולא מחיר יחידה', 'duplicate': 'רשומה כפולה',
                'same_project_not_independent': 'אותו פרויקט אינו מקור עצמאי'}
            text += ' סיבות פסילת הצעות (רשומה יכולה להיספר בכמה סיבות): ' + ', '.join(f'{rejection_labels.get(k, k)}: {v}' for k, v in top_rejections) + '.'
        if r['type'] == 'garden' and r['floor_amount'] is not None:
            alternative = r['adjusted'] - r['floor_amount'] if r['adjusted'] is not None else None
            text += (f' בדיקת קומה לגן: הוחלה התאמת קומה {_money(r["floor_amount"])}; התאמת כיוון {_money(r["orientation_amount"])}. '
                     f'ללא רכיב הקומה בלבד השווי התרחישי היה {_money(alternative)}; זו רגישות ולא מדיניות או מחיר מאושר.')
        if r['confidence'] == 'INDICATIVE_SCENARIO':
            text += ' פרוקסי בלבד: אין אומדן עסקאות מתאים לטיפוס; אין לראות בהתאמת הטיפוס או בתרחיש מחיר מאומת.'
        if 'no_evidence_based_sale_range_for_this_property_type' in (d.get('blockers') or []):
            text += ' פעולה ראשונה מוצעת: לאתר ולאמת עסקאות המתאימות לטיפוס הנכס ולמפרטו לפני הסתמכות על תרחיש.'
        elif 'insufficient_current_compatible_competitor_offers' in (d.get('blockers') or []):
            text += ' פעולה ראשונה מוצעת: לאסוף הצעות מתחרות עדכניות עם מחיר, שטח ומפרט לאותה יחידה; במקביל לאמת את תנאי עסקאות הבסיס.'
        if research and any(x['apartment_id'] == r['id'] for x in research['conflict_sensitivity']):
            text += ' אזהרה: עסקה שנבחרה לבסיס זה כוללת סתירת מחיר חיצונית לא פתורה; ראו בדיקת רגישות במחקר.'
        details.append('<details><summary>דירה ' + escape(str(r['id'])) + '</summary><p>' + escape(text) + '</p></details>')
    policy = bundle['config']
    orientation = ', '.join(f'{k}: {v:+.1%}' for k, v in (policy.get('unit_orientation_rates') or {}).items()) or 'לא הוגדר'
    decisions = sum(1 for r in bundle['results'] if r.get('pricing_decision'))
    market = bundle.get('market') or {}
    sales = market.get('sales') or []
    explicit_areas = sum(s.get('built_area') is not None for s in sales)
    project_pairs = sum(c['price'] is not None and c['area'] is not None for c in competitors)
    failures = bundle.get('failures') or []
    failure_text = '; '.join(f"{item.get('provider', 'unknown')}: {item.get('reason', 'unknown')}" for item in failures)
    provider_cached = [c for c in bundle.get('captures', []) if c.get('mode') == 'PROVIDER_CACHE']
    cached_providers = ', '.join(sorted({str(c.get('provider')) for c in provider_cached})) or 'אין'
    ai_status = (bundle.get('ai_analysis') or {}).get('status', 'NOT_RUN')
    captures = bundle.get('captures') or []
    missing_sources = {'over', 'apish', 'reef', 'cbs'} - {c.get('provider') for c in captures}
    historical = any(c.get('mode') == 'AUDIT_REPLAY' for c in captures)
    overall = 'PARTIAL' if failures or provider_cached or missing_sources or historical or ai_status != 'SUCCESS' else 'SUCCESS'
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><title>מחירון והחלטות תמחור — קבוצת גבאי</title>
<style>
body{{font-family:system-ui,"Segoe UI",Arial;margin:24px;background:#f5f6f8;color:#17202a;line-height:1.55}}
h1,h2{{margin:.6em 0}} .card{{background:#fff;border:1px solid #dde2e8;border-radius:10px;padding:16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}} th,td{{border:1px solid #d7dde5;padding:6px;text-align:right;vertical-align:top}}
th{{background:#e9eef5;position:sticky;top:0}} tr:nth-child(even) td{{background:#fafbfc}}
.warn{{background:#fff3cd;border:1px solid #e0b400;padding:10px;border-radius:8px;margin:10px 0}} .ok{{background:#e8f5e9;border:1px solid #43a047;padding:10px;border-radius:8px}}
.kpis{{display:flex;flex-wrap:wrap;gap:10px}} .kpi{{background:#fff;border:1px solid #dde2e8;border-radius:8px;padding:10px 14px;min-width:150px}}
.kpi b{{display:block;font-size:22px}} .small{{font-size:12px;color:#5b6773}} code{{background:#eef1f5;padding:1px 4px;border-radius:4px}}
</style></head><body>
<h1>מחירון תמחור והחלטות — קבוצת גבאי</h1>
<div class="warn"><b>סטטוס תהליך כולל: {overall}</b> — תקלות מקורות, מטמון ספק או ניתוח AI שלא הושלם הופכים את התהליך לחלקי.</div>
<div class="small">מסמך תומך החלטה לשיווק. אינו שמאות, אינו אישור מחיר, ואינו תחזית זמן מכירה. נכון ל־{escape(str(policy.get('as_of')))}. מדיניות: {escape(str((bundle.get('run_manifest') or {}).get('policy_id', 'לא נמסרה')))}; hash: {escape(str((bundle.get('run_manifest') or {}).get('config_sha256', 'לא נמסר')))}.</div>
{banner}
<div class="warn"><b>מקורות שלא היו זמינים:</b> {escape(failure_text or 'לא דווחו כשלים באיסוף')}<br>רשומות שנאספו ממטמון ספק (מספר לכידות): {len(provider_cached)}; ספקים: {escape(cached_providers)}. תאריך האיסוף אינו בהכרח תאריך המקור.</div>
<div class="card"><h2>תמונת מצב</h2><div class="kpis">
<div class="kpi"><b>{len(units)}</b>דירות בתוצר</div>
<div class="kpi"><b>{sum(1 for r in units if r['confidence'] == 'INSUFFICIENT_EVIDENCE')}</b>דירות ללא בסיס מספיק</div>
<div class="kpi"><b>{sum(1 for r in units if r['status'] == 'תרחיש מחיר לבדיקה')}</b>תרחישי מחיר לבדיקה</div>
<div class="kpi"><b>{sum(1 for r in units if 'אינדיקטיבי' in r['status'])}</b>תרחיש אינדיקטיבי</div>
<div class="kpi"><b>{sum(1 for r in units if r['decision'].get('recommended_opening_price') is not None)}</b>המלצות שעברו שערים (טרם אישור אנושי)</div>
<div class="kpi"><b>{len(competitors)}</b>רשומות מחיר פרויקטים להקשר בלבד, לא בהכרח כשירות</div>
<div class="kpi"><b>{project_pairs}/{len(competitors)}</b>רשומות פרויקט עם מחיר ושטח באותה רשומה (לא בהכרח כשירות)</div>
<div class="kpi"><b>{explicit_areas}/{len(sales)}</b>עסקאות עם שדה שטח בנוי מפורש (לא בהכרח מאומת)</div>
<div class="kpi"><b>{decisions}</b>פלטי החלטה</div>
</div><p><b>בדיקת AI אינה הוכחה:</b> ניתוח המודל ובדיקת הטענות הם פרשנות בלבד, לא עובדה מאומתת ולא אישור מחיר. טענות שסומנו דורשות בדיקה אנושית.</p></div>
<div class="card"><h2>איך לקרוא את המחיר</h2>
<p><b>מקרא מצבי החלטה:</b> CONDITIONAL_RECOMMENDATION_REQUIRES_HUMAN_APPROVAL = המלצת פתיחה מותנית, טרם אישור; CONDITIONAL_POSITIONING_UNAVAILABLE = לא ניתן לבסס מיצוב תחרותי; INDICATIVE_SCENARIO_ONLY = תרחיש אינדיקטיבי בלבד; BLOCKED_REQUIRES_REVIEW = חסום לבדיקה. NOT_APPROVED = לא אושר בידי אדם.</p>
<p><b>שווי בסיס</b> מגיע מעסקאות שבוצעו (או benchmark מסומן). <b>שווי מותאם</b> = בסיס + התאמות יחידה מוצהרות. התרחישים הם <b>מועמדי מחיר לבחינה</b>, לא המלצה או אישור. מיצוב תחרותי לא מבוסס ללא הצעות כשירות מספיקות. העדפה מותנית של AI היא הצעה לבדיקה, לא אישור. המלצת פתיחה קיימת רק אם מנוע ההחלטה העביר את שעריו; אישור אנושי נפרד.</p>
<p>אין בסיס לבחירת אסטרטגיה מיטבית ללא יעד עסקי מוגדר של החברה וראיות שוק מספקות. פרשנות AI אינה עובדה מאומתת ואינה מחליפה בדיקה אנושית.</p>
<p><b>בחינה מול העדפה עסקית, לא תחזית:</b> יעד למזער מחיר פתיחה עשוי להצדיק בחינת התרחיש הנמוך, במחיר ויתור אפשרי על תמורה; יעד לשמר רמת מחיר עשוי להצדיק בחינת התרחיש הגבוה, תוך סיכון לאי־התאמה לשוק; התרחיש המאוזן הוא נקודת ייחוס, לא ברירת מחדל מיטבית. ללא נתוני ביקוש ויעד מוסכם אין להסיק זמן מכירה או לאשר מחיר.</p>
<p>מקדמי הקומה והכיוון הם <b>הנחות מדיניות מוצהרות</b>, לא מקדמים שנאמדו מנתוני מכירות. מרפסת וגן מוצגים כרגישות בלבד ואינם מתווספים לבסיס, כדי לא לספור פעמיים תכונות שכבר בגין הקומפראבלים או ה־benchmark.</p>
<p><b>מקרא ראיות:</b> EVIDENCE_SUPPORTED = תוצאה מחושבת לפי עסקאות ספק שנבחרו, לא הערכת שוק מאומתת; שטחי העסקאות וסוגי הנכסים עשויים להיות לא מאומתים. אי־אימות השטח פוגע בהשוואת המחיר למ״ר ובאמינות האומדן. EVIDENCE_LIMITED_BENCHMARK = מבוסס benchmark מוגבל; INDICATIVE_SCENARIO = תרחיש אינדיקטיבי, לא שווי; INSUFFICIENT_EVIDENCE = אין בסיס. אישור אנושי נדרש בנפרד.</p>
<p><b>הנחות תרחישי המחיר:</b> מהיר {_pct(policy.get('marketing_fast_adjustment'))}; מאוזן {_pct(policy.get('marketing_balanced_adjustment'))}; פתיחה גבוהה {_pct(policy.get('marketing_high_adjustment'))}. אלו התאמות מדיניות ולא תחזית מכירה.</p>
<p class="small">מדיניות: קומה {_pct(policy.get('unit_floor_rate'))} לקומה מעל קומת ייחוס {policy.get('unit_reference_floor')}; כיוונים: {escape(orientation)}; מרפסת {_pct(policy.get('unit_balcony_equivalent'))}; גן {_pct(policy.get('unit_garden_equivalent'))}; דופלקס {_pct(policy.get('unit_duplex_premium'))}; טריפלקס {_pct(policy.get('unit_triplex_premium'))}.</p></div>
{_research_html(bundle, research)}
{_ai_html(bundle)}
<div class="card"><h2>מחירון כל {len(units)} הדירות</h2>
<table><thead><tr><th>דירה</th><th>סוג</th><th>קומה</th><th>חדרים</th><th>מ"ר</th><th>מס' ראיות</th><th>חציון ₪/מ"ר</th><th>שווי בסיס</th><th>שווי מותאם</th><th>טווח סופי</th><th>מחירים (מהיר / מאוזן / גבוה)</th><th>סטטוס תמחור</th><th>אישור</th><th>סטטוס החלטה</th><th>אזהרות</th></tr></thead><tbody>{''.join(units_html)}</tbody></table></div>
<div class="card"><h2>הסבר מפורט לכל דירה</h2><p>לחצו על מספר דירה לפתיחת שרשרת החישוב והראיות. תאריך איסוף אינו אימות עדכניות מודעה; זכויות השימוש במקורות לא אומתו.</p>{''.join(details)}</div>
<div class="card"><h2>תרומת התאמות היחידה (₪)</h2>
<p class="small">עמודות "רגישות" הן מה היה קורה אילו היינו מוסיפים מרפסת/גן; הן אינן חלק מהמחיר. "לא הוחל" = תכונה חסרה או כפילות אפשרית.</p>
<table><thead><tr><th>דירה</th><th>קומה</th><th>כיוון</th><th>מרפסת (רגישות)</th><th>גן (רגישות)</th><th>סך התאמות</th><th>סוג מקור</th><th>הערות</th></tr></thead><tbody>{''.join(adjust_html)}</tbody></table></div>
<div class="card"><h2>פרויקטים מתחרים</h2>
<p>רשומות אלה הן מחירי ביקוש ולא עסקאות. מחיר ללא שטח באותה רשומה אינו הצעת יחידה בת השוואה; מחיר פתיחה אינו מחיר יחידה. גם צירוף מחיר ושטח אינו מבטיח כשירות. אין להסיק מיצוב מדויק.</p>
<table><thead><tr><th>פרויקט</th><th>מרחק (מ')</th><th>סוג</th><th>חדרים</th><th>שטח</th><th>מחיר</th><th>₪/מ"ר</th><th>בסיס</th><th>תאריך</th><th>מגבלות</th></tr></thead><tbody>{''.join(comp_html) or '<tr><td colspan="10">אין הצעות פרויקט מתומחרות במקור המקומי.</td></tr>'}</tbody></table></div>
<div class="card"><h2>סכומי תרחישי מחירי ביקוש</h2>
<p>סכום תרחישי מחיר לבדיקה בנפרד מתרחישים אינדיקטיביים; אין זו תחזית הכנסות או מכירות. כיסוי {sum(1 for r in units if r['status'] == 'תרחיש מחיר לבדיקה')}/{len(units)}.</p>
<table><thead><tr><th>תרחיש</th><th>דירות בתרחיש לבדיקה</th><th>סכום תרחיש</th><th>ממוצע</th><th>דירות אינדיקטיביות</th><th>סכום אינדיקטיבי</th><th>כיסוי</th></tr></thead><tbody>{''.join(revenue_html)}</tbody></table></div>
<div class="card"><h2>מגבלות והנחות</h2>
<ul>
<li>הכתובת המוגדרת: <b>{escape(str(location.get('configured_anchor') or bundle.get('address')))}</b>; סטטוס: {escape(str(location.get('status')))}.</li>
<li>הנתונים מבוססים על {len(market.get('sales', []))} רשומות מכירה, {len(market.get('listings', []))} מודעות ו־{len(market.get('projects', []))} רשומות פרויקט מהצילום המקומי; לא כל רשומה כשירה.</li>
<li>שווי בסיס מבוסס עסקאות שדווחו על ידי ספק, לא אימות מסמכי רשות המיסים. כל המחירים טעונים אישור אנושי.</li>
<li>חניה, מחסן, מצב, מעלית ונוף אינם קיימים ב־workbook ולכן לא תומחרו.</li>
<li>טווחים הם תרחישי רגישות, לא רווחי סמך סטטיסטיים.</li>
</ul></div>
<div class="small">קבצים נלווים: PRICE_LIST_HE.csv, UNIT_ADJUSTMENTS_HE.csv, COMPETITORS_HE.csv, REVENUE_SCENARIOS_HE.csv, MARKETING_SUMMARY_HE.md. לא להפיץ נתוני מקור גולמיים.</div>
</body></html>"""


def write_marketing_reports(bundle, directory: Path, research=None):
    directory.mkdir(parents=True, exist_ok=True)
    units = _unit_rows(bundle)
    revenue = _revenue(bundle)
    competitors = _competitors(bundle)

    price_fields = ['דירה', 'סוג', 'קומה', 'חדרים', 'שטח בנוי', 'מרפסת/גן', 'מס\' ראיות', 'חציון ₪/מ"ר',
                    'שווי בסיס', 'טווח בסיס', 'התאמת קומה', 'התאמת כיוון', 'רגישות מרפסת', 'רגישות גן',
                    'שווי מותאם', 'טווח סופי', 'מחיר מהיר', 'מחיר מאוזן', 'מחיר גבוה', 'מיצוב',
                    'סיווג ראיות', 'סטטוס תמחור', 'אישור', 'סטטוס החלטה', 'אזהרות', 'סוג נתונים']
    with (directory / 'PRICE_LIST_HE.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = SafeWriter(csv.DictWriter(f, fieldnames=price_fields))
        writer.writeheader()
        for r in units:
            apartment = r['outdoor']
            outdoor = apartment.get('garden_area') if apartment.get('garden_area') is not None else apartment.get('balcony_area')
            writer.writerow({'דירה': r['id'], 'סוג': _type(r['type']), 'קומה': r['floor'], 'חדרים': r['rooms'],
                'שטח בנוי': r['built'], 'מרפסת/גן': outdoor, 'מס\' ראיות': r['evidence_count'],
                'חציון ₪/מ"ר': r['ppm_median'], 'שווי בסיס': r['base'],
                'טווח בסיס': f"{r['base_range'][0] or ''}–{r['base_range'][1] or ''}",
                'התאמת קומה': r['floor_amount'], 'התאמת כיוון': r['orientation_amount'],
                'רגישות מרפסת': r['balcony_sensitivity'], 'רגישות גן': r['garden_sensitivity'],
                'שווי מותאם': r['adjusted'], 'טווח סופי': f"{r['adjusted_range'][0] or ''}–{r['adjusted_range'][1] or ''}",
                'מחיר מהיר': next((v for v in r['ask_values']['quick_sale'] if v is not None), None),
                'מחיר מאוזן': next((v for v in r['ask_values']['balanced'] if v is not None), None),
                'מחיר גבוה': next((v for v in r['ask_values']['high_opening'] if v is not None), None),
                'מיצוב': r['positioning'], 'סיווג ראיות': r['confidence'], 'סטטוס תמחור': r['status'],
                'אישור': r['decision'].get('approval'), 'סטטוס החלטה': r['decision'].get('status'),
                'אזהרות': '; '.join(r['warnings']), 'סוג נתונים': bundle.get('data_classification', 'REAL_INPUT_UNVERIFIED')})

    with (directory / 'REVENUE_SCENARIOS_HE.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = SafeWriter(csv.writer(f))
        writer.writerow(['תרחיש', 'דירות בתרחיש לבדיקה', 'סכום תרחיש ביקוש', 'ממוצע תרחיש', 'דירות אינדיקטיביות', 'סכום אינדיקטיבי', 'כיסוי', 'סוג נתונים'])
        for s in revenue:
            writer.writerow([s['label'], s['supported_count'], s['supported_total'], s['supported_average'],
                             s['indicative_count'], s['indicative_total'], f"{s['coverage']:.1%}", bundle.get('data_classification', 'REAL_INPUT_UNVERIFIED')])

    with (directory / 'COMPETITORS_HE.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = SafeWriter(csv.writer(f))
        writer.writerow(['פרויקט', 'מרחק (מ\')', 'סוג', 'חדרים', 'שטח', 'מחיר', 'מחיר למ"ר', 'בסיס מחיר', 'תאריך', 'מגבלות', 'סוג נתונים'])
        for c in competitors:
            writer.writerow([c['name'], c['distance_m'], c['type'], c['rooms'], c['area'], c['price'], c['ppm'],
                             c['basis'], c['date'], c['limitations'], bundle.get('data_classification', 'REAL_INPUT_UNVERIFIED')])

    with (directory / 'UNIT_ADJUSTMENTS_HE.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = SafeWriter(csv.writer(f))
        writer.writerow(['דירה', 'שווי בסיס', 'התאמת קומה', 'התאמת כיוון', 'רגישות מרפסת', 'רגישות גן', 'סך התאמות', 'שווי מותאם', 'סוג מקור', 'תכונות שהוחלו', 'אזהרות', 'סוג נתונים'])
        for r in units:
            u = r['unit']
            writer.writerow([r['id'], r['base'], r['floor_amount'], r['orientation_amount'], r['balcony_sensitivity'],
                             r['garden_sensitivity'], u.get('applied_total'), r['adjusted'], u.get('policy_label'),
                             '; '.join(u.get('applied_adjustments') or []), '; '.join(u.get('warnings') or []), bundle.get('data_classification', 'REAL_INPUT_UNVERIFIED')])

    (directory / 'MARKETING_DASHBOARD.html').write_text(_html(bundle, units, revenue, competitors, research), encoding='utf-8')

    supported = sum(1 for r in units if r['status'] == 'תרחיש מחיר לבדיקה')
    indicative = sum(1 for r in units if 'אינדיקטיבי' in r['status'])
    location = bundle.get('location') or {}
    lines = ['# סיכום שיווקי — מחירון ותמחור', '',
             '**מסמך תומך החלטה; אינו שמאות ואינו אישור מחיר.**', '',
             f"- מדיניות: {(bundle.get('run_manifest') or {}).get('policy_id', 'PROGRAMMATIC_CONFIG')}; hash: {(bundle.get('run_manifest') or {}).get('config_sha256', 'ראו RUN_MANIFEST.json')}.",
             f"- דירות בתוצר: {len(units)}; תרחיש מחיר לבדיקה: {supported}; תרחיש אינדיקטיבי: {indicative}.",
             f"- מיקום: {location.get('configured_anchor') or bundle.get('address')} — {location.get('status')}. {str(location.get('note') or '').rstrip('.')}.",
             f"- פרויקטים מתומחרים בהשוואה: {len(competitors)}; פלטי החלטה: {sum(1 for r in bundle['results'] if r.get('pricing_decision'))}.",
             '', '## מתודולוגיה', '',
             f'1. קלט: {len(units)} דירות מה־workbook (חדרים, שטח, קומה, כיוון, מרפסת/גן).',
             '2. שווי שוק: קומפראבלים מעסקאות שבוצעו, עם שערי התאמה, בעלות וניתוח מדד; אחרת benchmark מסומן.',
             '3. התאמות יחידה: מקדמי קומה/כיוון מוצהרים; מרפסת/גן כרגישות בלבד למניעת ספירה כפולה.',
             f"4. שלוש אסטרטגיות: מהיר {_pct(bundle['config'].get('marketing_fast_adjustment'))}, מאוזן {_pct(bundle['config'].get('marketing_balanced_adjustment'))}, גבוה {_pct(bundle['config'].get('marketing_high_adjustment'))}; כולן טעונות אישור.",
             f"5. מיצוב תחרותי: סף מינימלי {bundle['config'].get('decision_min_competitors')} מתחרים כשירים ושערים נוספים; אחרת לא זמין.",
             '', '## סכומי תרחישי מחיר מבוקש (לא תחזית מכירות)', '',
             '| תרחיש | דירות | סכום | ממוצע | כיסוי |', '|---|---:|---:|---:|---:|']
    for s in revenue:
        lines.append(f"| {s['label']} | {s['supported_count']} | {_money(s['supported_total'])} | {_money(s['supported_average'])} | {_pct(s['coverage'])} |")
    lines += ['', 'הסכומים אינם תחזית הכנסה: תרחישים אינדיקטיביים מוצגים בנפרד.', '',
              '## דוגמאות', '']
    for row in units:
        aid = row['id']
        u = row['unit']
        lines.append(f"- **דירה {aid}** ({_type(row['type'])}, {row['rooms']} חדרים, קומה {row['floor']}): בסיס {_money(row['base'])}; "
                     f"התאמות {_money(u.get('applied_total'))} ({', '.join(u.get('applied_adjustments') or []) or 'ללא'}); "
                     f"מותאם {_money(row['adjusted'])}; {row['asks']}; {row['status']}; ראיות: {row['confidence']}; "
                     f"אישור: {row['decision'].get('approval')} / {row['decision'].get('status')}.")
    lines += ['', '## מה עדיין חסר', '',
              '- תמהיל התרגיל אינו משויך לבניין הייחוס; לצורך תמחור פרויקט אמיתי נדרש אימות מיקום ותמהיל.',
              '- מחירי יחידה בפרויקטים מתחרים: המקור מספק מחירי פתיחה בלבד, ולכן אין ₪/מ"ר ואין מיצוב מבוסס.',
              '- חניה, מחסן, מצב ומעלית: לא קיימים בקלט.',
              '- אישור אנושי לכל מחיר; כל הפלטים הם טיוטות.', '']
    (directory / 'MARKETING_SUMMARY_HE.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'units': len(units), 'supported': supported, 'indicative': indicative,
            'competitors': len(competitors), 'revenue': revenue}
