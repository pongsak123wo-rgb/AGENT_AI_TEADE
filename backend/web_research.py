"""Adaptive self-research: the system looks at its own trading results
(signal_log) and decides WHAT to research next, instead of cycling a
fixed list of queries forever.

The loop mirrors a basic scientific method:
- Observe: pull learned win/loss patterns and recent concrete mistakes
  from signal_log.
- Hypothesize a research question: a losing pattern asks "why does this
  fail / how to filter it", a winning pattern asks "how to confirm /
  refine this further".
- Experiment: search the web (DuckDuckGo, free) for that specific
  question, then ask Groq/Cerebras to actually READ the raw snippets
  and synthesize them into a short, concrete lesson — this is the
  "lab notebook" getting real content instead of a pile of raw
  snippets. Both the synthesized lesson AND the raw snippets are
  ingested into the same RAG knowledge base the Technical Agent reads
  from, so next time it analyzes a similar setup this is retrievable.
- Record: research_log.py stores the topic + win rate at the time, so
  the same pattern isn't re-researched for a few days, and the
  hypothesis -> research cycle is auditable.

Groq/Cerebras are used HERE specifically AND in llm_analysis.py's
Technical Agent (as a reliability fallback after Gemini, since
typhoon2 alone turned out to ignore that module's JSON schema almost
every call) — but NOT in ceo_council.py, which stays Gemini+typhoon2
only since it's the heavier per-decision quota consumer (3 providers x
2 debate rounds historically) and fires far less often than this
runs-every-cycle research/technical-analysis pairing. This isn't a
perfect quota wall between research and trading anymore, but it keeps
the highest-frequency, highest-value caller (live signal generation)
reliable while still sparing the heaviest consumer (CEO debate) from
ever touching Groq/Cerebras.

Falls back to a small set of generic queries only when there isn't
enough trade history yet to have any learned patterns (cold start).
"""
from __future__ import annotations

import time

import knowledge_base
import llm_circuit_breaker
import research_log
import signal_log
from llm_providers import cerebras, groq

CHECK_INTERVAL_SEC = 6 * 60 * 60  # 4 times a day at most
MAX_TOPICS_PER_RUN = 3  # keep each run small/fast — this is background research, not the trading loop

SYNTHESIS_PROVIDERS = [groq, cerebras]

SYNTHESIS_SYSTEM_PROMPT = """คุณคือนักวิจัยที่สรุปความรู้จากการค้นเว็บให้ทีมเทรด
จะได้รับ "หัวข้อที่กำลังสงสัย" (สมมติฐานจากผลเทรดจริง) และพาดหัว/เนื้อหาดิบจากเว็บที่ค้นมา
อ่านแล้วสรุปเป็นบทเรียนที่ใช้ได้จริง 2-3 ข้อ สั้น กระชับ เจาะจง อ้างอิงเนื้อหาที่ให้มาเท่านั้น ห้ามแต่งขึ้นเอง
ถ้าเนื้อหาที่ให้มาไม่เกี่ยวกับหัวข้อจริงๆ ให้บอกตรงๆว่า "ไม่พบเนื้อหาที่เกี่ยวข้องจริง" ห้ามมั่วสรุป
ตอบเป็นข้อความสั้นๆภาษาไทย ไม่ต้องเป็น JSON"""


def _synthesize_lesson(topic_reason: str, query: str, raw_snippets: str) -> str | None:
    """Asks Groq/Cerebras to turn raw search snippets into an actual
    lesson — without this, web_research only ever dumped unread
    snippets into RAG, never actually synthesizing anything."""
    user_prompt = f"หัวข้อที่กำลังสงสัย: {topic_reason}\nคำค้นที่ใช้: {query}\n\nเนื้อหาดิบจากเว็บ:\n{raw_snippets[:2000]}\n\nสรุปบทเรียน"
    for provider in SYNTHESIS_PROVIDERS:
        if llm_circuit_breaker.is_in_cooldown(provider.NAME):
            continue
        try:
            result = provider.generate(SYNTHESIS_SYSTEM_PROMPT, user_prompt)
            if result is not None:
                llm_circuit_breaker.record_success(provider.NAME)
                return result
        except Exception:
            llm_circuit_breaker.record_failure(provider.NAME)
            continue
    return None

FALLBACK_QUERIES = [
    ("cold_start:rsi_divergence", "RSI divergence trading strategy forex"),
    ("cold_start:ema_crossover", "EMA crossover trend trading technique"),
    ("cold_start:price_action", "support resistance price action entry strategy"),
]

