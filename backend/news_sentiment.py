"""Extends the News Agent from pure avoidance ("a high-impact event is
near, stay out") to actual interpretation ("what is recent news saying
about this currency"). This was the gap flagged during the agent audit:
News Agent could tell you an event was coming, but never what it meant.

The ForexFactory calendar feed used elsewhere (news_calendar.py) does
NOT carry an "actual" released value — checked directly against the
live feed, every event only has forecast/previous, never actual — so a
real beat/miss-vs-forecast calculation isn't possible from that source.
Instead, this module searches recent real headlines (DuckDuckGo, free,
same approach as web_research.py) and asks a free LLM to read them and
judge sentiment — real text, real reasoning, just not a beat/miss
percentage. Treat the result as a rough directional lean, not a precise
economic surprise index.
"""
from __future__ import annotations

import json
import time

from llm_providers import cerebras, groq

PROVIDERS = [groq, cerebras]
CHECK_INTERVAL_SEC = 4 * 60 * 60  # sentiment doesn't meaningfully shift every cycle

SYSTEM_PROMPT = """คุณคือผู้วิเคราะห์ข่าวเศรษฐกิจสำหรับสกุลเงินหนึ่ง
จะได้รับพาดหัวข่าวจริงล่าสุดเกี่ยวกับสกุลเงินนั้น ให้อ่านแล้วสรุปว่าโดยรวมข่าวพวกนี้เป็นบวก (bullish, มีแนวโน้มทำให้สกุลเงินแข็งค่า)
ลบ (bearish, มีแนวโน้มทำให้อ่อนค่า) หรือกลางๆ/ไม่ชัดเจน (neutral) — ต้องอ้างอิงพาดหัวจริงที่ให้มา ห้ามมั่วขึ้นมาเอง
ถ้าพาดหัวไม่เกี่ยวกับเศรษฐกิจ/นโยบายการเงินเลย ให้ตอบ neutral
ตอบเป็น JSON เท่านั้น: {"sentiment": "bullish"|"bearish"|"neutral", "reason": "เหตุผลสั้นๆ อ้างอิงพาดหัวที่ใช้ตัดสิน"}"""

_cache: dict[str, dict] = {}


def _fetch_headlines(currency: str, max_results: int = 5) -> str:
    from ddgs import DDGS

    query = f"{currency} currency forex news today"
    results = DDGS().text(query, max_results=max_results)
    return "\n".join(f"- {r['title']}: {r['body'][:150]}" for r in results)


def _call_first_available(system_prompt: str, user_prompt: str) -> str | None:
    for provider in PROVIDERS:
        try:
            raw = provider.generate(system_prompt, user_prompt)
        except Exception:
            continue
        if raw:
            return raw
    return None


def get_sentiment(currency: str) -> dict:
    """Returns {"sentiment": "bullish"|"bearish"|"neutral", "reason": str,
    "checked_at": float}. Cached per currency for CHECK_INTERVAL_SEC."""
    now = time.time()
    cached = _cache.get(currency)
    if cached and now - cached["checked_at"] < CHECK_INTERVAL_SEC:
        return cached

    try:
        headlines = _fetch_headlines(currency)
    except Exception as e:
        result = {"sentiment": "neutral", "reason": f"ค้นข่าวไม่ได้: {e}", "checked_at": now}
        _cache[currency] = result
        return result

    if not headlines:
        result = {"sentiment": "neutral", "reason": "ไม่พบพาดหัวข่าวล่าสุด", "checked_at": now}
        _cache[currency] = result
        return result

    raw = _call_first_available(
        SYSTEM_PROMPT,
        f"สกุลเงิน: {currency}\n\nพาดหัวข่าวล่าสุด:\n{headlines}\n\nวิเคราะห์ sentiment ตามรูปแบบ JSON ที่กำหนด",
    )
    if raw is None:
        result = {"sentiment": "neutral", "reason": "ไม่มี LLM provider ใดตอบ", "checked_at": now}
        _cache[currency] = result
        return result

    try:
        parsed = json.loads(raw.strip().strip("```json").strip("```"))
        sentiment = parsed.get("sentiment")
        if sentiment not in ("bullish", "bearish", "neutral"):
            sentiment = "neutral"
        result = {"sentiment": sentiment, "reason": (parsed.get("reason") or "")[:200], "checked_at": now}
    except (json.JSONDecodeError, AttributeError):
        result = {"sentiment": "neutral", "reason": f"parse ไม่ผ่าน: {raw[:150]}", "checked_at": now}

    _cache[currency] = result
    return result
