"""
Agent Message Bus — shared SQLite-based communication channel
between Claude and Codex daemons. All messages visible to user in web UI.
"""

import sqlite3
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "agent_bus.db")

def init_bus():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bus_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            msg_type TEXT NOT NULL DEFAULT 'message',
            content TEXT NOT NULL,
            status TEXT DEFAULT 'sent',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_claims (
            task_id TEXT NOT NULL,
            subtask TEXT NOT NULL,
            agent TEXT NOT NULL,
            status TEXT DEFAULT 'claimed',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, subtask)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bus_task ON bus_messages(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bus_time ON bus_messages(created_at)")
    conn.commit()
    conn.close()

def new_task_id():
    return f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"

def send_bus_message(task_id, sender, recipient, content, msg_type="message"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO bus_messages (task_id, sender, recipient, msg_type, content) VALUES (?, ?, ?, ?, ?)",
        (task_id, sender, recipient, msg_type, content)
    )
    conn.commit()
    conn.close()

def claim_subtask(task_id, subtask, agent):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO task_claims (task_id, subtask, agent) VALUES (?, ?, ?)",
            (task_id, subtask, agent)
        )
        conn.commit()
        return True
    except:
        return False
    finally:
        conn.close()

def get_subtask_owner(task_id, subtask):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT agent FROM task_claims WHERE task_id=? AND subtask=?",
        (task_id, subtask)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def get_task_messages(task_id, since_id=0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM bus_messages WHERE task_id=? AND id>? ORDER BY id ASC",
        (task_id, since_id)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_messages(limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM bus_messages ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

init_bus()
