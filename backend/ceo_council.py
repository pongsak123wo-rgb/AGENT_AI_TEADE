"""CEO Agent — votes on whether to act on a signal using Gemini API.
The approval threshold self-adjusts (45-65%) based on the system's
recent real win rate — not a fixed bar. The Risk Agent's veto is checked
before any vote is taken and overrides the council unconditionally — no
vote can override a risk rejection.

PROVIDERS is [gemini] only — the local Ollama fallback (typhoon2) was
removed; the system now relies on the Gemini API for all live trading
decisions. If Gemini errors or is in circuit-breaker cooldown (e.g.
quota exhausted), the CEO abstains rather than falling back to a weaker
local model — better no vote than a low-quality one.
"""
from __future__ import annotations

import json
import os

import cost_model
import llm_circuit_breaker
import signal_log
from llm_providers import cerebras, gemini, groq

# Gemini first (cheapest/best), then Cerebras + Groq as fallbacks. Without
# fallbacks the CEO cast ZERO votes whenever Gemini's free-tier quota hit 0
# (429) — every setup died at "no CEO provider available" and the system
# never traded. The cost guard still throttles Gemini spend; these are the
# free providers it falls back to.
PROVIDERS = [gemini, cerebras, groq]

SYSTEM_PROMPT = """คุณคือ CEO Agent ในทีมเทรด ตัดสินใจว่าจะ "อนุมัติ" หรือ "ปฏิเสธ" สัญญาณเทรดที่เสนอมา
โดยพิจารณาจากรายงานของ Technical Analysis, News Agent, Risk Management, และ "ค่า indicator ดิบ" ที่ให้มาด้วยตัวเอง
(ไม่ใช่แค่เชื่อสรุปของ Technical Agent อย่างเดียว — ตรวจสอบเองว่า indicator เห็นด้วยกับ bias ที่เสนอจริงไหม
เช่น ถ้า trend_confluence เป็น false หรือ macd_cross ขัดกับ bias ควรลดความเชื่อมั่นหรือปฏิเสธ)
ค่า indicator ดิบยังมี `smc` (CHoCH/BOS, order block, FVG, liquidity sweep) และ `elliott_wave` (rule_fit_confidence) ด้วย — ถ้า smc.structure_event เป็น CHoCH ขัดกับ bias ที่เสนอ ให้สงสัยอย่างมากเพราะ CHoCH คือสัญญาณกลับตัว
คุณจะได้รับค่า **spread จริง** และ **break-even distance** (ระยะที่ราคาต้องวิ่งก่อนจะคุ้มทุนหลังหัก spread+slippage จริง)
คุณยังได้รับ **knowledge_cited/knowledge_note** จาก Technical Agent — ต้องตรวจสอบเองว่า Technical Agent อ้างความรู้จริงหรือมั่วขึ้นมา:
- ถ้า knowledge_cited=true แต่ knowledge_note ดูคลุมเครือ/ไม่เจาะจง (เช่นบอกแค่ "สอดคล้องกับตำรา" ไม่มีเนื้อหา) ให้สงสัยว่าเป็นการอ้างลอยๆ และลดความเชื่อมั่น
- ถ้า knowledge_cited=false ไม่ใช่เรื่องผิด (บางสถานการณ์ไม่มีตำราตรงจริงๆ) แต่ให้พิจารณาด้วยว่า indicator confluence แข็งพอจะชดเชยไหม
**กฎเหล็ก:**
1. หากตลาดเป็น Sideway (กรอบ Bollinger Bands แคบ หรือไม่มี Trend ชัดเจน) ให้ "ปฏิเสธ" ทันที
2. ตรวจสอบ ML Probability หากมีค่าน้อยกว่า 50% ให้ "ปฏิเสธ"
3. ถ้า tp_covers_costs เป็น false (TP ไม่ห่างจาก break-even พอ) ให้ "ปฏิเสธ" — กำไรจะถูกกินด้วยต้นทุนจริง
4. พิจารณา Risk/Reward Ratio หากประเมินแล้วจุดเข้าไม่คุ้มค่า ให้ "ปฏิเสธ"
5. ถ้าสงสัยว่า Technical Agent อ้างความรู้แบบลอยๆไม่มีเนื้อหาจริง ให้ระบุในเหตุผลและลดความเชื่อมั่น
6. คุณจะได้รับ "News sentiment" ของสกุลเงินที่เกี่ยวข้อง (จากพาดหัวข่าวจริง วิเคราะห์โดย LLM อีกตัว ไม่ใช่ตัวเลข beat/miss ที่แม่นยำ — ใช้เป็นความเห็นประกอบ ไม่ใช่หลักฐานหนัก)
   ถ้าจะ buy แต่สกุลเงินอ้างอิงมี sentiment เป็น bearish ชัดเจน (หรือกลับกัน) ให้ลดความเชื่อมั่นลง เพราะกำลังเข้าไม้ทวนข่าวล่าสุด
7. **สำคัญที่สุด — คุณจะได้รับ "ผลงานจริงในอดีต (📊)" ของ setup แบบเดียวกันเป๊ะ (action/RSI/EMA เดียวกัน) จากไม้ที่ปิดจริง**
   นี่คือหลักฐานที่หนักที่สุด เพราะเป็นสิ่งที่เกิดขึ้นจริงกับระบบนี้เอง ไม่ใช่ทฤษฎีหรือ indicator ที่อาจหลอกได้:
   - ถ้า setup นี้เคยชนะ >55% จากหลายไม้ → นี่คือจุดแข็ง ให้เชื่อมั่นมากขึ้น
   - ถ้า setup นี้เคยชนะ <40% หรือ expectancy ติดลบ → ให้ "ปฏิเสธ" หรือระวังมาก แม้ indicator จะสวย เพราะพิสูจน์แล้วว่ามันไม่เวิร์ก
   - ถ้ายังไม่มีประวัติ/ไม้น้อย → ตัดสินจาก indicator + context ตามปกติ แต่อย่าเชื่อมั่นเกินไป
**สำคัญ: reason ห้ามเกิน 150 ตัวอักษร** เลือกพูดแค่เหตุผลที่หนักแน่นที่สุด 2-3 ข้อ ห้ามพยายามพูดถึงทุก field ที่ได้รับมา — ตอบ JSON ให้ครบและปิดวงเล็บให้จบเสมอ ห้ามตอบยาวจนตัดกลางคันเด็ดขาด
ตอบเป็น JSON เท่านั้น รูปแบบ: {"vote": "approve"|"reject", "reason": "เหตุผลสั้นๆ รวมถึงความเห็นต่อ knowledge_note ของ Technical Agent"}"""


