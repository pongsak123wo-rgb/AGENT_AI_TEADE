"""Multi-timeframe engine — the top-down structure a real trader uses.

Two entry/structure PAIRS (higher TF marks the zone, lower TF times the
entry):
    • H1  structure  ↔  M5  entry   (scalp pair)
    • H4  structure  ↔  M15 entry   (intraday pair)

Trend/bias direction is read from the higher timeframes only:
    • H1, H4, D1  → a trend consensus (bullish / bearish / mixed)

The idea: the higher TF (H1/H4) tells you WHERE the meaningful zones are
and WHICH direction the market is going (via the H1/H4/D1 trend
consensus); the lower TF (M5/M15) is only consulted to see if price has
actually arrived at one of those zones yet — that's the trigger to spend
an LLM call and consider an entry.

All timeframes are resampled from the EA's M1 + H1 streams (see
timeframe.py), so no extra EA export is required — only enough M1/H1
history (≥300 M1 for M15, ≥150 H1 for H4).
"""
from __future__ import annotations

import smc_analysis
import timeframe
import zone_watch

# (structure TF, entry TF, human label)
PAIRS = [
    {"name": "scalp",    "structure": "H1", "entry": "M5"},
    {"name": "intraday", "structure": "H4", "entry": "M15"},
]
TREND_TFS = ["H1", "H4", "D1"]


def _ema(vals: list[float], period: int) -> float | None:
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _trend_of(closes: list[float]) -> str:
    """Robust trend read that works even with few bars: fast vs slow EMA.
    fast>slow = bullish, fast<slow = bearish, near-equal = ranging."""
    if not closes or len(closes) < 5:
        return "unknown"
    fast = _ema(closes, min(8, len(closes)))
    slow = _ema(closes, min(21, len(closes)))
    if fast is None or slow is None:
        return "unknown"
    diff = (fast - slow) / slow if slow else 0
    if diff > 0.0003:
        return "bullish"
    if diff < -0.0003:
        return "bearish"
    return "ranging"


def build_timeframes(m1: dict | None, h1: dict | None, d1: dict | None = None,
                     h4: dict | None = None) -> dict:
    """Resample the EA's M1/H1 into every TF the engine needs. When a native
    D1/H4 stream is supplied (mt5_direct provides real Daily and 4-hour bars),
    use it instead of the coarse H1 resample: H1→H4 only yields ~37 bars from
    150 H1 (too few for the Stoch swing engine to form OB/OS chains), and
    H1→D1 only ~6 bars. Native bars give the higher-TF swing gate real
    structure to read; resample stays as a fallback when they're absent."""
    return {
        "M5":  timeframe.resample(m1, 5),
        "M15": timeframe.resample(m1, 15),
        "H1":  h1,
        "H4":  h4 if (h4 and len(h4.get("c", [])) >= 20) else timeframe.resample(h1, 4),
        "D1":  d1 if (d1 and len(d1.get("c", [])) >= 5) else timeframe.resample(h1, 24),
    }


def _trend_consensus(tfs: dict) -> dict:
    """Trend on each HTF + an overall consensus (majority direction)."""
    per_tf = {}
    votes = {"bullish": 0, "bearish": 0}
    for tf in TREND_TFS:
        c = tfs.get(tf)
        t = _trend_of(c["c"]) if c and c.get("c") else "unknown"
        per_tf[tf] = t
        if t in votes:
            votes[t] += 1
    if votes["bullish"] > votes["bearish"]:
        overall = "bullish"
    elif votes["bearish"] > votes["bullish"]:
        overall = "bearish"
    else:
        overall = "mixed"
    return {"per_tf": per_tf, "overall": overall, "votes": votes}


