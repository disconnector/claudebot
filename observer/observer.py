"""
Claude Observer — Read-Only Public View
Port 5004. Token-protected. No write access whatsoever.
"""

from flask import Flask, request, jsonify, Response, abort
import sqlite3
import time
import json
import threading
import os
import logging
from pathlib import Path

app = Flask(__name__)
DB_PATH    = str(Path(__file__).parent.parent / "web-chat" / "chat_history.db")
TOKEN_PATH = Path(__file__).parent / "token.txt"
LOG_PATH   = Path(__file__).parent / "access.log"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("observer")

RATE_LIMIT = 90  # requests per minute per IP
_rate_store = {}
_rate_lock  = threading.Lock()

# Viewer count tracking
_viewers     = {}  # token -> last_seen timestamp
_viewers_lock = threading.Lock()

def get_token():
    with open(TOKEN_PATH) as f:
        return f.read().strip()

TOKEN = get_token()

# ── Security headers ──
def secure(response):
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "no-referrer"
    response.headers["Content-Security-Policy"]   = (
        "default-src 'none'; "
        "script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "img-src 'none';"
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

@app.after_request
def after_request(resp):
    return secure(resp)

# ── Rate limiting ──
def check_rate(ip):
    now = int(time.time())
    with _rate_lock:
        # Prune stale IPs entirely (not just their timestamps)
        stale_ips = [k for k, v in _rate_store.items() if not v or max(v) < now - 60]
        for k in stale_ips:
            del _rate_store[k]
        ts = _rate_store.get(ip, [])
        ts = [t for t in ts if t > now - 60]
        if len(ts) >= RATE_LIMIT:
            return False
        ts.append(now)
        _rate_store[ip] = ts
    return True

# ── Access log ──
# Open log file once at module level - no open/close per request
_log_file = open(LOG_PATH, "a", buffering=1)  # line-buffered

def log_access(ip, path, status):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {path} {status}\n"
    _log_file.write(line)

@app.before_request
def before():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    if not check_rate(ip):
        log_access(ip, request.path, 429)
        abort(429)
    log_access(ip, request.path, "-")

# ── Auth check ──
def check_token():
    t = request.args.get("token") or request.headers.get("X-Auth-Token", "")
    if t != TOKEN:
        abort(403)
    return t

# ── Viewer tracking ──
def viewer_id():
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

def mark_viewer_active():
    vid = viewer_id()
    with _viewers_lock:
        _viewers[vid] = time.time()
    # clean up stale (>30s)
    now = time.time()
    with _viewers_lock:
        stale = [k for k,v in _viewers.items() if now - v > 30]
        for k in stale:
            del _viewers[k]

def viewer_count():
    now = time.time()
    with _viewers_lock:
        return sum(1 for v in _viewers.values() if now - v <= 30)

# ── DB helpers (read-only) ──
def get_chat(limit=200):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_work(limit=500):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM work_events ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,)
        ).fetchall()
    except:
        rows = []
    conn.close()
    return [dict(r) for r in rows]

def get_new_events(last_chat_id, last_work_id):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    chat = [dict(r) for r in conn.execute(
        "SELECT * FROM messages WHERE id > ? ORDER BY id ASC", (last_chat_id,)
    ).fetchall()]
    try:
        work = [dict(r) for r in conn.execute(
            "SELECT * FROM work_events WHERE id > ? ORDER BY id ASC", (last_work_id,)
        ).fetchall()]
    except:
        work = []
    conn.close()
    return chat, work

# ── Routes ──

