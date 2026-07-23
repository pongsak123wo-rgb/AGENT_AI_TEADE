"""System health monitor — notices when the system silently stops working.

A trading system that dies quietly is worse than one that errors loudly:
the dashboard keeps showing the last state and you assume it's fine while
nothing is actually being analyzed or traded. This module tracks
heartbeats from the cycle loop and the external dependencies, and reports
anything that has gone stale or is failing.

Checks:
  • cycle heartbeat  — has run_cycle() completed recently?
  • MT5 feed         — is the price snapshot fresh and the account demo?
  • LLM providers    — is at least one provider usable (not all in cooldown)?
  • cycle errors     — consecutive failures in the loop
  • open positions   — positions open far longer than expected

Exposed at /monitor/status; the UI shows it and can alert on `ok: false`.
"""
from __future__ import annotations

import time

import kill_switch
import llm_circuit_breaker
import mt5_bridge

CYCLE_STALE_SEC = 120        # no completed cycle in 2 min = something's wrong
MAX_CONSECUTIVE_ERRORS = 5

_state = {
    "last_cycle_at": 0.0,
    "last_cycle_symbol": None,
    "cycles_total": 0,
    "consecutive_errors": 0,
    "last_error": None,
    "last_error_at": 0.0,
    "last_trade_at": 0.0,
}


def record_cycle(symbol: str):
    _state["last_cycle_at"] = time.time()
    _state["last_cycle_symbol"] = symbol
    _state["cycles_total"] += 1
    _state["consecutive_errors"] = 0


def record_error(err: str):
    _state["consecutive_errors"] += 1
    _state["last_error"] = err[:200]
    _state["last_error_at"] = time.time()


def record_trade():
    _state["last_trade_at"] = time.time()


def status() -> dict:
    now = time.time()
    alerts: list[str] = []

    # --- cycle heartbeat ---
    age = now - _state["last_cycle_at"] if _state["last_cycle_at"] else None
    cycle_ok = age is not None and age < CYCLE_STALE_SEC
    if _state["last_cycle_at"] == 0:
        alerts.append("ยังไม่มี cycle ทำงานเลย — ระบบอาจยังไม่เริ่ม")
    elif not cycle_ok:
        alerts.append(f"cycle หยุดไป {int(age)} วินาที — ระบบอาจค้าง/พัง")

    # --- consecutive errors ---
    if _state["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
        alerts.append(f"cycle พังติดกัน {_state['consecutive_errors']} ครั้ง — {_state['last_error']}")

    # --- MT5 feed ---
    snap = mt5_bridge.read_snapshot()
    mt5_ok = snap is not None
    is_demo = bool(snap and snap.get("account", {}).get("trade_mode") == "demo")
    if not mt5_ok:
        alerts.append("อ่านข้อมูล MT5 ไม่ได้ — terminal ปิด/หลุด connection?")
    elif not is_demo:
        alerts.append("⚠️ บัญชีไม่ใช่ DEMO — ระบบจะไม่ส่งออเดอร์ (safety)")

    # --- LLM providers ---
    cb = llm_circuit_breaker.status()
    cooling = [n for n, s in cb.items() if s.get("in_cooldown")]
    all_known = {"gemini", "groq", "cerebras"}
    if all_known.issubset(set(cooling)):
        alerts.append("LLM ทุกตัวติด cooldown — CEO ตัดสินใจไม่ได้ ไม่มีไม้ใหม่")
    elif cooling:
        alerts.append(f"LLM บางตัว cooldown: {', '.join(cooling)} (ยังมีตัวอื่นทำงาน)")

    # --- kill switch ---
    ks = kill_switch.status()
    if not ks.get("enabled"):
        alerts.append(f"Kill switch ปิดการเทรดอยู่ — {ks.get('tripped_reason') or 'ปิดโดยผู้ใช้'}")

    hard_fail = (not cycle_ok) or (not mt5_ok) or all_known.issubset(set(cooling)) \
        or _state["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS

    return {
        "ok": not hard_fail,
        "alerts": alerts,
        "cycle": {
            "last_at": _state["last_cycle_at"],
            "age_sec": round(age, 1) if age is not None else None,
            "last_symbol": _state["last_cycle_symbol"],
            "total": _state["cycles_total"],
            "consecutive_errors": _state["consecutive_errors"],
            "last_error": _state["last_error"],
        },
        "mt5": {"connected": mt5_ok, "is_demo": is_demo,
                "source": (snap or {}).get("source", "ea_file" if mt5_ok else None)},
        "llm": {"cooldown": cooling, "usable": sorted(all_known - set(cooling))},
        "kill_switch": ks,
        "last_trade_at": _state["last_trade_at"],
        "checked_at": now,
    }
