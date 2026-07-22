"""Reads the JSON snapshot written by mt5_ea/PriceExporter.mq5.

This avoids the MetaTrader5 Python package entirely (its native DLL was
blocked by WDAC on this machine) — MT5 itself writes the file from
inside the already-trusted terminal process, and we just read it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SNAPSHOT_PATH = (
    Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "trading_room_snapshot.json"
)

STALE_AFTER_SEC = 10


def _read_file_snapshot() -> dict | None:
    """The original EA channel: read the JSON the EA writes."""
    if not SNAPSHOT_PATH.exists():
        return None

    age = time.time() - SNAPSHOT_PATH.stat().st_mtime
    if age > STALE_AFTER_SEC:
        return None

    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None


def read_snapshot() -> dict | None:
    """Unified data source: prefer the direct MT5 Python connection when
    it's available (no EA needed, all timeframes native), otherwise fall
    back to the EA-written JSON file. Returns None if neither works."""
    try:
        import mt5_direct
        if mt5_direct.available():
            snap = mt5_direct.read_snapshot()
            if snap:
                return snap
    except Exception:
        pass
    return _read_file_snapshot()


def is_live() -> bool:
    return read_snapshot() is not None