def _build_track_record(symbol: str, bias: str, rsi_state: str | None, ema_trend: str | None) -> str:
    """ดึงผลงานจริงในอดีตของ 'setup แบบเดียวกันเป๊ะ' จาก signal_log
    มาป้อนให้ CEO reasoning — นี่คือจุดที่ทำให้ CEO ตัดสินใจแบบ AI จริง
    (มีหลักฐานว่า setup นี้เคยได้ผลไหม) ไม่ใช่แค่ดู indicator ดิบอย่างเดียว
    หรือเชื่อ rule ตายตัว ทุกตัวเลขมาจากไม้ที่ปิดจริงในฐานข้อมูล"""
    lines = []

    # 1) win rate ของคอมโบ RSI×EMA เป๊ะๆ (แยก buy/sell)
    try:
        matrix = signal_log.get_rsi_ema_matrix().get("cells", [])
        match = next(
            (c for c in matrix if c["action"] == bias and c["rsi_state"] == rsi_state and c["ema_trend"] == ema_trend),
            None,
        )
        if match and match["samples"] >= 3:
            lines.append(
                f"- setup นี้เป๊ะๆ ({bias} ตอน RSI={rsi_state}/EMA={ema_trend}) "
                f"ในอดีตชนะ {match['win_rate_pct']}% จาก {match['samples']} ไม้ที่ปิดจริง"
            )
        elif match:
            lines.append(
                f"- setup นี้เพิ่งมี {match['samples']} ไม้ (ยังน้อยไป ชนะ {match['win_rate_pct']}% เชื่อได้ไม่มาก)"
            )
        else:
            lines.append(f"- setup นี้ ({bias}/RSI={rsi_state}/EMA={ema_trend}) ยังไม่เคยมีประวัติปิดไม้เลย")
    except Exception:
        pass

    # 2) win rate ของ symbol+direction นี้โดยรวม
    try:
        dir_stats = signal_log.get_direction_stats().get("by_symbol", [])
        sym_match = next((r for r in dir_stats if r["symbol"] == symbol and r["action"] == bias), None)
        if sym_match and sym_match["closed"] >= 3:
            lines.append(
                f"- {symbol} ฝั่ง {bias} โดยรวมชนะ {sym_match['win_rate_pct']}% "
                f"(ชนะ {sym_match['win']}/แพ้ {sym_match['loss']})"
            )
    except Exception:
        pass

    # 3) expectancy (R) ของ symbol นี้ — กำไรสุทธิจริงในแง่ R
    try:
        sym_exp = signal_log.get_symbol_expectancy_all()
        exp_match = next((r for r in sym_exp if r["symbol"] == symbol), None)
        if exp_match and exp_match["samples"] >= 3:
            sign = "กำไร" if exp_match["expectancy_r"] >= 0 else "ขาดทุน"
            lines.append(
                f"- {symbol} expectancy = {exp_match['expectancy_r']}R/ไม้ ({sign}สุทธิจริงจาก {exp_match['samples']} ไม้)"
            )
    except Exception:
        pass

    if not lines:
        return "(ยังไม่มีประวัติเทรดพอจะอ้างอิง — ตัดสินจาก indicator + context ล้วน)"
    return "\n".join(lines)


