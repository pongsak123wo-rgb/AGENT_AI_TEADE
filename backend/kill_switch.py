"""Global auto-trade kill switch + system health checks.

This is a manual/automatic safety valve sitting in front of
order_executor.send_order() — even if every agent approves a trade,
no order is sent while the kill switch is off. It can be toggled by
the user (e.g. a frontend button) or tripped automatically when the
risk manager detects daily-loss/drawdown breach, so a bad session
can't keep firing orders unattended.
"""
from __future__ import annotations

import time

import mt5_bridge
import order_executor

_state = {
    "enabled": True,
    "tripped_reason": None,
    "tripped_at": None,
}


def is_enabled() -> bool:
    return _state["enabled"]


def disable(reason: str):
    """Used both for manual user toggle and automatic trips (e.g. risk
    manager hitting daily loss / drawdown limit)."""
    _state["enabled"] = False
    _state["tripped_reason"] = reason
    _state["tripped_at"] = time.time()


def enable():
    _state["enabled"] = True
    _state["tripped_reason"] = None
    _state["tripped_at"] = None


def status() -> dict:
    return dict(_state)


def auto_trip_if_needed(risk_manager) -> str | None:
    """Called once per cycle. Trips the switch automatically if the
    risk manager's own daily-loss/drawdown limits are breached, so
    auto-trading stops the moment those limits are hit instead of
    relying on every future evaluate() call to keep rejecting trades.
    Returns the trip reason if it just tripped, else None.
    """
    if not _state["enabled"]:
        return None
    st = risk_manager.state
    cfg = risk_manager.config
    if st.daily_loss_used_pct >= cfg.daily_loss_limit_pct:
        reason = f"Auto-trip: daily loss {st.daily_loss_used_pct:.2f}% แตะ limit {cfg.daily_loss_limit_pct}%"
        disable(reason)
        return reason
    if st.total_drawdown_pct >= cfg.max_total_drawdown_pct:
        reason = f"Auto-trip: drawdown {st.total_drawdown_pct:.2f}% แตะ limit {cfg.max_total_drawdown_pct}%"
        disable(reason)
        return reason
    return None


# --- Health monitoring ---
# Freshness thresholds: how stale a data source can be before we flag it
# as unhealthy. MT5 snapshot is rewritten every EA tick (~1s); the result
# file only appears after an order, so "stale" there just means "no
# order recently" — not unhealthy by itself, just informational.
SNAPSHOT_STALE_SEC = 30


def check_health() -> dict:
    """`mt5_bridge.read_snapshot()` already returns None once the file is
    older than its own 10s staleness window, so a non-None result already
    means "fresh" — the extra SNAPSHOT_STALE_SEC check here is just a
    looser top-level signal for the health panel, based on the file's own
    mtime rather than re-deriving age from snapshot contents.
    """
    now = time.time()
    snapshot_exists = mt5_bridge.SNAPSHOT_PATH.exists()
    live = mt5_bridge.read_snapshot()

    if not snapshot_exists:
        mt5_health = {"ok": False, "detail": "ไม่พบ snapshot จาก EA เลย — EA ไม่ได้รันอยู่ หรือยังไม่เคยเขียนไฟล์"}
    elif live is None:
        age = now - mt5_bridge.SNAPSHOT_PATH.stat().st_mtime
        mt5_health = {"ok": False, "detail": f"Snapshot เก่าไป {age:.0f}s — EA อาจหยุดทำงาน"}
    else:
        age = now - mt5_bridge.SNAPSHOT_PATH.stat().st_mtime
        mt5_health = {"ok": True, "detail": f"Snapshot อายุ {age:.1f}s — ปกติ"}

    is_demo = order_executor.is_demo_account()

    overall_ok = mt5_health["ok"] and is_demo
    return {
        "ok": overall_ok,
        "mt5_snapshot": mt5_health,
        "account_mode": {"is_demo": is_demo},
        "kill_switch": status(),
        "checked_at": now,
    }
