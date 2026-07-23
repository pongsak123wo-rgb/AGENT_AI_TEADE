"""Backtest engine — estimates win rate on real historical OHLC before
trusting the live system's results.

The live EA only exports the last 60 M1 bars (just enough to run the
indicator pipeline in real time), nowhere near enough history to backtest
against. This module pulls free historical OHLC from Yahoo Finance
instead and replays the *same* deterministic indicator logic
(indicators.compute_snapshot) plus a simplified, rule-based version of
the bias decision over every historical bar.

This intentionally does NOT call any LLM — replaying hundreds of bars
through Gemini/Groq/Cerebras would be slow and burn free-tier quota for
a backtest. The rule below is a deterministic approximation of what the
LLM is instructed to look for (trend confluence + RSI not extended +
MACD agreement), not a perfect stand-in for the live agent's reasoning.
Treat results as a sanity check on the indicator logic itself, not a
prediction of the live system's exact win rate.
"""
from __future__ import annotations

import yfinance as yf

import backtest_log
import mt5_history_bridge
import smc_analysis
from indicators import compute_snapshot

# MT5 symbol -> Yahoo Finance ticker. Yahoo's forex/index data isn't
# identical to the broker's feed (different liquidity provider, no
# spread baked in), so absolute price levels can differ slightly —
# fine for testing indicator *logic*, not for matching live P/L exactly.
SYMBOL_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "XAUUSD": "GC=F",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "BTCUSD": "BTC-USD",
}

WINDOW = 60  # bars of history fed to compute_snapshot at each step
MAX_LOOKAHEAD = 48  # bars to scan forward for SL/TP before giving up


def _decide_bias(snap: dict) -> str:
    """Deterministic stand-in for the live LLM bias call — same signals
    (trend confluence, RSI not overextended, MACD agreement) without the
    LLM round-trip."""
    if not snap.get("ready") or snap.get("trend_confluence") is None or snap.get("macd_cross") is None:
        return "none"

    if (
        snap["ema_trend"] == "up"
        and snap["trend_confluence"]
        and snap["rsi_state"] != "overbought"
        and snap["macd_cross"] == "bullish"
    ):
        return "buy"
    if (
        snap["ema_trend"] == "down"
        and snap["trend_confluence"]
        and snap["rsi_state"] != "oversold"
        and snap["macd_cross"] == "bearish"
    ):
        return "sell"
    return "none"


# mtf_confluence values that historically backtested better — measured
# directly from the first 90-day batch run via backtest_log.get_structure_patterns():
# BOS+swing_only=50.0% (6 samples), none+swing_only=43.8% (16), vs
# CHoCH+none=38.1% (97), none+none=35.0% (377), BOS+none=31.0% (113).
# "swing_only" (macro trend confirms even before the fast structure
# breaks) beat plain "none" confluence across the board — small sample
# sizes for the winners, so this is a filter to test, not a proven edge.
FAVORABLE_CONFLUENCE = {"full", "swing_only"}


