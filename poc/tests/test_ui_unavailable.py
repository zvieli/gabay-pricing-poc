"""Live path with an unreachable HTTP proxy: no external service is contacted."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (ROOT.parent / 'DATA/חוברת1.xlsx').is_file(), reason='private workbook not supplied')
def test_live_address_with_unavailable_sources_is_explicit():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1'}
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
        workbook = ROOT.parent / 'DATA/חוברת1.xlsx'
        run_id = str(uuid4())
        def submit():
            with workbook.open('rb') as stream:
                return httpx.post(url + '/run', data={'address': 'הרצל 1, ירושלים', 'mode': 'live', 'run_id': run_id},
                                  files={'workbook': (workbook.name, stream)}, timeout=60)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(submit)
            messages = []
            while not future.done():
                progress = httpx.get(url + '/progress', params={'id': run_id}, timeout=2)
                if progress.status_code == 200:
                    messages.append(progress.text)
                    with workbook.open('rb') as stream:
                        concurrent = httpx.post(url + '/run', data={'address': 'הלסינקי 24, תל אביב'},
                                                files={'workbook': (workbook.name, stream)}, timeout=10)
                    assert concurrent.status_code == 409
                    break
                time.sleep(.05)
            result = future.result()
        assert any('בביצוע' in message or 'מחושבים' in message for message in messages)
        assert result.status_code == 200, result.text
        assert 'הרצל 1, ירושלים' in result.text
        assert 'הלסינקי 24' not in result.text
        assert 'מקורות שלא היו זמינים' in result.text
        assert 'ללא מחיר' in result.text
    finally:
        server.terminate()
        server.wait(timeout=10)
