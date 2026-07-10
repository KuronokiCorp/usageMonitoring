#!/usr/bin/env python3
"""
iterm_web - a local web admin for controlling iTerm2 sessions.

Reuses the AppleScript helpers in iterm_ctl.py. Stdlib only (no pip installs).
Serves a single-page admin at http://127.0.0.1:8765 that lets you:
  - see all live iTerm2 sessions (auto-refreshing)
  - type a command and send it to a chosen session (with Claude-TUI submit)
  - register recurring "cron" jobs that send a command on a schedule

The scheduler runs inside this process, so jobs only fire while the server is
running. Because the server is launched from your terminal (a GUI login
session), it inherits the automation permission that lets AppleScript drive
iTerm2 -- which a plain system crontab often lacks.

Usage:
    ./iterm_web.py                 # serve on 127.0.0.1:8765
    ./iterm_web.py --port 9000
    ./iterm_web.py --open          # also open the page in your browser
"""
import argparse
import json
import os
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import iterm_ctl
import iterm_ai

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_FILE = os.path.join(HERE, "iterm_jobs.json")

_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Core send (shared by API and scheduler)
# --------------------------------------------------------------------------- #
def do_send(target: str, command: str, submit: bool) -> list[dict]:
    """Send `command` to sessions matching `target`. Returns list of hit sessions.

    target: 'id:...', 'tty:...', 'name:...', an index like '2.1.1', a substring,
            or the special value '__all__' for every session.
    submit: when True, send an extra bare Enter afterward (Claude Code's TUI keeps
            pasted text as a draft, so a second Enter is needed to actually send).
    """
    sessions = iterm_ctl.list_sessions()
    all_flag = target == "__all__"
    targets = iterm_ctl.resolve_targets(sessions, None if all_flag else target, all_flag)
    hits = []
    for s in targets:
        iterm_ctl.send_text(s, command, enter=True)
        if submit:
            iterm_ctl.send_text(s, "", enter=True)
        hits.append({"index": s.index, "tty": s.tty, "name": s.name, "id": s.id})
    return hits


def run_job(job: dict) -> dict:
    """Execute a scheduled job. If ai_check is set, read each target and only send
    when the AI/heuristic judges the session should 'continue'. Returns a summary
    dict with a human-readable 'status' string."""
    target = job["target"]
    command = job["command"]
    submit = job.get("submit", False)
    if not job.get("ai_check", False):
        hits = do_send(target, command, submit)
        return {"status": f"sent to {len(hits)} session(s)", "sent": len(hits)}

    sessions = iterm_ctl.list_sessions()
    all_flag = target == "__all__"
    targets = iterm_ctl.resolve_targets(sessions, None if all_flag else target, all_flag)
    decisions, sent, resume_times = [], 0, []
    for s in targets:
        verdict = iterm_ai.judge(iterm_ctl.read_contents(s))
        action = verdict["action"]
        if action == "continue":
            iterm_ctl.send_text(s, command, enter=True)
            if submit:
                iterm_ctl.send_text(s, "", enter=True)
            sent += 1
        elif action == "wait" and verdict.get("resume_at"):
            resume_times.append(verdict["resume_at"])
        decisions.append({"index": s.index, "name": s.name, "action": action,
                          "reason": verdict["reason"], "backend": verdict.get("backend"),
                          "resume_at": verdict.get("resume_at")})
    brief = ", ".join(f'{d["index"]}:{d["action"]}' for d in decisions) or "no targets"
    # earliest known reset among sessions we're waiting on -> precise one-shot re-check
    resume_at = min(resume_times) if resume_times else None
    status = f"AI sent {sent}/{len(targets)} ({brief})"
    if resume_at:
        status += f" · waking at {resume_at}"
    return {"status": status, "sent": sent, "decisions": decisions, "resume_at": resume_at}


# --------------------------------------------------------------------------- #
# Cron parsing (5 fields: minute hour day-of-month month day-of-week)
# --------------------------------------------------------------------------- #
def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return {v for v in values if lo <= v <= hi}


