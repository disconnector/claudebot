"""
Multi-Agent Web Chat — two-column layout
Left: Chat (conversation between user, Claude, Codex)
Right: Work (commands, code, bash output — learn by watching)
Draggable split on desktop, tabs on mobile.
"""

import os
import sys
import json
import socket
import sqlite3
import threading
import logging
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, request, Response, jsonify
from queue import Queue

_HERE = Path(__file__).parent.resolve()

sys.path.insert(0, str(_HERE))
from orchestrator import route_message, parse_target, CLAUDE_SENTINEL
sys.path.insert(0, str(_HERE.parent / "ai-usage"))
import tracker as _tracker

DB_PATH = str(_HERE / "chat_history.db")

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --------------- SQLite helpers ---------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            agent TEXT DEFAULT NULL,
            content TEXT NOT NULL,
            tool_calls TEXT,
            elapsed_ms REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            event_type TEXT NOT NULL,
            name TEXT,
            command TEXT,
            output TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN agent TEXT DEFAULT NULL")
    except:
        pass
    conn.commit()
    conn.close()

def save_message(role, content, agent=None, tool_calls=None, elapsed_ms=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (role, agent, content, tool_calls, elapsed_ms) VALUES (?, ?, ?, ?, ?)",
        (role, agent, content, json.dumps(tool_calls) if tool_calls else None, elapsed_ms)
    )
    conn.commit()
    conn.close()

def save_work_event(agent, event_type, name=None, command=None, output=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO work_events (agent, event_type, name, command, output) VALUES (?, ?, ?, ?, ?)",
        (agent, event_type, name, command, output)
    )
    conn.commit()
    conn.close()

def get_history(limit=200):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_work_history(limit=500):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM work_events ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM work_events")
    conn.commit()
    conn.close()

init_db()

