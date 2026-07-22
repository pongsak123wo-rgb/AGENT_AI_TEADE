"""Lab notebook for the system's own self-research cycle.

Tracks *what* topic was researched, *why* (which learned pattern or
mistake triggered it, and what the win rate was at that moment), and
*how many* knowledge chunks came out of it — so the research loop in
web_research.py can avoid re-researching the same pattern every few
hours, and so the cycle is auditable (a scientist's lab notes, not a
black box): hypothesis -> experiment (web research) -> recorded result.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "research_log.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            topic_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            win_rate_at_time REAL,
            query TEXT NOT NULL,
            chunks_ingested INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def already_researched(topic_key: str, within_days: float = 3.0) -> bool:
    """True if this exact topic was already researched recently — stops
    the system from asking the same question over and over instead of
    moving on to the next pattern worth investigating."""
    conn = _connect()
    cutoff = time.time() - within_days * 24 * 60 * 60
    row = conn.execute(
        "SELECT id FROM research_log WHERE topic_key = ? AND created_at > ? LIMIT 1",
        (topic_key, cutoff),
    ).fetchone()
    conn.close()
    return row is not None


def log_research(topic_key: str, reason: str, win_rate_at_time: float | None, query: str, chunks_ingested: int):
    conn = _connect()
    conn.execute(
        "INSERT INTO research_log (created_at, topic_key, reason, win_rate_at_time, query, chunks_ingested) VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), topic_key, reason, win_rate_at_time, query, chunks_ingested),
    )
    conn.commit()
    conn.close()


def recent(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM research_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_db()
