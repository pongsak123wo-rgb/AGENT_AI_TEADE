"""Smart Money Concepts (SMC) analysis — real computation on real OHLC,
not a fabricated label. Implements the standard building blocks:

- Swing highs/lows via fractal detection (2-bar confirmation each side)
- Market structure sequence (HH/HL = uptrend structure, LH/LL = downtrend)
- BOS (Break of Structure) — price breaks the most recent swing in the
  direction of the existing trend, confirming continuation
- CHoCH (Change of Character) — price breaks structure AGAINST the
  existing trend, the first signal of a possible reversal
- Order blocks — the last opposite-colored candle before the impulse
  leg that caused a BOS/CHoCH (the candle "smart money" supposedly
  built the position in before the move)
- Fair Value Gaps (FVG) — 3-candle imbalance (gap between candle 1's
  wick and candle 3's wick) that price hasn't traded back into yet
- Liquidity sweeps — a wick that pierces a recent swing high/low (taking
  out stops) and closes back inside it, suggesting a stop-hunt/reversal
- Dual-timeframe structure (internal/fast vs swing/macro) — the fast
  structure above (2-bar fractal) reacts to every minor wiggle; a second,
  wider-window structure (10-bar fractal) tracks the bigger picture, so a
  fast CHoCH that contradicts the macro trend can be told apart from one
  that confirms a genuine larger reversal
- Equal highs/lows — liquidity pools where price tapped a similar level
  twice (within an ATR-relative tolerance); these are common stop-hunt
  targets before a real move
- Premium/discount/equilibrium zone — where current price sits within
  the most recent macro swing range (top half = "expensive"/premium,
  bottom half = "cheap"/discount), a basic value-area read used to judge
  whether a buy/sell is chasing price into a worse area to enter

This is real algorithmic SMC, not a magic oracle — it's still a heuristic
read on price structure, and like any technical method it can be wrong.
Needs real OHLC (real M1 candles or backtest bars); synthetic OHLC will
produce structure that doesn't mean anything.
"""
from __future__ import annotations


def _find_swings(highs: list[float], lows: list[float], window: int = 2) -> tuple[list, list]:
    n = len(highs)
    swing_highs = []
    swing_lows = []
    for i in range(window, n - window):
        local_highs = highs[i - window : i + window + 1]
        local_lows = lows[i - window : i + window + 1]
        if highs[i] == max(local_highs):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(local_lows):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def _market_structure(swing_highs: list, swing_lows: list) -> list[dict]:
    """Tags each swing point HH/LH (highs) or HL/LL (lows) relative to the
    previous swing of the same type, then merges both into one
    chronological structure sequence."""
    tagged = []
    last_high = None
    for i, p in swing_highs:
        tag = "HH" if last_high is not None and p > last_high else ("LH" if last_high is not None else "H")
        tagged.append({"idx": i, "price": p, "type": "high", "tag": tag})
        last_high = p

    last_low = None
    for i, p in swing_lows:
        tag = "HL" if last_low is not None and p > last_low else ("LL" if last_low is not None else "L")
        tagged.append({"idx": i, "price": p, "type": "low", "tag": tag})
        last_low = p

    tagged.sort(key=lambda s: s["idx"])
    return tagged


def _determine_trend(structure: list[dict]) -> str:
    """Looks at the last two highs and lows to decide if structure is
    currently bullish (HH+HL), bearish (LH+LL), or mixed/ranging."""
    recent_highs = [s["tag"] for s in structure if s["type"] == "high"][-2:]
    recent_lows = [s["tag"] for s in structure if s["type"] == "low"][-2:]
    bullish = recent_highs.count("HH") > 0 and recent_lows.count("HL") > 0
    bearish = recent_highs.count("LH") > 0 and recent_lows.count("LL") > 0
    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "ranging"


def _detect_bos_choch(structure: list[dict], closes: list[float], trend: str) -> dict:
    """BOS = latest close breaks beyond the most recent swing in the same
    direction as `trend` (continuation). CHoCH = latest close breaks the
    most recent swing AGAINST `trend` (first sign of reversal)."""
    if not structure:
        return {"event": "none", "detail": "ยังไม่มี swing พอจะตัดสิน structure"}

    last_close = closes[-1]
    last_high_swing = next((s for s in reversed(structure) if s["type"] == "high"), None)
    last_low_swing = next((s for s in reversed(structure) if s["type"] == "low"), None)

    if trend == "bullish" and last_high_swing and last_close > last_high_swing["price"]:
        return {"event": "BOS", "direction": "bullish", "detail": f"ราคาทะลุ swing high ล่าสุด {last_high_swing['price']} ยืนยันเทรนด์ขึ้นต่อ"}
    if trend == "bearish" and last_low_swing and last_close < last_low_swing["price"]:
        return {"event": "BOS", "direction": "bearish", "detail": f"ราคาทะลุ swing low ล่าสุด {last_low_swing['price']} ยืนยันเทรนด์ลงต่อ"}
    if trend == "bearish" and last_high_swing and last_close > last_high_swing["price"]:
        return {"event": "CHoCH", "direction": "bullish", "detail": f"ราคาทะลุ swing high {last_high_swing['price']} ทวนเทรนด์ลงเดิม — สัญญาณกลับตัวขึ้น"}
    if trend == "bullish" and last_low_swing and last_close < last_low_swing["price"]:
        return {"event": "CHoCH", "direction": "bearish", "detail": f"ราคาทะลุ swing low {last_low_swing['price']} ทวนเทรนด์ขึ้นเดิม — สัญญาณกลับตัวลง"}
    return {"event": "none", "detail": "ราคายังไม่ทะลุ swing สำคัญฝั่งใด"}


