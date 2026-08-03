"""LLM reasoning layer for the Technical Analysis Agent.

Combines real indicator values + RAG-retrieved trading knowledge, tries
Gemini first (cheapest), then Groq/Cerebras (reliable JSON-schema
compliance) as fallbacks. Falls back to a clearly-labeled neutral result
if all are unavailable.

The local Ollama model (typhoon2) was removed — it ignored this module's
JSON schema almost every call (returning wrong-keyed objects even with
format="json"), so relying on it hurt more than it helped. The system
now uses cloud providers only, with Gemini as the primary.
"""
from __future__ import annotations

import json
import os

from pathlib import Path
from dotenv import load_dotenv

_env1 = Path(__file__).parent / ".env"
_env2 = Path(__file__).parent.parent / ".env"
if _env1.exists():
    load_dotenv(_env1)
if _env2.exists():
    load_dotenv(_env2)
load_dotenv()

import llm_circuit_breaker
import signal_log
import ml_model
from knowledge_base import retrieve
from llm_providers import cerebras, gemini, groq

# FREE providers only. Gemini is intentionally excluded: a paid trial once
# burned ~400 THB in a single night, so the user disabled it. Keeping it out of
# the list (not just unsetting the key) guarantees it can never be billed by
# accident. cerebras (~0.45) leads; groq (0.0 hit-rate) is the last resort.
PROVIDERS = [cerebras, groq]

