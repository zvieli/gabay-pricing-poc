"""Local-only demonstration interface for the saved-evidence pricing pipeline."""
import argparse
import os
import json
import re
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from email.parser import BytesParser
from email.policy import default
from datetime import date
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import nullcontext

from gabay_pricing.models import Snapshot

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / 'data/snapshots/market-live.json'
MAX_UPLOAD = 10 * 1024 * 1024
PROGRESS = {}
RUN_LOCK = Lock()

FORM = '''<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<title>תמחור דירות</title><h1>כלי תומך החלטה לתמחור דירות</h1>
<p>נתוני שוק שמורים אינם בהכרח עדכניים. איסוף עדכני עשוי להשתמש בשירותים בתשלום; יש לוודא הרשאות ותנאי שימוש.</p>
<form action="/run" method="post" enctype="multipart/form-data" onsubmit="startProgress(this)">
<input type="hidden" name="run_id" id="run_id">
<label>מקור הנתונים <select name="mode"><option value="saved">נתונים שמורים להלסינקי</option><option value="live">נתוני שוק עדכניים</option></select></label><br>
<label>כתובת בישראל <input name="address" required value="הלסינקי 24, תל אביב"></label><br>
<label>תמהיל דירות (Excel) <input name="workbook" type="file" accept=".xlsx" required></label><br>
<label>מזהי דירות לבדיקה (ריק = כולן) <input name="apartments" placeholder="1"></label><br>
<label><input type="checkbox" name="ai"> ניתוח AI לדירות שנבחרו — רק לאחר אימות הרשאה להעברת נתוני צד שלישי ל־Groq</label><br>
<label>מקור מפתח Groq <select name="groq_key_source"><option value="env">משתנה סביבה</option><option value="dotenv">קובץ .env מקומי</option></select></label><br>
<label>מודל Groq <select name="ai_model"><option value="openai/gpt-oss-20b">GPT OSS 20B</option><option value="openai/gpt-oss-120b">GPT OSS 120B</option></select></label><br>
<label>ספק LLM ראשי <select name="ai_primary"><option value="groq">Groq</option><option value="gemini">Gemini</option></select></label><br>
<label>מגבלת בקשות LLM <input name="llm_max_requests" type="number" min="1" max="1000" value="100"></label><br>
<label><input type="checkbox" name="authorize_gemini"> אישור מפורש להעברת נתוני הראיות ל-Gemini כחלופה</label><br>
<label><input type="checkbox" name="authorize_kilo"> אישור מפורש להעברת נתוני הראיות ל-Kilo כחלופה (רק אם מודל אומת)</label><br>
<button>הפקת דוח</button></form><p id="progress" hidden role="status">העיבוד בביצוע.</p>
<script>
function startProgress(form) {
  const id = crypto.randomUUID(); document.getElementById('run_id').value = id;
  const progress = document.getElementById('progress'); progress.hidden = false;
  form.querySelector('button').disabled = true;
  setInterval(async () => { try { const r = await fetch('/progress?id=' + encodeURIComponent(id));
    if (r.ok) progress.textContent = await r.text(); } catch (_) {} }, 1000);
}
</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, html):
        data = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/' and getattr(self.server, 'report_dir', None):
            page = self.server.report_dir / 'MARKETING_DASHBOARD.html'
            if page.exists():
                return self.reply(200, page.read_text(encoding='utf-8'))
        if parsed.path == '/':
            form = FORM
            if getattr(self.server, 'offline_demo', False):
                form = form.replace('<form action=', '<p><b>הדגמה לא מקוונת: נתוני שוק משוחזרים וניתוח AI שנשמר בעבר; לא מבוצעות קריאות חיצוניות.</b></p><form action=')
            return self.reply(200, form)
        if parsed.path == '/progress':
            run_id = parse_qs(parsed.query).get('id', [''])[0]
            state = PROGRESS.get(run_id)
            if not state:
                return self.reply(404, 'Not found')
            message = state['message']
            if state.get('path'):
                try:
                    counts = json.loads(state['path'].read_text(encoding='utf-8'))
                    message = f"ניתוח AI ובדיקת טענות: {counts['completed']} מתוך {counts['total']} דירות"
                except (OSError, ValueError, KeyError):
                    pass
            return self.reply(200, message)
        self.reply(404, 'Not found')

    def do_POST(self):
        host = f'127.0.0.1:{self.server.server_port}'
        origin = self.headers.get('Origin')
        if self.headers.get('Host') != host or (origin and origin != f'http://{host}'):
            return self.reply(403, 'Forbidden')
        if self.path != '/run':
            return self.reply(404, 'Not found')
        if not RUN_LOCK.acquire(blocking=False):
            return self.reply(409, 'כבר מתבצע ניתוח; נסו שוב בסיום')
        run_id = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_UPLOAD:
                raise ValueError('הקובץ חסר או גדול מדי')
            content_type = self.headers.get('Content-Type', '')
            if not content_type.startswith('multipart/form-data;'):
                raise ValueError('טופס לא תקין')
            payload = self.rfile.read(length)
            msg = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + payload)
            fields = {p.get_param('name', header='content-disposition'): p.get_payload(decode=True)
                      for p in msg.iter_parts()}
            address = fields.get('address', b'').decode('utf-8').strip()
            candidate = fields.get('run_id', b'').decode('ascii')
            if re.fullmatch(r'[0-9a-f-]{36}', candidate):
                run_id = candidate
                PROGRESS[run_id] = {'message': 'הנתונים נבדקים ומחושבים'}
            mode = fields.get('mode', b'saved').decode('ascii')
            if mode not in {'saved', 'live'} or not address:
                raise ValueError('יש לבחור מקור נתונים ולהזין כתובת')
            if getattr(self.server, 'offline_demo', False) and (mode != 'saved' or fields.get('ai') == b'on'):
                raise ValueError('הדגמה לא מקוונת מאפשרת רק נתונים שמורים וניתוח AI שמור')
            saved_snapshot = getattr(self.server, 'snapshot', SNAPSHOT)
            if mode == 'saved':
                if not saved_snapshot.exists():
                    raise ValueError('נתוני השוק השמורים אינם זמינים')
                snapshot = Snapshot.model_validate_json(saved_snapshot.read_text())
                if address != snapshot.address:
                    raise ValueError('הכתובת אינה תואמת לנתוני השוק השמורים; לא נעשה שימוש בנתונים של כתובת אחרת')
            workbook = fields.get('workbook')
            if not workbook:
                raise ValueError('יש להעלות קובץ Excel')
            if self.server.artifacts:
                self.server.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
                workdir = nullcontext(tempfile.mkdtemp(prefix='run-', dir=self.server.artifacts))
            else:
                workdir = tempfile.TemporaryDirectory(prefix='gabay-ui-')
            with workdir as directory:
                folder = Path(directory)
                (folder / 'input.xlsx').write_bytes(workbook)
                from gabay_pricing.ingestion.excel import read_apartments
                try:
                    apartments = read_apartments(folder / 'input.xlsx')
                except Exception as exc:
                    raise ValueError('קובץ Excel לא תקין או אינו תואם לפורמט התמהיל') from exc
                selected = fields.get('apartments', b'').decode('ascii').strip()
                if selected:
                    ids = [x.strip() for x in selected.split(',')]
                    if any(not x.isdigit() for x in ids) or not set(ids) <= {str(a.id) for a in apartments}:
                        raise ValueError('מזהי הדירות אינם תקינים או אינם קיימים בקובץ')
                    selected = ','.join(ids)
                if mode == 'live':
                    if run_id:
                        PROGRESS[run_id]['message'] = 'איסוף נתוני שוק בביצוע'
                    snapshot_path = folder / 'market.json'
                    snapshot_path.write_text(Snapshot(address=address, as_of=date.today()).model_dump_json(), encoding='utf-8')
                else:
                    snapshot_path = saved_snapshot
                command = [sys.executable, '-m', 'gabay_pricing.cli', 'live' if mode == 'live' else 'report', '--snapshot', str(snapshot_path),
                           '--workbook', str(folder / 'input.xlsx'), '--reports', str(folder / 'report')]
                if selected:
                    command += ['--apartments', selected]
                if getattr(self.server, 'offline_demo', False):
                    command += ['--reuse-saved-ai', str(self.server.saved_ai)]
                    if self.server.saved_checkpoint:
                        command += ['--saved-ai-checkpoint', str(self.server.saved_checkpoint)]
                result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=300)
                if result.returncode:
                    raise ValueError('לא ניתן לעבד את קובץ ה־Excel או להפיק דוח. בדקו את פורמט התמהיל.')
                report_path = folder / 'report/MARKETING_DASHBOARD.html'
                deterministic_report = report_path.read_text(encoding='utf-8')
                if fields.get('ai') == b'on':
                    source = fields.get('groq_key_source', b'env').decode('ascii')
                    if source not in {'env', 'dotenv'}:
                        raise ValueError('מקור מפתח Groq אינו תקין')
                    model = fields.get('ai_model', b'openai/gpt-oss-20b').decode('ascii')
                    if model not in {'openai/gpt-oss-20b', 'openai/gpt-oss-120b'}:
                        raise ValueError('מודל Groq אינו תקין')
                    max_requests = fields.get('llm_max_requests', b'100').decode('ascii')
                    if not max_requests.isdigit() or not 1 <= int(max_requests) <= 1000:
                        raise ValueError('מגבלת בקשות LLM אינה תקינה')
                    primary = fields.get('ai_primary', b'groq').decode('ascii')
                    if primary not in {'groq', 'gemini'}:
                        raise ValueError('ספק LLM ראשי אינו תקין')
                    if primary == 'gemini' and fields.get('authorize_gemini') != b'on':
                        raise ValueError('יש לאשר במפורש העברת ראיות ל-Gemini לפני בחירתו כספק ראשי')
                    command[3] = 'report'
                    command += ['--ai', '--ai-max-units', str(len(apartments)), '--groq-key-source', source, '--ai-model', model, '--ai-primary', primary, '--llm-max-requests', max_requests]
                    for provider in ('gemini', 'kilo'):
                        if fields.get('authorize_' + provider) == b'on':
                            command += ['--ai-authorize-provider', provider]
                    try:
                        progress_path = folder / 'progress.json'
                        if run_id:
                            PROGRESS[run_id].update(message='ניתוח AI בביצוע', path=progress_path)
                        env = {**os.environ, 'GABAY_PROGRESS_FILE': str(progress_path)}
                        result = subprocess.run(command, cwd=ROOT, capture_output=True, env=env, timeout=max(300, len(apartments) * 600))
                        if result.returncode:
                            raise ValueError('AI pipeline failed')
                        deterministic_report = report_path.read_text(encoding='utf-8')
                    except (ValueError, OSError, subprocess.TimeoutExpired):
                        deterministic_report = deterministic_report.replace(
                            'NOT_ANALYZED — לא בוצע ניתוח לדירה זו',
                            'הניתוח לא זמין: AI_PIPELINE_ERROR — לא בוצע ניתוח לדירה זו')
                output = deterministic_report
        except (ValueError, UnicodeError, OSError, subprocess.TimeoutExpired) as exc:
            self.reply(400, '<html lang="he" dir="rtl"><meta charset="utf-8"><h1>לא ניתן להפיק דוח</h1><p>' + escape(str(exc)) + '</p><a href="/">חזרה</a></html>')
            return
        finally:
            if run_id:
                PROGRESS.pop(run_id, None)
            RUN_LOCK.release()
        self.reply(200, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--artifacts', type=Path, help='Preserve private local run data and reports in this directory')
    parser.add_argument('--report-dir', type=Path, help='Serve the completed report of a finished run instead of the upload form')
    parser.add_argument('--offline-demo-snapshot', type=Path, help='Saved snapshot for network-free demo')
    parser.add_argument('--offline-demo-ai', type=Path, help='Compatible saved pricing_results.json with prior AI')
    parser.add_argument('--offline-demo-checkpoint', type=Path, help='Verify the saved AI checkpoint origin')
    args = parser.parse_args()
    if args.offline_demo_checkpoint and not args.offline_demo_ai:
        parser.error('--offline-demo-checkpoint requires --offline-demo-ai')
    if bool(args.offline_demo_snapshot) != bool(args.offline_demo_ai):
        parser.error('offline demo requires both --offline-demo-snapshot and --offline-demo-ai')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.artifacts = args.artifacts
    server.report_dir = args.report_dir
    server.offline_demo = bool(args.offline_demo_snapshot)
    server.snapshot = args.offline_demo_snapshot or SNAPSHOT
    server.saved_ai = args.offline_demo_ai
    server.saved_checkpoint = args.offline_demo_checkpoint
    print(f'http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
