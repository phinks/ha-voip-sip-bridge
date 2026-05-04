#!/usr/bin/env python3
"""HTTP status dashboard served via HA ingress panel."""

import html
import json
import os
import re
import subprocess
import datetime
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

SHARE_DIR = '/share/voip'
CALL_LOG = os.path.join(SHARE_DIR, 'call_log.txt')
KNOWN_DIDS = os.path.join(SHARE_DIR, 'known_dids.json')
KNOWN_CONTACTS = os.path.join(SHARE_DIR, 'known_contacts.json')
TRANSCRIPTS_DIR = os.path.join(SHARE_DIR, 'transcripts')
RECORDINGS_DIR = os.path.join(SHARE_DIR, 'recordings')
PEOPLE_CONFIG = os.path.join(SHARE_DIR, 'people.json')
OPTIONS = '/data/options.json'
PORT = 8099


def get_options():
    try:
        with open(OPTIONS) as f:
            return json.load(f)
    except Exception:
        return {}


def get_timezone():
    try:
        return ZoneInfo(get_options().get('timezone', 'UTC') or 'UTC')
    except Exception:
        return ZoneInfo('UTC')


def localise(dt_str, tz):
    try:
        dt = datetime.datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
        dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone(tz)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return dt_str


def get_registration_status():
    try:
        result = subprocess.run(
            ['asterisk', '-rx', 'pjsip show registrations'],
            capture_output=True, text=True, timeout=5
        )
        out = result.stdout
        if 'Registered' in out and 'Unregistered' not in out.split('Registered')[0]:
            return 'Registered', '#4caf50'
        elif 'Unregistered' in out or 'Rejected' in out:
            return 'Not Registered', '#f44336'
        else:
            return 'Unknown', '#ff9800'
    except Exception as e:
        return f'Error: {e}', '#f44336'


def get_transcript_index():
    index = {}
    try:
        for fname in os.listdir(TRANSCRIPTS_DIR):
            m = re.match(r'^(\d{8})_(\d{6})_(\d+)\.txt$', fname)
            if m:
                key = (m.group(1), m.group(3))
                index.setdefault(key, []).append(fname)
        for key in index:
            index[key].sort()
    except Exception:
        pass
    return index


def get_recent_calls(n=25, tz=None):
    if tz is None:
        tz = ZoneInfo('UTC')
    transcript_index = get_transcript_index()
    try:
        if not os.path.exists(CALL_LOG):
            return []
        with open(CALL_LOG) as f:
            lines = f.readlines()
        active = {}
        completed = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 3)
            if len(parts) < 4:
                continue
            date, time_, event, rest = parts
            dt_local = localise(f"{date} {time_}", tz)
            rest = rest.strip()
            if '(' in rest and ')' in rest:
                number = rest[:rest.index('(')].strip()
                name = rest[rest.index('(')+1:rest.rindex(')')].strip()
                after = rest[rest.rindex(')')+1:].strip()
            else:
                number = rest.split()[0] if rest else rest
                name = ''
                after = ''
            if event == 'Newchannel:':
                date_key = date.replace('-', '')
                transcripts = transcript_index.get((date_key, number), [])
                active[number] = {'start': dt_local, 'name': name,
                                  'transcripts': transcripts}
            elif event == 'Hangup:':
                info = active.pop(number, {})
                completed.append({
                    'start': info.get('start', '?'),
                    'end': dt_local,
                    'number': number,
                    'name': name,
                    'reason': after,
                    'transcripts': info.get('transcripts', []),
                })
        for number, info in active.items():
            completed.append({'start': info['start'], 'end': '🟢 Active',
                               'number': number, 'name': info['name'],
                               'reason': '', 'transcripts': info.get('transcripts', [])})
        return list(reversed(completed[-n:]))
    except Exception:
        return []