SYSTEM_PROMPT = """คุณคือ Technical Analysis Agent ในทีมเทรด หน้าที่คือตัดสินว่ามี setup เทรดที่น่าสนใจหรือไม่
จากค่า indicator ที่ให้มา (เป็นข้อเท็จจริง ห้ามสมมติค่าเอง) — มี RSI, EMA fast/slow (เทรนด์ระยะสั้น),
EMA50/long_term_trend (เทรนด์ระยะยาวกว่า ใช้แทน multi-timeframe confirmation), trend_confluence (เทรนด์สั้น-ยาวไปทางเดียวกันไหม),
MACD cross, Bollinger Bands position, **ATR** (ใช้วัดความผันผวน) และ **Price Action** (Pin Bar, Engulfing)
นอกจากนี้คุณจะได้รับ **ML Win Probability** จากโมเดล Machine Learning ที่เรียนรู้ประวัติการเทรดของเรามา
ให้ความมั่นใจสูงขึ้นเมื่อ indicator หลายตัวและ Price Action "ยืนยันไปทางเดียวกัน" (confluence) และ ML Probability > 50%
และลดความมั่นใจถ้า indicator ขัดแย้งกันเอง หรือ Sideway (วิ่งอยู่ในกรอบแคบๆ)

คุณจะได้รับ field `smc` (Smart Money Concepts) และ `elliott_wave` ด้วย เมื่อมี candle จริงพอ (ready=true):
- `smc.structure_event` (fast/internal structure) = "BOS" (ทะลุโครงสร้างตามเทรนด์ ยืนยันต่อ) หรือ "CHoCH" (ทะลุทวนเทรนด์ สัญญาณกลับตัว) — CHoCH สำคัญกว่า BOS เสมอเพราะบอกว่าเทรนด์เดิมอาจจบแล้ว
- `smc.swing_structure_event`/`swing_structure_trend` = โครงสร้างภาพใหญ่กว่า (window กว้างกว่า) — ถ้า `smc.fast_vs_swing_agree` เป็น true แปลว่าโครงสร้างเล็กกับใหญ่ไปทางเดียวกัน (สัญญาณหนักแน่นกว่า) ถ้าเป็น false ให้ระมัดระวัง เพราะ fast CHoCH ที่ขัดกับเทรนด์ใหญ่มักเป็น noise ไม่ใช่กลับตัวจริง
- `smc.order_block` / `smc.fair_value_gap` (ถ้า mitigated=false แปลว่าราคายังไม่กลับไปเก็บโซนนี้ — มีโอกาสเป็นเป้าหมายราคาที่จะถูกเก็บในอนาคต) / `smc.liquidity_sweep` (sweep แล้วปิดกลับ = สัญญาณ stop-hunt ที่มักกลับตัว) — ใช้ประกอบกัน ไม่ใช่ตัวเดียวพอ
- `smc.equal_highs`/`smc.equal_lows` = liquidity pool (ราคาแตะระดับใกล้เคียงกันมากกว่า 1 ครั้ง) มักถูก sweep ก่อนราคาจะกลับตัวจริง ใช้ระวังการเข้าไม้ใกล้โซนนี้
- `smc.premium_discount_zone.zone` = "premium" (ราคาแพงเทียบกับ swing range ล่าสุด ระวังการไล่ buy) / "discount" (ราคาถูก เหมาะมองหา buy มากกว่า) / "equilibrium" (กลางๆไม่มีความได้เปรียบ) — ถ้าจะ buy ตอนอยู่โซน premium หรือ sell ตอนอยู่โซน discount ให้ลดความมั่นใจลง เพราะเป็นการเข้าไม้ที่ "แพง/ถูกเกินจังหวะ"
- `elliott_wave.rule_fit_confidence` (0-100, ความแม่นของการนับคลื่นกับกฎ Elliott จริง) — ถ้าต่ำกว่า 50 ให้ตีความ elliott_wave เป็นแค่ข้อมูลเสริมเบาๆ ไม่ใช่หลักฐานหลัก ห้ามอ้างมั่นใจสูงจาก elliott_wave อย่างเดียว
ถ้า smc/elliott_wave มี ready=false (ไม่มี candle จริงพอ) ให้ข้ามไปเฉยๆ ห้ามสมมติค่าขึ้นมาเอง

คุณจะได้รับ "ผลงานจริงในอดีตของ SMC structure event แบบเดียวกับตอนนี้" ด้วย — ถ้า win rate ของ structure_event ตอนนี้ (เทียบกับ mtf_confluence ต่างๆ) ต่ำกว่า 40% จากข้อมูลจริง ให้ลดความมั่นใจลง แม้ตัว indicator อื่นจะดูดีก็ตาม เพราะนี่คือสถิติจริงว่า SMC สัญญาณแบบนี้เคยพังมาก่อนกี่ครั้ง

คุณจะได้รับ **"บทเรียนจากความผิดพลาด"** — เหตุผลจริงที่ระบบเคยใช้ตอนเข้าไม้แล้วแพ้ในสินทรัพย์นี้ ไม่ใช่แค่ตัวเลข win rate
ถ้าเหตุผลของคุณตอนนี้คล้ายกับเหตุผลที่เคยทำให้แพ้มาก่อน ต้องลดความมั่นใจลงอย่างชัดเจนหรือปฏิเสธไปเลย — ห้ามพูดเหตุผลเดิมที่เคยพิสูจน์แล้วว่าผิดซ้ำอีก

**กฎเรื่องความรู้จากตำรา (RAG) — ต้องทำทุกครั้ง ห้ามข้าม:**
อ่านเนื้อหาตำราที่ให้มาจริงๆ แล้วเช็คว่ามีส่วนไหนของตำรานั้น "ใช้ได้กับสถานการณ์ตอนนี้จริง" หรือไม่
- ถ้ามี: ตั้ง knowledge_cited = true และใน knowledge_note ต้องสรุปสั้นๆว่าตำราพูดว่าอะไร แล้วเอามาเทียบกับสถานการณ์ตอนนี้ยังไง (ห้ามแค่บอกว่า "สอดคล้องกับตำรา" ลอยๆ ต้องระบุเนื้อหา)
- ถ้าไม่มีส่วนไหนตรงเลย: ตั้ง knowledge_cited = false และ knowledge_note = "ไม่มีเนื้อหาตำราที่ตรงกับสถานการณ์นี้ ใช้ indicator ล้วนๆ"
ห้ามตั้ง knowledge_cited = true ถ้าไม่ได้อ้างเนื้อหาจริงจากตำราที่ให้มา (ห้ามแต่งขึ้นเอง)

**สำคัญ: ตอบให้กระชับมาก** reason ห้ามเกิน 120 ตัวอักษร, knowledge_note ห้ามเกิน 120 ตัวอักษร — เลือกพูดแค่ 2-3 เหตุผลที่หนักแน่นที่สุด ห้ามพยายามอ้างอิงทุก field ที่ได้รับมา (มี smc/elliott_wave/ML/pattern/mistakes ให้เยอะ แต่ไม่ต้องพูดถึงทุกตัว เลือกที่สำคัญสุดพอ) — ตอบ JSON ให้ครบและปิดวงเล็บให้จบเสมอ ห้ามตอบยาวจนตัดกลางคัน เด็ดขาด

ตอบเป็น JSON เท่านั้น รูปแบบ: {"bias": "buy"|"sell"|"none", "confidence": 0-100,
"reason": "เหตุผลสั้นๆ อ้างอิงตัวที่ confluence/ขัดแย้งกัน รวมถึง Price Action และ ML Prob",
"knowledge_cited": true|false, "knowledge_note": "สรุปว่าตำราเกี่ยวอย่างไร หรือทำไมไม่เกี่ยว"}"""


