"""Backtest v2 — replays the LIVE pipeline, not a simplified stand-in.

The original engine tested a strategy the system no longer runs (plain
EMA/MACD entries, fixed ATR*1.5 stops, no zones). Its 30-36% win rates
said nothing about the current design. This one walks real MT5 history
bar by bar and applies the SAME components the live loop uses:

    mtf_engine   → M5/M15/H1/H4/D1 resampling, H1↔M5 + H4↔M15 zone pairs,
                   trend consensus from H1/H4/D1, and the zone gate that
                   decides whether a bar is even worth considering
    indicators   → the same RSI/EMA/MACD/ATR snapshot
    risk rules   → RSI gate, trend filter, no-double, spread gate
    SL/TP        → the adaptive max(ATR*2, spread*8, price*0.0012), TP = 2R
    decision_audit → the deterministic factor sheet stands in for the CEO:
                   a trade needs at least MIN_AUDIT_SCORE net supporting
                   factors, mirroring "the council approves when the
                   evidence is strong" without thousands of LLM calls.

Everything is computed from bars up to the current index only — no
lookahead. Timestamps drive the alignment between the M1-derived and
H1-derived timeframes, so an M15 bar only ever sees H4/D1 structure that
had already closed.

Reports the metrics that actually matter: expectancy in R, profit factor,
max drawdown, and the equity curve — not just a win count.
"""
from __future__ import annotations

import datetime

import decision_audit
import indicators as ind_mod
import mt5_history_bridge
import mtf_engine
import timeframe
import zone_watch

# A trade needs this many net supporting factors in the audit sheet.
# Stands in for the CEO council's approval threshold.
MIN_AUDIT_SCORE = 3

MAX_HOLD_BARS = 96      # give up on a trade after this many entry-TF bars
BLOCKED_HOURS = [(19, 20)]   # same trading-hours veto as live (Thai time)
TZ = datetime.timezone(datetime.timedelta(hours=7))


def _slice(series: dict, upto: int) -> dict:
    """OHLC series truncated to bars [0, upto] — prevents lookahead."""
    return {k: series[k][: upto + 1] for k in ("o", "h", "l", "c")}


def _bars_before(series: dict, ts: float) -> int:
    """Index of the last bar that had CLOSED at time `ts` (bisect on t)."""
    t = series.get("t") or []
    lo, hi = 0, len(t) - 1
    idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if t[mid] <= ts:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return idx


def _hour_blocked(ts: float) -> bool:
    h = datetime.datetime.fromtimestamp(ts, tz=TZ).hour
    return any(a <= h < b for a, b in BLOCKED_HOURS)


def _bias_from(ind: dict, trend_overall: str) -> str:
    """Deterministic bias — the rule core the LLM is asked to confirm:
    structure/trend agreement plus non-exhausted momentum."""
    ema = ind.get("ema_trend")
    macd = ind.get("macd_cross")
    rsi = ind.get("rsi_state")
    if ema == "up" and macd == "bullish" and rsi != "overbought" and trend_overall != "bearish":
        return "buy"
    if ema == "down" and macd == "bearish" and rsi != "oversold" and trend_overall != "bullish":
        return "sell"
    return "none"


def _risk_ok(bias: str, ind: dict, price: float, spread: float, open_side: str | None) -> tuple[bool, str]:
    """The live risk gates, in the same order."""
    rsi, ema = ind.get("rsi_state"), ind.get("ema_trend")
    if rsi == "oversold" and bias == "sell":
        return False, "RSI gate (oversold + sell)"
    if rsi == "overbought" and bias == "buy":
        return False, "RSI gate (overbought + buy)"
    if ema and ema != "neutral":
        aligned = (bias == "buy" and ema == "up") or (bias == "sell" and ema == "down")
        rsi_reversal = (rsi == "overbought" and bias == "sell") or (rsi == "oversold" and bias == "buy")
        if not aligned and not rsi_reversal:
            return False, "trend filter"
    if open_side == bias:
        return False, "no-double"
    min_stop = price * 0.0012
    if spread > min_stop * 0.30:
        return False, "spread too wide"
    return True, ""