OBSERVER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>observer · claudecode</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#0f0f0f; --bg2:#141414; --bg3:#1a1a1a;
    --text:#c8c8c8; --dim:#2e2e2e; --dim2:#5a5a5a; --border:#222;
    --user:#e8bf8a; --claude:#4ec9b0; --codex:#e09556;
    --work-bg:#0c0c0c; --cmd:#9cdcfe; --out:#6a9955; --red:#f48771;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { height:100%; overflow:hidden;
    font-family:'JetBrains Mono','SF Mono',Menlo,monospace;
    font-size:12px; line-height:1.4; background:var(--bg); color:var(--text); }

  .app { display:flex; flex-direction:column; height:100dvh; overflow:hidden; }

  /* Header */
  .header {
    display:flex; align-items:center; justify-content:space-between;
    padding:5px 12px; border-bottom:1px solid var(--border);
    background:var(--bg2); flex-shrink:0; min-height:30px;
  }
  .header-left { display:flex; align-items:center; gap:14px; }
  .logo { font-size:11px; font-weight:400; color:var(--dim2); }
  .logo b { color:var(--text); font-weight:600; }
  .badge {
    font-size:9px; padding:2px 7px; border-radius:10px;
    background:#1a2a1a; color:#4ec9b0; border:1px solid #2a4a2a;
    letter-spacing:.04em;
  }
  .agent-indicators { display:flex; gap:10px; }
  .agent-ind { font-size:9px; display:flex; align-items:center; gap:3px; opacity:.4; }
  .agent-ind.online { opacity:1; }
  .agent-ind .dot { width:5px; height:5px; border-radius:50%; }
  .agent-ind.claude .dot { background:var(--claude); }
  .agent-ind.codex  .dot { background:var(--codex); }
  .agent-ind.claude { color:var(--claude); }
  .agent-ind.codex  { color:var(--codex); }
  .viewer-count {
    font-size:9px; color:var(--dim2); display:flex; align-items:center; gap:4px;
  }
  .viewer-count .vdot {
    width:5px; height:5px; border-radius:50%; background:var(--user);
    animation: vpulse 2s infinite;
  }
  @keyframes vpulse { 0%,100%{opacity:.4} 50%{opacity:1} }

  /* Mobile tabs */
  .tab-bar { display:none; border-bottom:1px solid var(--border); background:var(--bg2); }
  .tab-bar button {
    flex:1; background:none; border:none; color:var(--dim2);
    font-family:inherit; font-size:11px; padding:8px 0; cursor:pointer;
    border-bottom:2px solid transparent;
  }
  .tab-bar button.active { color:var(--text); border-bottom-color:var(--claude); }
  .tab-bar button.tab-work.active { border-bottom-color:var(--codex); }

  /* Split */
  .split { flex:1; display:flex; overflow:hidden; min-height:0; }
  .panel { display:flex; flex-direction:column; overflow:hidden; min-width:0; }
  .panel-chat { flex:1; border-right:1px solid var(--border); }
  .panel-work { flex:1; background:var(--work-bg); }
  .panel-label {
    font-size:9px; color:var(--dim2); text-transform:uppercase;
    letter-spacing:.08em; padding:4px 10px 3px;
    border-bottom:1px solid var(--border); background:var(--bg2); flex-shrink:0;
  }
  .divider { width:4px; background:var(--border); cursor:col-resize; flex-shrink:0; }
  .divider:hover { background:var(--dim); }

  /* Read-only banner */
  .ro-banner {
    text-align:center; font-size:9px; color:var(--dim2);
    padding:2px 0; background:#111; border-bottom:1px solid var(--border);
    flex-shrink:0; letter-spacing:.06em;
  }

  /* Chat */
  .chat-scroll { flex:1; overflow-y:auto; padding:8px 10px 4px; }
  .chat-scroll::-webkit-scrollbar { width:3px; }
  .chat-scroll::-webkit-scrollbar-thumb { background:var(--dim); border-radius:2px; }

  .msg { display:flex; gap:8px; }
  .msg.user { margin-top:12px; }
  .msg-glyph { flex-shrink:0; width:10px; text-align:right; font-size:10px; padding-top:1px; }
  .msg.user   .msg-glyph { color:var(--user); }
  .msg.claude .msg-glyph { color:var(--claude); }
  .msg.codex  .msg-glyph { color:var(--codex); }
  .msg-body { flex:1; min-width:0; word-wrap:break-word; overflow-wrap:break-word; }
  .msg.user   .msg-body { color:var(--user); white-space:pre-wrap; }
  .msg.claude .msg-body, .msg.codex .msg-body { color:var(--text); }
  .msg-label { font-size:9px; font-weight:600; margin-bottom:2px;
    text-transform:uppercase; letter-spacing:.06em; }
  .msg.claude .msg-label { color:var(--claude); }
  .msg.codex  .msg-label { color:var(--codex); }
  .msg.agent-chat { margin:3px 0; padding:3px 7px;
    background:#1c1c1c; border-left:2px solid var(--dim); border-radius:0 2px 2px 0; }
  .msg.agent-chat .msg-body { color:#888; font-size:11px; }
  .msg.agent-chat.from-claude { border-left-color:var(--claude); }
  .msg.agent-chat.from-codex  { border-left-color:var(--codex); }
  .msg-meta { font-size:9px; color:var(--dim2); margin-top:2px; }
  .msg-body code { color:#ce9178; font-size:11px; }
  .msg-body pre { background:#0a0a0a; border-left:2px solid var(--dim);
    padding:5px 8px; margin:4px 0; font-size:11px; overflow-x:auto; white-space:pre; }
  .msg-body pre code { color:var(--text); }
  .msg-body strong { color:#ddd; font-weight:600; }
  .msg-body em { color:#aaa; }
  .msg-body h3 { font-size:12px; font-weight:600; color:#ddd; margin:6px 0 2px; }
  .msg-body ul { margin-left:16px; }
  .msg-body li { margin:1px 0; }

  /* Work */
  .work-scroll { flex:1; overflow-y:auto; padding:6px 10px 8px; font-size:11px; }
  .work-scroll::-webkit-scrollbar { width:3px; }
  .work-scroll::-webkit-scrollbar-thumb { background:var(--dim); border-radius:2px; }
  .work-empty { color:var(--dim2); font-size:10px; padding:12px 0; text-align:center; }
  .we { margin-bottom:8px; border-left:2px solid var(--dim); padding-left:8px; }
  .we.we-claude { border-left-color:var(--claude); }
  .we.we-codex  { border-left-color:var(--codex); }
  .we-header { display:flex; align-items:baseline; gap:6px; margin-bottom:2px; }
  .we-agent { font-size:8px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; }
  .we-claude .we-agent { color:var(--claude); }
  .we-codex  .we-agent { color:var(--codex); }
  .we-type { font-size:9px; color:var(--dim2); }
  .we-time { font-size:8px; color:var(--dim2); margin-left:auto; }
  .we-cmd { color:var(--cmd); font-size:11px; white-space:pre-wrap; word-break:break-all; }
  .we-cmd::before { content:'$ '; color:var(--dim2); }
  .we-code { background:#080808; border:1px solid var(--dim);
    padding:4px 8px; margin-top:3px; font-size:10px;
    overflow-x:auto; white-space:pre; color:var(--text);
    max-height:200px; overflow-y:auto; line-height:1.3; }
  .we-output { color:var(--out); font-size:10px; white-space:pre-wrap;
    word-break:break-all; margin-top:2px; max-height:120px; overflow-y:auto; }

  /* Live indicator */
  .live-dot {
    display:inline-block; width:6px; height:6px; border-radius:50%;
    background:var(--red); margin-right:4px;
    animation: blink 1.2s infinite;
  }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }

  /* Mobile */
  @media (max-width:768px) {
    .tab-bar { display:flex; }
    .divider { display:none !important; }
    .split { position:relative; }
    .panel { position:absolute; top:0; left:0; right:0; bottom:0; }
    .panel.hidden { display:none; }
    .panel-label { display:none; }
    body { font-size:13px; }
    .chat-scroll { padding:6px 8px 3px; }
  }
</style>
</head>
<body>
<div class="app">

  <div class="header">
    <div class="header-left">
      <div class="logo"><b>agents</b> · claudecode</div>
      <div class="badge"><span class="live-dot"></span>live · read-only</div>
      <div class="agent-indicators">
        <div class="agent-ind claude online" id="indClaude"><span class="dot"></span>claude</div>
        <div class="agent-ind codex online"  id="indCodex"><span class="dot"></span>codex</div>
      </div>
    </div>
    <div class="viewer-count">
      <span class="vdot"></span>
      <span id="viewerCount">1 viewer</span>
    </div>
  </div>

  <div class="ro-banner">👁 observer mode — read only — you cannot interact with the agents</div>

  <div class="tab-bar">
    <button class="tab-chat active" onclick="showTab('chat')">◆ chat</button>
    <button class="tab-work" onclick="showTab('work')">⚙ work</button>
  </div>

  <div class="split" id="split">
    <div class="panel panel-chat" id="panelChat">
      <div class="panel-label">◆ conversation</div>
      <div class="chat-scroll" id="chatScroll"></div>
    </div>
    <div class="divider" id="divider"></div>
    <div class="panel panel-work hidden" id="panelWork">
      <div class="panel-label">⚙ work · commands &amp; code</div>
      <div class="work-scroll" id="workScroll">
        <div class="work-empty">no work events yet</div>
      </div>
    </div>
  </div>

</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const chatScroll = document.getElementById('chatScroll');
const workScroll = document.getElementById('workScroll');
let chatAutoScroll = true, workAutoScroll = true;

function trackScroll(el, set) {
  el.addEventListener('scroll', () => {
    set(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  });
}
trackScroll(chatScroll, v => chatAutoScroll = v);
trackScroll(workScroll, v => workAutoScroll = v);
function scrollChat() { if (chatAutoScroll) chatScroll.scrollTop = chatScroll.scrollHeight; }
function scrollWork() { if (workAutoScroll) workScroll.scrollTop = workScroll.scrollHeight; }

// Mobile tabs
function showTab(tab) {
  const pc = document.getElementById('panelChat');
  const pw = document.getElementById('panelWork');
  document.querySelector('.tab-chat').classList.toggle('active', tab==='chat');
  document.querySelector('.tab-work').classList.toggle('active', tab==='work');
  if (tab==='chat') { pc.classList.remove('hidden'); pw.classList.add('hidden'); }
  else              { pw.classList.remove('hidden'); pc.classList.add('hidden'); }
}

// Draggable divider
(function(){
  const divider = document.getElementById('divider');
  const container = document.getElementById('split');
  const cp = document.getElementById('panelChat');
  const wp = document.getElementById('panelWork');
  let dragging=false, startX=0, startW=0;
  if (window.innerWidth > 768) {
    wp.classList.remove('hidden');
    cp.style.flex = wp.style.flex = 'none';
    const half = Math.floor((container.clientWidth-4)/2);
    cp.style.width = half+'px'; wp.style.width = (container.clientWidth-4-half)+'px';
  }
  divider.addEventListener('mousedown', e => { dragging=true; startX=e.clientX; startW=cp.offsetWidth; document.body.style.cursor='col-resize'; document.body.style.userSelect='none'; });
  document.addEventListener('mousemove', e => { if(!dragging)return; const dx=e.clientX-startX; const t=container.clientWidth-4; const n=Math.max(240,Math.min(t-240,startW+dx)); cp.style.width=n+'px'; wp.style.width=(t-n)+'px'; });
  document.addEventListener('mouseup', ()=>{ if(!dragging)return; dragging=false; document.body.style.cursor=''; document.body.style.userSelect=''; });
})();

function escapeHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function formatMsg(text){
  text=text.replace(/```(\w*)\n([\s\S]*?)```/g,(_,l,c)=>'<pre><code>'+escapeHtml(c.trim())+'</code></pre>');
  text=text.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  text=text.replace(/^#{1,3} (.+)$/gm,(_,t)=>'<h3>'+t+'</h3>');
  text=text.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  text=text.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g,'<em>$1</em>');
  text=text.replace(/^[-*] (.+)$/gm,'<li>$1</li>');
  text=text.replace(/(<li>.*<\/li>)+/gs,m=>'<ul>'+m+'</ul>');
  text=text.replace(/\n(?![^<]*<\/pre>)/g,'<br>');
  return text;
}
function formatTime(iso){if(!iso)return'';try{return new Date(iso+'Z').toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}catch{return'';}}

function addChatMsg(m, isHistory=false) {
  const role=m.role, agent=m.agent||'claude';
  const cssClass = role==='user'?'user':agent;
  const div=document.createElement('div');
  div.className='msg '+cssClass+(isHistory?' no-anim':'');
  const glyph={user:'❯',claude:'◆',codex:'⬡'}[cssClass]||'◆';
  const content=role==='user'?escapeHtml(m.content):formatMsg(m.content);
  const label=cssClass!=='user'?'<div class="msg-label">'+agent+'</div>':'';
  const parts=[]; if(m.elapsed_ms!=null)parts.push((m.elapsed_ms/1000).toFixed(1)+'s'); if(m.created_at)parts.push(formatTime(m.created_at));
  const meta=parts.length?'<div class="msg-meta">'+parts.join(' · ')+'</div>':'';
  div.innerHTML='<div class="msg-glyph">'+glyph+'</div><div class="msg-body">'+label+content+meta+'</div>';
  chatScroll.appendChild(div); scrollChat();
}

function addWorkEvent(w, isHistory=false) {
  const agent=w.agent||'claude', evtType=w.event_type;
  let bodyHtml='', typeLabel=evtType;
  if(evtType==='tool_start'){
    typeLabel=w.name||'tool';
    if(w.command)bodyHtml='<div class="we-cmd">'+escapeHtml(w.command)+'</div>';
  } else if(evtType==='tool_result'){
    typeLabel='output'; const out=(w.output||'').trim();
    if(out){const isCode=out.includes('\n')&&out.length>60;
      if(isCode)bodyHtml='<div class="we-code">'+escapeHtml(out.slice(0,2000))+'</div>';
      else bodyHtml='<div class="we-output">'+escapeHtml(out.slice(0,500))+'</div>';
    }
  }
  if(!bodyHtml&&evtType!=='tool_start')return;
  const div=document.createElement('div');
  div.className='we we-'+agent+(isHistory?' no-anim':'');
  const t=formatTime(w.created_at)||new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  div.innerHTML='<div class="we-header"><span class="we-agent">'+agent+'</span><span class="we-type">'+escapeHtml(typeLabel)+'</span><span class="we-time">'+t+'</span></div>'+bodyHtml;
  // Remove empty placeholder
  const emp=workScroll.querySelector('.work-empty');
  if(emp)emp.remove();
  workScroll.appendChild(div); scrollWork();
}

// Load history
async function loadHistory(){
  const r=await fetch('/history?token='+encodeURIComponent(TOKEN));
  if(!r.ok){document.body.innerHTML='<div style="color:#f48771;padding:20px;font-family:monospace">Access denied — invalid token</div>';return;}
  const d=await r.json();
  for(const m of d.chat_messages) addChatMsg(m,true);
  for(const w of d.work_events)   addWorkEvent(w,true);
}

// SSE stream
let lastChatId=0, lastWorkId=0;
function startStream(){
  const es=new EventSource('/stream?token='+encodeURIComponent(TOKEN));
  es.addEventListener('chat',e=>{
    const m=JSON.parse(e.data);
    if(m.id>lastChatId){lastChatId=m.id;addChatMsg(m);}
  });
  es.addEventListener('work',e=>{
    const w=JSON.parse(e.data);
    if(w.id>lastWorkId){lastWorkId=w.id;addWorkEvent(w);}
  });
  es.addEventListener('viewers',e=>{
    const d=JSON.parse(e.data);
    const el=document.getElementById('viewerCount');
    if(el)el.textContent=d.count+' viewer'+(d.count===1?'':'s');
  });
  es.onerror=()=>{setTimeout(startStream,5000);};
}

// Viewer count from agent status
async function checkAgents(){
  try{
    // We can't reach the agent socket, just show indicators as static
  }catch{}
}

loadHistory().then(()=>{
  // After history loads, note the last IDs
  setTimeout(()=>{
    const msgs=chatScroll.querySelectorAll('.msg');
    const wevts=workScroll.querySelectorAll('.we');
    // IDs tracked server-side via SSE
    startStream();
  },100);
});
setInterval(()=>{
  const el=document.getElementById('viewerCount');
  // Updated via SSE viewers event
},10000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    t = request.args.get("token", "")
    if t != TOKEN:
        # Return a minimal 403 page, no info leakage
        return Response(
            '<!DOCTYPE html><html><body style="background:#0f0f0f;color:#f48771;'
            'font-family:monospace;padding:20px">403 — Access Denied</body></html>',
            status=403, mimetype="text/html"
        )
    mark_viewer_active()
    resp = Response(OBSERVER_HTML, mimetype="text/html")
    return resp


@app.route("/history")
def history():
    check_token()
    return jsonify({
        "chat_messages": get_chat(200),
        "work_events":   get_work(500),
    })


@app.route("/stream")
def stream():
    check_token()
    mark_viewer_active()

    def generate():
        last_chat = 0
        last_work = 0
        # Get current max IDs first
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            r = conn.execute("SELECT MAX(id) FROM messages").fetchone()
            last_chat = r[0] or 0
            try:
                r = conn.execute("SELECT MAX(id) FROM work_events").fetchone()
                last_work = r[0] or 0
            except:
                pass
        finally:
            conn.close()

        tick = 0
        while True:
            mark_viewer_active()
            try:
                chat, work = get_new_events(last_chat, last_work)
                for m in chat:
                    last_chat = max(last_chat, m["id"])
                    yield f"event: chat\ndata: {json.dumps(m)}\n\n"
                for w in work:
                    last_work = max(last_work, w["id"])
                    yield f"event: work\ndata: {json.dumps(w)}\n\n"
                # Every 5 ticks (~10s) send viewer count
                if tick % 5 == 0:
                    vc = viewer_count()
                    yield f"event: viewers\ndata: {json.dumps({'count': vc})}\n\n"
                tick += 1
            except Exception as e:
                logger.error(f"Stream error: {e}")
            time.sleep(2)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no",
                             "Cache-Control": "no-cache"})


@app.route("/viewer-count")
def viewer_count_api():
    check_token()
    return jsonify({"count": viewer_count()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)
