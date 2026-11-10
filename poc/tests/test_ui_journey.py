"""Acceptance test through the local HTTP interface and real pricing pipeline."""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT / 'data/snapshots/market-live.json').is_file() or not (ROOT.parent / 'DATA/חוברת1.xlsx').is_file(), reason='private snapshot and workbook not supplied')
def test_saved_data_workbook_to_hebrew_report(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([sys.executable, '-m', 'gabay_pricing.web', '--port', str(port), '--artifacts', str(tmp_path)], cwd=ROOT)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                response = httpx.get(url, timeout=1)
                break
            except httpx.ConnectError:
                if server.poll() is not None:
                    raise AssertionError('UI server stopped before starting')
                time.sleep(.1)
        else:
            raise AssertionError('UI server did not start')
        assert 'dir="rtl"' in response.text
        assert 'נתוני שוק עדכניים' in response.text
        assert 'קובץ .env מקומי' in response.text
        assert 'בביצוע' in response.text
        rejected = httpx.post(url + '/run', headers={'Origin': 'https://attacker.invalid'}, data={'address': 'x'})
        assert rejected.status_code == 403
        workbook = ROOT.parent / 'DATA/חוברת1.xlsx'
        with workbook.open('rb') as stream:
            response = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב'},
                                  files={'workbook': (workbook.name, stream, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}, timeout=60)
        assert response.status_code == 200, response.text
        assert 'מחירון והחלטות תמחור' in response.text
        assert 'דירה 1' in response.text
        from gabay_pricing.ingestion.excel import read_apartments
        apartments = read_apartments(workbook)
        assert f'מחירון כל {len(apartments)} הדירות' in response.text
        assert response.text.count('מה נבדק:') == len(apartments)
        assert response.text.count('מסלול חלופי:') == len(apartments)
        assert 'הנחות תרחישי המחיר' in response.text
        assert 'מקור העסקה:' in response.text
        assert response.text.count('מגבלות ההתאמות:') == len(apartments)
        assert 'דירות ללא בסיס מספיק' in response.text
        assert 'בדיקת AI אינה הוכחה' in response.text
        assert 'רשומות שנאספו ממטמון ספק' in response.text
        assert 'סטטוס תהליך כולל: PARTIAL' in response.text
        assert 'רכיבי התאמה חסרים:' in response.text
        with workbook.open('rb') as stream:
            single = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב', 'apartments': '1'},
                                files={'workbook': (workbook.name, stream)}, timeout=60)
        assert single.status_code == 200
        assert 'מחירון כל 1 הדירות' in single.text
        assert len(list(tmp_path.glob('run-*/report/pricing_results.json'))) == 2
        with workbook.open('rb') as stream:
            other = httpx.post(url + '/run', data={'address': 'הרצל 1, ירושלים', 'mode': 'saved'},
                               files={'workbook': (workbook.name, stream)}, timeout=60)
        assert other.status_code == 400
        assert 'לא נעשה שימוש בנתונים של כתובת אחרת' in other.text
        malformed = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב'},
                               files={'workbook': ('bad.xlsx', b'not an Excel workbook')}, timeout=60)
        assert malformed.status_code == 400
        assert 'קובץ' in malformed.text
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_report_dir_serves_completed_run(tmp_path):
    import threading, urllib.request
    from gabay_pricing import web
    report_dir = tmp_path / 'report'
    report_dir.mkdir()
    (report_dir / 'MARKETING_DASHBOARD.html').write_text('<html lang="he" dir="rtl"><title>מחירון והחלטות תמחור — קבוצת גבאי</title><h1>סיכום</h1></html>', encoding='utf-8')
    server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    server.artifacts = None
    server.report_dir = report_dir
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        body = urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/').read().decode()
        assert 'מחירון והחלטות תמחור' in body and 'סיכום' in body
    finally:
        server.shutdown()


def test_ai_section_shows_actual_provider_model():
    from gabay_pricing.marketing_report import _ai_html
    bundle = {'results': [{'apartment_id': 1, 'estimated_market_value': None, 'marketing_strategies': {}, 'pricing_decision': {}}], 'ai_analysis': {'status': 'PARTIAL', 'model': 'openai/gpt-oss-20b', 'units': {'1': {'status': 'VALIDATED', 'provider': 'gemini', 'model': 'gemini-3.1-flash-lite', 'analysis': {}, 'claim_review': {'status': 'SUCCESS', 'review': {'claims': []}}}}}}
    html = _ai_html(bundle)
    assert 'gemini-3.1-flash-lite' in html and 'gemini' in html
