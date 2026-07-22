"""Auto-disable a specific setup pattern when its real recent
performance turns bad — same idea as the XAUUSD_Portfolio_v9.mq5 EA's
per-strategy auto-disable (loss streak / win-rate triggers, re-enabled
daily), applied to this system's setup patterns since it runs one
pipeline per symbol rather than 32 separate named sub-strategies.

Two independent pattern axes are tracked, each with its own disable
state:
- indicator patterns: (action, rsi_state, ema_trend) — the original.
- SMC patterns: (structure_event, mtf_confluence) — added after a real
  XAUUSD setup got stuck closing breakeven 7+ times in a row on the
  indicator axis with nothing catching it on the SMC axis at all,
  because that axis had no auto-disable yet.

This is a veto layered on top of (not a replacement for) the existing
win-rate-based dynamic risk sizing in risk.py — that scales position
size down on a bad streak; this stops a specific BAD SETUP from being
traded at all once it's proven itself unreliable, then gives it a fresh
chance the next day in case conditions changed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import signal_log

LOSS_STREAK_THRESHOLD = 3
BREAKEVEN_STREAK_THRESHOLD = 5
MIN_SAMPLES_FOR_WIN_RATE = 10
MIN_WIN_RATE_PCT = 35.0
RESET_INTERVAL_SEC = 24 * 60 * 60

# Persisted to disk so a backend restart / VPS reboot doesn't wipe the
# "these setups are proven-bad, keep them disabled" memory — the whole
# point of the 24h cooldown is defeated if it resets every restart.
_STATE_PATH = Path(__file__).parent / "pattern_disable_state.json"

_disabled: dict[str, dict] = {}
_last_reset = time.time()


def _load_state():
    global _disabled, _last_reset
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        _disabled = data.get("disabled", {})
        _last_reset = data.get("last_reset", time.time())
    except (OSError, json.JSONDecodeError):
        _disabled = {}
        _last_reset = time.time()


def _save_state():
    try:
        _STATE_PATH.write_text(
            json.dumps({"disabled": _disabled, "last_reset": _last_reset}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


_load_state()


def _maybe_reset_daily():
    global _last_reset
    now = time.time()
    if now - _last_reset >= RESET_INTERVAL_SEC:
        _disabled.clear()
        _last_reset = now
        _save_state()


def _disable(key: str, info: dict) -> dict:
    """Record a pattern as disabled AND persist immediately, so the state
    survives a restart."""
    _disabled[key] = info
    _save_state()
    return info


def _matches(row: dict, filters: dict) -> bool:
    return all(row.get(k) == v for k, v in filters.items())


def _current_streak(filters: dict, target_status: str, statuses: tuple, limit: int = 20) -> int:
    rows = [r for r in signal_log.recent(limit=200) if r["status"] in statuses and _matches(r, filters)]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    streak = 0
    for r in rows[:limit]:
        if r["status"] == target_status:
            streak += 1
        else:
            break
    return streak


def _check_generic(key: str, filters: dict, label: str) -> dict | None:
    """Shared logic for both pattern axes: loss streak -> breakeven
    streak -> low win-rate, in that order. `label` is just the
    human-readable description used in the Thai reason text."""
    if key in _disabled:
        return _disabled[key]

    # _current_streak filters its row list down to status IN statuses
    # BEFORE counting, which makes any status NOT in that tuple
    # invisible to the count entirely (not "a non-match that resets the
    # streak") — so the loss-streak check (statuses=(win,loss)) and the
    # breakeven-streak check (statuses=(win,loss,breakeven)) genuinely
    # look at different things and both are needed.
    loss_streak = _current_streak(filters, "loss", ("win", "loss"))
    if loss_streak >= LOSS_STREAK_THRESHOLD:
        info = {"reason": f"แพ้ติดกัน {loss_streak} ครั้งสำหรับ {label} — ปิดใช้ชั่วคราว เปิดใหม่วันถัดไป", "disabled_at": time.time()}
        return _disable(key, info)

    be_streak = _current_streak(filters, "breakeven", ("win", "loss", "breakeven"))
    if be_streak >= BREAKEVEN_STREAK_THRESHOLD:
        info = {
            "reason": (
                f"breakeven ติดกัน {be_streak} ครั้งสำหรับ {label} "
                f"— ไม่ได้แพ้แต่ก็ไม่ได้ไปไหน เสียค่า spread/commission ซ้ำๆโดยไม่มีความคืบหน้า ปิดใช้ชั่วคราว เปิดใหม่วันถัดไป"
            ),
            "disabled_at": time.time(),
        }
        return _disable(key, info)

    return None


def check(symbol: str, action: str, rsi_state: str | None, ema_trend: str | None) -> dict | None:
    """Call before approving a signal. Returns disable info (already
    disabled, or newly triggered now) or None if this pattern is fine.

    Keyed PER SYMBOL — confirmed live that a cross-symbol key let one
    symbol's bad streak (XAUUSD breaking even repeatedly on
    sell/neutral/up) auto-disable that combo for every other symbol
    too, blocking real simultaneous signals on EURUSD/GBPUSD/USDJPY for
    a full day even though none of them had actually misbehaved."""
    if rsi_state is None or ema_trend is None:
        return None

    _maybe_reset_daily()
    key = f"indicator:{symbol}/{action}/{rsi_state}/{ema_trend}"
    filters = {"symbol": symbol, "action": action, "rsi_state": rsi_state, "ema_trend": ema_trend}
    label = f"{symbol} {action} ตอน RSI={rsi_state}/EMA={ema_trend}"

    hit = _check_generic(key, filters, label)
    if hit:
        return hit

    try:
        patterns = signal_log.get_learned_patterns(min_samples=MIN_SAMPLES_FOR_WIN_RATE, symbol=symbol)
    except Exception:
        patterns = []

    for p in patterns:
        if p["action"] == action and p["rsi_state"] == rsi_state and p["ema_trend"] == ema_trend:
            if p["win_rate_pct"] < MIN_WIN_RATE_PCT:
                info = {
                    "reason": f"win rate {p['win_rate_pct']}% ต่ำกว่าเกณฑ์ {MIN_WIN_RATE_PCT}% จาก {p['samples']} ตัวอย่างจริงสำหรับ {label}",
                    "disabled_at": time.time(),
                }
                return _disable(key, info)
            break

    return None


def check_structure(symbol: str, structure_event: str | None, mtf_confluence: str | None) -> dict | None:
    """Same idea as check(), but keyed on the SMC axis (structure_event,
    mtf_confluence) instead of the indicator axis (action/rsi/ema) —
    a setup can get stuck on either axis independently. Also per-symbol
    for the same reason as check()."""
    if not structure_event or structure_event == "none" or not mtf_confluence:
        return None  # nothing distinctive to track

    _maybe_reset_daily()
    key = f"smc:{symbol}/{structure_event}/{mtf_confluence}"
    filters = {"symbol": symbol, "structure_event": structure_event, "mtf_confluence": mtf_confluence}
    label = f"{symbol} SMC structure={structure_event}/mtf={mtf_confluence}"

    hit = _check_generic(key, filters, label)
    if hit:
        return hit

    try:
        patterns = signal_log.get_structure_patterns(min_samples=MIN_SAMPLES_FOR_WIN_RATE, symbol=symbol)
    except Exception:
        patterns = []

    for p in patterns:
        if p["structure_event"] == structure_event and p["mtf_confluence"] == mtf_confluence:
            if p["win_rate_pct"] < MIN_WIN_RATE_PCT:
                info = {
                    "reason": f"win rate {p['win_rate_pct']}% ต่ำกว่าเกณฑ์ {MIN_WIN_RATE_PCT}% จาก {p['samples']} ตัวอย่างจริงสำหรับ {label}",
                    "disabled_at": time.time(),
                }
                return _disable(key, info)
            break

    return None


def status() -> dict:
    _maybe_reset_daily()
    return dict(_disabled)
