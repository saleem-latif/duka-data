"""
dashboard.py — Lightweight local control panel for the Dukascopy downloader
============================================================================
A single-file, zero-dependency (stdlib only) web UI to TRIGGER downloads and
WATCH live progress. No Flask/Node/build step — just:

    python3 dashboard.py            # opens http://127.0.0.1:8765 in your browser

Endpoints (all served by Python's built-in http.server):
    GET  /            -> the single-page UI (embedded HTML/CSS/JS)
    GET  /config      -> selectable symbol universe + default dates
    GET  /status      -> contents of .download_status.json (live progress)
    GET  /log?n=200   -> tail of the most recent logs/*.log file
    POST /start       -> launch download.py detached (refused if one is running)

Security: binds to 127.0.0.1 ONLY. Because /start executes a subprocess, this
must never be exposed to a network. Symbols/dates are validated before use and
the child is spawned with an argv list (no shell), so there is no shell
injection surface.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR    = Path(__file__).parent.resolve()
DOWNLOAD_PY = BASE_DIR / "download.py"
LOG_DIR     = BASE_DIR / "logs"
STATUS_FILE = BASE_DIR / ".download_status.json"

# Selectable universe shown in the UI. Labels are presentation-only; the symbol
# is what gets passed to download.py (Dukascopy datafeed name).
SYMBOL_GROUPS: dict[str, list[str]] = {
    "FX majors":  ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"],
    "FX crosses": ["EURGBP", "EURJPY", "GBPJPY", "AUDJPY"],
    "Metals":     ["XAUUSD", "XAGUSD"],
    "Energy":     ["LIGHTCMDUSD", "BRENTCMDUSD"],
    "Indices":    ["USA500IDXUSD", "USA30IDXUSD", "USATECHIDXUSD"],
}
LABELS = {
    "XAUUSD": "Gold", "XAGUSD": "Silver",
    "LIGHTCMDUSD": "WTI crude", "BRENTCMDUSD": "Brent crude",
    "USA500IDXUSD": "S&P 500", "USA30IDXUSD": "Dow 30", "USATECHIDXUSD": "Nasdaq 100",
}
ALL_SYMBOLS = {s for group in SYMBOL_GROUPS.values() for s in group}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")


# ---------------------------------------------------------------------------
# Download process helpers
# ---------------------------------------------------------------------------

def running_download_pids() -> list[int]:
    """PIDs of any live `download.py` process (started by us or anything else)."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "download.py"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    me = os.getpid()
    return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != me]


def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {"state": "idle"}
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {"state": "unknown"}


def tail_log(n: int = 200) -> dict:
    """Last n lines of the most recently modified logs/*.log file."""
    if not LOG_DIR.is_dir():
        return {"file": None, "lines": []}
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {"file": None, "lines": []}
    path = logs[0]
    with path.open("rb") as f:
        size = path.stat().st_size
        f.seek(max(0, size - 65536))          # only the tail, not the whole file
        chunk = f.read().decode("utf-8", "replace")
    lines = chunk.splitlines()[-n:]
    return {"file": path.name, "lines": lines}


def start_download(payload: dict) -> dict:
    """Validate payload and launch download.py detached. Returns a result dict."""
    if running_download_pids():
        return {"ok": False, "error": "A download is already running. Wait for it to finish."}

    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return {"ok": False, "error": "Select at least one symbol."}
    symbols = [str(s).upper() for s in symbols]
    bad = [s for s in symbols if not SYMBOL_RE.match(s)]
    if bad:
        return {"ok": False, "error": f"Invalid symbol(s): {', '.join(bad)}"}

    incremental = bool(payload.get("incremental"))
    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    try:
        datetime.date.fromisoformat(start)
        datetime.date.fromisoformat(end)
    except ValueError:
        return {"ok": False, "error": "start/end must be valid YYYY-MM-DD dates."}
    if start >= end:
        return {"ok": False, "error": "start date must be before end date."}

    cmd = [sys.executable, "-u", str(DOWNLOAD_PY),
           "--symbols", *symbols, "--start", start, "--end", end]
    if incremental:
        cmd.append("--incremental")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"dashboard_run_{ts}.log"
    logf = open(log_path, "ab")
    logf.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] launching: "
               f"{' '.join(cmd)}\n".encode())
    logf.flush()

    # start_new_session detaches the child so it survives the dashboard exiting.
    proc = subprocess.Popen(
        cmd, stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR), start_new_session=True,
    )
    return {"ok": True, "pid": proc.pid, "log": log_path.name,
            "cmd": " ".join(cmd)}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):           # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        route = urlparse(self.path)
        path, qs = route.path, parse_qs(route.query)

        if path == "/":
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/config":
            groups = {
                name: [{"symbol": s, "label": LABELS.get(s, s)} for s in syms]
                for name, syms in SYMBOL_GROUPS.items()
            }
            yesterday = datetime.date.today() - datetime.timedelta(days=1)
            self._json({"groups": groups,
                        "default_start": "2006-05-24",
                        "default_end": yesterday.isoformat()})
        elif path == "/status":
            st = read_status()
            st["_running_pids"] = running_download_pids()
            self._json(st)
        elif path == "/log":
            n = int(qs.get("n", ["200"])[0])
            self._json(tail_log(max(1, min(n, 2000))))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path)
        if route.path != "/start":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad JSON body"}, 400)
            return
        result = start_download(payload)
        self._json(result, 200 if result.get("ok") else 409)


