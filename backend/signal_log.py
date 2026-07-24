"""Persists every signal the CEO council approves, then checks live
price against each open signal's SL/TP to settle it as a win or loss —
the basis for a real win-rate measurement instead of a guess.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "signals.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            entry REAL NOT NULL,
            sl REAL NOT NULL,
            tp REAL NOT NULL,
            risk_pct REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at REAL,
            exit_price REAL,
            rsi_state TEXT,
            ema_trend TEXT,
            reason TEXT
        )
        """
    )
    # Attempt to add the new column for Machine Learning
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN indicators_json TEXT")
    except sqlite3.OperationalError:
        pass # Column might already exist

    # Attempt to add column for CEO votes
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN ai_votes TEXT")
    except sqlite3.OperationalError:
        pass

    # Real execution costs — filled in once the EA reports back after
    # placing the order. NULL until then (no execution yet, or rejected).
    for col in ("slippage REAL", "commission REAL", "swap REAL", "filled_price REAL"):
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # SMC structure event (BOS/CHoCH/none) and multi-timeframe confluence
    # (full/fast_only/swing_only/none) AT DECISION TIME — without storing
    # these per signal, get_learned_patterns() can never tell us whether
    # SMC/MTF sizing actually correlates with real win rate; it'd just be
    # static logic forever, never folded into the self-learning loop.
    for col in ("structure_event TEXT", "mtf_confluence TEXT"):
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    # Real MT5 ticket — needed because once the EA's partial-close/
    # trailing-stop management moves a position's SL away from the value
    # recorded here at entry time, check_open_signals() comparing live
    # price against the ORIGINAL sl/tp can miss the real close entirely
    # (price may never revisit those stale levels again). Tracking the
    # ticket lets check_settled_by_ticket() detect "this position no
    # longer exists in MT5" directly instead.
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN ticket INTEGER")
    except sqlite3.OperationalError:
        pass

    # Real net P/L in account currency, taken from MT5's closed-deal history
    # (commission + swap included). NULL for signals settled by the older
    # price-estimate fallback.
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN profit REAL")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def set_ticket(signal_id: int, ticket: int):
    conn = _connect()
    conn.execute("UPDATE signals SET ticket = ? WHERE id = ?", (ticket, signal_id))
    conn.commit()
    conn.close()


def record_execution(signal_id: int, slippage: float, commission: float, swap: float, filled_price: float):
    """Called once the EA's result file confirms a fill — stores the
    real cost of that trade so get_cost_stats() reflects this broker's
    actual behavior instead of an assumption.
    """
    conn = _connect()
    conn.execute(
        "UPDATE signals SET slippage = ?, commission = ?, swap = ?, filled_price = ? WHERE id = ?",
        (slippage, commission, swap, filled_price, signal_id),
    )
    conn.commit()
    conn.close()