def cron_match(expr: str, dt: datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron must have 5 fields: minute hour day month weekday")
    minute = _parse_field(fields[0], 0, 59)
    hour = _parse_field(fields[1], 0, 23)
    dom = _parse_field(fields[2], 1, 31)
    month = _parse_field(fields[3], 1, 12)
    # cron weekday: 0 and 7 are Sunday. Python weekday(): Mon=0..Sun=6.
    dow_raw = _parse_field(fields[4], 0, 7)
    dow = {d % 7 for d in dow_raw}
    py_dow = (dt.weekday() + 1) % 7  # convert Mon=0..Sun=6 -> Sun=0..Sat=6
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    # Standard cron: when both day-of-month and day-of-week are restricted, a match
    # on EITHER counts; if only one is restricted, use it; if neither, any day.
    if dom_restricted and dow_restricted:
        day_ok = (dt.day in dom) or (py_dow in dow)
    elif dom_restricted:
        day_ok = dt.day in dom
    elif dow_restricted:
        day_ok = py_dow in dow
    else:
        day_ok = True
    return (dt.minute in minute and dt.hour in hour and day_ok and dt.month in month)


def next_run_after(expr: str, after: datetime) -> str | None:
    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):  # scan up to ~1 year
        if cron_match(expr, dt):
            return dt.strftime("%Y-%m-%d %H:%M")
        dt += timedelta(minutes=1)
    return None


