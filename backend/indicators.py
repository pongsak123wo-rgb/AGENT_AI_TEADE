"""Real indicator calculations from price history (deterministic, no AI).

This is the "eyes" of the Technical Analysis Agent — it produces
numeric facts about current market state. The LLM (in llm_analysis.py)
interprets these facts together with RAG context; it never invents
indicator values itself.

Combines several confirmation signals instead of just RSI+EMA:
- RSI (momentum)
- EMA fast/slow (short-term trend) + EMA50 (longer-term trend, acts as a
  rough multi-timeframe filter since it reacts much slower than EMA9/21)
- MACD (trend momentum + crossover)
- Bollinger Bands (volatility/extremes)
- ATR + price action (pin bar / engulfing) — uses REAL M1 candle OHLC
  from the MT5 EA when available (`ohlc` param). Falls back to a
  synthetic spread around tick price only when no real candles have
  arrived yet (e.g. right after startup, or on mock/demo price feed) —
  that fallback is clearly weaker and shouldn't be trusted for pattern
  detection, only for keeping the pipeline from crashing.
"""
from __future__ import annotations

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands


def compute_snapshot(prices: list[float], ohlc: dict | None = None) -> dict:
    """prices: most recent N closes, oldest first.
    ohlc: optional {"o": [...], "h": [...], "l": [...], "c": [...]} of
    real M1 candles (same length, oldest first) from the MT5 EA.
    """
    if len(prices) < 20:
        return {"ready": False}

    series = pd.Series(prices)

    using_real_ohlc = bool(ohlc) and len(ohlc.get("c", [])) == len(prices)
    if using_real_ohlc:
        opens = pd.Series(ohlc["o"])
        highs = pd.Series(ohlc["h"])
        lows = pd.Series(ohlc["l"])
    else:
        highs = pd.Series([p * 1.0005 for p in prices])
        lows = pd.Series([p * 0.9995 for p in prices])
        opens = pd.Series([prices[i - 1] if i > 0 else prices[0] for i in range(len(prices))])

    last_price = prices[-1]
    last_open = opens.iloc[-1]
    last_high = highs.iloc[-1]
    last_low = lows.iloc[-1]
    prev_open = opens.iloc[-2]
    prev_close = prices[-2]

    rsi = RSIIndicator(series, window=14).rsi().iloc[-1]
    ema_fast = EMAIndicator(series, window=9).ema_indicator().iloc[-1]
    ema_slow = EMAIndicator(series, window=21).ema_indicator().iloc[-1]

    recent_low = min(prices[-20:])
    recent_high = max(prices[-20:])

    snapshot = {
        "ready": True,
        "price": round(last_price, 5),
        "rsi": round(float(rsi), 1),
        "rsi_state": "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral",
        "ema_fast": round(float(ema_fast), 5),
        "ema_slow": round(float(ema_slow), 5),
        "ema_trend": "up" if ema_fast > ema_slow else "down",
        "support": round(recent_low, 5),
        "resistance": round(recent_high, 5),
        "ohlc_source": "real" if using_real_ohlc else "synthetic",
    }

    # ATR
    atr_ind = AverageTrueRange(high=highs, low=lows, close=series, window=14)
    atr = atr_ind.average_true_range().iloc[-1]
    snapshot["atr"] = round(float(atr), 5)

    # Price action — only meaningful with real candles; still computed on
    # synthetic OHLC so the pipeline doesn't crash, but the LLM is told to
    # discount it when ohlc_source == "synthetic" (see llm_analysis.py).
    body = abs(last_price - last_open)
    upper_wick = last_high - max(last_price, last_open)
    lower_wick = min(last_price, last_open) - last_low
    total_range = last_high - last_low

    pin_bar = "none"
    if total_range > 0:
        if lower_wick > body * 2 and upper_wick < body:
            pin_bar = "bullish_hammer"
        elif upper_wick > body * 2 and lower_wick < body:
            pin_bar = "bearish_shooting_star"
    snapshot["pin_bar"] = pin_bar

    engulfing = "none"
    if last_price > last_open and prev_close < prev_open and last_price > prev_open and last_open < prev_close:
        engulfing = "bullish_engulfing"
    elif last_price < last_open and prev_close > prev_open and last_price < prev_open and last_open > prev_close:
        engulfing = "bearish_engulfing"
    snapshot["engulfing"] = engulfing

    # Longer-term trend filter — EMA50 moves much slower, so fast/slow
    # agreeing with it is the closest we can get to "higher timeframe
    # confirms this" without a separate H1/H4 data feed.
    if len(prices) >= 50:
        ema_long = EMAIndicator(series, window=50).ema_indicator().iloc[-1]
        snapshot["ema_long"] = round(float(ema_long), 5)
        snapshot["long_term_trend"] = "up" if last_price > ema_long else "down"
        snapshot["trend_confluence"] = snapshot["ema_trend"] == snapshot["long_term_trend"]
    else:
        snapshot["long_term_trend"] = None
        snapshot["trend_confluence"] = None

    if len(prices) >= 35:
        macd_ind = MACD(series)
        macd_line = macd_ind.macd().iloc[-1]
        macd_signal = macd_ind.macd_signal().iloc[-1]
        snapshot["macd"] = round(float(macd_line), 5)
        snapshot["macd_signal"] = round(float(macd_signal), 5)
        snapshot["macd_cross"] = "bullish" if macd_line > macd_signal else "bearish"
    else:
        snapshot["macd_cross"] = None

    bb = BollingerBands(series, window=20)
    bb_high_series = bb.bollinger_hband()
    bb_low_series = bb.bollinger_lband()
    bb_high = bb_high_series.iloc[-1]
    bb_low = bb_low_series.iloc[-1]
    if last_price >= bb_high:
        bb_position = "above_upper_band"
    elif last_price <= bb_low:
        bb_position = "below_lower_band"
    else:
        bb_position = "inside_bands"
    snapshot["bb_position"] = bb_position

    # BB squeeze — current band width vs its own 20-bar average. A
    # squeeze means the market is coiling; breakouts after a squeeze
    # tend to be more reliable than breakouts from an already-wide band.
    bandwidth = (bb_high_series - bb_low_series).dropna()
    if len(bandwidth) >= 20:
        current_width = bandwidth.iloc[-1]
        avg_width = bandwidth.iloc[-20:].mean()
        snapshot["bb_squeeze"] = bool(current_width < 0.5 * avg_width) if avg_width > 0 else False
    else:
        snapshot["bb_squeeze"] = None

    # RSI divergence — compare the RSI value at the most recent price
    # pivot against the RSI value at the prior pivot, over two adjacent
    # 15-bar windows. Bullish divergence: price makes a lower low while
    # RSI makes a higher low (momentum fading on the new low).
    rsi_series = RSIIndicator(series, window=14).rsi()
    snapshot["rsi_divergence"] = _detect_rsi_divergence(prices, rsi_series)

    return snapshot