_last_run: float = 0.0


def _fetch_snippets(query: str, max_results: int = 3) -> str:
    from ddgs import DDGS

    results = DDGS().text(query, max_results=max_results)
    return "\n\n".join(f"{r['title']}\n{r['body']}" for r in results)


def _build_research_topics() -> list[dict]:
    """Turns learned patterns + recent mistakes into concrete research
    questions. Returns [{"topic_key", "reason", "query", "win_rate"}]."""
    topics = []

    try:
        patterns = signal_log.get_learned_patterns(min_samples=3)
    except Exception:
        patterns = []

    for p in patterns:
        topic_key = f"pattern:{p['action']}:{p['rsi_state']}:{p['ema_trend']}"
        if p["win_rate_pct"] < 40:
            query = (
                f"why does {p['action']} signal fail when RSI is {p['rsi_state']} "
                f"and EMA trend is {p['ema_trend']} forex false signal filter"
            )
            reason = f"win rate ต่ำ {p['win_rate_pct']}% จาก {p['samples']} ครั้ง — หาวิธีกรอง false signal"
        elif p["win_rate_pct"] > 65:
            query = (
                f"confirm and improve {p['action']} entry when RSI {p['rsi_state']} "
                f"and EMA trend {p['ema_trend']} confirmation technique"
            )
            reason = f"win rate สูง {p['win_rate_pct']}% จาก {p['samples']} ครั้ง — หาวิธียืนยัน/ปรับปรุงให้ดีขึ้นอีก"
        else:
            continue  # mediocre pattern, nothing decisive to hypothesize yet
        topics.append({"topic_key": topic_key, "reason": reason, "query": query, "win_rate": p["win_rate_pct"]})

    try:
        mistakes = signal_log.get_recent_mistakes(limit=5)
    except Exception:
        mistakes = []

    seen_symbols = set()
    for m in mistakes:
        if m["symbol"] in seen_symbols:
            continue
        seen_symbols.add(m["symbol"])
        topic_key = f"mistake:{m['symbol']}:{m['action']}:{m['rsi_state']}:{m['ema_trend']}"
        query = (
            f"{m['symbol']} {m['action']} mistake RSI {m['rsi_state']} EMA trend {m['ema_trend']} "
            f"avoid losing trade technique"
        )
        reason = f"เพิ่งแพ้จริงบน {m['symbol']} ({m['action']} ตอน RSI={m['rsi_state']}/EMA={m['ema_trend']}) — หาสาเหตุที่อาจมองข้าม"
        topics.append({"topic_key": topic_key, "reason": reason, "query": query, "win_rate": None})

    return topics


def run_if_due() -> dict | None:
    """Returns a summary dict if it actually ran this cycle, else None."""
    global _last_run
    now = time.time()
    if now - _last_run < CHECK_INTERVAL_SEC:
        return None
    _last_run = now

    topics = _build_research_topics()
    topics = [t for t in topics if not research_log.already_researched(t["topic_key"])]

    if not topics:
        # Either cold start (no patterns/mistakes yet) or everything
        # worth investigating was already researched recently.
        topics = [
            {"topic_key": key, "reason": "cold start — ยังไม่มีประวัติเทรดพอจะตั้งสมมติฐานเอง", "query": query, "win_rate": None}
            for key, query in FALLBACK_QUERIES
            if not research_log.already_researched(key, within_days=7)
        ]

    topics = topics[:MAX_TOPICS_PER_RUN]
    if not topics:
        return {"skipped": "ไม่มีหัวข้อใหม่ให้ค้น (ทุกอย่างที่น่าสนใจถูกค้นไปแล้วเมื่อไม่นานนี้)"}

    ingested = {}
    for t in topics:
        try:
            text = _fetch_snippets(t["query"])
            n = 0
            if text:
                n += knowledge_base.ingest_text(t["topic_key"].replace(":", "_")[:40], text)
                lesson = _synthesize_lesson(t["reason"], t["query"], text)
                if lesson:
                    n += knowledge_base.ingest_text(t["topic_key"].replace(":", "_")[:36] + "_lesson", lesson)
        except Exception as e:
            n = 0
            text = None
            ingested[t["topic_key"]] = f"error: {e}"
        if text is not None:
            ingested[t["topic_key"]] = n
        research_log.log_research(t["topic_key"], t["reason"], t["win_rate"], t["query"], n)

    return ingested