# --------------------------------------------------------------------------- #
# Job storage
# --------------------------------------------------------------------------- #
def load_jobs() -> list[dict]:
    with _jobs_lock:
        if not os.path.exists(JOBS_FILE):
            return []
        try:
            with open(JOBS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []


def save_jobs(jobs: list[dict]) -> None:
    with _jobs_lock:
        with open(JOBS_FILE, "w") as f:
            json.dump(jobs, f, indent=2)


# --------------------------------------------------------------------------- #
# Scheduler thread
# --------------------------------------------------------------------------- #
def scheduler_loop():
    last_minute = None
    while True:
        now = datetime.now().replace(second=0, microsecond=0)
        if now != last_minute:
            last_minute = now
            now_str = now.strftime("%Y-%m-%d %H:%M")
            jobs = load_jobs()
            changed = False
            for job in jobs:
                if not job.get("enabled", True):
                    continue
                # Fire when the cron matches OR a precise one-shot resume is due
                # (resume_at was set on a prior run when the AI read a reset time).
                resume_due = job.get("resume_at") and job["resume_at"] <= now_str
                try:
                    if cron_match(job["schedule"], now) or resume_due:
                        result = run_job(job)
                        job["last_run"] = now_str
                        job["last_status"] = result["status"]
                        # carry the next precise wake time (or clear it once resumed)
                        job["resume_at"] = result.get("resume_at")
                        changed = True
                except Exception as e:  # keep the scheduler alive on any single failure
                    job["last_run"] = now_str
                    job["last_status"] = f"error: {e}"
                    changed = True
            if changed:
                save_jobs(jobs)
        # sleep until just past the next minute boundary
        time.sleep(max(1, 61 - datetime.now().second))


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    # ---- routing ----
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._html(PAGE)
        if self.path == "/api/sessions":
            try:
                sessions = iterm_ctl.list_sessions()
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            return self._json([
                {"index": s.index, "id": s.id, "tty": s.tty, "job": s.job, "name": s.name}
                for s in sessions
            ])
        if self.path == "/api/ai/health":
            return self._json(iterm_ai.health())
        if self.path == "/api/jobs":
            jobs = load_jobs()
            now = datetime.now()
            for j in jobs:
                try:
                    j["next_run"] = next_run_after(j["schedule"], now) if j.get("enabled", True) else None
                except Exception:
                    j["next_run"] = None
            return self._json(jobs)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()
        if self.path == "/api/send":
            command = body.get("command", "")
            target = body.get("target", "")
            submit = bool(body.get("submit", False))
            if not target or command == "":
                return self._json({"error": "target and command are required"}, 400)
            try:
                hits = do_send(target, command, submit)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            return self._json({"sent": hits})

        if self.path == "/api/read":
            target = body.get("target", "")
            try:
                sessions = iterm_ctl.list_sessions()
                targets = iterm_ctl.resolve_targets(sessions, target, target == "__all__")
                if not targets:
                    return self._json({"error": "no match"}, 404)
                return self._json({"contents": iterm_ctl.read_contents(targets[0])})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if self.path == "/api/jobs/create":
            required = ("name", "target", "command", "schedule")
            if any(not str(body.get(k, "")).strip() for k in required):
                return self._json({"error": "name, target, command, schedule are required"}, 400)
            try:
                next_run_after(body["schedule"], datetime.now())  # validate cron
            except Exception as e:
                return self._json({"error": f"bad schedule: {e}"}, 400)
            jobs = load_jobs()
            jobs.append({
                "id": uuid.uuid4().hex[:8],
                "name": body["name"].strip(),
                "target": body["target"].strip(),
                "command": body["command"],
                "submit": bool(body.get("submit", False)),
                "ai_check": bool(body.get("ai_check", False)),
                "schedule": body["schedule"].strip(),
                "enabled": True,
                "last_run": None,
                "last_status": None,
            })
            save_jobs(jobs)
            return self._json({"ok": True})

        if self.path == "/api/jobs/create_bulk":
            items = body.get("jobs", [])
            if not isinstance(items, list) or not items:
                return self._json({"error": "jobs must be a non-empty list"}, 400)
            new_entries = []
            for it in items:
                required = ("name", "target", "command", "schedule")
                if any(not str(it.get(k, "")).strip() for k in required):
                    return self._json({"error": "each job needs name, target, command, schedule"}, 400)
                try:
                    next_run_after(it["schedule"], datetime.now())  # validate cron
                except Exception as e:
                    return self._json({"error": f"bad schedule '{it['schedule']}': {e}"}, 400)
                new_entries.append({
                    "id": uuid.uuid4().hex[:8],
                    "name": it["name"].strip(),
                    "target": it["target"].strip(),
                    "command": it["command"],
                    "submit": bool(it.get("submit", False)),
                    "ai_check": bool(it.get("ai_check", False)),
                    "schedule": it["schedule"].strip(),
                    "enabled": True,
                    "last_run": None,
                    "last_status": None,
                })
            jobs = load_jobs()
            jobs.extend(new_entries)
            save_jobs(jobs)
            return self._json({"ok": True, "created": len(new_entries)})

        if self.path == "/api/jobs/toggle":
            jid = body.get("id")
            jobs = load_jobs()
            for j in jobs:
                if j["id"] == jid:
                    j["enabled"] = not j.get("enabled", True)
            save_jobs(jobs)
            return self._json({"ok": True})

        if self.path == "/api/jobs/delete":
            jid = body.get("id")
            jobs = [j for j in load_jobs() if j["id"] != jid]
            save_jobs(jobs)
            return self._json({"ok": True})

        if self.path == "/api/jobs/run":  # fire once now (manual test)
            jid = body.get("id")
            for j in load_jobs():
                if j["id"] == jid:
                    try:
                        return self._json(run_job(j))
                    except Exception as e:
                        return self._json({"error": str(e)}, 500)
            return self._json({"error": "job not found"}, 404)

        if self.path == "/api/ai/check":  # read a target and judge, WITHOUT sending
            target = body.get("target", "")
            try:
                sessions = iterm_ctl.list_sessions()
                targets = iterm_ctl.resolve_targets(sessions, target, target == "__all__")
                if not targets:
                    return self._json({"error": "no match"}, 404)
                out = []
                for s in targets:
                    v = iterm_ai.judge(iterm_ctl.read_contents(s))
                    out.append({"index": s.index, "name": s.name, **v})
                return self._json({"decisions": out})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# Front-end (single page, inline)
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>iTerm2 Admin</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#0f1115; color:#e6e6e6; }
  header { padding:14px 20px; background:#161a22; border-bottom:1px solid #2a2f3a;
           display:flex; align-items:center; gap:12px; position:sticky; top:0; z-index:5; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .dot { width:8px;height:8px;border-radius:50%;background:#38c172; }
  main { display:grid; grid-template-columns: 1.3fr 1fr; gap:16px; padding:16px; max-width:1200px; margin:0 auto; }
  @media (max-width:900px){ main{grid-template-columns:1fr;} }
  .card { background:#161a22; border:1px solid #2a2f3a; border-radius:10px; padding:14px 16px; }
  .card h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#8b93a7;
             margin:0 0 10px; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #232833; font-size:13px; }
  th { color:#8b93a7; font-weight:500; }
  tr:hover td { background:#1b202a; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .pill { display:inline-block; padding:1px 7px; border-radius:20px; font-size:11px;
          background:#243244; color:#9cc4ff; }
  .pill.node { background:#2b2340; color:#c8a8ff; }
  button { font:inherit; cursor:pointer; border:1px solid #33465f; background:#20304a;
           color:#dbe7ff; border-radius:6px; padding:5px 10px; }
  button:hover { background:#2a3d5c; }
  button.ghost { background:transparent; border-color:#394150; color:#aab3c5; padding:3px 8px; font-size:12px; }
  button.danger { border-color:#5c3333; background:#3a2020; color:#ffcaca; }
  label { display:block; margin:10px 0 4px; color:#aab3c5; font-size:12px; }
  input,select,textarea { width:100%; font:inherit; background:#0f1319; color:#e6e6e6;
           border:1px solid #2a2f3a; border-radius:6px; padding:7px 9px; }
  textarea { resize:vertical; min-height:60px; font-family:ui-monospace,Menlo,monospace; }
  .row { display:flex; gap:10px; align-items:center; }
  .row > * { flex:1; }
  .check { display:flex; align-items:center; gap:8px; margin-top:10px; }
  .check input { width:auto; }
  .muted { color:#8b93a7; font-size:12px; }
  .result { margin-top:10px; padding:8px 10px; border-radius:6px; font-size:12px; white-space:pre-wrap;
            font-family:ui-monospace,Menlo,monospace; display:none; }
  .result.ok { background:#132b1d; color:#9ff0c0; display:block; }
  .result.err { background:#2b1414; color:#ffb0b0; display:block; }
  .presets button { font-size:11px; padding:3px 7px; margin:0 4px 4px 0; }
  .jobrow .st { font-size:11px; color:#8b93a7; }
  .full { grid-column:1 / -1; }
</style></head>
<body>
<header>
  <span class="dot"></span><h1>iTerm2 Admin</h1>
  <span class="muted" id="clock"></span>
  <span class="pill" id="aiBadge" style="margin-left:auto" title="AI backend for auto-continue">AI: …</span>
</header>
<main>
  <!-- Sessions -->
  <section class="card">
    <h2>Sessions <button class="ghost" onclick="loadSessions()">refresh</button></h2>
    <table><thead><tr><th>idx</th><th>job</th><th>name</th><th></th></tr></thead>
      <tbody id="sessions"></tbody></table>
    <div class="muted" style="margin-top:8px">Indexes shift as windows reorder — sending always uses the stable session id.</div>
  </section>

  <!-- Send -->
  <section class="card">
    <h2>Send a command</h2>
    <label>Target session</label>
    <select id="sendTarget"></select>
    <label>Command / message</label>
    <textarea id="sendCmd" placeholder="e.g. continue"></textarea>
    <div class="check"><input type="checkbox" id="sendSubmit" checked>
      <label for="sendSubmit" style="margin:0">Submit (extra Enter — needed for Claude Code sessions)</label></div>
    <div class="row" style="margin-top:12px">
      <button onclick="send()">Send</button>
      <button class="ghost" onclick="readTarget()">Preview screen</button>
      <button class="ghost" onclick="aiCheck()">AI check</button>
    </div>
    <div class="result" id="sendResult"></div>
    <pre class="result" id="readResult" style="max-height:220px;overflow:auto"></pre>
  </section>

  <!-- Cron register -->
  <section class="card full">
    <h2>Register a scheduled (cron) send</h2>
    <div class="row">
      <div>
        <label>Job name</label>
        <input id="jobName" placeholder="e.g. hourly continue">
      </div>
      <div>
        <label>Target session(s) — ⌘/Ctrl-click for multiple</label>
        <select id="jobTargets" multiple size="6"></select>
      </div>
      <div>
        <label>Schedule (cron: min hour dom mon dow)</label>
        <input id="jobSchedule" class="mono" placeholder="0 * * * *" value="0 * * * *">
        <div class="muted" style="margin-top:6px">Selecting N sessions registers N jobs (one each) in a single click.</div>
      </div>
    </div>
    <div class="presets" style="margin-top:8px">
      <span class="muted">presets:</span>
      <button onclick="setCron('*/5 * * * *')">every 5 min</button>
      <button onclick="setCron('0 * * * *')">hourly</button>
      <button onclick="setCron('0 9 * * *')">daily 9am</button>
      <button onclick="setCron('0 9 * * 1-5')">weekdays 9am</button>
      <button onclick="setCron('*/30 * * * *')">every 30 min</button>
    </div>
    <label>Command / message</label>
    <textarea id="jobCmd" placeholder="continue"></textarea>
    <div class="check"><input type="checkbox" id="jobSubmit" checked>
      <label for="jobSubmit" style="margin:0">Submit (extra Enter for Claude Code)</label></div>
    <div class="check"><input type="checkbox" id="jobAi">
      <label for="jobAi" style="margin:0">AI check first — only send if the session looks idle / interrupted / limit-reset (skips busy sessions)</label></div>
    <div style="margin-top:12px"><button onclick="createJob()">Register job</button></div>
    <div class="result" id="jobResult"></div>
  </section>

  <!-- Cron list -->
  <section class="card full">
    <h2>Scheduled jobs <button class="ghost" onclick="loadJobs()">refresh</button></h2>
    <table><thead><tr><th>name</th><th>schedule</th><th>target</th><th>next run</th><th>last run</th><th></th></tr></thead>
      <tbody id="jobs"></tbody></table>
    <div class="muted" style="margin-top:8px">Jobs fire only while this server is running.</div>
  </section>
</main>

<script>
let SESSIONS = [];
const $ = id => document.getElementById(id);

function tick(){ $('clock').textContent = new Date().toLocaleTimeString(); }
setInterval(tick, 1000); tick();

async function api(path, body){
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

function targetOptions(){
  let html = '<option value="__all__">— ALL sessions —</option>';
  for(const s of SESSIONS){
    const label = `${s.index}  ${s.job||''}  ${s.name}`;
    html += `<option value="id:${s.id}">${label.replace(/</g,'&lt;')}</option>`;
  }
  return html;
}

async function loadSessions(){
  SESSIONS = await api('/api/sessions');
  if(SESSIONS.error){ return; }
  const tb = $('sessions'); tb.innerHTML='';
  for(const s of SESSIONS){
    const cls = (s.job==='node')?'pill node':'pill';
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="mono">${s.index}</td>
      <td><span class="${cls}">${s.job||'?'}</span></td>
      <td>${s.name.replace(/</g,'&lt;')}</td>
      <td><button class="ghost" data-id="id:${s.id}">use</button></td>`;
    tr.querySelector('button').onclick = () => {
      $('sendTarget').value = 'id:'+s.id;
      for(const o of $('jobTargets').options){ if(o.value==='id:'+s.id) o.selected = true; }
    };
    tb.appendChild(tr);
  }
  const keepSend = $('sendTarget').value;
  const keepJob = [...$('jobTargets').selectedOptions].map(o=>o.value);
  $('sendTarget').innerHTML = targetOptions();
  $('jobTargets').innerHTML = targetOptions();
  if(keepSend) $('sendTarget').value = keepSend;
  for(const o of $('jobTargets').options){ if(keepJob.includes(o.value)) o.selected = true; }
}

function show(id, obj){
  const el = $(id);
  if(obj.error){ el.className='result err'; el.textContent = 'Error: '+obj.error; }
  else if(obj.sent){ el.className='result ok';
    el.textContent = 'Sent to:\n' + obj.sent.map(h=>`  ${h.index}  ${h.tty}  ${h.name}`).join('\n'); }
  else { el.className='result ok'; el.textContent = JSON.stringify(obj); }
}

async function send(){
  const r = await api('/api/send', {
    target:$('sendTarget').value, command:$('sendCmd').value, submit:$('sendSubmit').checked});
  show('sendResult', r);
}

async function readTarget(){
  const r = await api('/api/read', {target:$('sendTarget').value});
  const el = $('readResult');
  if(r.error){ el.className='result err'; el.textContent='Error: '+r.error; }
  else { el.className='result ok'; el.textContent = r.contents; el.scrollTop = el.scrollHeight; }
}

async function aiCheck(){
  const el = $('sendResult');
  el.className='result ok'; el.textContent='Reading & judging…';
  const r = await api('/api/ai/check', {target:$('sendTarget').value});
  if(r.error){ el.className='result err'; el.textContent='Error: '+r.error; return; }
  el.className='result ok';
  el.textContent = r.decisions.map(d =>
    `${d.index}  ${d.name}\n  → ${d.action.toUpperCase()} — ${d.reason}\n  [${d.backend||''}]`
  ).join('\n\n');
}

async function loadHealth(){
  try{
    const h = await api('/api/ai/health');
    const b = $('aiBadge');
    if(h.backend === 'minimax'){
      b.textContent = 'AI: MiniMax ('+h.model+')'; b.style.background='#132b1d'; b.style.color='#9ff0c0';
      b.title = 'Auto-continue judged by MiniMax — '+h.base_url;
    } else {
      b.textContent = 'AI: heuristic'; b.style.background='#2b2718'; b.style.color='#ffe0a0';
      b.title = 'MINIMAX_API_KEY not set — using built-in rules. Set MINIMAX_API_KEY (and optionally MINIMAX_MODEL / MINIMAX_BASE_URL) to enable MiniMax.';
    }
  }catch(e){ $('aiBadge').textContent='AI: ?'; }
}

function setCron(v){ $('jobSchedule').value = v; }

async function createJob(){
  const picked = [...$('jobTargets').selectedOptions].map(o=>({value:o.value, label:o.textContent}));
  const el = $('jobResult');
  if(!picked.length){ el.className='result err'; el.textContent='Pick at least one target session.'; return; }
  const baseName = $('jobName').value.trim();
  if(!baseName){ el.className='result err'; el.textContent='Job name is required.'; return; }
  // If "ALL sessions" is among the picks, collapse to a single __all__ job.
  const hasAll = picked.some(p=>p.value==='__all__');
  const targets = hasAll ? [{value:'__all__', label:'ALL sessions'}] : picked;
  const multi = targets.length > 1;
  const jobs = targets.map(t => ({
    name: multi ? `${baseName} — ${t.label.trim().slice(0,24)}` : baseName,
    target: t.value, command:$('jobCmd').value,
    schedule:$('jobSchedule').value, submit:$('jobSubmit').checked, ai_check:$('jobAi').checked}));
  const r = await api('/api/jobs/create_bulk', {jobs});
  if(r.ok){ el.className='result ok'; el.textContent=`Registered ${r.created} job(s).`;
    $('jobName').value=''; $('jobCmd').value=''; loadJobs(); }
  else { el.className='result err'; el.textContent='Error: '+(r.error||'failed'); }
}

async function loadJobs(){
  const jobs = await api('/api/jobs');
  const tb = $('jobs'); tb.innerHTML='';
  if(!jobs.length){ tb.innerHTML='<tr><td colspan="6" class="muted">No jobs yet.</td></tr>'; return; }
  for(const j of jobs){
    const tr = document.createElement('tr'); tr.className='jobrow';
    const targetName = (SESSIONS.find(s=>'id:'+s.id===j.target)||{}).name || j.target;
    tr.innerHTML = `
      <td>${j.name.replace(/</g,'&lt;')}<div class="st">${j.enabled?'enabled':'paused'} · “${(j.command||'').replace(/</g,'&lt;').slice(0,40)}”${j.submit?' ⏎':''}${j.ai_check?' · 🤖 AI-gated':''}</div></td>
      <td class="mono">${j.schedule}</td>
      <td>${targetName.replace(/</g,'&lt;').slice(0,28)}</td>
      <td class="mono">${j.next_run||'—'}</td>
      <td class="mono">${j.last_run||'—'}<div class="st">${j.last_status||''}${j.resume_at?` · ⏰ wake ${j.resume_at}`:''}</div></td>
      <td></td>`;
    const actions = tr.lastElementChild;
    const mk = (label, cls, fn) => { const b=document.createElement('button'); b.className=cls; b.textContent=label; b.onclick=fn; b.style.marginRight='4px'; return b; };
    actions.appendChild(mk('run', 'ghost', async()=>{ const r=await api('/api/jobs/run',{id:j.id}); show('jobResult', r); }));
    actions.appendChild(mk(j.enabled?'pause':'resume', 'ghost', async()=>{ await api('/api/jobs/toggle',{id:j.id}); loadJobs(); }));
    actions.appendChild(mk('delete', 'danger', async()=>{ await api('/api/jobs/delete',{id:j.id}); loadJobs(); }));
    tb.appendChild(tr);
  }
}

loadSessions().then(loadJobs);
loadHealth();
setInterval(loadSessions, 5000);
setInterval(loadJobs, 15000);
setInterval(loadHealth, 30000);
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Web admin for iTerm2 session control.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"iTerm2 admin running at {url}  (Ctrl-C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
