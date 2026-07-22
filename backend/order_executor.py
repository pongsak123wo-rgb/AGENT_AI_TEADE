"""Sends trade commands to the MT5 EA via a shared file (mirrors the
mt5_bridge price-export channel, just in the other direction).

Auto-execution only ever fires on a confirmed DEMO account — both this
module and the EA itself independently check `account.trade_mode`
before sending/executing anything, so a misconfigured live account
can't get an order through even if one safety check is bypassed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import mt5_bridge

COMMAND_PATH = (
    Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "trading_room_command.json"
)
RESULT_PATH = (
    Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "trading_room_command_result.json"
)


def is_demo_account() -> bool:
    snapshot = mt5_bridge.read_snapshot()
    if not snapshot:
        return False
    return snapshot.get("account", {}).get("trade_mode") == "demo"


def _direct():
    """Return the mt5_direct module if the direct MT5 path is usable."""
    try:
        import mt5_direct
        if mt5_direct.available():
            return mt5_direct
    except Exception:
        pass
    return None


def send_order(decision: dict, equity: float) -> dict:
    """Returns {"sent": bool, "reason": str, "id": int|None}. Routes through
    the direct MT5 Python connection when available, else the EA file."""
    direct = _direct()
    if direct is not None:
        return direct.send_order(decision, equity)

    if not is_demo_account():
        return {"sent": False, "reason": "ปฏิเสธ — บัญชีนี้ไม่ใช่ DEMO (safety check)", "id": None}

    command_id = int(time.time() * 1000)
    risk_money = round(equity * decision["risk_pct"] / 100, 2)

    payload = {
        "id": command_id,
        "symbol": decision["symbol"],
        "action": decision["action"],
        "risk_money": risk_money,
        "sl": decision["sl"],
        "tp": decision["tp"],
    }

    COMMAND_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {"sent": True, "reason": f"ส่งคำสั่งไปยัง MT5 (DEMO) — risk {risk_money} {decision['symbol']}", "id": command_id}


def try_read_result(command_id: int) -> dict | None:
    """Single non-blocking check for the matching command id's result."""
    direct = _direct()
    if direct is not None:
        return direct.try_read_result(command_id)

    if not RESULT_PATH.exists():
        return None
    try:
        result = json.loads(RESULT_PATH.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None
    return result if result.get("id") == command_id else None
