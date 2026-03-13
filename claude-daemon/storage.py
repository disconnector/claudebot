"""
storage.py — SQLite persistence for Claude Daemon
Stores conversation messages, session metadata, and backup history.
"""

import sqlite3
import json
import shutil
import gzip
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "claude_daemon.db"
BACKUP_DIR = Path(__file__).parent / "backups"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,   -- JSON-encoded (string or list)
                source      TEXT DEFAULT 'terminal',  -- 'terminal' or 'telegram'
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT,
                started_at  TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at    TEXT,
                message_count INTEGER DEFAULT 0,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS backups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT NOT NULL,
                message_count INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                type        TEXT DEFAULT 'json'  -- 'json' or 'snapshot'
            );
        """)


def save_message(role: str, content, source: str = "terminal") -> int:
    """Persist a single message. content can be str or list (tool calls)."""
    serialized = json.dumps(content) if not isinstance(content, str) else json.dumps(content)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (role, content, source) VALUES (?, ?, ?)",
            (role, serialized, source)
        )
        return cur.lastrowid


def load_messages() -> list[dict]:
    """Load all messages in chronological order."""
    with _connect() as conn:
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    messages = []
    for row in rows:
        content = json.loads(row["content"])
        messages.append({"role": row["role"], "content": content})
    return messages


def message_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def clear_messages():
    """Wipe conversation history (irreversible — backup first)."""
    with _connect() as conn:
        conn.execute("DELETE FROM messages")


def backup_to_json(label: str = None) -> Path:
    """Export full conversation to a timestamped gzipped JSON file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{label}" if label else ""
    path = BACKUP_DIR / f"messages_{ts}{tag}.json.gz"

    messages = load_messages()
    payload = {
        "exported_at": datetime.now().isoformat(),
        "label": label,
        "message_count": len(messages),
        "messages": messages,
    }

    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    with _connect() as conn:
        conn.execute(
            "INSERT INTO backups (path, message_count, type) VALUES (?, ?, 'json')",
            (str(path), len(messages))
        )

    return path


def restore_from_json(path: str) -> int:
    """
    Restore messages from a JSON or gzipped JSON backup.
    REPLACES current conversation — backs up first automatically.
    Returns number of messages restored.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Backup not found: {path}")

    # Auto-backup current state before overwriting
    current = message_count()
    if current > 0:
        backup_to_json(label="pre_restore")

    opener = gzip.open if path.endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8") as f:
        payload = json.load(f)

    messages = payload.get("messages", payload)  # support bare list or wrapped

    clear_messages()
    with _connect() as conn:
        for msg in messages:
            content = msg["content"]
            serialized = json.dumps(content)
            conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (msg["role"], serialized)
            )

    return len(messages)


def list_backups() -> list[dict]:
    """Return list of backups metadata."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM backups ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def prune_backups(keep: int = 30):
    """Delete JSON backups older than the most recent `keep` entries."""
    with _connect() as conn:
        old = conn.execute(
            "SELECT id, path FROM backups WHERE type='json' ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (keep,)
        ).fetchall()
        for row in old:
            p = Path(row["path"])
            if p.exists():
                p.unlink()
            conn.execute("DELETE FROM backups WHERE id=?", (row["id"],))