# --------------- HTML ---------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>agents · claudecode</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:         #0f0f0f;
    --bg2:        #141414;
    --bg3:        #1a1a1a;
    --text:       #c8c8c8;
    --dim:        #2e2e2e;
    --dim2:       #5a5a5a;
    --border:     #222;
    --user:       #e8bf8a;
    --claude:     #4ec9b0;
    --codex:      #e09556;
    --work-bg:    #0c0c0c;
    --cmd-color:  #9cdcfe;
    --out-color:  #6a9955;
    --red:        #f48771;
    --string:     #ce9178;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body {
    height: 100%; overflow: hidden;
    font-family: 'JetBrains Mono','SF Mono',Menlo,Consolas,monospace;
    font-size: 12px; line-height: 1.4;
    background: var(--bg); color: var(--text);
  }

  /* ═══════════════════════════════
     LAYOUT: header + split body
  ═══════════════════════════════ */
  .app {
    display: flex; flex-direction: column;
    height: 100dvh; overflow: hidden;
  }

  /* ── Header ── */
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 5px 12px; border-bottom: 1px solid var(--border);
    background: var(--bg2); flex-shrink: 0; min-height: 30px;
  }
  .header-left { display: flex; align-items: center; gap: 14px; }
  .logo { font-size: 11px; font-weight: 400; color: var(--dim2); }
  .logo b { color: var(--text); font-weight: 600; }
  .agent-indicators { display: flex; gap: 10px; }
  .agent-ind {
    font-size: 9px; display: flex; align-items: center; gap: 3px;
    opacity: 0.4; transition: opacity .2s;
  }
  .agent-ind.online { opacity: 1; }
  .agent-ind .dot { width: 5px; height: 5px; border-radius: 50%; }
  .agent-ind.claude .dot { background: var(--claude); }
  .agent-ind.codex  .dot { background: var(--codex); }
  .agent-ind.claude { color: var(--claude); }
  .agent-ind.codex  { color: var(--codex); }
  .header-right { display: flex; align-items: center; gap: 12px; }
  .hdr-btn {
    background: none; border: none; color: var(--dim2);
    font-family: inherit; font-size: 10px; cursor: pointer; padding: 2px 4px;
    transition: color .15s;
  }
  .hdr-btn:hover { color: var(--text); }
  .hdr-btn.danger:hover { color: var(--red); }

  /* ── Mobile tabs (hidden on desktop) ── */
  .tab-bar {
    display: none;
    border-bottom: 1px solid var(--border);
    background: var(--bg2); flex-shrink: 0;
  }
  .tab-bar button {
    flex: 1; background: none; border: none;
    color: var(--dim2); font-family: inherit; font-size: 11px;
    padding: 8px 0; cursor: pointer; border-bottom: 2px solid transparent;
    transition: all .15s;
  }
  .tab-bar button.active {
    color: var(--text); border-bottom-color: var(--claude);
  }
  .tab-bar button.tab-work.active { border-bottom-color: var(--codex); }

  /* ── Split container ── */
  .split-container {
    flex: 1; display: flex; overflow: hidden; min-height: 0;
  }

  /* ── Panels ── */
  .panel {
    display: flex; flex-direction: column; overflow: hidden; min-width: 0;
  }
  .panel-chat { flex: 1; border-right: 1px solid var(--border); }
  .panel-work { flex: 1; background: var(--work-bg); }
  .panel-label {
    font-size: 9px; color: var(--dim2); text-transform: uppercase;
    letter-spacing: .08em; padding: 4px 10px 3px;
    border-bottom: 1px solid var(--border); background: var(--bg2);
    flex-shrink: 0;
  }

  /* ── Drag divider ── */
  .divider {
    width: 4px; background: var(--border); cursor: col-resize;
    flex-shrink: 0; transition: background .15s; position: relative;
  }
  .divider:hover, .divider.dragging { background: var(--dim); }
  .divider::after {
    content: ''; position: absolute; top: 50%; left: 50%;
    transform: translate(-50%,-50%);
    width: 2px; height: 24px;
    background: var(--dim2); border-radius: 2px; opacity: 0;
    transition: opacity .2s;
  }
  .divider:hover::after { opacity: 1; }

  /* ═══════════════════════════════
     CHAT PANEL
  ═══════════════════════════════ */
  .chat-scroll {
    flex: 1; overflow-y: auto; padding: 8px 10px 4px;
  }
  .chat-scroll::-webkit-scrollbar { width: 3px; }
  .chat-scroll::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 2px; }

  .msg { display: flex; gap: 8px; animation: fadein .1s ease; }
  .msg.no-anim { animation: none; }
  .msg.user { margin-top: 12px; }
  .msg.user:first-child { margin-top: 0; }
  @keyframes fadein { from { opacity:0; transform:translateY(2px); } to { opacity:1; transform:none; } }

  .msg-glyph {
    flex-shrink: 0; width: 10px; text-align: right;
    font-size: 10px; padding-top: 1px; user-select: none;
  }
  .msg.user   .msg-glyph { color: var(--user); }
  .msg.claude .msg-glyph { color: var(--claude); }
  .msg.codex  .msg-glyph { color: var(--codex); }

  .msg-body { flex: 1; min-width: 0; word-wrap: break-word; overflow-wrap: break-word; }
  .msg.user   .msg-body { color: var(--user); white-space: pre-wrap; }
  .msg.claude .msg-body,
  .msg.codex  .msg-body { color: var(--text); }

  .msg-label {
    font-size: 9px; font-weight: 600; margin-bottom: 2px;
    text-transform: uppercase; letter-spacing: .06em;
  }
  .msg.claude .msg-label { color: var(--claude); }
  .msg.codex  .msg-label { color: var(--codex); }

  /* Agent-to-agent chat in chat pane */
  .msg.agent-chat {
    margin: 3px 0; padding: 3px 7px;
    background: #1c1c1c; border-left: 2px solid var(--dim);
    border-radius: 0 2px 2px 0;
  }
  .msg.agent-chat .msg-label { font-size: 8px; letter-spacing: .1em; }
  .msg.agent-chat .msg-body { color: #888; font-size: 11px; }
  .msg.agent-chat.from-claude { border-left-color: var(--claude); }
  .msg.agent-chat.from-codex  { border-left-color: var(--codex); }

  .msg-meta { font-size: 9px; color: var(--dim2); margin-top: 2px; }

  /* Typing */
  .typing-bar {
    padding: 2px 10px 2px 28px; font-size: 11px;
    min-height: 18px; flex-shrink: 0;
  }
  .typing-bar span { letter-spacing: .1em; }
  .typing-bar .t-claude { color: var(--claude); }
  .typing-bar .t-codex  { color: var(--codex); }

  /* Input area */
  .input-area {
    padding: 5px 10px 8px; border-top: 1px solid var(--border);
    background: var(--bg2); flex-shrink: 0;
  }
  .input-hint {
    font-size: 9px; color: var(--dim2); margin-bottom: 3px;
    display: flex; gap: 10px; flex-wrap: wrap;
  }
  .input-hint .hint-target { color: var(--text); font-weight: 600; }
  .input-row { display: flex; align-items: flex-end; gap: 6px; }
  .input-glyph {
    font-size: 10px; padding-bottom: 4px; flex-shrink: 0;
    user-select: none; transition: color .15s;
  }
  .input-glyph.t-claude { color: var(--claude); }
  .input-glyph.t-codex  { color: var(--codex); }
  .input-glyph.t-both   { color: var(--user); }
  textarea {
    flex: 1; background: transparent; border: none;
    border-bottom: 1px solid var(--dim);
    padding: 2px 0 4px; color: var(--text);
    font-family: inherit; font-size: 12px; line-height: 1.4;
    resize: none; min-height: 22px; max-height: 130px;
    outline: none; caret-color: var(--user);
  }
  textarea:focus { border-bottom-color: var(--user); }
  textarea::placeholder { color: var(--dim2); }
  .send-btn {
    background: none; border: none; color: var(--dim2);
    font-family: inherit; font-size: 14px; cursor: pointer;
    padding: 0 2px 3px; flex-shrink: 0; transition: color .1s;
  }
  .send-btn:hover:not(:disabled) { color: var(--user); }
  .send-btn:disabled { opacity: .2; cursor: not-allowed; }

  /* Markdown */
  .msg-body code { color: var(--string); font-size: 11px; }
  .msg-body pre {
    background: #0a0a0a; border-left: 2px solid var(--dim);
    padding: 5px 8px; margin: 4px 0; font-size: 11px;
    overflow-x: auto; white-space: pre; line-height: 1.3;
  }
  .msg-body pre code { color: var(--text); }
  .msg-body strong { color: #ddd; font-weight: 600; }
  .msg-body em { color: #aaa; }
  .msg-body h1,.msg-body h2,.msg-body h3 {
    font-size: 12px; font-weight: 600; color: #ddd; margin: 6px 0 2px;
  }
  .msg-body ul,.msg-body ol { margin-left: 16px; }
  .msg-body li { margin: 1px 0; }
  .msg-body table { border-collapse: collapse; font-size: 11px; margin: 4px 0; }
  .msg-body th,.msg-body td { border: 1px solid var(--dim); padding: 2px 8px; }
  .msg-body th { background: var(--bg2); color: var(--dim2); }
  .msg-body hr { border: none; border-top: 1px solid var(--dim); margin: 6px 0; }
  .msg-body blockquote {
    border-left: 2px solid var(--dim); padding-left: 8px;
    color: var(--dim2); margin: 4px 0;
  }

  /* ═══════════════════════════════
     WORK PANEL
  ═══════════════════════════════ */
  .work-scroll {
    flex: 1; overflow-y: auto; padding: 6px 10px 8px;
    font-size: 11px; line-height: 1.35;
  }
  .work-scroll::-webkit-scrollbar { width: 3px; }
  .work-scroll::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 2px; }

  .work-empty {
    color: var(--dim2); font-size: 10px; padding: 12px 0;
    text-align: center;
  }

  /* Work event block */
  .we {
    margin-bottom: 8px; animation: fadein .1s ease;
    border-left: 2px solid var(--dim); padding-left: 8px;
  }
  .we.no-anim { animation: none; }
  .we.we-claude { border-left-color: var(--claude); }
  .we.we-codex  { border-left-color: var(--codex); }
  .we.we-agent-msg { border-left-color: var(--dim2); }

  .we-header {
    display: flex; align-items: baseline; gap: 6px;
    margin-bottom: 2px;
  }
  .we-agent {
    font-size: 8px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .06em;
  }
  .we-claude .we-agent { color: var(--claude); }
  .we-codex  .we-agent { color: var(--codex); }
  .we-agent-msg .we-agent { color: var(--dim2); }

  .we-type {
    font-size: 9px; color: var(--dim2); letter-spacing: .04em;
  }
  .we-time { font-size: 8px; color: var(--dim2); margin-left: auto; }

  .we-cmd {
    color: var(--cmd-color); font-size: 11px; white-space: pre-wrap;
    word-break: break-all;
  }
  .we-cmd::before { content: '$ '; color: var(--dim2); }

  .we-code {
    background: #080808; border: 1px solid var(--dim);
    padding: 4px 8px; margin-top: 3px; font-size: 10px;
    overflow-x: auto; white-space: pre; color: var(--text);
    max-height: 200px; overflow-y: auto; line-height: 1.3;
  }
  .we-output {
    color: var(--out-color); font-size: 10px; white-space: pre-wrap;
    word-break: break-all; margin-top: 2px; max-height: 120px;
    overflow-y: auto;
  }
  .we-msg { color: #888; font-size: 11px; font-style: italic; }

  /* ═══════════════════════════════
     MOBILE
  ═══════════════════════════════ */
  @media (max-width: 768px) {
    .tab-bar { display: flex; }
    .divider { display: none !important; }
    .split-container { position: relative; }
    .panel { position: absolute; top: 0; left: 0; right: 0; bottom: 0; }
    .panel.hidden { display: none; }
    .panel-label { display: none; }
    body, textarea { font-size: 13px; }
    .chat-scroll { padding: 6px 8px 3px; }
    .input-area { padding: 4px 8px 8px; }
  }
</style>
</head>
<body>
<div class="app">

  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <div class="logo"><b>agents</b> · claudecode</div>
      <div class="agent-indicators">
        <div class="agent-ind claude online" id="indClaude"><span class="dot"></span>claude</div>
        <div class="agent-ind codex online" id="indCodex"><span class="dot"></span>codex</div>
      </div>
    </div>
    <div class="header-right">
      <a href="/usage" class="hdr-btn">usage</a>
      <button class="hdr-btn danger" onclick="clearAll()">clear</button>
    </div>
  </div>

  <!-- Mobile tab bar -->
  <div class="tab-bar">
    <button class="tab-chat active" onclick="showTab('chat')">◆ chat</button>
    <button class="tab-work" onclick="showTab('work')">⚙ work</button>
  </div>

  <!-- Split container -->
  <div class="split-container" id="splitContainer">

    <!-- LEFT: Chat -->
    <div class="panel panel-chat" id="panelChat">
      <div class="panel-label">◆ conversation</div>
      <div class="chat-scroll" id="chatScroll"></div>
      <div class="typing-bar" id="typingBar"></div>
      <div class="input-area">
        <div class="input-hint">
          <span>↵ send · shift+↵ newline</span>
          <span id="hintTarget" class="hint-target">→ claude</span>
        </div>
        <div class="input-row">
          <span class="input-glyph t-claude" id="inputGlyph">❯</span>
          <textarea id="input" placeholder="message…" rows="1" autofocus></textarea>
          <button class="send-btn" id="sendBtn">↵</button>
        </div>
      </div>
    </div>

    <!-- Drag divider -->
    <div class="divider" id="divider"></div>

    <!-- RIGHT: Work -->
    <div class="panel panel-work hidden" id="panelWork">
      <div class="panel-label">⚙ work · commands &amp; code</div>
      <div class="work-scroll" id="workScroll">
        <div class="work-empty" id="workEmpty">no work events yet — send a task to watch the agents work</div>
      </div>
    </div>

  </div>
</div>

<script>
const SENTINEL = 'Mxyzptlk';
const chatScroll = document.getElementById('chatScroll');
const workScroll = document.getElementById('workScroll');
const workEmpty  = document.getElementById('workEmpty');
const typingBar  = document.getElementById('typingBar');
const input      = document.getElementById('input');
const sendBtn    = document.getElementById('sendBtn');
const inputGlyph = document.getElementById('inputGlyph');
const hintTarget = document.getElementById('hintTarget');

let busy = false;
let chatAutoScroll = true;
let workAutoScroll = true;
let hasWorkEvents  = false;

// ── Auto-scroll tracking ──
function trackScroll(el, flagSetter) {
  el.addEventListener('scroll', () => {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    flagSetter(atBottom);
  });
}
trackScroll(chatScroll, v => chatAutoScroll = v);
trackScroll(workScroll, v => workAutoScroll = v);

function scrollChat() { if (chatAutoScroll) chatScroll.scrollTop = chatScroll.scrollHeight; }
function scrollWork() { if (workAutoScroll) workScroll.scrollTop = workScroll.scrollHeight; }

// ── Mobile tabs ──
let activeTab = 'chat';
function showTab(tab) {
  activeTab = tab;
  const pc = document.getElementById('panelChat');
  const pw = document.getElementById('panelWork');
  document.querySelector('.tab-chat').classList.toggle('active', tab === 'chat');
  document.querySelector('.tab-work').classList.toggle('active', tab === 'work');
  if (tab === 'chat') { pc.classList.remove('hidden'); pw.classList.add('hidden'); }
  else                { pw.classList.remove('hidden'); pc.classList.add('hidden'); }
}

// ── Draggable divider ──
(function() {
  const divider   = document.getElementById('divider');
  const container = document.getElementById('splitContainer');
  const chatPanel = document.getElementById('panelChat');
  const workPanel = document.getElementById('panelWork');
  let dragging = false, startX = 0, startChatW = 0;

  // Show work panel on desktop
  if (window.innerWidth > 768) {
    workPanel.classList.remove('hidden');
    // Default 50/50
    chatPanel.style.flex = 'none';
    workPanel.style.flex = 'none';
    const half = Math.floor((container.clientWidth - 4) / 2);
    chatPanel.style.width = half + 'px';
    workPanel.style.width = (container.clientWidth - 4 - half) + 'px';
  }

  divider.addEventListener('mousedown', e => {
    dragging = true;
    startX = e.clientX;
    startChatW = chatPanel.offsetWidth;
    divider.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const totalW = container.clientWidth - 4;
    const newChat = Math.max(280, Math.min(totalW - 280, startChatW + dx));
    chatPanel.style.width = newChat + 'px';
    workPanel.style.width = (totalW - newChat) + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });

  window.addEventListener('resize', () => {
    if (window.innerWidth <= 768) return;
    workPanel.classList.remove('hidden');
    const totalW = container.clientWidth - 4;
    const chatW = chatPanel.offsetWidth;
    chatPanel.style.width = Math.min(chatW, totalW - 280) + 'px';
    workPanel.style.width = (totalW - parseFloat(chatPanel.style.width)) + 'px';
  });
})();

// ── Target detection ──
function detectTarget(text) {
  const t = text.trimStart().toLowerCase();
  if (t.startsWith('@both') || t.startsWith('@all')) return 'both';
  if (t.startsWith('@codex')) return 'codex';
  if (t.startsWith('@claude')) return 'claude';
  return 'claude';
}

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  const t = detectTarget(input.value);
  inputGlyph.className = 'input-glyph t-' + t;
  const labels = { claude: '→ claude', codex: '→ codex', both: '→ both' };
  hintTarget.textContent = labels[t] || '→ claude';
  hintTarget.style.color = t === 'codex' ? 'var(--codex)' : t === 'both' ? 'var(--user)' : 'var(--claude)';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
sendBtn.addEventListener('click', send);

// ── Helpers ──
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function formatMsg(text) {
  // Code blocks first
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_,lang,code) =>
    '<pre><code>' + escapeHtml(code.trim()) + '</code></pre>');
  text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // Headers
  text = text.replace(/^#{1,3} (.+)$/gm, (_,t) => '<h3>' + t + '</h3>');
  // Bold / italic
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  // Tables
  text = text.replace(/\|(.+)\|\n\|[-| :]+\|\n((?:\|.+\|\n?)*)/g, (match) => {
    const rows = match.trim().split('\n').filter(r => !r.match(/^[\|\s-:]+$/));
    if (rows.length < 2) return match;
    const hdr = rows[0].split('|').filter(c => c.trim()).map(c => '<th>' + c.trim() + '</th>').join('');
    const body = rows.slice(1).map(r => '<tr>' + r.split('|').filter(c=>c.trim()).map(c=>'<td>'+c.trim()+'</td>').join('') + '</tr>').join('');
    return '<table><thead><tr>' + hdr + '</tr></thead><tbody>' + body + '</tbody></table>';
  });
  // Lists
  text = text.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  text = text.replace(/(<li>.*<\/li>)+/gs, m => '<ul>' + m + '</ul>');
  // Horizontal rules
  text = text.replace(/^---+$/gm, '<hr>');
  // Blockquotes
  text = text.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  // Line breaks (but not inside pre)
  text = text.replace(/\n(?![^<]*<\/pre>)/g, '<br>');
  return text;
}
function formatTime(iso) {
  if (!iso) return '';
  try { return new Date(iso + 'Z').toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
  catch { return ''; }
}
function timeNow() {
  return new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}

// ── Chat rendering ──
function addChatMsg(cssClass, content, elapsed, timestamp, isHistory, agent, role) {
  const div = document.createElement('div');
  div.className = 'msg ' + cssClass + (isHistory ? ' no-anim' : '');
  const glyphs = { user:'❯', claude:'◆', codex:'⬡' };
  const glyph = glyphs[cssClass] || '◆';
  const formatted = (role || cssClass) === 'user' ? escapeHtml(content) : formatMsg(content);
  const label = cssClass !== 'user'
    ? '<div class="msg-label">' + (agent || cssClass) + '</div>' : '';
  const parts = [];
  if (elapsed != null) parts.push((elapsed/1000).toFixed(1)+'s');
  if (timestamp) parts.push(formatTime(timestamp));
  const meta = parts.length ? '<div class="msg-meta">' + parts.join(' · ') + '</div>' : '';
  div.innerHTML = '<div class="msg-glyph">'+glyph+'</div><div class="msg-body">'+label+formatted+meta+'</div>';
  chatScroll.appendChild(div);
  scrollChat();
  return div;
}
function addAgentChatMsg(fromAgent, toAgent, content, isHistory) {
  const div = document.createElement('div');
  div.className = 'msg agent-chat from-' + fromAgent + (isHistory ? ' no-anim' : '');
  const glyph = fromAgent === 'codex' ? '⬡' : '◆';
  div.innerHTML = '<div class="msg-glyph">'+glyph+'</div>' +
    '<div class="msg-body">' +
      '<div class="msg-label">'+fromAgent+' → '+toAgent+'</div>' +
      formatMsg(content) +
    '</div>';
  chatScroll.appendChild(div);
  scrollChat();
  return div;
}
function appendToMsg(div, content) {
  const body = div.querySelector('.msg-body');
  const label = body.querySelector('.msg-label');
  const metas = [...body.querySelectorAll('.msg-meta')];
  const cleaned = content.replace(new RegExp(SENTINEL,'g'),'').trimEnd();
  body.innerHTML = (label ? label.outerHTML : '') + formatMsg(cleaned);
  metas.forEach(m => body.appendChild(m));
  scrollChat();
}

// ── Work panel rendering ──
function addWorkEvent(agent, evtType, data, isHistory) {
  if (workEmpty) workEmpty.style.display = 'none';
  hasWorkEvents = true;

  const div = document.createElement('div');
  div.className = 'we we-' + agent + (isHistory ? ' no-anim' : '');

  let typeLabel = evtType;
  let bodyHtml = '';

  if (evtType === 'tool_start') {
    const name = data.name || 'tool';
    const cmd  = data.command || '';
    typeLabel = name;
    if (cmd) bodyHtml = '<div class="we-cmd">' + escapeHtml(cmd) + '</div>';
  } else if (evtType === 'tool_result') {
    typeLabel = 'output';
    const out = (data.output || '').trim();
    if (out) {
      // Detect code-like output
      const isCode = out.includes('\n') && out.length > 60;
      if (isCode) bodyHtml = '<div class="we-code">' + escapeHtml(out.slice(0,2000)) + (out.length>2000?'\n…':'') + '</div>';
      else bodyHtml = '<div class="we-output">' + escapeHtml(out.slice(0,500)) + (out.length>500?'…':'') + '</div>';
    }
    div.className += ' we-result';
  } else if (evtType === 'agent_msg') {
    div.className = 'we we-agent-msg' + (isHistory ? ' no-anim' : '');
    typeLabel = (data.from_agent || agent) + ' → ' + (data.to_agent || '?');
    bodyHtml = '<div class="we-msg">' + escapeHtml(data.content || '') + '</div>';
  } else if (evtType === 'code') {
    typeLabel = 'code';
    bodyHtml = '<div class="we-code">' + escapeHtml(data.content || '') + '</div>';
  }

  if (!bodyHtml) return null; // Skip empty events

  div.innerHTML =
    '<div class="we-header">' +
      '<span class="we-agent">' + agent + '</span>' +
      '<span class="we-type">' + escapeHtml(typeLabel) + '</span>' +
      '<span class="we-time">' + (data.time || timeNow()) + '</span>' +
    '</div>' + bodyHtml;

  workScroll.appendChild(div);
  scrollWork();
  return div;
}

// ── Typing indicator ──
let typingAgents = new Set();
function setTyping(agent, active) {
  if (active) typingAgents.add(agent);
  else typingAgents.delete(agent);
  if (typingAgents.size === 0) {
    typingBar.innerHTML = '';
  } else {
    const parts = [...typingAgents].map(a =>
      `<span class="t-${a}">${a === 'claude' ? '◆' : '⬡'} ${a}</span>`);
    typingBar.innerHTML = parts.join(' &nbsp; ') + ' <span style="color:var(--dim2)">···</span>';
  }
}

// ── Load history ──
async function loadHistory() {
  try {
    const [msgs, work] = await Promise.all([
      fetch('/api/history').then(r => r.json()),
      fetch('/api/work').then(r => r.json()),
    ]);
    for (const m of msgs) {
      const tc = m.tool_calls ? JSON.parse(m.tool_calls) : null;
      const agent = m.agent || 'claude';
      const cssClass = m.role === 'user' ? 'user' : agent;
      addChatMsg(cssClass, m.content, m.elapsed_ms, m.created_at, true, agent, m.role);
    }
    for (const w of work) {
      const data = { name: w.name, command: w.command, output: w.output,
                     time: formatTime(w.created_at) };
      addWorkEvent(w.agent, w.event_type, data, true);
    }
  } catch(e) { console.error('history load failed:', e); }
}

// ── Clear ──
async function clearAll() {
  if (!confirm('Clear all history?')) return;
  await fetch('/api/history', { method: 'DELETE' });
  chatScroll.innerHTML = '';
  workScroll.innerHTML = '<div class="work-empty" id="workEmpty">no work events yet</div>';
  hasWorkEvents = false;
}

// ── Send ──
async function send() {
  const text = input.value.trim();
  if (!text || busy) return;
  const target = detectTarget(text);
  busy = true;
  sendBtn.disabled = true;
  input.value = '';
  input.style.height = 'auto';
  inputGlyph.className = 'input-glyph t-claude';
  hintTarget.textContent = '→ claude';
  hintTarget.style.color = '';

  addChatMsg('user', text, null, null, false, null, 'user');

  const t0 = performance.now();
  let agentDivs  = {};
  let agentTexts = {};

  setTyping(target === 'both' ? 'claude' : target, true);
  if (target === 'both') setTyping('codex', true);

  // Switch to work tab on mobile when a task starts
  if (window.innerWidth <= 768 && target !== 'claude') showTab('work');

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6);
        if (raw === '[DONE]') continue;
        let msg;
        try { msg = JSON.parse(raw); } catch { continue; }

        const agent = msg.agent || 'claude';

        if (msg.type === 'text') {
          setTyping(agent, false);
          if (!agentTexts[agent]) agentTexts[agent] = '';
          agentTexts[agent] += msg.content;
          if (!agentDivs[agent])
            agentDivs[agent] = addChatMsg(agent, '', null, null, false, agent, 'assistant');
          appendToMsg(agentDivs[agent], agentTexts[agent]);

        } else if (msg.type === 'tool_start') {
          // Work panel
          addWorkEvent(agent, 'tool_start', {
            name: msg.name, command: msg.command, time: timeNow()
          }, false);
          // Tiny indicator in chat
          if (!agentDivs[agent]) {
            agentDivs[agent] = addChatMsg(agent, '', null, null, false, agent, 'assistant');
            agentTexts[agent] = agentTexts[agent] || '';
          }

        } else if (msg.type === 'tool_result') {
          // Work panel output
          const out = (msg.output || '').trim();
          if (out) addWorkEvent(agent, 'tool_result', { output: out, time: timeNow() }, false);

        } else if (msg.type === 'agent_chat') {
          // Inter-agent: show in both panes
          addAgentChatMsg(msg.from_agent, msg.to_agent, msg.content, false);
          addWorkEvent(msg.from_agent, 'agent_msg', {
            from_agent: msg.from_agent, to_agent: msg.to_agent,
            content: msg.content, time: timeNow()
          }, false);

        } else if (msg.type === 'processing') {
          setTyping(agent, true);

        } else if (msg.type === 'error') {
          setTyping(agent, false);
          if (!agentTexts[agent]) agentTexts[agent] = '';
          agentTexts[agent] += '\n⚠ ' + msg.content;
          if (!agentDivs[agent])
            agentDivs[agent] = addChatMsg(agent, '', null, null, false, 'system', 'assistant');
          appendToMsg(agentDivs[agent], agentTexts[agent]);

        } else if (msg.type === 'done') {
          setTyping(agent, false);
        }
      }
    }
  } catch(err) {
    setTyping('claude', false); setTyping('codex', false);
    addChatMsg('claude', '⚠ connection error: ' + err.message, null, null, false, 'system', 'assistant');
  }

  const elapsed = performance.now() - t0;
  for (const [agent, div] of Object.entries(agentDivs)) {
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.textContent = (elapsed/1000).toFixed(1) + 's · ' + timeNow();
    div.querySelector('.msg-body').appendChild(meta);
  }
  setTyping('claude', false); setTyping('codex', false);
  busy = false;
  sendBtn.disabled = false;
  input.focus();
}