def log_signal(decision: dict, indicators: dict | None = None, reason: str | None = None, mtf_confluence: str | None = None) -> int:
    import json
    indicators = indicators or {}
    ind_json = json.dumps(indicators)
    votes_json = json.dumps(decision.get("council", {}).get("votes", []))
    smc = indicators.get("smc") or {}
    structure_event = smc.get("structure_event")
    conn = _connect()
    cur = conn.execute(
        """
        INSERT INTO signals (created_at, symbol, action, entry, sl, tp, risk_pct, status, rsi_state, ema_trend, reason, indicators_json, ai_votes, structure_event, mtf_confluence)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(),
            decision["symbol"],
            decision["action"],
            decision["entry"],
            decision["sl"],
            decision["tp"],
            decision["risk_pct"],
            indicators.get("rsi_state"),
            indicators.get("ema_trend"),
            reason,
            ind_json,
            votes_json,
            structure_event,
            mtf_confluence,
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def check_open_signals(current_prices: dict[str, float]) -> list[dict]:
    """Settle any open signal whose symbol's current price has hit SL/TP.
    Returns the list of newly-settled signals."""
    conn = _connect()
    open_rows = conn.execute("SELECT * FROM signals WHERE status = 'open'").fetchall()

    settled = []
    for row in open_rows:
        price = current_prices.get(row["symbol"])
        if price is None:
            continue

        hit = None
        if row["action"] == "buy":
            if price >= row["tp"]:
                hit = "win"
            elif price <= row["sl"]:
                hit = "loss"
        else:  # sell
            if price <= row["tp"]:
                hit = "win"
            elif price >= row["sl"]:
                hit = "loss"

        if hit:
            conn.execute(
                "UPDATE signals SET status = ?, closed_at = ?, exit_price = ? WHERE id = ?",
                (hit, time.time(), price, row["id"]),
            )
            settled.append({"id": row["id"], "symbol": row["symbol"], "action": row["action"], "result": hit})

    conn.commit()
    conn.close()
    return settled


def get_equity_curve() -> list[dict]:
    """Cumulative realized P/L over closed trades, oldest→newest — the
    equity curve. Uses the real MT5 `profit` when available, else falls
    back to R-multiple × a nominal 1R so older estimate-settled trades
    still contribute a shape."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, symbol, action, closed_at, created_at, entry, sl, exit_price, status, profit "
        "FROM signals WHERE ticket IS NOT NULL AND status IN ('win','loss','breakeven') "
        "ORDER BY COALESCE(closed_at, created_at)"
    ).fetchall()
    conn.close()

    curve, cum = [], 0.0
    for r in rows:
        if r["profit"] is not None:
            pnl = float(r["profit"])
        else:
            # no real deal P/L — approximate from R so the curve isn't blank
            risk = abs((r["entry"] or 0) - (r["sl"] or 0))
            if risk and r["exit_price"] is not None:
                move = (r["exit_price"] - r["entry"]) if r["action"] == "buy" else (r["entry"] - r["exit_price"])
                pnl = max(-20.0, min(20.0, move / risk))  # in R, capped
            else:
                pnl = 0.0
        cum += pnl
        curve.append({
            "id": r["id"], "symbol": r["symbol"], "action": r["action"],
            "closed_at": r["closed_at"] or r["created_at"],
            "pnl": round(pnl, 2), "cumulative": round(cum, 2),
            "status": r["status"], "real": r["profit"] is not None,
        })
    return curve