def _build_prompt(technical: dict, news: dict, risk: dict, snapshot: dict) -> str:
    indicators = technical.get("indicators", {})
    symbol = snapshot["symbol"]
    price = snapshot["price"]
    spread = snapshot.get("spread", 0)

    # Preliminary SL/TP using the SAME adaptive formula CEOAgent.decide uses
    # for the real order, so the break-even check reflects the actual stop.
    atr = indicators.get("atr") or price * 0.001
    sl_dist = max(atr * 2.0, spread * 8.0, price * 0.0012)
    tp_dist = sl_dist * 2.0
    if technical["bias"] == "buy":
        sl, tp = price - sl_dist, price + tp_dist
    else:
        sl, tp = price + sl_dist, price - tp_dist

    be = cost_model.breakeven_info(symbol, technical["bias"], price, sl, tp, spread)

    track_record = _build_track_record(
        symbol, technical["bias"], indicators.get("rsi_state"), indicators.get("ema_trend")
    )

    sentiment = news.get("sentiment") or {}
    if sentiment:
        sentiment_block = ", ".join(f"{cur}={s['sentiment']} ({s.get('reason', '')[:80]})" for cur, s in sentiment.items())
    else:
        sentiment_block = "(ไม่มีข้อมูล sentiment)"

    cost_block = (
        f"spread ปัจจุบัน {be['spread']}, slippage เฉลี่ยที่เคยเจอจริง {be['avg_slippage']} "
        f"(จาก {be['cost_samples']} ไม้ที่ execute จริง), ค่าคอมเฉลี่ย {be['avg_commission_per_trade']} ต่อไม้ → "
        f"break-even distance ≈ {be['breakeven_distance']} (ราคาต้องวิ่งเกินนี้ก่อนจะเริ่มได้กำไรจริง) "
        f"เทียบกับ TP ที่ตั้งไว้ {be['tp_distance']} → tp_covers_costs = {be['tp_covers_costs']}"
    )

    return f"""สินทรัพย์: {symbol} ราคา {price}

Technical Analysis เสนอ: bias={technical['bias']}, confidence={technical.get('confidence')}%, เหตุผล: {technical.get('reason')}
ค่า indicator ดิบ (ตรวจสอบเอง): {json.dumps(indicators, ensure_ascii=False)}
Technical Agent อ้างความรู้จาก PDF: knowledge_cited={technical.get('knowledge_cited')}, knowledge_note="{technical.get('knowledge_note')}"
News Agent: ปลอดภัยที่จะเทรด = {news.get('safe', True)}
News sentiment ของสกุลเงินที่เกี่ยวข้อง (จากพาดหัวข่าวจริง): {sentiment_block}
Risk Management: อนุมัติ = {risk['approved']}, เหตุผล: {risk['reason']}
ต้นทุนการเทรดจริงและ break-even: {cost_block}

📊 ผลงานจริงในอดีตของ setup แบบนี้ (จากไม้ที่ปิดจริงในระบบ ไม่ใช่ทฤษฎี):
{track_record}

ลงคะแนนว่าควรเปิดเทรดนี้หรือไม่ โดยเช็ค indicator ดิบด้วยตัวเองว่าสนับสนุน bias ที่เสนอจริงไหม, tp_covers_costs คุ้มจริงไหม, knowledge_note ของ Technical Agent ดูสมเหตุสมผลหรือลอยๆ, และที่สำคัญ **ให้น้ำหนักกับผลงานจริงในอดีต (📊) ด้วย** — ถ้า setup แบบเดียวกันเป๊ะเคยแพ้บ่อยหรือ expectancy ติดลบ ให้ระวังเป็นพิเศษแม้ indicator จะดูดี"""