// ── Agent health ──
async function checkAgents() {
  try {
    const d = await fetch('/api/agents').then(r => r.json());
    document.getElementById('indClaude').className = 'agent-ind claude' + (d.claude ? ' online' : '');
    document.getElementById('indCodex').className  = 'agent-ind codex'  + (d.codex  ? ' online' : '');
  } catch {}
}
checkAgents();
setInterval(checkAgents, 15000);

// Load history on start
loadHistory();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    resp = app.make_response(render_template_string(HTML))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route("/api/history", methods=["GET"])
def history_api():
    return jsonify(get_history(request.args.get("limit", 200, type=int)))


@app.route("/api/history", methods=["DELETE"])
def clear_history_api():
    clear_history()
    return jsonify({"ok": True})


@app.route("/api/work", methods=["GET"])
def work_api():
    return jsonify(get_work_history(request.args.get("limit", 500, type=int)))


@app.route("/api/agents", methods=["GET"])
def agents_api():
    status = {}
    for name, path in [("claude", "/run/user/1000/claude-daemon.sock"),
                        ("codex",  "/run/user/1000/codex-daemon.sock")]:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(path)
            s.close()
            status[name] = True
        except:
            status[name] = False
    return jsonify(status)


@app.route("/api/chat", methods=["POST"])
def chat_api():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return Response('data: {"type":"error","content":"Empty message"}\ndata: [DONE]\n\n',
                        content_type="text/event-stream")

    target, _ = parse_target(text)
    save_message("user", text)

    def generate():
        t0 = time.time()
        q  = Queue()
        done_ev = threading.Event()

        def send_event(event):
            q.put(event)

        def save_cb(agent_name, resp_text, tools):
            elapsed_ms = (time.time() - t0) * 1000
            cleaned = resp_text.replace(CLAUDE_SENTINEL, "").rstrip() if agent_name == "claude" else resp_text
            if cleaned:
                save_message("assistant", cleaned, agent=agent_name,
                             tool_calls=tools or None, elapsed_ms=elapsed_ms)
            # Persist work events (tool calls)
            if tools:
                for tc in tools:
                    save_work_event(agent_name, "tool_start",
                                    name=tc.get("name"), command=tc.get("command"))

        def run():
            route_message(text, "web", send_event, save_callback=save_cb)
            done_ev.set()
            q.put(None)

        threading.Thread(target=run, daemon=True).start()

        while True:
            try:
                event = q.get(timeout=620)
            except:
                break
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done" and done_ev.is_set():
                break

        # Drain any remaining
        while not q.empty():
            ev = q.get_nowait()
            if ev:
                yield f"data: {json.dumps(ev)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), content_type="text/event-stream")


