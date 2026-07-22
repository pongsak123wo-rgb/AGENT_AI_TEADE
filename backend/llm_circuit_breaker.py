"""Shared per-provider circuit breaker for LLM calls.

Some failures (a daily token quota hard-exhausted, a free-tier rate
limit hit repeatedly, a billing issue) will fail on every single call
until something external changes (quota reset, the user fixes billing).
Hammering that provider every cycle just adds a full request+timeout of
latency for no benefit. After a few consecutive failures, callers should
skip straight to their fallback for a cooldown period instead of
re-trying a provider known to be down right now.

Used by both ceo_council.py (3-provider CEO vote) and llm_analysis.py
(Technical Agent's provider chain) — one shared failure-tracking table
so a provider that's down for one doesn't need re-discovering by the
other; keyed by provider name, no cross-talk needed.
"""
from __future__ import annotations

import time

FAILURE_THRESHOLD = 3
COOLDOWN_SEC = 30 * 60

_failures: dict[str, dict] = {}


def is_in_cooldown(name: str) -> bool:
    info = _failures.get(name)
    return bool(info and time.time() < info.get("until", 0))


def record_failure(name: str):
    info = _failures.setdefault(name, {"count": 0, "until": 0.0})
    info["count"] += 1
    if info["count"] >= FAILURE_THRESHOLD:
        info["until"] = time.time() + COOLDOWN_SEC


def record_success(name: str):
    _failures.pop(name, None)


def status() -> dict:
    now = time.time()
    return {
        name: {"count": info["count"], "in_cooldown": now < info.get("until", 0), "cooldown_remaining_sec": max(0, round(info.get("until", 0) - now))}
        for name, info in _failures.items()
    }
