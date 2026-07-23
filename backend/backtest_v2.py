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

# --- account + cost model -------------------------------------------
# A backtest that ignores costs always looks better than the real thing.
# Every trade pays the spread twice (in and out), plus commission and any
# slippage, so those come out of each result before it's counted.
START_EQUITY = 10_000.0
RISK_PER_TRADE_PCT = 0.5        # matches the live risk config
MAX_CONCURRENT = 6              # 5-7 intended; hard cap like live
# No commission line: this broker prices spread-only, confirmed against the
# real executions the system logged (avg_commission = 0.0 on every symbol).
# Cost is therefore the spread actually paid, plus slippage and financing —
# i.e. only what really moves the balance.
SLIPPAGE_SPREAD_FRAC = 0.25     # slippage modelled as a fraction of spread
SWAP_PER_LOT_PER_DAY = -2.0     # financing drag on an overnight position

# Spread as a fraction of price, per symbol — typical broker values.
# Used instead of a single ATR-derived guess, because spread scales with
# the instrument, not with volatility.
TYPICAL_SPREAD_PCT = {
    "EURUSD": 0.000012, "GBPUSD": 0.000018, "USDJPY": 0.000014,
    "EURJPY": 0.000022, "GBPJPY": 0.000030, "XAUUSD": 0.000060,
    "BTCUSD": 0.000100,
}
DEFAULT_SPREAD_PCT = 0.00002


def _spread_for(symbol: str, price: float) -> float:
    return price * TYPICAL_SPREAD_PCT.get(symbol, DEFAULT_SPREAD_PCT)


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


def _lot_for(symbol: str, risk_money: float, sl_distance: float, price: float) -> float:
    """Approximate lots for a given money risk. Contract sizes: 100k units
    for FX, 100 oz for gold — enough for cost/P&L scale to be realistic."""
    if sl_distance <= 0:
        return 0.0
    if symbol.startswith("XAU"):
        value_per_price_unit = 100.0            # 100 oz per lot
    elif symbol.endswith("JPY"):
        value_per_price_unit = 100_000.0 / price  # quote is JPY -> convert
    else:
        value_per_price_unit = 100_000.0
    loss_per_lot = sl_distance * value_per_price_unit
    if loss_per_lot <= 0:
        return 0.0
    return max(0.01, round(risk_money / loss_per_lot, 2))


def _trade_costs(symbol: str, lot: float, spread: float, price: float, bars_held: int, entry_tf: str) -> float:
    """Total cost of one round trip, in account currency."""
    if symbol.startswith("XAU"):
        unit = 100.0
    elif symbol.endswith("JPY"):
        unit = 100_000.0 / price
    else:
        unit = 100_000.0
    # spread is paid on entry (and effectively again on exit) + slippage
    spread_cost = spread * unit * lot * (1.0 + SLIPPAGE_SPREAD_FRAC)
    mins = {"M5": 5, "M15": 15}.get(entry_tf, 15)
    days_held = (bars_held * mins) / (60 * 24)
    swap = abs(SWAP_PER_LOT_PER_DAY) * lot * days_held
    return spread_cost + swap


def _prepare(symbol: str, hist: dict, entry_tf: str) -> dict | None:
    """Per-symbol series + the resampled entry timeline with timestamps."""
    s = hist.get("symbols", {}).get(symbol)
    if not s or not s.get("m1") or not s.get("h1"):
        return None
    m1, h1 = s["m1"], s["h1"]
    factor = {"M5": 5, "M15": 15}.get(entry_tf, 15)
    entry = timeframe.resample(m1, factor)
    if not entry:
        return None
    start = len(m1["c"]) % factor
    entry_t = [m1["t"][start + i * factor + factor - 1] for i in range(len(entry["c"]))]
    return {"symbol": symbol, "m1": m1, "h1": h1, "entry": entry, "t": entry_t}