def _find_order_block(opens: list[float], closes: list[float], event: dict, bos_idx_hint: int) -> dict | None:
    """The last opposite-colored candle before the impulse leg. For a
    bullish BOS/CHoCH, look backward from the break point for the last
    bearish (red) candle — that's the bullish order block."""
    if event["event"] == "none":
        return None
    direction = event["direction"]
    search_from = min(bos_idx_hint, len(closes) - 1)
    for i in range(search_from, max(search_from - 15, 0), -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if direction == "bullish" and is_bearish:
            return {"idx": i, "type": "bullish_ob", "low": min(opens[i], closes[i]), "high": max(opens[i], closes[i])}
        if direction == "bearish" and is_bullish:
            return {"idx": i, "type": "bearish_ob", "low": min(opens[i], closes[i]), "high": max(opens[i], closes[i])}
    return None


def _find_fvg(highs: list[float], lows: list[float], closes: list[float]) -> dict | None:
    """3-candle imbalance: candle[i-1]'s high below candle[i+1]'s low (gap
    up, bullish FVG) or candle[i-1]'s low above candle[i+1]'s high (gap
    down, bearish FVG). Returns the most recent one price hasn't traded
    back into yet (still 'unmitigated')."""
    n = len(closes)
    found = None
    for i in range(1, n - 1):
        if highs[i - 1] < lows[i + 1]:
            gap_low, gap_high = highs[i - 1], lows[i + 1]
            mitigated = any(lows[k] <= gap_high and lows[k] >= gap_low for k in range(i + 2, n))
            found = {"idx": i, "type": "bullish_fvg", "low": gap_low, "high": gap_high, "mitigated": mitigated}
        elif lows[i - 1] > highs[i + 1]:
            gap_low, gap_high = highs[i + 1], lows[i - 1]
            mitigated = any(highs[k] >= gap_low and highs[k] <= gap_high for k in range(i + 2, n))
            found = {"idx": i, "type": "bearish_fvg", "low": gap_low, "high": gap_high, "mitigated": mitigated}
    return found


def _detect_liquidity_sweep(highs: list[float], lows: list[float], closes: list[float], lookback: int = 10) -> dict | None:
    """Checks the last 3 bars for a wick that pierced the prior
    `lookback`-bar high/low and then closed back inside it — the classic
    'stop hunt then reverse' signature."""
    n = len(highs)
    if n < lookback + 3:
        return None
    for i in range(n - 3, n):
        prior_high = max(highs[max(0, i - lookback) : i])
        prior_low = min(lows[max(0, i - lookback) : i])
        if highs[i] > prior_high and closes[i] < prior_high:
            return {"idx": i, "type": "bearish_sweep", "swept_level": prior_high, "detail": "แทงขึ้นเหนือไฮเดิมแล้วปิดกลับลงมา — โดน sweep ฝั่ง buy stop"}
        if lows[i] < prior_low and closes[i] > prior_low:
            return {"idx": i, "type": "bullish_sweep", "swept_level": prior_low, "detail": "แทงลงต่ำกว่าโลว์เดิมแล้วปิดกลับขึ้นมา — โดน sweep ฝั่ง sell stop"}
    return None


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def _detect_equal_levels(swings: list, atr: float, threshold_ratio: float = 0.1) -> list[dict]:
    """Two swings of the same type (both highs, or both lows) within
    threshold_ratio * ATR of each other = a liquidity pool: the market
    tapped roughly the same level twice, leaving stops clustered there
    that often get swept before the real move."""
    events = []
    if atr <= 0:
        return events
    for i in range(1, len(swings)):
        idx_a, price_a = swings[i - 1]
        idx_b, price_b = swings[i]
        if abs(price_a - price_b) <= threshold_ratio * atr:
            events.append({"idx_a": idx_a, "idx_b": idx_b, "price_a": round(price_a, 5), "price_b": round(price_b, 5)})
    return events


def _premium_discount_zone(range_high: float, range_low: float, price: float) -> dict:
    """Classic ICT-style value read: where does current price sit within
    the most recent significant swing range? Top half = premium (relatively
    expensive — favors looking for sells/avoiding fresh buys), bottom half =
    discount (relatively cheap — favors looking for buys), a thin band
    around the midpoint = equilibrium (no edge either way)."""
    rng = range_high - range_low
    if rng <= 0:
        return {"zone": "unknown", "position_pct": None}
    position = (price - range_low) / rng
    if abs(position - 0.5) <= 0.05:
        zone = "equilibrium"
    elif position > 0.5:
        zone = "premium"
    else:
        zone = "discount"
    return {"zone": zone, "position_pct": round(position * 100, 1)}


def classify_mtf_confluence(smc: dict | None, bias: str) -> str:
    """Classifies how the fast (small-TF-equivalent) structure and the
    macro/swing (big-TF-equivalent) structure relate for the proposed
    `bias` ("buy"/"sell") — this is what drives position sizing:
    - "full": both fast structure AND the macro trend agree with bias —
      highest-conviction setup, size up.
    - "fast_only": only the small/fast structure supports this bias, the
      bigger picture doesn't confirm yet — lower conviction, size down.
    - "swing_only": the macro trend supports this bias but the fast
      structure hasn't broken anything in that direction yet — early/
      anticipatory, moderate size.
    - "none": neither timeframe's structure actually supports this bias.
    """
    if not smc or not smc.get("ready") or bias not in ("buy", "sell"):
        return "none"

    fast_event = smc.get("structure_event")
    fast_dir = smc.get("structure_direction")
    fast_signal = fast_event != "none" and fast_dir == bias

    swing_event = smc.get("swing_structure_event")
    swing_dir = smc.get("swing_structure_direction")
    swing_trend = smc.get("swing_structure_trend")
    bias_trend = "bullish" if bias == "buy" else "bearish"
    swing_confirms = (swing_event != "none" and swing_dir == bias) or swing_trend == bias_trend

    if fast_signal and swing_confirms:
        return "full"
    if fast_signal and not swing_confirms:
        return "fast_only"
    if not fast_signal and swing_confirms:
        return "swing_only"
    return "none"


def analyze_smc(ohlc: dict, macro_window: int = 10) -> dict:
    """ohlc: {"o": [...], "h": [...], "l": [...], "c": [...]}, oldest first,
    real candles only (>=20 bars, ideally 40+ for structure to mean
    anything)."""
    opens, highs, lows, closes = ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"]
    if len(closes) < 20:
        return {"ready": False, "reason": "ต้องมีอย่างน้อย 20 แคนเดิลจริงสำหรับ SMC"}

    # Fast/internal structure — reacts to every minor wiggle (small window).
    swing_highs, swing_lows = _find_swings(highs, lows, window=2)
    structure = _market_structure(swing_highs, swing_lows)
    trend = _determine_trend(structure)
    event = _detect_bos_choch(structure, closes, trend)
    bos_idx_hint = event.get("idx", len(closes) - 1) if "idx" in event else len(closes) - 1
    order_block = _find_order_block(opens, closes, event, bos_idx_hint)
    fvg = _find_fvg(highs, lows, closes)
    sweep = _detect_liquidity_sweep(highs, lows, closes)

    result = {
        "ready": True,
        "structure_trend": trend,
        "structure_event": event["event"],
        "structure_direction": event.get("direction"),
        "structure_detail": event["detail"],
        "order_block": order_block,
        "fair_value_gap": fvg,
        "liquidity_sweep": sweep,
        "swing_count": {"highs": len(swing_highs), "lows": len(swing_lows)},
    }

    # Macro/swing structure — wider window, the "bigger picture" trend.
    # A fast CHoCH that contradicts this is a much weaker signal than one
    # that lines up with it (early sign of a real reversal vs just noise).
    if len(closes) >= macro_window * 2 + 5:
        macro_highs, macro_lows = _find_swings(highs, lows, window=macro_window)
        macro_structure = _market_structure(macro_highs, macro_lows)
        macro_trend = _determine_trend(macro_structure)
        macro_event = _detect_bos_choch(macro_structure, closes, macro_trend)
        atr = _atr(highs, lows, closes)

        result["swing_structure_trend"] = macro_trend
        result["swing_structure_event"] = macro_event["event"]
        result["swing_structure_direction"] = macro_event.get("direction")
        result["swing_structure_detail"] = macro_event["detail"]
        result["fast_vs_swing_agree"] = (
            event.get("direction") == macro_event.get("direction") if event["event"] != "none" and macro_event["event"] != "none" else None
        )
        result["equal_highs"] = _detect_equal_levels(macro_highs, atr)[-3:]
        result["equal_lows"] = _detect_equal_levels(macro_lows, atr)[-3:]

        if macro_highs and macro_lows:
            range_high = max(macro_highs[-1][1], macro_lows[-1][1])
            range_low = min(macro_highs[-1][1], macro_lows[-1][1])
            result["premium_discount_zone"] = _premium_discount_zone(range_high, range_low, closes[-1])
        else:
            result["premium_discount_zone"] = {"zone": "unknown", "position_pct": None}
    else:
        result["swing_structure_trend"] = None
        result["swing_structure_event"] = "none"
        result["equal_highs"] = []
        result["equal_lows"] = []
        result["premium_discount_zone"] = {"zone": "unknown", "position_pct": None}

    return result
