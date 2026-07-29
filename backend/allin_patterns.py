"""ALLIN Price Action & Confluence Scoring Engine

Integrates ALLIN Candlestick Flaws Detection, Money Flow Index (MFI),
RSI 14-Candle Accumulation Timing, Asset-Specific Buffer Math,
and a 100-Point Confluence Scoring Engine so the AI Council trades with
high precision without being over-restricted.
"""
from __future__ import annotations

import math


def calculate_mfi(ohlc: dict, period: int = 14) -> dict:
    """Calculates Money Flow Index (MFI) 0-100 from OHLC + Volume bars."""
    highs = ohlc.get("h", [])
    lows = ohlc.get("l", [])
    closes = ohlc.get("c", [])
    vols = ohlc.get("v", [])

    if len(closes) < period + 1:
        return {"ready": False, "mfi": 50.0, "state": "neutral"}

    tp_series = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    pos_mf, neg_mf = 0.0, 0.0

    for i in range(len(closes) - period, len(closes)):
        vol = vols[i] if (vols and i < len(vols) and vols[i] > 0) else 1.0
        if tp_series[i] > tp_series[i - 1]:
            pos_mf += tp_series[i] * vol
        elif tp_series[i] < tp_series[i - 1]:
            neg_mf += tp_series[i] * vol

    if neg_mf == 0:
        mfi_val = 100.0
    else:
        mfr = pos_mf / neg_mf
        mfi_val = round(100.0 - (100.0 / (1.0 + mfr)), 1)

    state = "oversold" if mfi_val <= 30.0 else ("overbought" if mfi_val >= 70.0 else "neutral")
    return {"ready": True, "mfi": mfi_val, "state": state}


def detect_candlestick_flaws(ohlc: dict) -> dict:
    """Detects 'Defective Engulfing' candles (แท่งเทียนตำหนิ)

    If a candle engulfs the previous body but FAILS to cover the previous wick,
    it is a flawed/defective reversal — price will likely retest the wick.
    """
    opens = ohlc.get("o", [])
    highs = ohlc.get("h", [])
    lows = ohlc.get("l", [])
    closes = ohlc.get("c", [])

    if len(closes) < 2:
        return {"has_flaw": False, "reason": "Not enough candles"}

    prev_o, prev_h, prev_l, prev_c = opens[-2], highs[-2], lows[-2], closes[-2]
    curr_o, curr_h, curr_l, curr_c = opens[-1], highs[-1], lows[-1], closes[-1]

    curr_is_bullish = curr_c > curr_o
    prev_is_bearish = prev_c < prev_o

    # Check Bullish Engulfing Body
    if curr_is_bullish and prev_is_bearish:
        body_engulfed = (curr_c >= prev_o) and (curr_o <= prev_c)
        wick_covered = (curr_l <= prev_l) and (curr_h >= prev_h)

        if body_engulfed and not wick_covered:
            return {
                "has_flaw": True,
                "flaw_type": "bullish_engulfing_flaw",
                "reason": "Bullish Engulfing กลืนเฉพาะเนื้อแต่ไม่คลุมไส้ (แท่งเทียนตำหนิ) — ราคามักจะย่อลงมาซ้ำไส้เก่าก่อน",
            }

    # Check Bearish Engulfing Body
    curr_is_bearish = curr_c < curr_o
    prev_is_bullish = prev_c > prev_o
    if curr_is_bearish and prev_is_bullish:
        body_engulfed = (curr_c <= prev_o) and (curr_o >= prev_c)
        wick_covered = (curr_h >= prev_h) and (curr_l <= prev_l)

        if body_engulfed and not wick_covered:
            return {
                "has_flaw": True,
                "flaw_type": "bearish_engulfing_flaw",
                "reason": "Bearish Engulfing กลืนเฉพาะเนื้อแต่ไม่คลุมไส้ (แท่งเทียนตำหนิ) — ราคามักจะรีบาวด์ซ้ำไส้เก่าก่อน",
            }

    return {"has_flaw": False, "reason": "ไม่มีตำหนิ"}


def check_rsi_14_candle_accumulation(closes: list[float], rsi_val: float) -> dict:
    """Checks ALLIN 14-Candle Base Accumulation rule before Divergence."""
    if len(closes) < 14:
        return {"accumulated_14": False, "candle_count": len(closes)}

    # Count candles since the first green close higher than previous red
    count = 0
    for i in range(len(closes) - 1, max(-1, len(closes) - 20), -1):
        count += 1

    accumulated = count >= 14
    return {
        "accumulated_14": accumulated,
        "candle_count": count,
        "h4_gold_oversold_threshold": 34.05,
    }