@app.route("/api/usage")
def usage_api():
    return jsonify(_tracker.get_summary(request.args.get("days", 30, type=int)))


@app.route("/api/usage/prices", methods=["GET"])
def prices_api():
    import sqlite3 as _sq
    conn = _sq.connect(str(_HERE.parent / "ai-usage" / "usage.db"))
    conn.row_factory = _sq.Row
    rows = conn.execute("SELECT * FROM prices ORDER BY model").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/usage/prices", methods=["POST"])
def set_price_api():
    d = request.get_json()
    _tracker.set_price(d["model"], d["input_per_mtok"], d["output_per_mtok"])
    return jsonify({"ok": True})


USAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ai usage · claudecode</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0f0f0f; --bg2:#141414; --text:#c8c8c8; --dim:#2e2e2e; --dim2:#5a5a5a;
          --border:#222; --claude:#4ec9b0; --codex:#e09556; --user:#e8bf8a; --red:#f48771; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'JetBrains Mono',Menlo,monospace; font-size:12px; line-height:1.4;
         background:var(--bg); color:var(--text); min-height:100vh; padding:16px 20px; }
  h1 { font-size:11px; font-weight:400; color:var(--dim2); margin-bottom:16px; }
  h1 b { color:var(--text); font-weight:600; }
  .nav { margin-bottom:16px; display:flex; align-items:center; gap:14px; }
  .nav a,.nav button { color:var(--dim2); text-decoration:none; font-size:10px;
    background:none; border:none; font-family:inherit; cursor:pointer; }
  .nav a:hover,.nav button:hover { color:var(--text); }
  select { background:var(--bg2); border:1px solid var(--dim); color:var(--text);
    font-family:inherit; font-size:10px; padding:2px 6px; cursor:pointer; }
  .section { margin-bottom:20px; }
  .section-title { font-size:9px; color:var(--dim2); text-transform:uppercase;
    letter-spacing:.08em; margin-bottom:8px; }
  .totals { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .total-box { background:var(--bg2); border:1px solid var(--border); padding:8px 14px; flex:1; min-width:100px; }
  .total-box .lbl { font-size:9px; color:var(--dim2); text-transform:uppercase; margin-bottom:4px; }
  .total-box .val { font-size:16px; font-weight:500; }
  .card { background:var(--bg2); border:1px solid var(--border); padding:10px 14px; margin-bottom:6px; }
  .card-row { display:flex; justify-content:space-between; align-items:baseline; }
  .card-lbl { font-size:11px; }
  .ac { color:var(--claude); } .ax { color:var(--codex); }
  .model { color:var(--dim2); font-size:10px; margin-left:6px; }
  .card-stats { font-size:10px; color:var(--dim2); margin-top:3px; }
  .cost { font-size:13px; font-weight:500; }
  .bar-wrap { height:3px; background:var(--dim); margin-top:5px; }
  .bar { height:3px; }
  .daily-chart { display:flex; align-items:flex-end; gap:2px; height:48px; margin-top:6px; }
  .daily-bar { flex:1; min-width:3px; background:var(--dim); transition:background .1s; cursor:default; }
  .daily-bar:hover { background:var(--claude); }
  .prices-table { width:100%; border-collapse:collapse; font-size:10px; }
  .prices-table th { color:var(--dim2); font-weight:400; text-align:left; padding:3px 8px; border-bottom:1px solid var(--border); }
  .prices-table td { padding:3px 8px; border-bottom:1px solid var(--dim); }
  .prices-table tr:last-child td { border-bottom:none; }
</style>
</head>
<body>
<h1><b>ai usage</b> · claudecode</h1>
<div class="nav">
  <a href="/">← chat</a>
  period: <select id="days" onchange="load(this.value)">
    <option value="7">7 days</option><option value="30" selected>30 days</option>
    <option value="90">90 days</option><option value="365">1 year</option>
  </select>
  <button onclick="load(document.getElementById('days').value)">↺ refresh</button>
</div>
<div class="totals" id="totals"></div>
<div class="section"><div class="section-title">daily spend</div><div class="daily-chart" id="chart"></div></div>
<div class="section"><div class="section-title">by model</div><div id="models"></div></div>
<div class="section"><div class="section-title">model prices ($/M tokens)</div>
  <table class="prices-table"><thead><tr><th>model</th><th>input</th><th>output</th><th>updated</th></tr></thead>
  <tbody id="prices"></tbody></table></div>
<script>
const fmt=(n,d=4)=>n==null?'—':'$'+Number(n).toFixed(d);
const fmtK=n=>!n?'0':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':n;
async function load(days){
  const [u,p]=await Promise.all([fetch('/api/usage?days='+days).then(r=>r.json()),fetch('/api/usage/prices').then(r=>r.json())]);
  const t=u.totals||{};
  document.getElementById('totals').innerHTML=`
    <div class="total-box"><div class="lbl">total spend</div><div class="val" style="color:var(--user)">${fmt(t.total_cost,4)}</div></div>
    <div class="total-box"><div class="lbl">api calls</div><div class="val">${t.calls||0}</div></div>
    <div class="total-box"><div class="lbl">input tokens</div><div class="val">${fmtK(t.input_tokens)}</div></div>
    <div class="total-box"><div class="lbl">output tokens</div><div class="val">${fmtK(t.output_tokens)}</div></div>`;
  const daily=u.daily||[];const mx=Math.max(...daily.map(d=>d.cost||0),.0001);
  document.getElementById('chart').innerHTML=daily.map(d=>{
    const h=Math.max(2,Math.round((d.cost/mx)*100));
    return`<div class="daily-bar" style="height:${h}%" title="${d.day}: ${fmt(d.cost)} · ${fmtK(d.tokens)} tokens"></div>`;
  }).join('')||'<span style="color:var(--dim2);font-size:10px">no data</span>';
  const ms=u.by_model||[];const mx2=Math.max(...ms.map(m=>m.total_cost||0),.0001);
  document.getElementById('models').innerHTML=ms.map(m=>{
    const bp=Math.round((m.total_cost/mx2)*100);
    const ac=m.agent==='claude'?'ac':'ax';
    return`<div class="card"><div class="card-row">
      <div class="card-lbl"><span class="${ac}">${m.agent}</span><span class="model">${m.model}</span></div>
      <div class="cost">${fmt(m.total_cost,4)}</div></div>
      <div class="card-stats">${m.calls} calls · ${fmtK(m.input_tokens)} in · ${fmtK(m.output_tokens)} out</div>
      <div class="bar-wrap"><div class="bar" style="width:${bp}%;background:var(--${m.agent==='claude'?'claude':'codex'})"></div></div></div>`;
  }).join('')||'<div style="color:var(--dim2);font-size:10px">no usage yet</div>';
  document.getElementById('prices').innerHTML=p.map(r=>`
    <tr><td>${r.model}</td><td>${fmt(r.input_per_mtok,2)}</td><td>${fmt(r.output_per_mtok,2)}</td>
    <td style="color:var(--dim2)">${(r.updated_at||'').slice(0,10)}</td></tr>`).join('');
}
load(30);
</script>
</body>
</html>"""


@app.route("/usage")
def usage_page():
    resp = app.make_response(USAGE_HTML)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False, threaded=True)