def run(symbol: str, entry_tf: str = "M15", max_bars: int = 600) -> dict:
    """Replay `symbol` over its MT5 history using the live logic."""
    hist = mt5_history_bridge.read_history()
    if not hist or symbol not in hist.get("symbols", {}):
        return {"error": f"ไม่มีข้อมูล history ของ {symbol} (รัน HistoryExporter.mq5 ก่อน)"}

    s = hist["symbols"][symbol]
    m1, h1 = s.get("m1"), s.get("h1")
    if not m1 or not h1:
        return {"error": f"{symbol}: ต้องมีทั้ง m1 และ h1"}

    factor = {"M5": 5, "M15": 15}.get(entry_tf, 15)
    entry_series = timeframe.resample(m1, factor)
    if not entry_series:
        return {"error": f"{symbol}: resample {entry_tf} ไม่ได้"}
    # timestamps for the resampled entry bars (close time of each group)
    n_m1 = len(m1["c"])
    start = n_m1 % factor
    entry_t = [m1["t"][start + i * factor + factor - 1] for i in range(len(entry_series["c"]))]

    warmup = 60
    total = len(entry_series["c"])
    begin = max(warmup, total - max_bars)

    trades: list[dict] = []
    open_trade: dict | None = None
    gate_stats = {"bars": 0, "engaged": 0, "bias_none": 0, "risk_blocked": 0,
                  "low_score": 0, "hours_blocked": 0, "entered": 0}

    for i in range(begin, total):
        ts = entry_t[i]
        price = entry_series["c"][i]

        # ---- manage an open trade first (SL/TP on this bar's range) ----
        if open_trade:
            hi, lo = entry_series["h"][i], entry_series["l"][i]
            hit = None
            if open_trade["side"] == "buy":
                if lo <= open_trade["sl"]:
                    hit, exit_px = "loss", open_trade["sl"]
                elif hi >= open_trade["tp"]:
                    hit, exit_px = "win", open_trade["tp"]
            else:
                if hi >= open_trade["sl"]:
                    hit, exit_px = "loss", open_trade["sl"]
                elif lo <= open_trade["tp"]:
                    hit, exit_px = "win", open_trade["tp"]
            if not hit and i - open_trade["bar"] >= MAX_HOLD_BARS:
                hit, exit_px = "timeout", price
            if hit:
                risk = abs(open_trade["entry"] - open_trade["sl"])
                move = (exit_px - open_trade["entry"]) if open_trade["side"] == "buy" else (open_trade["entry"] - exit_px)
                open_trade.update({"result": hit, "exit": round(exit_px, 5),
                                   "r": round(move / risk, 2) if risk else 0.0})
                trades.append(open_trade)
                open_trade = None

        gate_stats["bars"] += 1
        if open_trade:
            continue
        if _hour_blocked(ts):
            gate_stats["hours_blocked"] += 1
            continue

        # ---- build the same multi-TF view the live loop builds ----
        h1_idx = _bars_before(h1, ts)
        if h1_idx < 40:
            continue
        m1_idx = _bars_before(m1, ts)
        if m1_idx < 300:
            continue

        m1_slice = _slice(m1, m1_idx)
        h1_slice = _slice(h1, h1_idx)
        entry_slice = _slice(entry_series, i)

        ind = ind_mod.compute_snapshot(entry_slice["c"], ohlc=entry_slice)
        if not ind.get("ready"):
            continue
        atr = ind.get("atr") or price * 0.001
        spread = atr * 0.08  # historical spread proxy (~8% of ATR)

        mtf = mtf_engine.analyze(m1_slice, h1_slice, price, atr)
        if not mtf["engage"]:
            continue
        gate_stats["engaged"] += 1

        bias = _bias_from(ind, mtf["trend"]["overall"])
        if bias == "none":
            gate_stats["bias_none"] += 1
            continue

        ok, _why = _risk_ok(bias, ind, price, spread, None)
        if not ok:
            gate_stats["risk_blocked"] += 1
            continue

        audit = decision_audit.build(bias, ind, mtf, {"approved": True, "reason": "ok"})
        if audit["score"] < MIN_AUDIT_SCORE:
            gate_stats["low_score"] += 1
            continue

        sl_dist = max(atr * 2.0, spread * 8.0, price * 0.0012)
        tp_dist = sl_dist * 2.0
        sl = price - sl_dist if bias == "buy" else price + sl_dist
        tp = price + tp_dist if bias == "buy" else price - tp_dist
        open_trade = {"bar": i, "ts": ts, "side": bias, "entry": round(price, 5),
                      "sl": round(sl, 5), "tp": round(tp, 5),
                      "score": audit["score"], "trend": mtf["trend"]["overall"]}
        gate_stats["entered"] += 1

    return _summarize(symbol, entry_tf, trades, gate_stats)


def _summarize(symbol: str, entry_tf: str, trades: list[dict], gate: dict) -> dict:
    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    closed = len(wins) + len(losses)
    rs = [t["r"] for t in trades]

    gross_win = sum(t["r"] for t in wins)
    gross_loss = abs(sum(t["r"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None

    cum, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    for t in trades:
        cum += t["r"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append(round(cum, 2))

    return {
        "symbol": symbol,
        "entry_tf": entry_tf,
        "trades": len(trades),
        "win": len(wins),
        "loss": len(losses),
        "timeout": len([t for t in trades if t["result"] == "timeout"]),
        "win_rate_pct": round(len(wins) / closed * 100, 1) if closed else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "profit_factor": profit_factor,
        "max_drawdown_r": round(max_dd, 2),
        "equity_curve": curve,
        "funnel": gate,
        "sample_trades": trades[-8:],
    }


def run_batch(symbols: list[str] | None = None, entry_tf: str = "M15", max_bars: int = 600) -> dict:
    hist = mt5_history_bridge.read_history() or {}
    syms = symbols or list(hist.get("symbols", {}).keys())
    return {s: run(s, entry_tf=entry_tf, max_bars=max_bars) for s in syms}