def _ask(provider, prompt: str, symbol: str | None = None) -> dict | None:
    used_name = provider.NAME

    # ถ้า provider อยู่ใน cooldown (fail ซ้ำ เช่น quota=0) → งดออกเสียง
    # ไม่มี local fallback อีกต่อไป (ตัด typhoon2 ออก จะใช้ Gemini API ล้วน)
    if llm_circuit_breaker.is_in_cooldown(provider.NAME):
        return None

    try:
        raw = provider.generate(SYSTEM_PROMPT, prompt)
        if raw is not None:
            llm_circuit_breaker.record_success(provider.NAME)
    except Exception as e:
        llm_circuit_breaker.record_failure(provider.NAME)
        return None  # provider error — งดออกเสียง

    if raw is None:
        return None  # no API key configured — abstain (not counted in votes)

    try:
        parsed = json.loads(raw.strip().strip("```json").strip("```"))
    except (json.JSONDecodeError, AttributeError):
        parsed = {"vote": "reject", "reason": f"ตอบไม่เป็น JSON: {raw[:150]}"}
    parsed["provider"] = used_name
    return parsed


def _build_debate_prompt(base_prompt: str, self_name: str, round1_votes: list[dict]) -> str:
    others = [v for v in round1_votes if v["provider"] != self_name]
    others_text = "\n".join(f"- {v['provider']} โหวต {v['vote']}: {v['reason']}" for v in others) or "(ไม่มีเพื่อนร่วมทีมโหวตได้)"
    return f"""{base_prompt}

--- ความเห็นของ CEO อีก {len(others)} คนในทีม (รอบแรก) ---
{others_text}

หลังจากเห็นความเห็นของเพื่อนร่วมทีมแล้ว คุณยังยืนยันความเห็นเดิมไหม หรือเปลี่ยนใจ?
ถ้ามีเหตุผลที่เพื่อนพูดมาที่คุณมองข้ามไป ให้ปรับความเห็นตามนั้น ถ้าคุณยังเชื่อว่าตัวเองถูก ให้ยืนยันพร้อมอธิบายว่าทำไมเหตุผลของเพื่อนไม่หนักแน่นพอ
**reason ห้ามเกิน 150 ตัวอักษร** ตอบ JSON เหมือนเดิม: {{"vote": "approve"|"reject", "reason": "เหตุผลสั้นๆ ระบุด้วยว่าเปลี่ยนใจหรือยืนยันเดิมและทำไม"}}"""