def get_trade_journal(limit: int = 50) -> list[dict]:
    """Per-trade detail for the journal view: why it was entered, what the
    levels were, and how it actually closed."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, created_at, closed_at, symbol, action, entry, sl, tp, exit_price, status, "
        "profit, risk_pct, rsi_state, ema_trend, structure_event, mtf_confluence, reason, ticket, "
        "slippage, commission, swap "
        "FROM signals WHERE ticket IS NOT NULL ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        risk = abs((d["entry"] or 0) - (d["sl"] or 0))
        if risk and d["exit_price"] is not None:
            move = (d["exit_price"] - d["entry"]) if d["action"] == "buy" else (d["entry"] - d["exit_price"])
            d["r_multiple"] = round(max(-20.0, min(20.0, move / risk)), 2)
        else:
            d["r_multiple"] = None
        out.append(d)
    return out


def get_open_tickets() -> list[int]:
    """Tickets of signals still marked open — the set to ask MT5 about."""
    conn = _connect()
    rows = conn.execute(
        "SELECT ticket FROM signals WHERE status = 'open' AND ticket IS NOT NULL"
    ).fetchall()
    conn.close()
    return [r["ticket"] for r in rows]


def settle_by_real_deals(close_info: dict, live_tickets: set | None = None) -> list[dict]:
    """Settle open signals using REAL closed-deal data from MT5.

    close_info: {ticket: {net_profit, exit_price, closed_at, ...}} from
    mt5_direct.get_close_info(). Because MT5 reports the actual money the
    position made or lost (commission and swap included), win/loss here is
    fact, not the "last price vs entry" estimate the fallback below has to
    use. Anything netting within ±BREAKEVEN_MONEY of zero settles as
    breakeven so scratch trades don't inflate the win rate.

    Returns the list of newly-settled signals.
    """
    if not close_info:
        return []
    BREAKEVEN_MONEY = 0.50  # account currency; below this = a scratch trade

    conn = _connect()
    rows = conn.execute(
        "SELECT id, symbol, action, ticket FROM signals WHERE status = 'open' AND ticket IS NOT NULL"
    ).fetchall()

    settled = []
    for row in rows:
        # A partial close (the EA's TP1/trailing management) also writes a
        # DEAL_ENTRY_OUT, so a deal existing does NOT mean the position is
        # finished. Only settle once the ticket is genuinely gone from MT5's
        # open-position list — otherwise a half-closed winner gets recorded
        # as fully closed while it's still running.
        if live_tickets is not None and row["ticket"] in live_tickets:
            continue
        info = close_info.get(row["ticket"])
        if not info:
            continue
        net = info.get("net_profit", 0.0)
        exit_p = info.get("exit_price") or 0.0
        entry_p = row.get("entry") or 0.0
        tp_p = row.get("tp") or 0.0

        # Require price to reach >= 75% of TP distance to count as a real "WIN"
        # Small profits from trailing stop or early closes settle as "breakeven"
        # so they don't inflate the win rate with incomplete setups.
        tp_dist = abs(tp_p - entry_p) if (tp_p and entry_p) else 0.0
        reached_dist = abs(exit_p - entry_p) if (exit_p and entry_p) else 0.0

        if net > BREAKEVEN_MONEY:
            if tp_dist > 0 and reached_dist >= (tp_dist * 0.75):
                status = "win"
            else:
                status = "breakeven"
        elif net < -BREAKEVEN_MONEY:
            status = "loss"
        else:
            status = "breakeven"
        conn.execute(
            "UPDATE signals SET status = ?, closed_at = ?, exit_price = ?, profit = ? WHERE id = ?",
            (status, info.get("closed_at", time.time()), info.get("exit_price"), net, row["id"]),
        )
        settled.append({
            "id": row["id"], "symbol": row["symbol"], "action": row["action"],
            "result": status, "profit": net, "source": "mt5_deal",
        })

    conn.commit()
    conn.close()
    return settled


def check_settled_by_ticket(current_prices: dict[str, float], live_tickets: set[int]) -> list[dict]:
    BREAKEVEN_BAND_PCT = 0.0005  # 0.05% of entry price

    conn = _connect()
    open_rows = conn.execute("SELECT * FROM signals WHERE status = 'open' AND ticket IS NOT NULL").fetchall()

    settled = []
    for row in open_rows:
        if row["ticket"] in live_tickets:
            continue  # still open in MT5, nothing to do

        price = current_prices.get(row["symbol"])
        if price is None:
            continue

        entry = row["entry"]
        tp_p = row.get("tp") or 0.0
        tp_dist = abs(tp_p - entry) if (tp_p and entry) else 0.0
        reached_dist = abs(price - entry)

        band = abs(entry) * BREAKEVEN_BAND_PCT
        if abs(price - entry) <= band:
            hit = "breakeven"
        elif row["action"] == "buy":
            is_pos = price > entry
            hit = "win" if (is_pos and tp_dist > 0 and reached_dist >= (tp_dist * 0.75)) else ("breakeven" if is_pos else "loss")
        else:
            is_pos = price < entry
            hit = "win" if (is_pos and tp_dist > 0 and reached_dist >= (tp_dist * 0.75)) else ("breakeven" if is_pos else "loss")

        conn.execute(
            "UPDATE signals SET status = ?, closed_at = ?, exit_price = ? WHERE id = ?",
            (hit, time.time(), price, row["id"]),
        )
        settled.append({"id": row["id"], "symbol": row["symbol"], "action": row["action"], "result": hit})

    conn.commit()
    conn.close()
    return settled


def get_recent_mistakes(symbol: str | None = None, limit: int = 5) -> list[dict]:
    """The actual reasoning text behind the most recent LOSSES — not just
    a win-rate number per indicator bucket, but what the agent actually
    said at entry time, so it can be told directly 'here is what you said
    last time this went wrong' instead of only seeing an aggregate stat.
    This is the literal 'learn from its own mistakes' mechanism.
    """
    conn = _connect()
    query = "SELECT id, symbol, action, reason, rsi_state, ema_trend, structure_event, mtf_confluence, created_at FROM signals WHERE status = 'loss'"
    params = ()
    if symbol:
        query += " AND symbol = ?"
        params = (symbol,)
    query += " ORDER BY created_at DESC LIMIT ?"
    params = params + (limit,)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_learned_patterns(min_samples: int = 3, symbol: str | None = None) -> list[dict]:
    """Win rate per (action, rsi_state, ema_trend) combo seen in closed
    signals — only combos with enough samples to mean anything. This is
    what 'self-learning from its own trades' actually is: no model
    retraining, just surfacing which setups historically worked so the
    LLM can weigh them when reasoning about a new one.

    `symbol` filters to one symbol's own history — needed because a
    purely cross-symbol pattern (no symbol filter) let one symbol's bad
    streak (e.g. XAUUSD breaking even repeatedly on "sell/neutral/up")
    auto-disable that exact combo for EVERY symbol, even ones that never
    had a problem with it (confirmed live: blocked simultaneous real
    sell signals on EURUSD/GBPUSD/USDJPY for a full day because they
    happened to share that one indicator bucket).
    """
    conn = _connect()
    query = "SELECT action, rsi_state, ema_trend, status, COUNT(*) as n FROM signals WHERE ticket IS NOT NULL AND status IN ('win', 'loss') AND rsi_state IS NOT NULL"
    params = ()
    if symbol:
        query += " AND symbol = ?"
        params = (symbol,)
    query += " GROUP BY action, rsi_state, ema_trend, status"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    combos: dict[tuple, dict] = {}
    for row in rows:
        key = (row["action"], row["rsi_state"], row["ema_trend"])
        combos.setdefault(key, {"win": 0, "loss": 0})
        combos[key][row["status"]] = row["n"]

    patterns = []
    for (action, rsi_state, ema_trend), counts in combos.items():
        total = counts["win"] + counts["loss"]
        if total < min_samples:
            continue
        patterns.append(
            {
                "action": action,
                "rsi_state": rsi_state,
                "ema_trend": ema_trend,
                "win": counts["win"],
                "loss": counts["loss"],
                "samples": total,
                "win_rate_pct": round(counts["win"] / total * 100, 1),
            }
        )
    patterns.sort(key=lambda p: p["win_rate_pct"], reverse=True)
    return patterns


def get_structure_patterns(min_samples: int = 3, symbol: str | None = None) -> list[dict]:
    """Win rate per (structure_event, mtf_confluence) combo — this is what
    turns SMC/Elliott/multi-timeframe sizing from static rules we wrote
    into something the self-learning loop can actually evaluate: does a
    CHoCH signal with 'full' MTF confluence really win more than a BOS
    with 'fast_only'? Without this breakdown there was no way to tell —
    the logic just ran the same regardless of whether it actually helped.

    `symbol` filters to one symbol's own history — see get_learned_patterns()
    for why this matters (one symbol's bad streak shouldn't auto-disable
    every other symbol that happens to share the same SMC bucket).
    """
    conn = _connect()
    try:
        query = "SELECT structure_event, mtf_confluence, status, COUNT(*) as n FROM signals WHERE ticket IS NOT NULL AND status IN ('win', 'loss') AND structure_event IS NOT NULL"
        params = ()
        if symbol:
            query += " AND symbol = ?"
            params = (symbol,)
        query += " GROUP BY structure_event, mtf_confluence, status"
        rows = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    combos: dict[tuple, dict] = {}
    for row in rows:
        key = (row["structure_event"], row["mtf_confluence"])
        combos.setdefault(key, {"win": 0, "loss": 0})
        combos[key][row["status"]] = row["n"]

    patterns = []
    for (structure_event, mtf_confluence), counts in combos.items():
        total = counts["win"] + counts["loss"]
        if total < min_samples:
            continue
        patterns.append(
            {
                "structure_event": structure_event,
                "mtf_confluence": mtf_confluence,
                "win": counts["win"],
                "loss": counts["loss"],
                "samples": total,
                "win_rate_pct": round(counts["win"] / total * 100, 1),
            }
        )
    patterns.sort(key=lambda p: p["win_rate_pct"], reverse=True)
    return patterns


def get_cost_stats(symbol: str | None = None) -> dict:
    """Average slippage (in price units, signed) and commission+swap (in
    account currency) per symbol from real executions. This is what
    'learning slippage and fees' actually means here — no model, just
    an honest running average of what this broker actually charges,
    so position sizing and TP distance can account for it instead of
    assuming a frictionless fill.
    """
    conn = _connect()
    query = "SELECT symbol, action, slippage, commission, swap FROM signals WHERE slippage IS NOT NULL"
    params = ()
    if symbol:
        query += " AND symbol = ?"
        params = (symbol,)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    by_symbol: dict[str, dict] = {}
    for row in rows:
        sym = row["symbol"]
        bucket = by_symbol.setdefault(
            sym, {"symbol": sym, "samples": 0, "slippage_sum": 0.0, "commission_sum": 0.0, "swap_sum": 0.0}
        )
        # Direction-aware: for a buy, filling higher than requested is bad
        # (positive cost); for a sell, filling lower than requested is bad.
        # Flip sign on sell so "positive" always means "cost the trader money".
        signed_slippage = row["slippage"] if row["action"] == "buy" else -row["slippage"]
        bucket["samples"] += 1
        bucket["slippage_sum"] += signed_slippage
        bucket["commission_sum"] += row["commission"] or 0.0
        bucket["swap_sum"] += row["swap"] or 0.0

    results = []
    for sym, b in by_symbol.items():
        n = b["samples"]
        results.append(
            {
                "symbol": sym,
                "samples": n,
                "avg_slippage": round(b["slippage_sum"] / n, 6),
                "avg_commission": round(b["commission_sum"] / n, 2),
                "avg_swap": round(b["swap_sum"] / n, 2),
            }
        )

    if symbol:
        return results[0] if results else {"symbol": symbol, "samples": 0, "avg_slippage": 0, "avg_commission": 0, "avg_swap": 0}
    return {"results": results}


def get_stats() -> dict:
    import datetime
    conn = _connect()
    # REAL_ONLY: every stat counts only trades that actually filled on MT5
    # (ticket IS NOT NULL). Signals that never executed — mock-data cycles,
    # symbols not in Market Watch (US30/NAS100) — are excluded so win rate,
    # expectancy and the calendar reflect real trades only.
    rows = conn.execute("SELECT status, COUNT(*) as n FROM signals WHERE ticket IS NOT NULL GROUP BY status").fetchall()
    dir_rows = conn.execute("SELECT action, COUNT(*) as n FROM signals WHERE ticket IS NOT NULL GROUP BY action").fetchall()

    # Daily trade counts — group by date (UTC+7)
    all_signals = conn.execute("SELECT created_at, action FROM signals WHERE ticket IS NOT NULL ORDER BY created_at").fetchall()
    conn.close()

    counts = {"open": 0, "win": 0, "loss": 0, "breakeven": 0}
    for row in rows:
        counts[row["status"]] = row["n"]

    direction = {"buy": 0, "sell": 0}
    for row in dir_rows:
        if row["action"] in direction:
            direction[row["action"]] = row["n"]

    # Count per calendar day (Thai time UTC+7)
    tz = datetime.timezone(datetime.timedelta(hours=7))
    daily: dict[str, dict] = {}
    for row in all_signals:
        dt = datetime.datetime.fromtimestamp(row["created_at"], tz=tz).strftime("%Y-%m-%d")
        bucket = daily.setdefault(dt, {"date": dt, "total": 0, "buy": 0, "sell": 0})
        bucket["total"] += 1
        if row["action"] in ("buy", "sell"):
            bucket[row["action"]] += 1
    daily_list = sorted(daily.values(), key=lambda x: x["date"], reverse=True)

    closed = counts["win"] + counts["loss"]
    win_rate = round(counts["win"] / closed * 100, 1) if closed > 0 else None

    return {
        "total": sum(counts.values()),
        "open": counts["open"],
        "win": counts["win"],
        "loss": counts["loss"],
        "breakeven": counts["breakeven"],
        "closed": closed,
        "win_rate_pct": win_rate,
        "buy_count": direction["buy"],
        "sell_count": direction["sell"],
        "daily": daily_list,
    }


def get_direction_stats() -> dict:
    """Win/loss/breakeven แยกตาม buy vs sell พร้อม win rate และ
    แยกตาม symbol ด้วย — ใช้วิเคราะห์ว่าระบบชนะด้วย direction ไหน
    และ symbol ไหนทำกำไรได้จริง"""
    import datetime
    conn = _connect()
    rows = conn.execute(
        "SELECT action, symbol, status, COUNT(*) as n FROM signals "
        "WHERE ticket IS NOT NULL AND status IN ('win','loss','breakeven') "
        "GROUP BY action, symbol, status"
    ).fetchall()

    # daily breakdown แยก direction
    daily_rows = conn.execute(
        "SELECT created_at, action, status FROM signals ORDER BY created_at"
    ).fetchall()
    conn.close()

    # รวมตาม direction
    by_dir: dict[str, dict] = {
        "buy":  {"win": 0, "loss": 0, "breakeven": 0},
        "sell": {"win": 0, "loss": 0, "breakeven": 0},
    }
    # รวมตาม symbol+direction
    by_sym: dict[str, dict] = {}
    for row in rows:
        d = row["action"]
        s = row["symbol"]
        st = row["status"]
        if d in by_dir:
            by_dir[d][st] = by_dir[d].get(st, 0) + row["n"]
        key = f"{s}_{d}"
        bucket = by_sym.setdefault(key, {"symbol": s, "action": d, "win": 0, "loss": 0, "breakeven": 0})
        bucket[st] = bucket.get(st, 0) + row["n"]

    def _wr(b):
        closed = b["win"] + b["loss"]
        return round(b["win"] / closed * 100, 1) if closed > 0 else None

    direction_summary = []
    for d, b in by_dir.items():
        closed = b["win"] + b["loss"]
        direction_summary.append({
            "action": d,
            "win": b["win"],
            "loss": b["loss"],
            "breakeven": b["breakeven"],
            "closed": closed,
            "win_rate_pct": _wr(b),
        })

    symbol_breakdown = []
    for b in sorted(by_sym.values(), key=lambda x: x["win"] + x["loss"], reverse=True):
        closed = b["win"] + b["loss"]
        symbol_breakdown.append({
            "symbol": b["symbol"],
            "action": b["action"],
            "win": b["win"],
            "loss": b["loss"],
            "breakeven": b["breakeven"],
            "closed": closed,
            "win_rate_pct": _wr(b),
        })

    # daily win/loss แยก direction (UTC+7)
    tz = datetime.timezone(datetime.timedelta(hours=7))
    daily: dict[str, dict] = {}
    for row in daily_rows:
        dt = datetime.datetime.fromtimestamp(row["created_at"], tz=tz).strftime("%Y-%m-%d")
        bucket = daily.setdefault(dt, {
            "date": dt,
            "buy_win": 0, "buy_loss": 0, "buy_be": 0,
            "sell_win": 0, "sell_loss": 0, "sell_be": 0,
        })
        d = row["action"]
        st = row["status"]
        if d == "buy":
            if st == "win": bucket["buy_win"] += 1
            elif st == "loss": bucket["buy_loss"] += 1
            elif st == "breakeven": bucket["buy_be"] += 1
        elif d == "sell":
            if st == "win": bucket["sell_win"] += 1
            elif st == "loss": bucket["sell_loss"] += 1
            elif st == "breakeven": bucket["sell_be"] += 1

    def _wr2(w, l): return round(w / (w + l) * 100, 1) if (w + l) > 0 else None
    daily_list = []
    for b in sorted(daily.values(), key=lambda x: x["date"], reverse=True):
        daily_list.append({
            "date": b["date"],
            "buy": {"win": b["buy_win"], "loss": b["buy_loss"], "be": b["buy_be"], "win_rate": _wr2(b["buy_win"], b["buy_loss"])},
            "sell": {"win": b["sell_win"], "loss": b["sell_loss"], "be": b["sell_be"], "win_rate": _wr2(b["sell_win"], b["sell_loss"])},
        })

    return {
        "direction": direction_summary,
        "by_symbol": symbol_breakdown,
        "daily": daily_list,
    }


def get_hourly_stats() -> list[dict]:
    """Win/loss แยกตามชั่วโมง (เวลาไทย UTC+7) — บอกว่าชั่วโมงไหน
    ระบบชนะ/แพ้บ่อย ใช้วางแผนว่าควรเปิดระบบช่วงเวลาไหน"""
    import datetime
    conn = _connect()
    rows = conn.execute(
        "SELECT created_at, status FROM signals WHERE ticket IS NOT NULL AND status IN ('win','loss','breakeven')"
    ).fetchall()
    conn.close()

    tz = datetime.timezone(datetime.timedelta(hours=7))
    hourly: dict[int, dict] = {h: {"hour": h, "win": 0, "loss": 0, "breakeven": 0} for h in range(24)}
    for row in rows:
        h = datetime.datetime.fromtimestamp(row["created_at"], tz=tz).hour
        st = row["status"]
        hourly[h][st] = hourly[h].get(st, 0) + 1

    result = []
    for h in range(24):
        b = hourly[h]
        closed = b["win"] + b["loss"]
        wr = round(b["win"] / closed * 100, 1) if closed > 0 else None
        result.append({
            "hour": h,
            "win": b["win"],
            "loss": b["loss"],
            "breakeven": b["breakeven"],
            "total": closed + b["breakeven"],
            "win_rate_pct": wr,
        })
    return result


def get_daily_pnl() -> list[dict]:
    """Per-calendar-day (Thai time UTC+7) result from closed trades.

    Reports BOTH:
      • net_profit — real money from MT5's closed deals (commission+swap in)
      • net_r      — R-multiple, for days whose trades predate real-P/L data

    `has_money` says whether every trade that day carried a real P/L, so the
    calendar can show actual currency and fall back to R only where the
    money isn't known.
    Returns newest day first.
    """
    import datetime
    conn = _connect()
    rows = conn.execute(
        "SELECT created_at, action, entry, sl, exit_price, status, profit FROM signals "
        "WHERE ticket IS NOT NULL AND status IN ('win','loss','breakeven') AND exit_price IS NOT NULL AND sl IS NOT NULL"
    ).fetchall()
    conn.close()

    MAX_R = 20.0
    tz = datetime.timezone(datetime.timedelta(hours=7))
    daily: dict[str, dict] = {}
    for row in rows:
        risk_dist = abs(row["entry"] - row["sl"])
        if risk_dist == 0:
            continue
        move = (row["exit_price"] - row["entry"]) if row["action"] == "buy" else (row["entry"] - row["exit_price"])
        r = move / risk_dist
        if abs(r) > MAX_R:
            continue
        d = datetime.datetime.fromtimestamp(row["created_at"], tz=tz).strftime("%Y-%m-%d")
        b = daily.setdefault(d, {"date": d, "net_r": 0.0, "net_profit": 0.0,
                                 "wins": 0, "losses": 0, "breakeven": 0,
                                 "trades": 0, "money_trades": 0})
        b["net_r"] += r
        b["trades"] += 1
        if row["profit"] is not None:
            b["net_profit"] += float(row["profit"])
            b["money_trades"] += 1
        if row["status"] == "win":
            b["wins"] += 1
        elif row["status"] == "loss":
            b["losses"] += 1
        else:
            b["breakeven"] += 1

    out = []
    for b in sorted(daily.values(), key=lambda x: x["date"], reverse=True):
        b["net_r"] = round(b["net_r"], 2)
        b["net_profit"] = round(b["net_profit"], 2)
        b["has_money"] = b["money_trades"] == b["trades"] and b["trades"] > 0
        out.append(b)
    return out


def get_symbol_expectancy_all() -> list[dict]:
    """Expectancy (R-multiple) แยกตาม symbol — ช่วยบอกว่าสินทรัพย์ไหน
    ทำกำไรจริงในแง่ R ไม่ใช่แค่ win rate"""
    import math
    conn = _connect()
    rows = conn.execute(
        "SELECT symbol, action, entry, sl, exit_price, status FROM signals "
        "WHERE ticket IS NOT NULL AND status IN ('win','loss','breakeven') AND exit_price IS NOT NULL AND sl IS NOT NULL"
    ).fetchall()
    conn.close()

    MAX_R = 20.0
    by_sym: dict[str, list] = {}
    for row in rows:
        risk_dist = abs(row["entry"] - row["sl"])
        if risk_dist == 0:
            continue
        pnl = (row["exit_price"] - row["entry"]) if row["action"] == "buy" else (row["entry"] - row["exit_price"])
        r = pnl / risk_dist
        if abs(r) > MAX_R:
            continue
        by_sym.setdefault(row["symbol"], []).append(r)

    result = []
    for sym, rs in sorted(by_sym.items()):
        if not rs:
            continue
        exp = round(sum(rs) / len(rs), 3)
        wins  = [r for r in rs if r > 0.05]
        losses = [r for r in rs if r < -0.05]
        result.append({
            "symbol": sym,
            "samples": len(rs),
            "expectancy_r": exp,
            "avg_win_r":  round(sum(wins)  / len(wins),  3) if wins   else None,
            "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
            "win_count":  len(wins),
            "loss_count": len(losses),
        })
    result.sort(key=lambda x: x["expectancy_r"], reverse=True)
    return result


def get_rsi_ema_matrix() -> dict:
    """ตาราง win rate แบบ RSI state × EMA trend — เห็นทันทีว่าคอมโบไหน
    ชนะ/แพ้ แยกตาม buy/sell ด้วย"""
    conn = _connect()
    rows = conn.execute(
        "SELECT action, rsi_state, ema_trend, status, COUNT(*) as n FROM signals "
        "WHERE ticket IS NOT NULL AND status IN ('win','loss') AND rsi_state IS NOT NULL AND ema_trend IS NOT NULL "
        "GROUP BY action, rsi_state, ema_trend, status"
    ).fetchall()
    conn.close()

    combos: dict[tuple, dict] = {}
    for row in rows:
        key = (row["action"], row["rsi_state"], row["ema_trend"])
        combos.setdefault(key, {"win": 0, "loss": 0})
        combos[key][row["status"]] += row["n"]

    cells = []
    for (action, rsi, ema), c in combos.items():
        closed = c["win"] + c["loss"]
        cells.append({
            "action": action,
            "rsi_state": rsi,
            "ema_trend": ema,
            "win": c["win"],
            "loss": c["loss"],
            "samples": closed,
            "win_rate_pct": round(c["win"] / closed * 100, 1) if closed > 0 else None,
        })
    cells.sort(key=lambda x: (x["action"], x["rsi_state"], x["ema_trend"]))
    return {"cells": cells}


def get_expectancy(symbol: str | None = None) -> dict:
    """R-multiple expectancy — how many multiples of the ORIGINAL risk
    distance (entry to SL at order time) each closed trade actually
    made or lost. This is what win_rate_pct alone can't tell you: a
    pattern can win 70% of the time and still lose money overall if
    those wins average +0.3R while the rare losses average -2R.
    breakeven trades are included (contribute ~0R) since they're real
    outcomes that drag expectancy toward zero even though they're
    excluded from win_rate_pct's win/loss ratio.
    """
    conn = _connect()
    query = "SELECT action, entry, sl, exit_price, status FROM signals WHERE ticket IS NOT NULL AND status IN ('win', 'loss', 'breakeven') AND exit_price IS NOT NULL"
    params = ()
    if symbol:
        query += " AND symbol = ?"
        params = (symbol,)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Sanity cap — a few rows from early in development settled against a
    # stale mock fallback price (literally the hardcoded initial seed
    # price, recorded before MT5 was ever connected), producing R values
    # in the hundreds that would otherwise dominate the average. SL/TP is
    # set at 1.5x/3x ATR, so a real full-TP hit is ~2R; anything beyond
    # this cap is a data artifact, not a real outcome, and is excluded.
    MAX_REASONABLE_R = 20.0

    r_values, win_rs, loss_rs = [], [], []
    excluded_outliers = 0
    for row in rows:
        risk_dist = abs(row["entry"] - row["sl"])
        if risk_dist <= 0:
            continue
        if row["action"] == "buy":
            r = (row["exit_price"] - row["entry"]) / risk_dist
        else:
            r = (row["entry"] - row["exit_price"]) / risk_dist
        if abs(r) > MAX_REASONABLE_R:
            excluded_outliers += 1
            continue
        r_values.append(r)
        if row["status"] == "win":
            win_rs.append(r)
        elif row["status"] == "loss":
            loss_rs.append(r)

    if not r_values:
        return {"samples": 0, "expectancy_r": None, "avg_win_r": None, "avg_loss_r": None, "win_count": 0, "loss_count": 0, "excluded_outliers": excluded_outliers}

    return {
        "samples": len(r_values),
        "expectancy_r": round(sum(r_values) / len(r_values), 3),
        "avg_win_r": round(sum(win_rs) / len(win_rs), 3) if win_rs else None,
        "avg_loss_r": round(sum(loss_rs) / len(loss_rs), 3) if loss_rs else None,
        "win_count": len(win_rs),
        "loss_count": len(loss_rs),
        "excluded_outliers": excluded_outliers,
    }


def get_provider_accuracy() -> dict:
    import json
    conn = _connect()
    try:
        rows = conn.execute("SELECT status, ai_votes FROM signals WHERE ticket IS NOT NULL AND status IN ('win', 'loss')").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    stats = {}
    for row in rows:
        status = row["status"]
        if not row["ai_votes"]:
            continue
        try:
            votes = json.loads(row["ai_votes"])
        except Exception:
            continue
        for v in votes:
            provider = v.get("provider")
            if not provider:
                continue
            if provider not in stats:
                stats[provider] = {"correct": 0, "total": 0}
            
            # If the provider voted approve and it was a win, they are correct.
            # If they voted approve and it was a loss, they are wrong.
            if v.get("vote") == "approve":
                stats[provider]["total"] += 1
                if status == "win":
                    stats[provider]["correct"] += 1
                    
    scores = {}
    for p, s in stats.items():
        if s["total"] > 0:
            scores[p] = s["correct"] / s["total"]
        else:
            scores[p] = 0.5 # Default middle score
            
    return scores


def recent(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_db()