def load_json(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_ha_entities(domain):
    """Fetch all HA entities of a given domain via REST API."""
    try:
        opts = get_options()
        ha_url = opts.get('ha_url', 'http://homeassistant:8123').rstrip('/')
        ha_token = opts.get('ha_token', '')
        if not ha_token:
            return []
        req = Request(
            f'{ha_url}/api/states',
            headers={'Authorization': f'Bearer {ha_token}'},
        )
        with urlopen(req, timeout=5) as resp:
            states = json.loads(resp.read())
        result = []
        for s in states:
            if s['entity_id'].startswith(f'{domain}.'):
                friendly = s.get('attributes', {}).get('friendly_name', s['entity_id'])
                result.append({'entity_id': s['entity_id'], 'name': friendly})
        return sorted(result, key=lambda x: x['name'])
    except Exception:
        return []


def render_transcript(filename, base_path):
    path = os.path.join(TRANSCRIPTS_DIR, filename)
    if not os.path.exists(path):
        return b'Not found'
    with open(path) as f:
        content = f.read()

    lines = content.splitlines()
    meta = {}
    transcript_lines = []
    in_transcript = False
    in_summary = False
    summary_lines = []
    for line in lines:
        if line.startswith('Transcript:'):
            in_transcript = True
            in_summary = False
            continue
        if line.startswith('Summary:'):
            in_summary = True
            in_transcript = False
            continue
        if in_transcript:
            transcript_lines.append(line)
        elif in_summary:
            summary_lines.append(line)
        elif ':' in line and not in_transcript:
            k, _, v = line.partition(':')
            meta[k.strip()] = v.strip()

    summary = ' '.join(summary_lines).strip() or 'No summary'
    urgent = meta.get('Urgent', 'False') == 'True'

    bubbles = ''
    for line in transcript_lines:
        if not line.strip():
            continue
        if line.startswith('ASSISTANT:'):
            text = html.escape(line[10:].strip())
            bubbles += f'<div class="bubble ai"><strong>AI</strong><p>{text}</p></div>'
        elif line.startswith('USER:'):
            text = html.escape(line[5:].strip())
            bubbles += f'<div class="bubble caller"><strong>Caller</strong><p>{text}</p></div>'

    urgent_banner = '<div class="urgent">🚨 Urgent</div>' if urgent else ''

    recording_name = filename.replace('.txt', '.wav')
    recording_path = os.path.join(RECORDINGS_DIR, recording_name)
    if os.path.exists(recording_path):
        recording_url = html.escape(f'?recording={recording_name}')
        audio_player = f'<audio controls src="{recording_url}" style="width:100%;margin-bottom:20px"></audio>'
    else:
        audio_player = ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Call Transcript</title>
<style>
  body{{font-family:system-ui,sans-serif;margin:24px;background:#f5f5f5;color:#333;max-width:700px}}
  .back{{color:#1565c0;text-decoration:none;font-size:.9em;display:inline-block;margin-bottom:16px}}
  .back:hover{{text-decoration:underline}}
  h1{{margin:0 0 4px;color:#1565c0;font-size:1.3em}}
  .meta{{color:#666;font-size:.85em;margin-bottom:16px}}
  .summary{{background:#e3f2fd;border-left:4px solid #1565c0;padding:10px 14px;
            border-radius:4px;margin-bottom:20px;font-size:.9em}}
  .urgent{{background:#ffebee;border-left:4px solid #f44336;padding:8px 14px;
           border-radius:4px;margin-bottom:12px;font-weight:bold;color:#c62828}}
  .bubble{{margin:10px 0;padding:10px 14px;border-radius:12px;max-width:85%;font-size:.9em}}
  .bubble strong{{display:block;font-size:.75em;margin-bottom:4px;opacity:.7}}
  .bubble p{{margin:0}}
  .ai{{background:#fff;border:1px solid #ddd;align-self:flex-start}}
  .caller{{background:#e8eaf6;margin-left:auto;text-align:right}}
  .chat{{display:flex;flex-direction:column}}
</style>
</head>
<body>
<a class="back" href=".">← Back to dashboard</a>
<h1>📞 Call from {html.escape(meta.get('Caller ID', ''))}
  {(' — ' + html.escape(meta.get('Caller Name', ''))) if meta.get('Caller Name') else ''}
</h1>
<div class="meta">{html.escape(meta.get('Date', '')[:19].replace('T', ' '))}</div>
{urgent_banner}
{audio_player}
<div class="summary"><strong>Summary:</strong> {html.escape(summary)}</div>
<div class="chat">{bubbles or '<p style="color:#aaa">No transcript available</p>'}</div>
</body>
</html>""".encode('utf-8')


def render_config_page():
    people = load_json(PEOPLE_CONFIG) if os.path.exists(PEOPLE_CONFIG) else []
    if not isinstance(people, list):
        people = []

    person_entities = get_ha_entities('person')
    calendar_entities = get_ha_entities('calendar')

    def entity_options(entities, selected=''):
        opts = '<option value="">— none —</option>'
        for e in entities:
            sel = ' selected' if e['entity_id'] == selected else ''
            label = html.escape(f"{e['name']} ({e['entity_id']})")
            eid = html.escape(e['entity_id'])
            opts += f'<option value="{eid}"{sel}>{label}</option>'
        return opts

    def person_card(p):
        name = html.escape(p.get('name', ''))
        title = html.escape(p.get('title', ''))
        ha_person_opts = entity_options(person_entities, p.get('ha_person', ''))
        calendar_opts = entity_options(calendar_entities, p.get('calendar', ''))
        extension = html.escape(p.get('extension', ''))
        phone = html.escape(p.get('phone', ''))
        owner = 'checked' if p.get('owner') else ''
        card_label = html.escape(p.get('name', 'New Person') or 'New Person')
        return f"""<div class="person-card">
  <div class="card-header">
    <span class="card-name">{card_label}</span>
    <button type="button" class="del-btn" onclick="removeCard(this)">Remove</button>
  </div>
  <div class="fields">
    <label>Name<input type="text" name="name" value="{name}" oninput="updateCardName(this)" placeholder="Paul" required></label>
    <label>Title<input type="text" name="title" value="{title}" placeholder="Mr Hinks"></label>
    <label>HA Person<select name="ha_person">{ha_person_opts}</select></label>
    <label>Calendar<select name="calendar">{calendar_opts}</select></label>
    <label>Extension (SIP)<input type="text" name="extension" value="{extension}" placeholder="PJSIP/paul"></label>
    <label>Mobile<input type="text" name="phone" value="{phone}" placeholder="+14155551234"></label>
    <label class="cb-label"><input type="checkbox" name="owner" {owner}> Primary owner</label>
  </div>
</div>"""

    cards_html = '\n'.join(person_card(p) for p in people)
    template_html = person_card({})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VoIP Bridge — People</title>
<style>
  body{{font-family:system-ui,sans-serif;margin:24px;background:#f5f5f5;color:#333;max-width:860px}}
  a.back{{color:#1565c0;text-decoration:none;font-size:.9em;display:inline-block;margin-bottom:16px}}
  a.back:hover{{text-decoration:underline}}
  h1{{margin:0 0 4px;color:#1565c0}}
  .subtitle{{color:#888;font-size:.85em;margin:0 0 24px}}
  .person-card{{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.12);margin-bottom:16px;overflow:hidden}}
  .card-header{{background:#1565c0;color:#fff;padding:10px 16px;display:flex;justify-content:space-between;align-items:center}}
  .card-name{{font-weight:bold;font-size:.95em}}
  .del-btn{{background:rgba(255,255,255,.18);border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:.8em}}
  .del-btn:hover{{background:rgba(255,255,255,.32)}}
  .fields{{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:16px}}
  .fields label{{display:flex;flex-direction:column;font-size:.78em;font-weight:600;color:#555;gap:4px}}
  .fields input,.fields select{{font-size:.88em;padding:6px 8px;border:1px solid #ddd;border-radius:4px;width:100%;box-sizing:border-box;background:#fff}}
  .fields input:focus,.fields select:focus{{outline:none;border-color:#1565c0;box-shadow:0 0 0 2px rgba(21,101,192,.15)}}
  .cb-label{{flex-direction:row !important;align-items:center;gap:8px;grid-column:span 2;font-size:.88em !important;color:#333 !important}}
  .cb-label input{{width:auto !important}}
  .actions{{display:flex;gap:12px;margin-top:4px}}
  .btn{{padding:9px 22px;border:none;border-radius:6px;font-size:.9em;cursor:pointer;font-weight:600}}
  .btn-add{{background:#e3f2fd;color:#1565c0}}
  .btn-add:hover{{background:#bbdefb}}
  .btn-save{{background:#1565c0;color:#fff}}
  .btn-save:hover{{background:#0d47a1}}
  #msg{{padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:.9em;display:none}}
  #msg.ok{{background:#e8f5e9;color:#2e7d32;border-left:4px solid #4caf50;display:block}}
  #msg.err{{background:#ffebee;color:#c62828;border-left:4px solid #f44336;display:block}}
</style>
</head>
<body>
<a class="back" href=".">← Back to dashboard</a>
<h1>👥 Household People</h1>
<p class="subtitle">The AI receptionist queries presence and calendar live on each call. Changes take effect on the next call — no restart needed.</p>
<div id="msg"></div>
<div id="people-list">{cards_html}</div>
<div class="actions">
  <button class="btn btn-add" onclick="addCard()">+ Add Person</button>
  <button class="btn btn-save" onclick="saveConfig()">Save</button>
</div>

<template id="card-tpl">{template_html}</template>

<script>
function updateCardName(input) {{
  input.closest('.person-card').querySelector('.card-name').textContent = input.value || 'New Person';
}}
function removeCard(btn) {{
  btn.closest('.person-card').remove();
}}
function addCard() {{
  const frag = document.getElementById('card-tpl').content.cloneNode(true);
  document.getElementById('people-list').appendChild(frag);
}}
function saveConfig() {{
  const people = [];
  document.querySelectorAll('.person-card').forEach(card => {{
    const v = n => card.querySelector('[name="' + n + '"]');
    const name = v('name').value.trim();
    if (!name) return;
    people.push({{
      name,
      title:     v('title').value.trim(),
      ha_person: v('ha_person').value,
      calendar:  v('calendar').value,
      extension: v('extension').value.trim(),
      phone:     v('phone').value.trim(),
      owner:     v('owner').checked,
    }});
  }});
  const msg = document.getElementById('msg');
  fetch('config', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(people),
  }})
  .then(r => r.json())
  .then(() => {{
    msg.className = 'ok'; msg.textContent = 'Saved.';
    setTimeout(() => {{ msg.className = ''; msg.style.display = ''; }}, 3000);
  }})
  .catch(() => {{
    msg.className = 'err'; msg.textContent = 'Save failed — check addon logs.';
  }});
}}
</script>
</body>
</html>""".encode('utf-8')


def render_page(base_path=''):
    tz = get_timezone()
    reg_status, reg_color = get_registration_status()
    calls = get_recent_calls(tz=tz)
    contacts = load_json(KNOWN_CONTACTS)
    dids = load_json(KNOWN_DIDS)

    call_rows = ''
    for c in calls:
        active = 'Active' in c['end']
        style = 'background:#e8f5e9' if active else ''
        t = c['transcripts']
        if t:
            transcript_url = f'?transcript={t[-1]}'
            num_cell = f'<a href="{html.escape(transcript_url)}" class="tlink">{html.escape(c["number"])}</a>'
            start_cell = f'<a href="{html.escape(transcript_url)}" class="tlink">{html.escape(c["start"])}</a>'
        else:
            num_cell = html.escape(c['number'])
            start_cell = html.escape(c['start'])
        call_rows += (
            f'<tr style="{style}">'
            f'<td>{start_cell}</td>'
            f'<td>{num_cell}</td>'
            f'<td>{html.escape(c["name"])}</td>'
            f'<td>{html.escape(c["end"])}</td>'
            f'<td>{html.escape(c["reason"])}</td>'
            f'</tr>'
        )

    contact_rows = ''.join(
        f'<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>'
        for k, v in sorted(contacts.items())
    )
    did_rows = ''.join(
        f'<tr><td>{html.escape(k)}</td><td>{html.escape(localise(v.get("first_seen","")[:19], tz))}</td></tr>'
        for k, v in sorted(dids.items())
    )

    empty = '<tr><td colspan="5" class="empty">No calls yet</td></tr>'
    empty2 = '<tr><td colspan="2" class="empty">None</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VoIP Bridge</title>
<meta http-equiv="refresh" content="30">
<style>
  body{{font-family:system-ui,sans-serif;margin:24px;background:#f5f5f5;color:#333}}
  h1{{margin:0 0 6px;color:#1565c0}}
  h2{{border-bottom:2px solid #ddd;padding-bottom:4px;margin:24px 0 10px;color:#444}}
  .badge{{display:inline-block;padding:4px 14px;border-radius:20px;color:#fff;
          font-weight:bold;font-size:.9em;background:{reg_color}}}
  .note{{color:#888;font-size:.8em;margin:4px 0 0}}
  .config-link{{display:inline-block;color:#1565c0;text-decoration:none;font-size:.85em;
               padding:4px 12px;border:1px solid #1565c0;border-radius:20px;margin-left:12px;
               vertical-align:middle;cursor:pointer}}
  .config-link:hover{{background:#e3f2fd}}
  table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;
        box-shadow:0 1px 3px rgba(0,0,0,.12);margin-bottom:8px;overflow:hidden}}
  th{{background:#1565c0;color:#fff;padding:9px 12px;text-align:left;font-size:.85em}}
  td{{padding:7px 12px;border-bottom:1px solid #f0f0f0;font-size:.85em}}
  tr:last-child td{{border-bottom:none}}
  .empty{{color:#aaa;font-style:italic}}
  .tlink{{color:#1565c0;text-decoration:none;font-weight:500}}
  .tlink:hover{{text-decoration:underline}}
</style>
</head>
<body>
<h1>📞 VoIP SIP Bridge</h1>
<a class="config-link" href="config">⚙ People</a>
<span class="badge">{html.escape(reg_status)}</span>
<p class="note">Auto-refreshes every 30 s &nbsp;·&nbsp; Click a call to view transcript</p>

<h2>Recent Calls</h2>
<table>
  <thead><tr><th>Started</th><th>Number</th><th>Caller</th><th>Ended</th><th>Reason</th></tr></thead>
  <tbody>{call_rows or empty}</tbody>
</table>

<h2>Contacts ({len(contacts)})</h2>
<table>
  <thead><tr><th>Number</th><th>Name</th></tr></thead>
  <tbody>{contact_rows or empty2}</tbody>
</table>

<h2>Auto-captured DIDs ({len(dids)})</h2>
<table>
  <thead><tr><th>DID</th><th>First Seen</th></tr></thead>
  <tbody>{did_rows or empty2}</tbody>
</table>
</body>
</html>""".encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        base_path = self.headers.get('X-Ingress-Path', '')
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path.rstrip('/').endswith('/config') or parsed.path == '/config':
            body = render_config_page()
            ct = 'text/html; charset=utf-8'
        elif 'recording' in params:
            filename = os.path.basename(params['recording'][0])
            path = os.path.join(RECORDINGS_DIR, filename)
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    body = f.read()
                ct = 'audio/wav'
            else:
                body = b'Not found'
                ct = 'text/plain'
        elif 'transcript' in params:
            filename = os.path.basename(params['transcript'][0])
            body = render_transcript(filename, base_path)
            ct = 'text/html; charset=utf-8'
        else:
            body = render_page(base_path)
            ct = 'text/html; charset=utf-8'

        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        try:
            length = int(self.headers.get('Content-Length', 0))
            data = self.rfile.read(length)
            people = json.loads(data)
            os.makedirs(SHARE_DIR, exist_ok=True)
            with open(PEOPLE_CONFIG, 'w') as f:
                json.dump(people, f, indent=2)
            response = json.dumps({'ok': True}).encode()
        except Exception as e:
            response = json.dumps({'ok': False, 'error': str(e)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Status server on port {PORT}')
    server.serve_forever()