# ---------------------------------------------------------------------------
# Single-page UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>duka-data dashboard</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2a3340; --fg:#e6edf3; --mut:#8b98a5;
          --accent:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:12px; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  .badge { font-size:12px; padding:2px 10px; border-radius:999px; background:var(--line);
           color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  .badge.running { background:#13315c; color:#7cc0ff; }
  .badge.completed { background:#10341f; color:#6ee7a0; }
  .badge.error,.badge.unknown { background:#3a1417; color:#ff9a9a; }
  main { max-width:1080px; margin:0 auto; padding:24px; display:grid;
         grid-template-columns:1fr 1fr; gap:20px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:18px; }
  .panel h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em;
              color:var(--mut); margin:0 0 14px; }
  .full { grid-column:1 / -1; }
  .grp { margin-bottom:12px; }
  .grp .gh { font-size:12px; color:var(--mut); margin-bottom:4px; display:flex;
             justify-content:space-between; }
  .grp .gh a { color:var(--accent); cursor:pointer; text-decoration:none; font-size:11px; }
  label.chip { display:inline-flex; align-items:center; gap:5px; padding:4px 9px; margin:3px 4px 3px 0;
               border:1px solid var(--line); border-radius:7px; cursor:pointer; font-size:13px; }
  label.chip:hover { border-color:var(--accent); }
  label.chip input { accent-color:var(--accent); }
  .row { display:flex; gap:14px; align-items:end; flex-wrap:wrap; margin-top:10px; }
  .field { display:flex; flex-direction:column; gap:4px; }
  .field span { font-size:11px; color:var(--mut); }
  input[type=date] { background:var(--bg); border:1px solid var(--line); color:var(--fg);
                     border-radius:7px; padding:7px 9px; font-size:13px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:9px 18px;
           font-size:14px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .msg { margin-top:10px; font-size:13px; min-height:18px; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--err); }
  .bar { height:14px; background:var(--bg); border:1px solid var(--line); border-radius:7px;
         overflow:hidden; }
  .bar > div { height:100%; width:0; background:linear-gradient(90deg,#2563eb,#3b82f6);
               transition:width .4s; }
  .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:10px 16px; margin-top:14px; }
  .stat { font-size:13px; } .stat b { display:block; color:var(--mut); font-size:11px;
          text-transform:uppercase; letter-spacing:.04em; font-weight:600; }
  pre#log { background:#0b0e12; border:1px solid var(--line); border-radius:8px; padding:12px;
            font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; color:#b8c4d0;
            max-height:340px; overflow:auto; white-space:pre-wrap; margin:0; }
  .hint { color:var(--mut); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>duka-data</h1>
  <span id="state" class="badge">idle</span>
  <span id="sub" class="hint"></span>
</header>
<main>
  <section class="panel">
    <h2>Start a download</h2>
    <div id="groups"></div>
    <div class="row">
      <label class="field"><span>Start</span><input type="date" id="start"></label>
      <label class="field"><span>End</span><input type="date" id="end"></label>
      <label class="chip"><input type="checkbox" id="incremental"> Incremental</label>
      <button id="go">Start download</button>
    </div>
    <div id="msg" class="msg"></div>
    <div class="hint">Refused if a download is already running. Runs detached — safe to close this tab.</div>
  </section>

  <section class="panel">
    <h2>Live progress</h2>
    <div class="bar"><div id="prog"></div></div>
    <div id="progtxt" class="hint" style="margin-top:8px">No active download.</div>
    <div class="stats" id="stats"></div>
  </section>

  <section class="panel full">
    <h2>Log <span id="logname" class="hint"></span></h2>
    <pre id="log">…</pre>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
const fmt = n => (n==null?'—':Number(n).toLocaleString());
const eta = m => m==null?'—':(m>=60?`${Math.floor(m/60)}h ${Math.round(m%60)}m`:`${Math.round(m)}m`);

async function loadConfig(){
  const c = await (await fetch('/config')).json();
  const wrap = $('groups'); wrap.innerHTML='';
  for(const [name,syms] of Object.entries(c.groups)){
    const g = document.createElement('div'); g.className='grp';
    const def = name.startsWith('FX'); // FX preselected by default
    g.innerHTML = `<div class="gh"><span>${name}</span>
      <span><a data-all>all</a> · <a data-none>none</a></span></div>`;
    const box = document.createElement('div');
    for(const s of syms){
      const l = document.createElement('label'); l.className='chip';
      l.innerHTML = `<input type="checkbox" value="${s.symbol}" ${def?'checked':''}>`+
                    `${s.label}${s.label!==s.symbol?` <span class="hint">${s.symbol}</span>`:''}`;
      box.appendChild(l);
    }
    g.appendChild(box);
    g.querySelector('[data-all]').onclick=()=>box.querySelectorAll('input').forEach(i=>i.checked=true);
    g.querySelector('[data-none]').onclick=()=>box.querySelectorAll('input').forEach(i=>i.checked=false);
    wrap.appendChild(g);
  }
  $('start').value=c.default_start; $('end').value=c.default_end;
}

function chosen(){ return [...document.querySelectorAll('#groups input:checked')].map(i=>i.value); }

$('go').onclick = async () => {
  const m=$('msg'); m.className='msg'; m.textContent='Starting…'; $('go').disabled=true;
  try{
    const r = await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:chosen(),start:$('start').value,end:$('end').value,
        incremental:$('incremental').checked})});
    const j = await r.json();
    if(j.ok){ m.className='msg ok'; m.textContent=`Started (PID ${j.pid}) → ${j.log}`; }
    else { m.className='msg err'; m.textContent=j.error||'Failed to start.'; }
  }catch(e){ m.className='msg err'; m.textContent=String(e); }
  $('go').disabled=false;
};