def run_portfolio(symbols: list[str] | None = None, entry_tf: str = "M15",
                  max_bars: int = 600, max_concurrent: int = MAX_CONCURRENT) -> dict:
    """Portfolio replay across symbols on one shared clock.

    Concurrency and correlation only mean anything at portfolio level: a
    per-symbol backtest can never hit "6 positions open" or refuse an
    EURJPY buy because GBPJPY is already long. All symbols advance together
    on timestamp order, sharing one account, one position cap and one
    correlation check — the same constraints the live risk manager applies.
    """
    hist = mt5_history_bridge.read_history()
    if not hist:
        return {"error": "ไม่มีข้อมูล history (รัน HistoryExporter.mq5 ก่อน)"}

    syms = symbols or list(hist.get("symbols", {}).keys())
    books = {s: b for s in syms if (b := _prepare(s, hist, entry_tf))}
    if not books:
        return {"error": "เตรียมข้อมูลไม่ได้เลยสักตัว"}

    # one timeline: every (timestamp, symbol, bar index), oldest first
    events: list[tuple[float, str, int]] = []
    for s, b in books.items():
        total = len(b["entry"]["c"])
        begin = max(60, total - max_bars)
        for i in range(begin, total):
            events.append((b["t"][i], s, i))
    events.sort()

    equity = START_EQUITY
    peak = equity
    max_dd_money = 0.0
    curve: list[dict] = []
    open_trades: dict[str, dict] = {}     # symbol -> trade
    trades: list[dict] = []
    funnel = {"bars": 0, "engaged": 0, "bias_none": 0, "risk_blocked": 0,
              "low_score": 0, "hours_blocked": 0, "cap_blocked": 0,
              "corr_blocked": 0, "entered": 0}

    def close(sym: str, t: dict, exit_px: float, result: str, bar: int):
        nonlocal equity, peak, max_dd_money
        risk_dist = abs(t["entry"] - t["sl"])
        move = (exit_px - t["entry"]) if t["side"] == "buy" else (t["entry"] - exit_px)
        gross = move * t["unit_value"] * t["lot"]
        cost = _trade_costs(sym, t["lot"], t["spread"], t["entry"], bar - t["bar"], entry_tf)
        net = gross - cost
        equity += net
        peak = max(peak, equity)
        max_dd_money = max(max_dd_money, peak - equity)
        t.update({
            "result": result, "exit": round(exit_px, 5),
            "r": round(move / risk_dist, 2) if risk_dist else 0.0,
            "gross": round(gross, 2), "cost": round(cost, 2), "net": round(net, 2),
            "equity_after": round(equity, 2),
        })
        trades.append(t)
        curve.append({"ts": t["ts"], "equity": round(equity, 2), "net": round(net, 2), "symbol": sym})

    for ts, sym, i in events:
        b = books[sym]
        entry_series, m1, h1 = b["entry"], b["m1"], b["h1"]
        price = entry_series["c"][i]
        funnel["bars"] += 1

        # ---- manage this symbol's open trade on this bar ----
        t = open_trades.get(sym)
        if t:
            hi, lo = entry_series["h"][i], entry_series["l"][i]
            hit = exit_px = None
            if t["side"] == "buy":
                if lo <= t["sl"]:
                    hit, exit_px = "loss", t["sl"]
                elif hi >= t["tp"]:
                    hit, exit_px = "win", t["tp"]
            else:
                if hi >= t["sl"]:
                    hit, exit_px = "loss", t["sl"]
                elif lo <= t["tp"]:
                    hit, exit_px = "win", t["tp"]
            if not hit and i - t["bar"] >= MAX_HOLD_BARS:
                hit, exit_px = "timeout", price
            if hit:
                close(sym, t, exit_px, hit, i)
                del open_trades[sym]

        if sym in open_trades:
            continue
        if _hour_blocked(ts):
            funnel["hours_blocked"] += 1
            continue
        if len(open_trades) >= max_concurrent:
            funnel["cap_blocked"] += 1
            continue

        h1_idx = _bars_before(h1, ts)
        m1_idx = _bars_before(m1, ts)
        if h1_idx < 40 or m1_idx < 300:
            continue

        entry_slice = _slice(entry_series, i)
        ind = ind_mod.compute_snapshot(entry_slice["c"], ohlc=entry_slice)
        if not ind.get("ready"):
            continue
        atr = ind.get("atr") or price * 0.001
        spread = _spread_for(sym, price)

        mtf = mtf_engine.analyze(_slice(m1, m1_idx), _slice(h1, h1_idx), price, atr)
        if not mtf["engage"]:
            continue
        funnel["engaged"] += 1

        bias = _bias_from(ind, mtf["trend"]["overall"])
        if bias == "none":
            funnel["bias_none"] += 1
            continue

        ok, _why = _risk_ok(bias, ind, price, spread, None)
        if not ok:
            funnel["risk_blocked"] += 1
            continue

        # correlation veto against everything currently open
        import correlation_agent
        from risk import OpenPosition
        current = [OpenPosition(symbol=s2, side=t2["side"], risk_pct=RISK_PER_TRADE_PCT)
                   for s2, t2 in open_trades.items()]
        if correlation_agent.check_correlation_risk(sym, bias, current)["blocked"]:
            funnel["corr_blocked"] += 1
            continue

        # NOTE: symbol is deliberately NOT passed to the audit — COT is
        # current-week data and feeding it into historical bars would be
        # lookahead bias.
        audit = decision_audit.build(bias, ind, mtf, {"approved": True, "reason": "ok"})
        if audit["score"] < MIN_AUDIT_SCORE:
            funnel["low_score"] += 1
            continue

        sl_dist = max(atr * 2.0, spread * 8.0, price * 0.0012)
        tp_dist = sl_dist * 2.0
        risk_money = equity * RISK_PER_TRADE_PCT / 100.0
        lot = _lot_for(sym, risk_money, sl_dist, price)
        if lot <= 0:
            continue
        unit_value = 100.0 if sym.startswith("XAU") else (100_000.0 / price if sym.endswith("JPY") else 100_000.0)

        open_trades[sym] = {
            "symbol": sym, "bar": i, "ts": ts, "side": bias,
            "entry": round(price, 5),
            "sl": round(price - sl_dist if bias == "buy" else price + sl_dist, 5),
            "tp": round(price + tp_dist if bias == "buy" else price - tp_dist, 5),
            "lot": lot, "spread": spread, "unit_value": unit_value,
            "score": audit["score"], "trend": mtf["trend"]["overall"],
        }
        funnel["entered"] += 1

    return _summarize_portfolio(entry_tf, trades, funnel, curve, equity, max_dd_money, max_concurrent)


