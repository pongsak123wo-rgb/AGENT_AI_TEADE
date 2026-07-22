"""Zone Watch — the cheap "watch and wait" layer that decides WHEN it's
worth spending an LLM call.

Instead of asking the (paid) LLM to analyze every symbol every few
seconds, we first compute — with pure math, no LLM — the key price zones
a real trader would mark on the chart, across MULTIPLE timeframes, then
only escalate to the LLM when price actually reaches one of them (or a
fresh structure break just happened). Most of the time price is nowhere
near a meaningful level, so most cycles cost nothing.

Multi-timeframe: zones are collected from BOTH the working timeframe
(M1 candles) AND the higher timeframe (H1 candles) when available, each
tagged with its source TF. An H1 order block is a much heavier level than
an M1 one, so the tf tag lets the LLM (and sizing) weight them
differently later.

Zone kinds detected per timeframe:
- order_block   — last opposite candle before an impulse (SMC)
- fvg           — unmitigated fair value gap (SMC imbalance)
- equal_level   — liquidity pool (price tapped ~same level 2x)
- support / resistance — from indicator swing levels

Each zone is {tf, kind, low, high, dir}. `dir` is "bullish"/"bearish"/
None — which side the zone favors if it holds.
"""
from __future__ import annotations

import smc_analysis


def _zones_from_smc(smc: dict, tf: str) -> list[dict]:
    """Pull tradable zones out of an already-computed SMC result."""
    zones: list[dict] = []
    if not smc or not smc.get("ready"):
        return zones

    ob = smc.get("order_block")
    if ob and ob.get("low") is not None:
        zones.append({
            "tf": tf, "kind": "order_block",
            "low": round(ob["low"], 5), "high": round(ob["high"], 5),
            "dir": "bullish" if ob["type"] == "bullish_ob" else "bearish",
        })

    fvg = smc.get("fair_value_gap")
    if fvg and not fvg.get("mitigated") and fvg.get("low") is not None:
        zones.append({
            "tf": tf, "kind": "fvg",
            "low": round(fvg["low"], 5), "high": round(fvg["high"], 5),
            "dir": "bullish" if fvg["type"] == "bullish_fvg" else "bearish",
        })

    # equal highs = resistance liquidity, equal lows = support liquidity
    for eh in (smc.get("equal_highs") or []):
        lvl = eh.get("price_a")
        if lvl is not None:
            zones.append({"tf": tf, "kind": "equal_level", "low": lvl, "high": lvl, "dir": "bearish"})
    for el in (smc.get("equal_lows") or []):
        lvl = el.get("price_a")
        if lvl is not None:
            zones.append({"tf": tf, "kind": "equal_level", "low": lvl, "high": lvl, "dir": "bullish"})

    return zones


def build_zones(indicators: dict, m1_candles: dict | None, h1_candles: dict | None) -> list[dict]:
    """Collect key zones across timeframes. `indicators` already carries the
    SMC computed on the primary structure source; we additionally compute
    SMC on the OTHER timeframe so both M1 and H1 zones are represented."""
    zones: list[dict] = []
    primary_tf = indicators.get("structure_timeframe")

    # zones from the SMC already computed in the pipeline (primary TF)
    if primary_tf:
        zones += _zones_from_smc(indicators.get("smc") or {}, primary_tf)

    # compute SMC on the other timeframe too, for true multi-TF coverage
    def _other(cands, tf):
        if cands and len(cands.get("c", [])) >= 20 and tf != primary_tf:
            try:
                return _zones_from_smc(smc_analysis.analyze_smc(cands), tf)
            except Exception:
                return []
        return []

    zones += _other(h1_candles, "H1")
    zones += _other(m1_candles, "M1")

    # plain support/resistance from indicators (whatever TF they came from)
    sup = indicators.get("support")
    res = indicators.get("resistance")
    if sup is not None:
        zones.append({"tf": "IND", "kind": "support", "low": round(sup, 5), "high": round(sup, 5), "dir": "bullish"})
    if res is not None:
        zones.append({"tf": "IND", "kind": "resistance", "low": round(res, 5), "high": round(res, 5), "dir": "bearish"})

    return zones


def check_price_at_zone(price: float, zones: list[dict], atr: float | None, tol_ratio: float = 0.25) -> list[dict]:
    """Return the zones price is currently touching. A zone is 'hit' when
    price is inside [low - tol, high + tol] where tol scales with ATR so
    the tolerance is proportional to the instrument's real volatility."""
    if not zones:
        return []
    tol = (atr or price * 0.0005) * tol_ratio
    hits = []
    for z in zones:
        if (z["low"] - tol) <= price <= (z["high"] + tol):
            dist = 0.0 if z["low"] <= price <= z["high"] else min(abs(price - z["low"]), abs(price - z["high"]))
            hits.append({**z, "distance": round(dist, 5)})
    hits.sort(key=lambda h: h["distance"])
    return hits


def should_engage(price: float, indicators: dict, zones: list[dict], atr: float | None) -> dict:
    """The gate: decide whether this cycle is worth an LLM call.

    Engage (spend the LLM) when EITHER:
    - price is currently at a key zone (multi-TF), OR
    - a fresh structure break (BOS/CHoCH) just printed — that's an
      actionable event on its own even if price isn't sitting in a zone.

    Otherwise: keep watching for free.
    """
    hits = check_price_at_zone(price, zones, atr)

    smc = indicators.get("smc") or {}
    fresh_break = smc.get("ready") and smc.get("structure_event") in ("BOS", "CHoCH")

    if hits:
        top = hits[0]
        reason = f"ราคาแตะโซน {top['kind']} ({top['tf']}) ที่ {top['low']}–{top['high']} — เข้าเงื่อนไขให้ AI วิเคราะห์"
        return {"engage": True, "reason": reason, "zones_hit": hits, "trigger": "zone"}
    if fresh_break:
        reason = f"เพิ่งเกิด {smc['structure_event']} ({indicators.get('structure_timeframe')}) — เข้าเงื่อนไขให้ AI วิเคราะห์"
        return {"engage": True, "reason": reason, "zones_hit": [], "trigger": "structure_break"}

    nearest = check_price_at_zone(price, zones, atr, tol_ratio=99)  # all, to report nearest
    near_txt = ""
    if nearest:
        n = nearest[0]
        near_txt = f" (ใกล้สุด: {n['kind']} {n['tf']} ห่าง {n['distance']})"
    return {"engage": False, "reason": f"ราคายังไม่ถึงโซนสำคัญ — เฝ้าดูต่อ (ฟรี){near_txt}", "zones_hit": [], "trigger": None}
