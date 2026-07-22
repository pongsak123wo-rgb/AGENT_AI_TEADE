"""Reads real historical OHLC exported once by mt5_ea/HistoryExporter.mq5
(a manually-run Script, not the always-on EA) — the SAME broker feed used
for live trading, instead of Yahoo Finance's different liquidity provider
with no real spread baked in.

This file only exists after the user runs the script in MT5 — until then,
get_symbol_series() returns None and callers (backtest_engine.py) should
fall back to the Yahoo Finance path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HISTORY_PATH = (
    Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "mt5_history.json"
)


def read_history() -> dict | None:
    if not HISTORY_PATH.exists():
        return None
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None


def get_symbol_series(symbol: str, timeframe: str = "h1") -> dict | None:
    """timeframe: 'h1' or 'm1'. Returns {"o","h","l","c","t"} (oldest
    first) or None if the export doesn't exist / doesn't cover this
    symbol+timeframe yet."""
    data = read_history()
    if not data:
        return None
    sym_data = data.get("symbols", {}).get(symbol)
    if not sym_data:
        return None
    return sym_data.get(timeframe)


def status() -> dict:
    data = read_history()
    if not data:
        return {"available": False, "reason": "ยังไม่เคยรัน HistoryExporter.mq5 — ไฟล์ mt5_history.json ไม่มี"}
    symbols = data.get("symbols", {})
    return {
        "available": True,
        "exported_at": data.get("exported_at"),
        "symbols": {
            sym: {
                "h1_bars": len((d.get("h1") or {}).get("c", [])),
                "m1_bars": len((d.get("m1") or {}).get("c", [])),
            }
            for sym, d in symbols.items()
        },
    }
