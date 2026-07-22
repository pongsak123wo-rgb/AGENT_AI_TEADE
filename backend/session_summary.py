"""'Welcome back' summary — instead of a fixed daily cron (the user
doesn't always have the dashboard open, so a midnight summary might
never be seen), this fires the moment someone reconnects after the
dashboard was closed/idle, recapping everything that happened while
nobody was watching: trade results, risk adjustments, kill-switch
trips, new CHoCH/BOS structure events, and what the system researched
on its own.

State (last_summary_at) persists to disk so a backend restart doesn't
lose track of when the last recap was given.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import kill_switch
import research_log
import signal_log

STATE_PATH = Path(__file__).parent / "session_summary_state.json"
MIN_GAP_SEC = 10 * 60  # don't re-summarize if someone reconnects within 10 min of the last recap


def get_last_summary_at() -> float:
    if not STATE_PATH.exists():
        return 0.0
    try:
        return json.loads(STATE_PATH.read_text()).get("last_summary_at", 0.0)
    except (json.JSONDecodeError, OSError):
        return 0.0


def set_last_summary_at(ts: float):
    STATE_PATH.write_text(json.dumps({"last_summary_at": ts}))


def _structure_events_since(since: float) -> list[dict]:
    """Scans closed/open signals logged since `since` for notable SMC
    structure events (CHoCH especially — the reversal signal) recorded
    at decision time in indicators_json."""
    events = []
    for row in signal_log.recent(limit=50):
        if row["created_at"] <= since:
            continue
        try:
            indicators = json.loads(row["indicators_json"] or "{}")
        except json.JSONDecodeError:
            continue
        smc = indicators.get("smc") or {}
        if smc.get("structure_event") == "CHoCH":
            events.append({"symbol": row["symbol"], "direction": smc.get("structure_direction"), "detail": smc.get("structure_detail")})
    return events


def build_summary(since: float) -> str | None:
    """Returns a Thai-language recap string, or None if nothing
    happened since `since` (so callers can skip broadcasting noise)."""
    now = time.time()
    trades = [r for r in signal_log.recent(limit=100) if r["created_at"] > since]
    closed = [t for t in trades if t["status"] in ("win", "loss")]
    wins = sum(1 for t in closed if t["status"] == "win")
    losses = len(closed) - wins
    open_count = sum(1 for t in trades if t["status"] == "open")

    research_items = [r for r in research_log.recent(limit=30) if r["created_at"] > since]
    structure_events = _structure_events_since(since)

    ks_status = kill_switch.status()
    kill_switch_note = None
    if not ks_status["enabled"] and ks_status["tripped_at"] and ks_status["tripped_at"] > since:
        kill_switch_note = ks_status["tripped_reason"]

    if not trades and not research_items and not structure_events and not kill_switch_note:
        return None

    away_minutes = round((now - since) / 60) if since > 0 else None
    parts = []
    if away_minutes is not None and away_minutes > 1:
        parts.append(f"ช่วงที่คุณไม่ได้เปิดมา ({away_minutes} นาทีที่แล้ว) ระบบทำงานต่อ:")
    else:
        parts.append("สรุปสถานะตอนนี้:")

    if closed:
        parts.append(f"- ปิดไม้ไปแล้ว {len(closed)} ไม้ — ชนะ {wins} แพ้ {losses}")
    if open_count:
        parts.append(f"- มีไม้เปิดใหม่อยู่ {open_count} ไม้ที่ยังไม่ปิดผล")
    if kill_switch_note:
        parts.append(f"- ⚠ Kill switch ตัดอัตโนมัติ: {kill_switch_note}")
    if structure_events:
        ev_text = ", ".join(f"{e['symbol']} ({e['direction']})" for e in structure_events[:5])
        parts.append(f"- เจอสัญญาณกลับตัว (CHoCH) บน: {ev_text}")
    if research_items:
        topics = ", ".join(r["topic_key"] for r in research_items[:5])
        parts.append(f"- ค้นคว้าเองเพิ่ม {len(research_items)} เรื่อง: {topics}")

    return "\n".join(parts)