def _detect_rsi_divergence(prices: list[float], rsi_series: pd.Series) -> str:
    if len(prices) < 32:
        return "none"

    recent_prices = prices[-15:]
    prior_prices = prices[-30:-15]
    recent_rsi = rsi_series.iloc[-15:]
    prior_rsi = rsi_series.iloc[-30:-15]

    if recent_rsi.isna().any() or prior_rsi.isna().any():
        return "none"

    recent_low_idx = recent_rsi.idxmin()
    prior_low_idx = prior_rsi.idxmin()
    recent_high_idx = recent_rsi.idxmax()
    prior_high_idx = prior_rsi.idxmax()

    # Bullish: price lower low, RSI higher low
    price_lower_low = recent_prices[recent_low_idx - (len(prices) - 15)] < prior_prices[prior_low_idx - (len(prices) - 30)]
    rsi_higher_low = recent_rsi[recent_low_idx] > prior_rsi[prior_low_idx]
    if price_lower_low and rsi_higher_low:
        return "bullish"

    # Bearish: price higher high, RSI lower high
    price_higher_high = recent_prices[recent_high_idx - (len(prices) - 15)] > prior_prices[prior_high_idx - (len(prices) - 30)]
    rsi_lower_high = recent_rsi[recent_high_idx] < prior_rsi[prior_high_idx]
    if price_higher_high and rsi_lower_high:
        return "bearish"

    return "none"