def analyze(m1: dict | None, h1: dict | None, price: float, atr: float | None,
            d1: dict | None = None, h4: dict | None = None) -> dict:
    """Full multi-TF read. Returns trend consensus, per-pair zone state,
    and a single engage gate (spend an LLM call now?)."""
    import stoch_swing_engine
    tfs = build_timeframes(m1, h1, d1, h4)
    trend = _trend_consensus(tfs)

    pairs_out = []
    engage = False
    engage_reason = None

    for p in PAIRS:
        struct = tfs.get(p["structure"])
        entry = tfs.get(p["entry"])
        entry_price = entry["c"][-1] if entry and entry.get("c") else price

        zones = []
        if struct and len(struct.get("c", [])) >= 20:
            smc = smc_analysis.analyze_smc(struct)
            zones = zone_watch._zones_from_smc(smc, p["structure"])

        hits = zone_watch.check_price_at_zone(entry_price, zones, atr)

        # Higher-TF Stoch (9,3,3) swing direction for THIS pair's structure TF
        # (H1 for scalp, H4 for intraday) — the single source of truth for the
        # pair's tradable direction. A pair may ONLY be entered in the same
        # direction as its own HTF Stoch swing (top-down), never counter-trend.
        # Needs native H4 bars (mt5_direct) for enough structure; if the HTF
        # swing isn't ready, the pair is blocked (strict default).
        htf_up = htf_down = False
        htf_ready = False
        if struct and len(struct.get("c", [])) >= 20:
            htf_swing = stoch_swing_engine.detect_stoch_swings(
                struct.get("h", []), struct.get("l", []), struct.get("c", []))
            htf_up = htf_swing.get("uptrend", {}).get("valid", False)
            htf_down = htf_swing.get("downtrend", {}).get("valid", False)
            htf_ready = htf_swing.get("ready", False)
        htf_dir = "bullish" if htf_up else ("bearish" if htf_down else None)

        # SMC zone hit counts only as CONFLUENCE in the HTF swing direction —
        # never counter-trend. The old code let mixed/unknown consensus or a
        # dir-less zone through, which is exactly how the system took BUYS in a
        # downtrend (price pulling back UP into a resistance zone). Now a zone
        # fires only when its own direction matches the pair's HTF Stoch swing.
        aligned_hit = None
        if htf_dir is not None:
            for hh in hits:
                if hh["dir"] == htf_dir:
                    aligned_hit = hh
                    break

        pair_state = {
            "name": p["name"],
            "structure_tf": p["structure"],
            "entry_tf": p["entry"],
            "entry_price": round(entry_price, 5),
            "zones": zones,
            "zones_hit": hits,
            "fired": aligned_hit is not None,
            "fired_zone": aligned_hit,
            "htf_swing": {"tf": p["structure"], "ready": htf_ready,
                          "uptrend": htf_up, "downtrend": htf_down, "dir": htf_dir},
            "bars": {p["structure"]: len(struct.get("c", [])) if struct else 0,
                     p["entry"]: len(entry.get("c", [])) if entry else 0},
        }
        pairs_out.append(pair_state)

        if aligned_hit and not engage:
            engage = True
            engage_reason = (
                f"{p['entry']} แตะโซน {aligned_hit['kind']} ของ {p['structure']} "
                f"({aligned_hit['low']}–{aligned_hit['high']}) "
                f"ตามทิศ HTF {htf_dir} — เข้าเงื่อนไขให้ AI วิเคราะห์"
            )

        # Check Stoch (9,3,3) Swing Engine + Engulfing trigger per pair
        if not engage and entry and len(entry.get("c", [])) >= 20:
            entry_c = entry.get("c", [])
            entry_h = entry.get("h", [])
            entry_l = entry.get("l", [])
            entry_o = entry.get("o", [])
            stoch_res = stoch_swing_engine.detect_stoch_swings(entry_h, entry_l, entry_c)
            up = stoch_res.get("uptrend", {})
            down = stoch_res.get("downtrend", {})
            if up.get("valid") and htf_up:
                eng_buy = stoch_swing_engine.check_multi_candle_engulfing(entry_h, entry_l, entry_o, entry_c, up.get("os2_index"), side="buy")
                if eng_buy.get("engulfed"):
                    engage = True
                    engage_reason = f"🌊 สวิง Stoch (9,3,3) ขาขึ้น {p['structure']}+{p['entry']} (HTF & LTF L2>L1) + Multi-Bar Engulfing — เข้าเงื่อนไขให้ AI วิเคราะห์"
            elif down.get("valid") and htf_down:
                eng_sell = stoch_swing_engine.check_multi_candle_engulfing(entry_h, entry_l, entry_o, entry_c, down.get("ob2_index"), side="sell")
                if eng_sell.get("engulfed"):
                    engage = True
                    engage_reason = f"🌊 สวิง Stoch (9,3,3) ขาลง {p['structure']}+{p['entry']} (HTF & LTF H2<H1) + Multi-Bar Engulfing — เข้าเงื่อนไขให้ AI วิเคราะห์"

    if not engage:
        # nothing at a zone → free watch cycle
        near = []
        for pr in pairs_out:
            allz = zone_watch.check_price_at_zone(pr["entry_price"], pr["zones"], atr, tol_ratio=99)
            if allz:
                near.append(f"{pr['entry_tf']}→{allz[0]['kind']}({pr['structure_tf']}) ห่าง {allz[0]['distance']}")
        engage_reason = "ราคายังไม่ถึงโซนของคู่ TF ไหน — เฝ้าดูต่อ (ฟรี)" + (f" | ใกล้สุด: {near[0]}" if near else "")

    return {
        "trend": trend,
        "pairs": pairs_out,
        "engage": engage,
        "reason": engage_reason,
    }