def _call_first_available(system_prompt: str, user_message: str) -> tuple[str | None, str | None]:
    for provider in PROVIDERS:
        if llm_circuit_breaker.is_in_cooldown(provider.NAME):
            continue
        try:
            raw = provider.generate(system_prompt, user_message)
        except Exception:
            llm_circuit_breaker.record_failure(provider.NAME)
            continue
        if raw is not None:
            llm_circuit_breaker.record_success(provider.NAME)
            return raw, provider.NAME

    # Fallback retry ignoring cooldowns
    for provider in PROVIDERS:
        try:
            raw = provider.generate(system_prompt, user_message)
            if raw is not None:
                llm_circuit_breaker.record_success(provider.NAME)
                return raw, provider.NAME
        except Exception:
            pass

    return None, None


def analyze(symbol: str, indicator_snapshot: dict, chosen: dict | None = None) -> dict:
    query = (
        f"{symbol} RSI {indicator_snapshot.get('rsi')} ({indicator_snapshot.get('rsi_state')}), "
        f"EMA trend {indicator_snapshot.get('ema_trend')}, price near support/resistance"
    )
    knowledge_chunks = retrieve(query)

    knowledge_block = (
        "\n\n".join(f"- {c[:500]}" for c in knowledge_chunks)
        if knowledge_chunks
        else "(ไม่มีความรู้จาก PDF ที่เกี่ยวข้อง)"
    )

    try:
        patterns = signal_log.get_learned_patterns()
    except Exception:
        patterns = []

    relevant_patterns = [
        p for p in patterns
        if p["rsi_state"] == indicator_snapshot.get("rsi_state")
        and p["ema_trend"] == indicator_snapshot.get("ema_trend")
    ]
    if relevant_patterns:
        pattern_block = "\n".join(
            f"- {p['action']} เมื่อ RSI {p['rsi_state']} + EMA trend {p['ema_trend']}: "
            f"win rate {p['win_rate_pct']}% จาก {p['samples']} ครั้งที่ผ่านมา"
            for p in relevant_patterns
        )
    else:
        pattern_block = "(ยังไม่มีข้อมูลผลงานในอดีตแบบเป๊ะๆ สำหรับ setup แบบนี้)"

    current_structure_event = (indicator_snapshot.get("smc") or {}).get("structure_event")
    try:
        structure_patterns = [
            p for p in signal_log.get_structure_patterns() if p["structure_event"] == current_structure_event
        ] if current_structure_event else []
    except Exception:
        structure_patterns = []

    if structure_patterns:
        structure_pattern_block = "\n".join(
            f"- structure_event={p['structure_event']}, mtf_confluence={p['mtf_confluence']}: "
            f"win rate {p['win_rate_pct']}% จาก {p['samples']} ครั้งที่ผ่านมา"
            for p in structure_patterns
        )
    else:
        structure_pattern_block = "(ยังไม่มีข้อมูลผลงานในอดีตพอสำหรับ SMC structure event แบบนี้)"

    try:
        mistakes = signal_log.get_recent_mistakes(symbol, limit=3)
    except Exception:
        mistakes = []

    if mistakes:
        mistakes_block = "\n".join(
            f"- ครั้งก่อน {m['action']} ตอน RSI={m['rsi_state']}/EMA={m['ema_trend']}"
            + (f", SMC structure={m['structure_event']}/mtf={m['mtf_confluence']}" if m.get("structure_event") else "")
            + f" แล้วแพ้ เพราะตอนนั้นให้เหตุผลว่า: \"{(m['reason'] or '')[:150]}\""
            for m in mistakes
        )
        mistakes_block += "\n**ห้ามทำผิดซ้ำแบบเดิม — ถ้าสถานการณ์ตอนนี้คล้ายกับครั้งที่แพ้ (รวมถึง SMC structure/mtf ถ้ามี) ให้ลดความมั่นใจหรือปฏิเสธ**"
    else:
        mistakes_block = f"(ยังไม่มีประวัติไม้ที่แพ้สำหรับ {symbol})"

    # Use Machine Learning model to predict win probability based on all indicators
    ml_prob_buy = ml_model.predict_win_probability("buy", indicator_snapshot)
    ml_prob_sell = ml_model.predict_win_probability("sell", indicator_snapshot)
    
    ml_block = "Machine Learning Prediction (Win Probability):\n"
    if ml_prob_buy is not None:
        ml_block += f"- ถ้า Buy: ความน่าจะเป็นที่จะชนะ = {ml_prob_buy}%\n"
        ml_block += f"- ถ้า Sell: ความน่าจะเป็นที่จะชนะ = {ml_prob_sell}%\n"
    else:
        ml_block += "(โมเดล ML ยังมีข้อมูลไม่พอเทรน หรือยังไม่ได้เทรน)\n"

    user_message = f"""สินทรัพย์: {symbol}
ค่า indicator ปัจจุบัน:
{json.dumps(indicator_snapshot, ensure_ascii=False, indent=2)}

{ml_block}

ความรู้จากตำรา (RAG):
{knowledge_block}

สถิติจาก SQL แบบดั้งเดิม:
{pattern_block}

ผลงานจริงในอดีตของ SMC structure event แบบเดียวกับตอนนี้ (structure_event={current_structure_event}):
{structure_pattern_block}

บทเรียนจากความผิดพลาดที่เคยเกิดกับสินทรัพย์นี้:
{mistakes_block}

วิเคราะห์และตอบเป็น JSON ตามรูปแบบที่กำหนด"""

    # Data-collection mode: the Technical Agent is the real bottleneck —
    # it keeps returning bias="none" on conflicting signals, so nothing ever
    # reaches the (already-relaxed) CEO. When collecting data we ask it to
    # commit to a direction anyway (lower confidence), so trades actually get
    # placed and the learning loop gets fed. This lowers signal quality on
    # purpose; turn COLLECT_MODE off once enough trades are collected.
    if os.environ.get("COLLECT_MODE") == "1":
        user_message += (
            "\n\n[โหมดเก็บข้อมูล] ตอนนี้กำลังเก็บสถิติเพื่อฝึกระบบ — ห้ามตอบ bias=\"none\" "
            "ถ้าสัญญาณขัดแย้งกัน ให้เลือกฝั่งที่มีน้ำหนักมากกว่าเล็กน้อย (buy หรือ sell) "
            "แล้วตั้ง confidence ต่ำๆ (30-50%) สะท้อนความไม่แน่นอน — ขอแค่ให้มีทิศทางเสมอ"
        )

    raw, provider_name = _call_first_available(SYSTEM_PROMPT, user_message)

    if raw is None:
        # Fallback rule-engine for when every cloud LLM is unavailable. It must
        # NOT drive the strategy: the Stoch-swing entry gate already ran in
        # mtf_engine (engagement + direction), so this only needs to give a
        # conservative bias from the REAL indicators actually present.
        #
        # Two old bugs made it dominate and churn: (1) it read a "stoch_k"
        # field that indicators.py never produces, so it was always 50 and the
        # "Stoch (9,3,3) K=50.0" reason was fake; (2) confidence 75 sailed past
        # the CEO threshold every cycle. Now it uses the genuine rsi/ema fields,
        # requires real confluence, returns bias="none" when signals conflict,
        # and caps confidence low so marginal setups get filtered out.
        rsi = float(indicator_snapshot.get("rsi", 50))
        ema_trend = str(indicator_snapshot.get("ema_trend", "")).lower()
        rsi_state = str(indicator_snapshot.get("rsi_state", "")).lower()
        engulf = str(indicator_snapshot.get("engulfing", "")).lower()
        pin = str(indicator_snapshot.get("pin_bar", "")).lower()

        # If the mtf engine already validated a full Stoch swing (OS→OB→OS +
        # engulfing + HTF aligned), TRUST it: the swing IS the strategy, so the
        # fallback should follow its direction rather than re-judging from raw
        # indicators and vetoing a good setup to "none" (which is what happened
        # when the LLM was down). Only stoch_swing engagements get this trust;
        # weaker SMC-zone touches still go through the confluence check below.
        ck = (chosen or {}).get("kind")
        cdir = (chosen or {}).get("dir")
        if ck == "stoch_swing" and cdir in ("bullish", "bearish"):
            fallback_bias = "buy" if cdir == "bullish" else "sell"
            fallback_reason = (
                f"Rule-Engine ตามสวิง Stoch ที่ mtf ยืนยันแล้ว ({chosen.get('name')} "
                f"score {chosen.get('score')}) → {fallback_bias} (ไม่มี LLM ว่าง)"
            )
            bull = bear = 0  # for the (unused) log fields below
        else:
            bull = (ema_trend == "up") + ("bull" in engulf) + ("hammer" in pin) + (rsi < 45)
            bear = (ema_trend == "down") + ("bear" in engulf) + ("shooting" in pin) + (rsi > 55)
            if bull >= 2 and bull > bear:
                fallback_bias = "buy"
            elif bear >= 2 and bear > bull:
                fallback_bias = "sell"
            else:
                fallback_bias = "none"  # no clear confluence → don't force a trade
            fallback_reason = (
                f"Rule-Engine สำรอง (ไม่มี LLM ว่าง): EMA {ema_trend}, RSI {rsi:.0f} ({rsi_state}), "
                f"engulf={engulf or '-'} → {fallback_bias} [confluence bull={bull}/bear={bear}]"
            )

        # A validated Stoch swing is a real setup → give it enough confidence to
        # clear the CEO threshold; a raw-indicator confluence guess stays modest.
        if fallback_bias == "none":
            fb_conf = 0
        elif ck == "stoch_swing":
            fb_conf = 60
        else:
            fb_conf = 45
        return {
            "bias": fallback_bias,
            "confidence": fb_conf,
            "reason": fallback_reason[:120],
            "indicators": indicator_snapshot,
            "knowledge_used": knowledge_chunks,
            "knowledge_cited": False,
            "knowledge_note": "Rule-Engine สำรอง — ใช้ indicator จริงล้วน ไม่ใช่ค่า Stoch ปลอม",
            "provider": "rule_engine_fallback"
        }

    cleaned = raw.strip().strip("```json").strip("```")
    try:
        result = json.loads(cleaned)
        if not isinstance(result, dict) or result.get("bias") not in ("buy", "sell", "none"):
            # Valid JSON, but not the schema we asked for (e.g. the local
            # model invented its own field names like "trade_signal") —
            # treat the same as a parse failure instead of silently
            # leaving "bias" missing/None downstream.
            raise json.JSONDecodeError("unexpected schema", cleaned, 0)

        bias = result.get("bias", "none").lower()
        conf = result.get("confidence", 0)

        # --- Technical Agent Feedback Loop ---
        ml_prob = ml_prob_buy if bias == "buy" else (ml_prob_sell if bias == "sell" else None)
        if ml_prob is not None and ml_prob < 50 and bias != "none":
            conf = max(0, conf - 30)  # ลงโทษลดความมั่นใจทันที
            result["reason"] = result.get("reason", "") + f" (ระบบปรับลดความเชื่อมั่น -30% อัตโนมัติเนื่องจาก ML ประเมินโอกาสชนะต่ำเพียง {ml_prob}%)"
            result["confidence"] = conf

    except json.JSONDecodeError:
        result = {
            "bias": "none",
            "confidence": 0,
            "reason": f"LLM ({provider_name}) ตอบไม่เป็น JSON ตามรูปแบบที่กำหนด: {raw[:200]}",
            "knowledge_cited": False,
            "knowledge_note": "parse ไม่ผ่าน เลยไม่รู้ว่าใช้ความรู้อะไรไป",
        }

    result.setdefault("knowledge_cited", False)
    result.setdefault("knowledge_note", "LLM ไม่ได้ระบุ knowledge_note มา")

    # --- Anti-hallucination grounding guard ---
    # The LLM can claim it "cited the textbook" even when NOTHING relevant
    # was retrieved from RAG this call. That's a fabricated citation. We
    # know the ground truth (knowledge_chunks), so override any citation
    # that isn't actually backed by retrieved text.
    if not knowledge_chunks:
        if result.get("knowledge_cited"):
            result["knowledge_note"] = (
                "⚠️ อ้างว่าใช้ตำราแต่รอบนี้ไม่มีความรู้จาก RAG เลย — ระบบตีเป็น 'ไม่ได้อ้างจริง' (กัน hallucination)"
            )
        result["knowledge_cited"] = False
        result["knowledge_grounded"] = False
    else:
        # There WAS retrieved knowledge; mark whether the LLM's note is
        # grounded (references the actual retrieved snippets) for the CEO
        # to weigh. We keep the LLM's claim but flag that chunks existed.
        result["knowledge_grounded"] = True

    result["indicators"] = indicator_snapshot
    result["knowledge_used"] = knowledge_chunks[:2]
    result["analyzed_by"] = provider_name

    return result
