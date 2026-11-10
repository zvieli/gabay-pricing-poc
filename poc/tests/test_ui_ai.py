"""Every unit remains visible when the external AI service is unavailable."""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT / 'data/snapshots/market-live.json').is_file() or not (ROOT.parent / 'DATA/חוברת1.xlsx').is_file(), reason='private snapshot and workbook not supplied')
def test_every_apartment_reports_ai_failure_without_changing_prices(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'GROQ_API_KEY': ''}
    server = subprocess.Popen([sys.executable, '-m', 'gabay_pricing.web', '--port', str(port)], cwd=ROOT, env=env)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=1)
                break
            except httpx.ConnectError:
                if server.poll() is not None:
                    raise AssertionError('server stopped')
                time.sleep(.1)
        from openpyxl import load_workbook
        workbook = tmp_path / 'mix.xlsx'
        book = load_workbook(ROOT.parent / 'DATA/חוברת1.xlsx')
        book.worksheets[0].append([1, 999, 3, 70, 8, 'מזרח', None])
        book.save(workbook)
        book.close()
        with workbook.open('rb') as stream:
            baseline = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב'},
                                  files={'workbook': (workbook.name, stream)}, timeout=60)
        assert baseline.status_code == 200
        with workbook.open('rb') as stream:
            result = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב', 'ai': 'on'},
                                files={'workbook': (workbook.name, stream)}, timeout=60)
        assert result.status_code == 200, result.text
        assert result.text.count('הניתוח לא זמין:') == 40
        assert 'מחירון כל 40 הדירות' in result.text
        assert 'NOT_APPROVED' in result.text
        pattern = r'<h2>מחירון כל 40 הדירות</h2>(.*?)</table>'
        assert re.search(pattern, baseline.text, re.S).group(1) == re.search(pattern, result.text, re.S).group(1)
    finally:
        server.terminate()
        server.wait(timeout=10)
