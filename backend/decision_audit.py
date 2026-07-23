"""Decision audit — a transparent, code-computed factor sheet for every
trade, so a decision is never a pure LLM black box.

Instead of trusting the LLM to explain itself (which it can fabricate),
we build the explanation from the SAME real numbers the pipeline already
computed: trend consensus, the zone that triggered, RSI/EMA/MACD, SMC
structure, ML probability, and the risk verdict. Each factor is tagged
FOR / AGAINST / NEUTRAL relative to the proposed direction, with a short
human-readable detail. This is 100% deterministic and auditable — you can
always see exactly which facts supported the trade and which contradicted
it, next to whatever the LLM said.
"""
from __future__ import annotations

# Elliott only carries weight when its wave count actually satisfies
# Elliott's hard rules this well. Below it, the count is too loose to be
# evidence and is reported as NEUTRAL instead of nudging the decision.
ELLIOTT_MIN_FIT = 70.0


def _stance_from_dir(direction: str | None, bias: str) -> str:
    if not direction or direction == "none":
        return "neutral"
    want = "bullish" if bias == "buy" else "bearish"
    if direction in ("buy", "sell"):
        return "for" if direction == bias else "against"
    return "for" if direction == want else "against"


def build(bias: str, indicators: dict, mtf: dict | None, risk: dict, symbol: str | None = None) -> dict:
    """Returns {factors: [{name, stance, detail}], score, summary}."""
    factors: list[dict] = []

    def add(name, stance, detail):
        factors.append({"name": name, "stance": stance, "detail": detail})

    # --- HTF trend consensus ---
    if mtf and mtf.get("trend"):
        overall = mtf["trend"]["overall"]
        per = mtf["trend"].get("per_tf", {})
        want = "bullish" if bias == "buy" else "bearish"
        stance = "for" if overall == want else "against" if overall in ("bullish", "bearish") else "neutral"
        add("เทรน H1/H4/D1", stance, f"รวม={overall} (H1={per.get('H1')}/H4={per.get('H4')}/D1={per.get('D1')})")

    # --- Zone that triggered ---
    if mtf and mtf.get("pairs"):
        fired = next((p for p in mtf["pairs"] if p.get("fired")), None)
        if fired and fired.get("fired_zone"):
            z = fired["fired_zone"]
            add("โซนที่แตะ", _stance_from_dir(z.get("dir"), bias),
                f"{fired['entry_tf']} แตะ {z['kind']} ของ {fired['structure_tf']} ({z['low']}–{z['high']})")

    # --- RSI ---
    rsi_state = indicators.get("rsi_state")
    rsi = indicators.get("rsi")
    if rsi_state:
        if rsi_state == "oversold":
            st = "for" if bias == "buy" else "against"
        elif rsi_state == "overbought":
            st = "for" if bias == "sell" else "against"
        else:
            st = "neutral"
        add("RSI", st, f"{rsi} ({rsi_state})")

    # --- EMA trend ---
    ema = indicators.get("ema_trend")
    if ema and ema != "neutral":
        st = "for" if (bias == "buy" and ema == "up") or (bias == "sell" and ema == "down") else "against"
        add("EMA trend", st, ema)

    # --- MACD cross ---
    macd = indicators.get("macd_cross")
    if macd and macd != "none":
        st = "for" if (bias == "buy" and macd == "bullish") or (bias == "sell" and macd == "bearish") else "against"
        add("MACD cross", st, macd)

    # --- SMC structure ---
    smc = indicators.get("smc") or {}
    ev = smc.get("structure_event")
    if ev and ev != "none":
        add(f"SMC {ev}", _stance_from_dir(smc.get("structure_direction"), bias),
            smc.get("structure_detail", "")[:60])

    # --- Price action (pin bar / engulfing) ---
    for key, label in (("pin_bar", "Pin bar"), ("engulfing", "Engulfing")):
        pa = indicators.get(key)
        if pa and pa != "none":
            pa_dir = "bullish" if pa.startswith("bullish") else "bearish"
            add(label, _stance_from_dir(pa_dir, bias), pa)

    # --- Elliott Wave (only counts when the count actually fits the rules) ---
    # Elliott is subjective: a low rule_fit_confidence means the wave count
    # barely satisfies Elliott's own hard rules, so treating it as evidence
    # would be reading noise. Below the threshold it's reported but stays
    # NEUTRAL — it never pushes a trade for or against.
    ew = indicators.get("elliott_wave") or {}
    if ew.get("ready"):
        fit = ew.get("rule_fit_confidence")
        ew_dir = ew.get("direction")
        pos = (ew.get("current_position") or "")[:45]
        if fit is not None and fit >= ELLIOTT_MIN_FIT and ew_dir in ("up", "down"):
            wave_dir = "bullish" if ew_dir == "up" else "bearish"
            add("Elliott Wave", _stance_from_dir(wave_dir, bias), f"fit {fit}% · {ew_dir} · {pos}")
        else:
            add("Elliott Wave", "neutral",
                f"fit {fit}% < {ELLIOTT_MIN_FIT}% — นับคลื่นไม่แม่นพอ ไม่นับน้ำหนัก")

    # --- ML probability ---
    ind = indicators
    ml = ind.get("ml_prob_buy") if bias == "buy" else ind.get("ml_prob_sell")
    if ml is not None:
        add("ML win prob", "for" if ml >= 50 else "against", f"{ml}%")

    # --- COT: where large speculators are actually positioned ---
    # The only factor here that isn't derived from price history, so it's
    # worth its own line even though it's weekly/lagged.
    if symbol:
        try:
            import cot_report
            c = cot_report.get_bias(symbol)
            if c.get("available") and c["bias"] != "neutral":
                add("COT รายใหญ่", _stance_from_dir(c["bias"], bias),
                    f"{c['bias']} score {c['score']:+.3f} ({c['trend']}, {c['date']})")
            elif c.get("available"):
                add("COT รายใหญ่", "neutral", f"ไม่เอียงชัด (score {c['score']:+.3f})")
        except Exception:
            pass

    # --- Risk verdict ---
    add("Risk", "for" if risk.get("approved") else "against", (risk.get("reason") or "")[:60])

    fors = sum(1 for f in factors if f["stance"] == "for")
    againsts = sum(1 for f in factors if f["stance"] == "against")
    score = fors - againsts
    summary = f"สนับสนุน {fors} · ค้าน {againsts} · สุทธิ {'+' if score >= 0 else ''}{score}"

    return {"factors": factors, "for": fors, "against": againsts, "score": score, "summary": summary}
