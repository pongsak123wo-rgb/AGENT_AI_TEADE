"""Stochastic (9,3,3) + RSI (14) Swing Engine & Multi-Timeframe Matrix.

Implements the user's master trading strategy:
1. Stoch (9,3,3) Swings MUST occur in OB (>80) or OS (<20) zones ONLY.
2. Uptrend Chain: OS1 (L1) -> OB1 (H1) -> OS2 (L2) with L2 > L1 (Higher Low).
3. Anchor OS1 (L1) as Major Support. OB0 (H0) as Major Resistance.
4. Entry 1: OS2 (L2 > L1) + Multi-Bar Bullish Engulfing (candle close beats candle before lowest low of OS2).
5. Entry 2 (DCA Layer): Drops 2 Fibo levels down (passing 38.2% / 23.6%). Max 2 positions total.
6. TP 1: High of OB1 (H1).
7. TP 2: Fibo Extension 161.8% from OB1 to OS2.
8. Emergency Close: If Stoch reaches OB2 but H2 < H1 (fails to make Higher High), AI closes all positions immediately.
9. Hard SL / Structure Destroyed: Price breaks below OS1 (L3 < L1) -> Hard Cut Loss all positions.
10. MTF Selection: AI compares [H1+M5] vs [H4+M15] and selects the higher scoring pair!
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def compute_stoch_933(highs: list[float], lows: list[float], closes: list[float], k_period: int = 9, d_period: int = 3, smooth_k: int = 3) -> tuple[list[float], list[float]]:
    """Calculates Stochastic (9,3,3) %K and %D lines."""
    if len(closes) < k_period + smooth_k + d_period:
        return [], []

    s_high = pd.Series(highs)
    s_low = pd.Series(lows)
    s_close = pd.Series(closes)

    lowest_low = s_low.rolling(window=k_period).min()
    highest_high = s_high.rolling(window=k_period).max()

    raw_k = 100 * ((s_close - lowest_low) / (highest_high - lowest_low + 1e-9))
    smooth_k_line = raw_k.rolling(window=smooth_k).mean()
    d_line = smooth_k_line.rolling(window=d_period).mean()

    return smooth_k_line.fillna(50).tolist(), d_line.fillna(50).tolist()


def compute_rsi(closes: list[float], period: int = 14) -> list[float]:
    """RSI (Wilder's smoothing). Returns a list aligned to closes (NaN-free,
    warm-up filled with 50)."""
    if len(closes) < period + 1:
        return []
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50).tolist()


def detect_rsi_cycle(highs: list[float], lows: list[float], closes: list[float]) -> dict:
    """RSI(14) as the SLOWER, bigger-cycle read that sits behind the Stoch
    swing — RSI reaches OB/OS later than Stoch, so it frames the main trend.
    This is ADVISORY only (a supplementary score), never a hard entry gate.

    Also flags RSI divergence: the user's rule is that when a divergence
    prints on a timeframe, Stoch tends to cut one more OB/OS round in that
    direction — i.e. a bullish divergence hints the next Stoch OS is a buy
    opportunity (and mirror for bearish).
    """
    rsi = compute_rsi(closes, 14)
    if not rsi or len(rsi) < 32:
        return {"ready": False, "rsi": rsi[-1] if rsi else 50.0,
                "trend": "unknown", "divergence": "none"}

    latest = round(rsi[-1], 1)
    # Main-trend bias: RSI sitting above/below the 50 midline, smoothed over
    # the last few bars so a single spike doesn't flip it.
    recent_avg = sum(rsi[-5:]) / 5
    if recent_avg > 55:
        trend = "bullish"
    elif recent_avg < 45:
        trend = "bearish"
    else:
        trend = "ranging"

    # Divergence over two adjacent 15-bar windows (price pivot vs RSI pivot).
    prior_p, recent_p = closes[-30:-15], closes[-15:]
    prior_r, recent_r = rsi[-30:-15], rsi[-15:]
    divergence = "none"
    # Bullish: price lower low, RSI higher low
    if min(recent_p) < min(prior_p) and min(recent_r) > min(prior_r):
        divergence = "bullish"
    # Bearish: price higher high, RSI lower high
    elif max(recent_p) > max(prior_p) and max(recent_r) < max(prior_r):
        divergence = "bearish"

    return {"ready": True, "rsi": latest, "trend": trend, "divergence": divergence}


def detect_stoch_swings(highs: list[float], lows: list[float], closes: list[float]) -> dict:
    """Detects OB (>80) and OS (<20) swings only for Stochastic (9,3,3)."""
    k_period, smooth_k, d_period = 9, 3, 3  # must match compute_stoch_933 defaults
    k_line, d_line = compute_stoch_933(highs, lows, closes, k_period, d_period, smooth_k)
    if not k_line or len(k_line) < 15:
        return {"ready": False, "reason": "ข้อมูลไม่พอคำนวณ Stochastic (9,3,3)"}

    # Find OB (>80) and OS (<20) pivot points. Thresholds match the strategy
    # spec exactly (OB > 80 / OS < 20) — the old 75/25 was 5 points looser and
    # tagged swings that never truly reached the OB/OS zone, producing weaker
    # signals. The %K warm-up region is padded with fillna(50), which can
    # fabricate fake pivots at the very start of the series, so pivot detection
    # only begins once %K is actually computed (after the k+smooth+d window).
    swings = []
    n = len(k_line)
    warmup = k_period + smooth_k + d_period  # bars before %K is real

    for i in range(max(2, warmup), n - 1):
        k_val = k_line[i]
        # Check OS Swing Low (local min inside the <20 zone)
        if k_val < 20:
            if k_val <= k_line[i-1] and k_val <= k_line[i+1]:
                swings.append({
                    "type": "OS",
                    "index": i,
                    "stoch_k": round(k_val, 2),
                    "price_low": lows[i],
                    "price_close": closes[i]
                })
        # Check OB Swing High (local max inside the >80 zone)
        elif k_val > 80:
            if k_val >= k_line[i-1] and k_val >= k_line[i+1]:
                swings.append({
                    "type": "OB",
                    "index": i,
                    "stoch_k": round(k_val, 2),
                    "price_high": highs[i],
                    "price_close": closes[i]
                })

    latest_k_val = round(k_line[-1], 2) if k_line else 50.0
    latest_d_val = round(d_line[-1], 2) if d_line else 50.0

    if len(swings) < 3:
        return {
            "ready": True,
            "latest_k": latest_k_val,
            "latest_d": latest_d_val,
            "valid_structure": False,
            "bias": "none",
            "reason": "ยังสะสมรอบสวิง OB/OS ไม่ครบ 3 สวิง",
            "swings_count": len(swings),
            "uptrend": {"valid": False, "l1_price": None, "h1_price": None, "l2_price": None},
            "downtrend": {"valid": False, "h1_price": None, "l1_price": None, "h2_price": None},
            "all_swings": swings
        }

    # Separate into OS and OB sequences
    os_swings = [s for s in swings if s["type"] == "OS"]
    ob_swings = [s for s in swings if s["type"] == "OB"]

    # 1. Check Uptrend Chain: OS1 -> OB1 -> OS2 (STRICT chronological order).
    # The old code just took os_swings[-2], os_swings[-1] and ob_swings[-1]
    # without checking that OB1 actually sat BETWEEN the two OS swings — so the
    # "OB1" could be after OS2 or before OS1, i.e. not a real OS→OB→OS structure
    # at all. Here we anchor on the most recent OS (OS2), find the last OB
    # strictly before it (OB1), then the last OS strictly before that OB (OS1).
    # That guarantees index(OS1) < index(OB1) < index(OS2).
    uptrend_valid = False
    major_support_l1 = None
    major_resistance_h0 = None
    l1_price = None
    l2_price = None
    h1_price = None
    os2_index = None

    if len(os_swings) >= 2 and len(ob_swings) >= 1:
        os2 = os_swings[-1]
        ob1 = next((s for s in reversed(ob_swings) if s["index"] < os2["index"]), None)
        os1 = next((s for s in reversed(os_swings) if ob1 and s["index"] < ob1["index"]), None)

        if os1 and ob1:
            l1_price = os1["price_low"]
            l2_price = os2["price_low"]
            h1_price = ob1["price_high"]
            os2_index = os2["index"]

            if l2_price > l1_price:  # Higher Low
                uptrend_valid = True
                major_support_l1 = l1_price
                major_resistance_h0 = h1_price

    # 2. Check Downtrend Chain: OB1 -> OS1 -> OB2 (STRICT chronological order).
    # Same fix, mirrored: anchor on the most recent OB (OB2), find the last OS
    # strictly before it (OS1/L1), then the last OB strictly before that OS
    # (OB1/H1). Guarantees index(OB1) < index(OS1) < index(OB2).
    downtrend_valid = False
    major_resistance_h1 = None
    major_support_l0 = None
    h1_down = None
    h2_down = None
    l1_down = None
    ob2_index = None

    if len(ob_swings) >= 2 and len(os_swings) >= 1:
        ob2_d = ob_swings[-1]
        os1_d = next((s for s in reversed(os_swings) if s["index"] < ob2_d["index"]), None)
        ob1_d = next((s for s in reversed(ob_swings) if os1_d and s["index"] < os1_d["index"]), None)

        if ob1_d and os1_d:
            h1_down = ob1_d["price_high"]
            h2_down = ob2_d["price_high"]
            l1_down = os1_d["price_low"]
            ob2_index = ob2_d["index"]

            if h2_down < h1_down:  # Lower High
                downtrend_valid = True
                major_resistance_h1 = h1_down
                major_support_l0 = l1_down

    return {
        "ready": True,
        "latest_k": round(k_line[-1], 2),
        "latest_d": round(d_line[-1], 2),
        "uptrend": {
            "valid": uptrend_valid,
            "l1_price": l1_price,
            "h1_price": h1_price,
            "l2_price": l2_price,
            "major_support": major_support_l1,
            "major_resistance": major_resistance_h0,
            "os2_index": os2_index
        },
        "downtrend": {
            "valid": downtrend_valid,
            "h1_price": h1_down,
            "l1_price": l1_down,
            "h2_price": h2_down,
            "major_resistance": major_resistance_h1,
            "major_support": major_support_l0,
            "ob2_index": ob2_index
        },
        "all_swings": swings[-5:]
    }


def check_multi_candle_engulfing(highs: list[float], lows: list[float], opens: list[float], closes: list[float], os2_index: int, side: str = "buy") -> dict:
    """Checks if a candle after OS2/OB2 closes beyond the candle before the lowest/highest bar."""
    n = len(closes)
    if os2_index is None or os2_index >= n - 1:
        return {"engulfed": False, "reason": "รอแท่งเทียนถัดไปหลัง OS2/OB2"}

    if side == "buy":
        # Find lowest low bar around os2_index
        sub_lows = lows[max(0, os2_index - 2): min(n, os2_index + 3)]
        lowest_bar_idx = max(0, os2_index - 2) + sub_lows.index(min(sub_lows))

        if lowest_bar_idx == 0:
            return {"engulfed": False, "reason": "ไม่มีแท่งก่อนหน้าแท่งต่ำสุด"}

        # Target level to beat: High or Open of the candle BEFORE lowest low bar
        candle_before_lowest_high = max(highs[lowest_bar_idx - 1], opens[lowest_bar_idx - 1])

        # Check if ANY subsequent candle close beats candle_before_lowest_high
        for j in range(lowest_bar_idx + 1, n):
            if closes[j] > candle_before_lowest_high:
                return {
                    "engulfed": True,
                    "side": "buy",
                    "trigger_bar_index": j,
                    "target_price_beaten": candle_before_lowest_high,
                    "close_price": closes[j],
                    "bars_taken": j - lowest_bar_idx
                }

        return {"engulfed": False, "reason": f"ราคายังไม่ปิดสูงกว่า {candle_before_lowest_high:.5f} (แท่งก่อนหน้าแท่งต่ำสุด)"}

    else:  # sell
        sub_highs = highs[max(0, os2_index - 2): min(n, os2_index + 3)]
        highest_bar_idx = max(0, os2_index - 2) + sub_highs.index(max(sub_highs))

        if highest_bar_idx == 0:
            return {"engulfed": False, "reason": "ไม่มีแท่งก่อนหน้าแท่งสูงสุด"}

        candle_before_highest_low = min(lows[highest_bar_idx - 1], opens[highest_bar_idx - 1])

        for j in range(highest_bar_idx + 1, n):
            if closes[j] < candle_before_highest_low:
                return {
                    "engulfed": True,
                    "side": "sell",
                    "trigger_bar_index": j,
                    "target_price_beaten": candle_before_highest_low,
                    "close_price": closes[j],
                    "bars_taken": j - highest_bar_idx
                }

        return {"engulfed": False, "reason": f"ราคายังไม่ปิดต่ำกว่า {candle_before_highest_low:.5f} (แท่งก่อนหน้าแท่งสูงสุด)"}


def calculate_fibo_161_8(ob1_price: float, os2_price: float, side: str = "buy") -> tuple[float, float]:
    """Calculates TP1 (Previous High/Low) and TP2 (Fibonacci Extension 161.8%)."""
    if side == "buy":
        tp1 = ob1_price
        height = abs(ob1_price - os2_price)
        tp2 = os2_price + (height * 1.618)
    else:
        tp1 = ob1_price  # Previous Low for sell
        height = abs(ob1_price - os2_price)
        tp2 = os2_price - (height * 1.618)

    return round(tp1, 5), round(tp2, 5)


def check_rr_and_sl(entry_price: float, os1_price: float, tp1_price: float, atr: float, side: str = "buy") -> tuple[float, float, str]:
    """Calculates SL and checks if RR >= 1.5. Swaps to ATR Dynamic SL if RR < 1.5."""
    if side == "buy":
        raw_sl = os1_price
        risk_dist = entry_price - raw_sl
        reward_dist = tp1_price - entry_price

        if risk_dist <= 0:
            risk_dist = atr * 1.5
            raw_sl = entry_price - risk_dist

        rr = reward_dist / risk_dist if risk_dist > 0 else 0

        if rr < 1.5:
            # Fall back to ATR SL to keep RR >= 1.5
            atr_sl = entry_price - (atr * 1.5)
            atr_tp = entry_price + (atr * 1.5 * 2.0)
            return round(atr_sl, 5), round(atr_tp, 5), f"RR จาก OS1 ต่ำไป ({rr:.2f}) -> สลับใช้ ATR SL ({atr_sl:.5f})"
        else:
            return round(raw_sl, 5), round(tp1_price, 5), f"ใช้แนวรับ OS1 สลบ SL ({raw_sl:.5f}) RR = {rr:.2f}"
    else:  # sell
        raw_sl = os1_price
        risk_dist = raw_sl - entry_price
        reward_dist = entry_price - tp1_price

        if risk_dist <= 0:
            risk_dist = atr * 1.5
            raw_sl = entry_price + risk_dist

        rr = reward_dist / risk_dist if risk_dist > 0 else 0

        if rr < 1.5:
            atr_sl = entry_price + (atr * 1.5)
            atr_tp = entry_price - (atr * 1.5 * 2.0)
            return round(atr_sl, 5), round(atr_tp, 5), f"RR จาก OB1 ต่ำไป ({rr:.2f}) -> สลับใช้ ATR SL ({atr_sl:.5f})"
        else:
            return round(raw_sl, 5), round(tp1_price, 5), f"ใช้แนวต้าน OB1 สลบ SL ({raw_sl:.5f}) RR = {rr:.2f}"


def check_emergency_dca(entry1_price: float, current_price: float, ob1_price: float, os2_price: float, side: str = "buy") -> bool:
    """Checks if price dropped 2 Fibo levels down from entry 1 (passing 38.2% / 23.6%) to trigger Entry 2 DCA."""
    height = abs(ob1_price - os2_price)
    if height <= 0:
        return False

    if side == "buy":
        # Fibo 38.2% level from OS2
        fibo_382 = os2_price + (height * 0.382)
        return current_price <= fibo_382
    else:
        fibo_382 = os2_price - (height * 0.382)
        return current_price >= fibo_382