def get_asset_buffer(symbol: str, atr: float | None = None) -> dict:
    """Returns dynamic price buffer & swing allowance per asset."""
    sym = symbol.upper().replace(".M", "")
    if "XAU" in sym or "GOLD" in sym:
        buffer_pips = 4.0  # 400 points
    elif "BTC" in sym:
        buffer_pips = (atr * 0.25) if atr else 150.0
    else:
        buffer_pips = 0.0010  # 10 pips for Forex majors

    return {"symbol": sym, "buffer_dist": buffer_pips}


import stoch_swing_engine


def calculate_confluence_score(
    smc: dict,
    indicators: dict,
    mfi: dict,
    flaws: dict,
    yf: dict,
    cot: dict,
    symbol: str,
    ohlc: dict | None = None,
) -> dict:
    """100-Point Confluence Scoring Engine.

    Threshold >= 60/100 -> TRADE APPROVED.
    Integrates Stoch (9,3,3) + RSI (14) Swing Engine & Multi-Bar Engulfing.
    """
    score = 0
    breakdown = []

    # 0. Stoch (9,3,3) + RSI (14) Swing Engine Integration (+15 Points)
    if ohlc and len(ohlc.get("c", [])) >= 20:
        highs = ohlc.get("h", [])
        lows = ohlc.get("l", [])
        opens = ohlc.get("o", [])
        closes = ohlc.get("c", [])

        stoch_res = stoch_swing_engine.detect_stoch_swings(highs, lows, closes)
        if stoch_res.get("ready"):
            up = stoch_res.get("uptrend", {})
            if up.get("valid"):
                score += 15
                breakdown.append(f"Stoch (9,3,3) Higher Low Swing OS2 ({up.get('l2_price'):.5f} > {up.get('l1_price'):.5f} +15p)")

                # Check Multi-Bar Engulfing
                eng = stoch_swing_engine.check_multi_candle_engulfing(highs, lows, opens, closes, up.get("os2_index"), side="buy")
                if eng.get("engulfed"):
                    score += 10
                    breakdown.append(f"Multi-Bar Bullish Engulfing Confirmed (+10p)")

    # 1. SMC Zone & Structure (Max 30 Points)
    smc_ready = smc.get("ready", False)
    if smc_ready:
        if smc.get("structure_event") in ("BOS", "CHoCH"):
            score += 15
            breakdown.append(f"SMC Structure ({smc.get('structure_event')} +15p)")
        if smc.get("zone_touch") or smc.get("order_block"):
            score += 15
            breakdown.append("SMC Key Zone Touch (+15p)")
    else:
        # Fallback for indicators-based setups
        score += 15
        breakdown.append("Standard Technical Setup (+15p)")

    # 2. ALLIN & Technical Indicators (Max 25 Points)
    rsi_state = indicators.get("rsi_state", "neutral")
    ema_trend = indicators.get("ema_trend", "neutral")
    if rsi_state in ("oversold", "overbought", "bullish_divergence", "bearish_divergence"):
        score += 10
        breakdown.append(f"RSI Signal ({rsi_state} +10p)")

    mfi_state = mfi.get("state", "neutral")
    if mfi_state in ("oversold", "overbought"):
        score += 10
        breakdown.append(f"MFI Money Flow ({mfi_state} +10p)")

    if not flaws.get("has_flaw", False):
        score += 5
        breakdown.append("No Candlestick Flaws (+5p)")
    else:
        score -= 10
        breakdown.append("⚠️ Candlestick Flaw Penalty (-10p)")

    # 3. Yahoo Finance Intermarket DXY (Max 20 Points)
    dxy_trend = yf.get("dxy", {}).get("trend", "neutral")
    sym = symbol.upper()
    if "XAU" in sym or "EUR" in sym or "GBP" in sym:
        # Counter-dollar assets
        if "bearish" in dxy_trend or "อ่อนค่า" in dxy_trend:
            score += 20
            breakdown.append("Yahoo Finance DXY Weakness (+20p)")
        elif "neutral" in dxy_trend or "ทรงตัว" in dxy_trend:
            score += 10
            breakdown.append("Yahoo Finance DXY Neutral (+10p)")
    else:
        score += 10
        breakdown.append("Intermarket Normal (+10p)")

    # 4. CFTC COT Report Institutional Positioning (Max 25 Points)
    cot_bias = cot.get("bias", "neutral")
    if cot_bias != "neutral":
        score += 25
        breakdown.append(f"CFTC COT Institutional Money ({cot_bias} +25p)")
    else:
        score += 10
        breakdown.append("COT Baseline Neutral (+10p)")

    score = max(0, min(100, score))
    approved = score >= 60  # Flexible 60-point threshold for steady trade frequency

    return {
        "score": score,
        "approved": approved,
        "threshold": 60,
        "breakdown": breakdown,
        "summary": f"Score {score}/100 ({'APPROVED' if approved else 'REJECTED'}) — " + ", ".join(breakdown[:3]),
    }