def _debate_round(prompt: str, round1_raw: list[tuple], symbol: str | None = None) -> list[dict]:
    """Round 2 — each provider sees the other two's round-1 votes and
    reasoning, then re-votes. This is the actual 'talking to each other'
    part: round 1 alone is just three independent opinions on the same
    prompt, nobody reacting to anybody.
    """
    round1_votes = [v for _, v in round1_raw if v is not None]
    if len(round1_votes) < 2:
        return round1_votes  # nothing to debate with only 0-1 opinions

    final_votes = []
    for provider, v1 in round1_raw:
        if v1 is None:
            continue
        debate_prompt = _build_debate_prompt(prompt, v1["provider"], round1_votes)
        v2 = _ask(provider, debate_prompt, symbol)
        if v2:
            v2["round1_vote"] = v1["vote"]
            v2["changed_mind"] = v2.get("vote") != v1["vote"]
            final_votes.append(v2)
        else:
            v1["changed_mind"] = False
            final_votes.append(v1)
    return final_votes


def decide(technical: dict, news: dict, risk: dict, snapshot: dict) -> dict:
    """Returns {"approved": bool, "votes": [...], "reason": str}"""
    if not risk["approved"]:
        return {"approved": False, "votes": [], "reason": f"Risk veto: {risk['reason']}"}

    if technical["bias"] == "none":
        return {"approved": False, "votes": [], "reason": "ไม่มี technical setup ให้ลงคะแนน"}

    prompt = _build_prompt(technical, news, risk, snapshot)
    symbol = snapshot.get("symbol")
    round1_raw = [(p, _ask(p, prompt, symbol)) for p in PROVIDERS]
    votes = _debate_round(prompt, round1_raw, symbol)
    total_votes = len(votes)
    if total_votes == 0:
        return {
            "approved": False,
            "votes": [],
            "reason": "ไม่มี CEO provider ใดตั้งค่า API key ไว้ — ไม่มีเสียงโหวต",
        }

    provider_accuracy = signal_log.get_provider_accuracy()

    weighted_score = 0.0
    total_weight = 0.0

    for v in votes:
        provider = v["provider"]
        accuracy = provider_accuracy.get(provider, 0.5)
        # 50% accuracy = 1.0 weight, 100% = 2.0 weight
        weight = accuracy * 2.0
        total_weight += weight
        
        v["trust_score"] = round(accuracy * 100, 1)

        if v["vote"] == "approve":
            weighted_score += weight

    # Self-adjusting bar: raise the approval threshold when the system's
    # own recent results are bad, lower it when they're good — the
    # council gets stricter with itself after losing streaks instead of
    # keeping a fixed 50% bar forever.
    stats = signal_log.get_stats()
    recent_win_rate = stats.get("win_rate_pct")
    if recent_win_rate is None:
        threshold = 0.5
    elif recent_win_rate < 40:
        threshold = 0.65
    elif recent_win_rate > 65:
        threshold = 0.45
    else:
        threshold = 0.5

    # Data-collection mode (COLLECT_MODE=1): the learning mechanisms (ML,
    # win-rate patterns, RSI×EMA matrix) need dozens of closed trades before
    # they can learn anything, but the live gates are strict enough that few
    # trades ever get placed. This lowers the council's bar so more setups go
    # through — deliberately trading quantity for the data the system needs
    # to start learning. Turn OFF once enough real trades are collected.
    if os.environ.get("COLLECT_MODE") == "1":
        threshold = min(threshold, 0.30)

    score_ratio = (weighted_score / total_weight) if total_weight > 0 else 0.0
    approved = score_ratio >= threshold

    win_rate_note = f"win rate ล่าสุด {recent_win_rate}%" if recent_win_rate is not None else "ยังไม่มีประวัติ"
    changed_count = sum(1 for v in votes if v.get("changed_mind"))
    debate_note = f"{changed_count} เปลี่ยนใจหลังคุยกัน" if changed_count else "ทุกคนยืนความเห็นเดิมหลังคุยกัน"

    if approved:
        return {
            "approved": True,
            "votes": votes,
            "reason": f"เสียงข้างมาก (Weighted Score: {weighted_score:.1f}/{total_weight:.1f}, threshold {threshold:.0%}, {win_rate_note}, {debate_note}) อนุมัติ",
        }
    else:
        return {
            "approved": False,
            "votes": votes,
            "reason": f"เสียงข้างมาก (Weighted Score: {weighted_score:.1f}/{total_weight:.1f}, threshold {threshold:.0%}, {win_rate_note}, {debate_note}) ไม่อนุมัติ หรือข้อมูลขัดแย้งกัน",
        }