def run_backtest(
    symbol: str,
    period: str = "60d",
    interval: str = "1h",
    log_to_db: bool = False,
    require_mtf_confluence: bool = False,
    source: str = "yahoo",
) -> dict:
    if log_to_db:
        backtest_log.clear_symbol(symbol)

    if source == "mt5":
        # Real broker feed exported once via mt5_ea/HistoryExporter.mq5 —
        # same price source as live trading, no Yahoo Finance mismatch.
        # interval "1h"/"1m" map to the two timeframes the script exports.
        timeframe_key = "m1" if interval in ("1m", "m1") else "h1"
        series = mt5_history_bridge.get_symbol_series(symbol, timeframe_key)
        if not series or len(series.get("c", [])) < WINDOW + MAX_LOOKAHEAD:
            got = 0 if not series else len(series.get("c", []))
            return {
                "error": (
                    f"ไม่มีข้อมูล MT5 history พอสำหรับ {symbol} ({timeframe_key}): มี {got} bars, "
                    f"ต้องการอย่างน้อย {WINDOW + MAX_LOOKAHEAD} — รัน HistoryExporter.mq5 ในเทอร์มินัล MT5 ก่อน "
                    f"(ลากจาก Navigator > Scripts ไปวางบนกราฟ) หรือใช้ source='yahoo'"
                )
            }
        ticker = f"MT5:{symbol}:{timeframe_key}"
        closes = series["c"]
        highs = series["h"]
        lows = series["l"]
        opens = series["o"]
    else:
        ticker = SYMBOL_MAP.get(symbol)
        if not ticker:
            return {"error": f"ไม่รู้จัก symbol {symbol} (ไม่มีใน SYMBOL_MAP)"}

        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df is None or len(df) < WINDOW + MAX_LOOKAHEAD:
            return {"error": f"ข้อมูลย้อนหลังไม่พอ ({0 if df is None else len(df)} bars, ต้องการอย่างน้อย {WINDOW + MAX_LOOKAHEAD})"}

        if df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        closes = df["Close"].tolist()
        highs = df["High"].tolist()
        lows = df["Low"].tolist()
        opens = df["Open"].tolist()

    trades = []
    i = WINDOW
    while i < len(closes) - MAX_LOOKAHEAD:
        window_closes = closes[i - WINDOW : i]
        window_ohlc = {
            "o": opens[i - WINDOW : i],
            "h": highs[i - WINDOW : i],
            "l": lows[i - WINDOW : i],
            "c": window_closes,
        }
        snap = compute_snapshot(window_closes, ohlc=window_ohlc)
        bias = _decide_bias(snap)

        if bias == "none":
            i += 1
            continue

        smc = smc_analysis.analyze_smc(window_ohlc)
        mtf_confluence = smc_analysis.classify_mtf_confluence(smc, bias)

        if require_mtf_confluence and mtf_confluence not in FAVORABLE_CONFLUENCE:
            i += 1
            continue

        entry = closes[i]
        atr = snap.get("atr") or entry * 0.001
        sl_dist = atr * 1.5
        tp_dist = atr * 3.0
        if bias == "buy":
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist

        result = None
        for j in range(i + 1, min(i + 1 + MAX_LOOKAHEAD, len(closes))):
            if bias == "buy":
                if highs[j] >= tp:
                    result = "win"
                    break
                if lows[j] <= sl:
                    result = "loss"
                    break
            else:
                if lows[j] <= tp:
                    result = "win"
                    break
                if highs[j] >= sl:
                    result = "loss"
                    break

        final_result = result or "no_hit"
        trades.append({"bar": i, "bias": bias, "entry": round(entry, 5), "result": final_result})

        if log_to_db:
            backtest_log.log_trade(
                symbol, bias, entry, sl, tp, final_result,
                structure_event=smc.get("structure_event"),
                mtf_confluence=mtf_confluence,
                indicators=snap,
            )

        # Skip ahead past this trade's resolution so trades don't overlap
        # on the same move (rough approximation of "wait for it to close").
        i += MAX_LOOKAHEAD if result is None else max(1, j - i)

    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    no_hit = sum(1 for t in trades if t["result"] == "no_hit")
    closed = wins + losses

    return {
        "symbol": symbol,
        "ticker": ticker,
        "source": source,
        "bars_used": len(closes),
        "total_signals": len(trades),
        "wins": wins,
        "losses": losses,
        "no_hit_within_lookahead": no_hit,
        "win_rate_pct": round(wins / closed * 100, 1) if closed else None,
        "trades": trades[-20:],  # most recent 20 only, to keep the response small
    }


def run_backtest_batch(period: str = "90d", interval: str = "1h", require_mtf_confluence: bool = False, source: str = "yahoo") -> dict:
    """Runs run_backtest for every symbol in SYMBOL_MAP and persists every
    simulated trade (with full SMC/MTF context) to backtest_signals.db.
    period="90d" = ~3 months — yfinance allows up to 730 days of 1h bars,
    so this is well within range. source="mt5" uses the real broker feed
    (HistoryExporter.mq5 must have been run first) instead. This is the
    corpus that feeds backtest_log.get_structure_patterns() and,
    eventually, a fine-tune corpus for typhoon2 — explicitly NOT mixed
    into live signal_log."""
    results = {}
    for symbol in SYMBOL_MAP:
        results[symbol] = run_backtest(
            symbol, period=period, interval=interval, log_to_db=True,
            require_mtf_confluence=require_mtf_confluence, source=source,
        )
    return results