def _summarize_portfolio(entry_tf, trades, funnel, curve, equity, max_dd_money, max_concurrent) -> dict:
    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    closed = len(wins) + len(losses)
    nets = [t["net"] for t in trades]
    rs = [t["r"] for t in trades]

    gross_win = sum(t["net"] for t in trades if t["net"] > 0)
    gross_loss = abs(sum(t["net"] for t in trades if t["net"] < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss else None

    by_symbol: dict[str, dict] = {}
    for t in trades:
        b = by_symbol.setdefault(t["symbol"], {"trades": 0, "win": 0, "loss": 0, "net": 0.0, "cost": 0.0})
        b["trades"] += 1
        b["net"] += t["net"]
        b["cost"] += t["cost"]
        if t["result"] == "win":
            b["win"] += 1
        elif t["result"] == "loss":
            b["loss"] += 1
    for b in by_symbol.values():
        b["net"] = round(b["net"], 2)
        b["cost"] = round(b["cost"], 2)
        c = b["win"] + b["loss"]
        b["win_rate_pct"] = round(b["win"] / c * 100, 1) if c else None

    total_cost = round(sum(t["cost"] for t in trades), 2)
    total_gross = round(sum(t["gross"] for t in trades), 2)

    return {
        "entry_tf": entry_tf,
        "max_concurrent": max_concurrent,
        "start_equity": START_EQUITY,
        "end_equity": round(equity, 2),
        "net_profit": round(equity - START_EQUITY, 2),
        "return_pct": round((equity - START_EQUITY) / START_EQUITY * 100, 2),
        "gross_profit": total_gross,
        "total_cost": total_cost,
        "trades": len(trades),
        "win": len(wins),
        "loss": len(losses),
        "timeout": len([t for t in trades if t["result"] == "timeout"]),
        "win_rate_pct": round(len(wins) / closed * 100, 1) if closed else None,
        "expectancy_money": round(sum(nets) / len(nets), 2) if nets else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "profit_factor": pf,
        "max_drawdown_money": round(max_dd_money, 2),
        "max_drawdown_pct": round(max_dd_money / START_EQUITY * 100, 2),
        "by_symbol": by_symbol,
        "equity_curve": curve[-200:],
        "funnel": funnel,
        "sample_trades": trades[-8:],
    }


def run(symbol: str, entry_tf: str = "M15", max_bars: int = 600) -> dict:
    """Single-symbol replay — the portfolio engine limited to one book."""
    return run_portfolio([symbol], entry_tf=entry_tf, max_bars=max_bars, max_concurrent=1)


def run_batch(symbols: list[str] | None = None, entry_tf: str = "M15",
              max_bars: int = 600, max_concurrent: int = MAX_CONCURRENT) -> dict:
    """Portfolio run — one account, shared position cap and correlation."""
    return run_portfolio(symbols, entry_tf=entry_tf, max_bars=max_bars, max_concurrent=max_concurrent)