function renderStatus(s){
  const running = (s._running_pids||[]).length>0;
  const state = running ? 'running' : (s.state||'idle');
  const b=$('state'); b.textContent=state; b.className='badge '+state;
  $('sub').textContent = s.date_range ? s.date_range : '';

  const done=s.days_completed, tot=s.days_total;
  if(tot){
    const pct=Math.min(100,100*done/tot);
    $('prog').style.width=pct+'%';
    $('progtxt').textContent=`${s.current_symbol||''} ${s.symbol_progress||''} · `+
      `${fmt(done)}/${fmt(tot)} days (${pct.toFixed(1)}%) · ${s.symbol_status||''}`;
  } else { $('prog').style.width='0'; $('progtxt').textContent='No active download.'; }

  const updatedAgo = s.updated ? Math.round((Date.now()-new Date(s.updated))/1000)+'s ago' : '—';
  const st=[
    ['Ticks', fmt(s.ticks_total)], ['Rate', s.rate_days_per_sec!=null?s.rate_days_per_sec+' d/s':'—'],
    ['ETA', eta(s.eta_minutes)], ['Retries', fmt(s.http_retries)],
    ['Failures', fmt(s.http_failures)], ['Downloaded', s.bytes_downloaded_mb!=null?s.bytes_downloaded_mb+' MB':'—'],
    ['RSS peak', s.rss_peak_mb!=null?s.rss_peak_mb+' MB':'—'], ['Day p95', s.day_p95_seconds!=null?s.day_p95_seconds+'s':'—'],
    ['Updated', updatedAgo],
  ];
  $('stats').innerHTML = st.map(([k,v])=>`<div class="stat"><b>${k}</b>${v}</div>`).join('');
  $('go').disabled = running;
}

async function pollStatus(){ try{ renderStatus(await (await fetch('/status')).json()); }catch(e){} }
async function pollLog(){
  try{ const j=await (await fetch('/log?n=200')).json();
    $('logname').textContent=j.file?('· '+j.file):'';
    const el=$('log'); const atBottom=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
    el.textContent=(j.lines||[]).join('\n'); if(atBottom) el.scrollTop=el.scrollHeight;
  }catch(e){}
}

loadConfig();
pollStatus(); pollLog();
setInterval(pollStatus,2000);
setInterval(pollLog,4000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Local control panel for download.py")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"duka-data dashboard → {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
