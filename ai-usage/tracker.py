"""
AI Usage Tracker
================
Logs token usage and cost for both Claude and Codex daemons to SQLite.
Prices are stored in the DB and can be updated without code changes.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "usage.db"
log = logging.getLogger("ai_usage")

# Current prices in USD per million tokens (update as needed)
DEFAULT_PRICES = {
    "claude-opus-4-6":   {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  0.80, "output":  4.00},
    "gpt-4.1":           {"input":  2.00, "output":  8.00},
    "gpt-4.1-mini":      {"input":  0.40, "output":  1.60},
    "gpt-4.1-nano":      {"input":  0.10, "output":  0.40},
    "gpt-4o":            {"input":  2.50, "output": 10.00},
    "o3":                {"input": 10.00, "output": 40.00},
    "o4-mini":           {"input":  1.10, "output":  4.40},
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL DEFAULT (datetime('now')),
            agent         TEXT    NOT NULL,
            model         TEXT    NOT NULL,
            source        TEXT,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            input_cost    REAL    NOT NULL DEFAULT 0,
            output_cost   REAL    NOT NULL DEFAULT 0,
            total_cost    REAL    NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS prices (
            model          TEXT PRIMARY KEY,
            input_per_mtok REAL NOT NULL,
            output_per_mtok REAL NOT NULL,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_usage_ts    ON usage(ts);
        CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage(agent);
        CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
    """)
    # Seed prices (INSERT OR IGNORE — won't overwrite manual updates)
    for model, p in DEFAULT_PRICES.items():
        conn.execute(
            "INSERT OR IGNORE INTO prices (model, input_per_mtok, output_per_mtok) VALUES (?,?,?)",
            (model, p["input"], p["output"])
        )
    conn.commit()
    conn.close()


def get_price(model: str) -> tuple[float, float]:
    """Return (input_per_mtok, output_per_mtok) for a model. Falls back to 0,0."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT input_per_mtok, output_per_mtok FROM prices WHERE model=?", (model,)
    ).fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    log.warning(f"No price found for model '{model}' — logging cost as $0")
    return 0.0, 0.0


def log_usage(agent: str, model: str, source: str,
              input_tokens: int, output_tokens: int):
    """Record a single API call's token usage and compute cost."""
    try:
        in_price, out_price = get_price(model)
        input_cost  = input_tokens  / 1_000_000 * in_price
        output_cost = output_tokens / 1_000_000 * out_price
        total_cost  = input_cost + output_cost

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO usage
               (agent, model, source, input_tokens, output_tokens,
                input_cost, output_cost, total_cost)
               VALUES (?,?,?,?,?,?,?,?)""",
            (agent, model, source, input_tokens, output_tokens,
             input_cost, output_cost, total_cost)
        )
        conn.commit()
        conn.close()

        log.info(f"[usage] {agent}/{model} in={input_tokens} out={output_tokens} "
                 f"cost=${total_cost:.4f}")
    except Exception as e:
        log.error(f"Failed to log usage: {e}")


def get_summary(days: int = 30) -> dict:
    """Return usage summary for reporting."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Totals by agent+model
    by_model = conn.execute("""
        SELECT agent, model,
               SUM(input_tokens)  AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(total_cost)    AS total_cost,
               COUNT(*)           AS calls
        FROM usage
        WHERE ts >= datetime('now', ? || ' days')
        GROUP BY agent, model
        ORDER BY total_cost DESC
    """, (f"-{days}",)).fetchall()

    # Daily totals for sparkline
    daily = conn.execute("""
        SELECT date(ts) AS day,
               SUM(total_cost)    AS cost,
               SUM(input_tokens + output_tokens) AS tokens
        FROM usage
        WHERE ts >= datetime('now', ? || ' days')
        GROUP BY day
        ORDER BY day
    """, (f"-{days}",)).fetchall()

    # Grand total (COALESCE avoids None when table is empty)
    totals = conn.execute("""
        SELECT COALESCE(SUM(input_tokens), 0)  AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(total_cost), 0)    AS total_cost,
               COUNT(*)                         AS calls
        FROM usage
        WHERE ts >= datetime('now', ? || ' days')
    """, (f"-{days}",)).fetchone()

    conn.close()

    return {
        "days": days,
        "by_model": [dict(r) for r in by_model],
        "daily":    [dict(r) for r in daily],
        "totals":   dict(totals) if totals else {},
    }


def set_price(model: str, input_per_mtok: float, output_per_mtok: float):
    """Update price for a model."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO prices (model, input_per_mtok, output_per_mtok, updated_at)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(model) DO UPDATE SET
               input_per_mtok=excluded.input_per_mtok,
               output_per_mtok=excluded.output_per_mtok,
               updated_at=excluded.updated_at""",
        (model, input_per_mtok, output_per_mtok)
    )
    conn.commit()
    conn.close()


# Auto-init on import
init_db()
