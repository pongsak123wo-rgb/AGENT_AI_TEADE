"""Storage for backtest-simulated trades — kept in a SEPARATE database
from signal_log.py's signals.db on purpose. Backtest results come from
Yahoo Finance data with no real spread/slippage and a deterministic
rule instead of the live LLM's reasoning, so they are NOT the same
quality of evidence as a real executed trade. Keeping them apart means
any pattern-learning code can treat backtest-derived win rates as a
prior/starting point and never accidentally mix them into the "real"
win rate the live system reports.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "backtest_signals.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at REAL NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            entry REAL NOT NULL,
            sl REAL NOT NULL,
            tp REAL NOT NULL,
            result TEXT NOT NULL,
            structure_event TEXT,
            mtf_confluence TEXT,
            rsi_state TEXT,
            ema_trend TEXT,
            indicators_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_trade(
    symbol: str, action: str, entry: float, sl: float, tp: float, result: str,
    structure_event: str | None, mtf_confluence: str | None, indicators: dict,
):
    conn = _connect()
    conn.execute(
        """
        INSERT INTO backtest_signals
            (run_at, symbol, action, entry, sl, tp, result, structure_event, mtf_confluence, rsi_state, ema_trend, indicators_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(), symbol, action, entry, sl, tp, result,
            structure_event, mtf_confluence,
            indicators.get("rsi_state"), indicators.get("ema_trend"),
            json.dumps(indicators, default=str),
        ),
    )
    conn.commit()
    conn.close()


def clear_symbol(symbol: str):
    """Wipe previous backtest rows for a symbol before a fresh run, so
    re-running doesn't keep stacking duplicate historical windows."""
    conn = _connect()
    conn.execute("DELETE FROM backtest_signals WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()


def get_structure_patterns(min_samples: int = 5) -> list[dict]:
    """Same shape as signal_log.get_structure_patterns() but computed
    purely from backtest data — a prior to use ONLY until enough real
    trades accumulate, never to be confused with live performance."""
    conn = _connect()
    rows = conn.execute(
        """
        SELECT structure_event, mtf_confluence, result, COUNT(*) as n
        FROM backtest_signals
        WHERE result IN ('win', 'loss') AND structure_event IS NOT NULL
        GROUP BY structure_event, mtf_confluence, result
        """
    ).fetchall()
    conn.close()

    combos: dict[tuple, dict] = {}
    for row in rows:
        key = (row["structure_event"], row["mtf_confluence"])
        combos.setdefault(key, {"win": 0, "loss": 0})
        combos[key][row["result"]] = row["n"]

    patterns = []
    for (structure_event, mtf_confluence), counts in combos.items():
        total = counts["win"] + counts["loss"]
        if total < min_samples:
            continue
        patterns.append(
            {
                "structure_event": structure_event,
                "mtf_confluence": mtf_confluence,
                "win": counts["win"],
                "loss": counts["loss"],
                "samples": total,
                "win_rate_pct": round(counts["win"] / total * 100, 1),
            }
        )
    patterns.sort(key=lambda p: p["win_rate_pct"], reverse=True)
    return patterns


def summary() -> dict:
    conn = _connect()
    rows = conn.execute("SELECT symbol, result, COUNT(*) as n FROM backtest_signals GROUP BY symbol, result").fetchall()
    conn.close()
    by_symbol: dict[str, dict] = {}
    for row in rows:
        s = by_symbol.setdefault(row["symbol"], {"win": 0, "loss": 0, "no_hit": 0})
        s[row["result"]] = row["n"]
    return by_symbol


init_db()
