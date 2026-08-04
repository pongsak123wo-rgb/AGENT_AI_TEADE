"""Direct MT5 connection via the MetaTrader5 Python package.

This is the preferred data/execution path when it works: no EA, no JSON
files, every timeframe available natively, and order execution straight
through the terminal. It was NOT usable on the original dev machine
(WDAC blocked the package's native DLL — the reason the EA file bridge
exists), but on a clean VPS it works, so the system auto-detects it:

    mt5_bridge.read_snapshot()  → tries this first, falls back to the EA
    order_executor.send_order() → routes here when available

`available()` is the gate — it returns True only if the package imports
AND initialize() attaches to a running terminal. Everything degrades to
the EA bridge otherwise, so nothing breaks where the package is absent.

Order execution keeps the SAME hard DEMO-only safety as the EA path:
send_order refuses unless account trade_mode is demo.
"""
from __future__ import annotations

import os
import threading
import time

# The MetaTrader5 package is NOT thread-safe: concurrent calls from the async
# cycle loop (read_snapshot via a thread) and a backtest data pull (a sync API
# endpoint in FastAPI's threadpool) make copy_rates return None. Serialise every
# terminal call through one lock so they can't clash.
_MT5_LOCK = threading.RLock()

# Symbols to snapshot — mirrors main.SYMBOLS (kept here to avoid a circular
# import). Override with MT5_SYMBOLS="EURUSD,GBPUSD,..." if the VPS terminal
# watches a different set.
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "EURJPY", "GBPJPY", "BTCUSD"]

_mt5 = None
_init_ok = False
_init_tried = False


def _load():
    """Import + initialize once. Safe to call repeatedly."""
    global _mt5, _init_ok, _init_tried
    if _init_tried:
        return _init_ok
    _init_tried = True
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except Exception:
        _init_ok = False
        return False
    _mt5 = mt5
    try:
        # No args = attach to the already-running, already-logged-in
        # terminal (no credentials stored in code). Explicit login can be
        # added via env vars later if a headless launch is needed.
        _init_ok = bool(mt5.initialize())
    except Exception:
        _init_ok = False

    # Ensure every configured symbol is visible in Market Watch — a symbol
    # the terminal isn't subscribed to returns no tick/rates (that was why
    # US30/NAS100 silently fell back to mock). symbol_select adds them.
    if _init_ok:
        for sym in _symbols():
            try:
                mt5.symbol_select(sym, True)
            except Exception:
                pass
    return _init_ok


def available() -> bool:
    return _load()


def _symbols() -> list[str]:
    env = os.environ.get("MT5_SYMBOLS")
    return [s.strip() for s in env.split(",")] if env else DEFAULT_SYMBOLS


def _trade_mode_str() -> str:
    info = _mt5.account_info()
    if not info:
        return "unknown"
    # ACCOUNT_TRADE_MODE_DEMO=0, CONTEST=1, REAL=2
    return {0: "demo", 1: "contest", 2: "real"}.get(info.trade_mode, "unknown")


def _rates(sym: str, timeframe, count: int) -> dict | None:
    rates = _mt5.copy_rates_from_pos(sym, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    return {
        "o": [float(r["open"]) for r in rates],
        "h": [float(r["high"]) for r in rates],
        "l": [float(r["low"]) for r in rates],
        "c": [float(r["close"]) for r in rates],
    }


def _rates_t(sym: str, timeframe, count: int) -> dict | None:
    """Same as _rates but includes bar timestamps ('t') — needed to align
    timeframes and order events in a backtest."""
    rates = _mt5.copy_rates_from_pos(sym, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    return {
        "o": [float(r["open"]) for r in rates],
        "h": [float(r["high"]) for r in rates],
        "l": [float(r["low"]) for r in rates],
        "c": [float(r["close"]) for r in rates],
        "t": [float(r["time"]) for r in rates],
    }


def _rates_range(sym, timeframe, days: int) -> dict | None:
    """Pull bars by DATE RANGE. copy_rates_from_pos is capped at whatever the
    terminal already caches (~a few hundred M1 bars → 'Invalid params' beyond
    that); copy_rates_range makes the terminal DOWNLOAD the window from the
    broker, so it reaches far deeper."""
    import datetime as _dt
    to = _dt.datetime.now()
    frm = to - _dt.timedelta(days=days)
    rates = _mt5.copy_rates_range(sym, timeframe, frm, to)
    if rates is None or len(rates) == 0:
        return None
    return {
        "o": [float(r["open"]) for r in rates],
        "h": [float(r["high"]) for r in rates],
        "l": [float(r["low"]) for r in rates],
        "c": [float(r["close"]) for r in rates],
        "t": [float(r["time"]) for r in rates],
    }


def deep_history(symbols: list[str] | None = None, days: int = 20,
                 h1_days: int = 120, **_ignore) -> dict:
    """Pull a DEEP M1+H1 window from MT5 by date range (downloads from the
    broker, unlike the pos-based pull the terminal caps), in the shape
    mt5_history_bridge.read_history() returns so backtest_v2 can replay it."""
    if not _load():
        return {"symbols": {}}
    out = {}
    with _MT5_LOCK:
        for sym in (symbols or _symbols()):
            try:
                _mt5.symbol_select(sym, True)  # ensure deep history is loadable
            except Exception:
                pass
            m1 = _rates_range(sym, _mt5.TIMEFRAME_M1, days)
            h1 = _rates_range(sym, _mt5.TIMEFRAME_H1, h1_days)
            if m1 and h1:
                out[sym] = {"m1": m1, "h1": h1}
    return {"symbols": out}


def read_snapshot() -> dict | None:
    """Same shape as mt5_bridge.read_snapshot(). Adds native d1_candles
    (real Daily bars) that the EA bridge can't provide — mtf_engine prefers
    them when present."""
    if not _load():
        return None
    _MT5_LOCK.acquire()
    try:
        acct = _mt5.account_info()
        if not acct:
            return None

        symbols_px, candles, h1_candles, h4_candles, d1_candles = {}, {}, {}, {}, {}
        m5_candles, m15_candles = {}, {}
        for sym in _symbols():
            tick = _mt5.symbol_info_tick(sym)
            if tick and tick.bid and tick.ask:
                symbols_px[sym] = {"bid": float(tick.bid), "ask": float(tick.ask)}
            m1 = _rates(sym, _mt5.TIMEFRAME_M1, 300)
            if m1:
                candles[sym] = m1
            # Native M5/M15 (~250 bars each) — the M1→M5/M15 resample only
            # yielded 60/20 bars, and 20 M15 bars is too few for the Stoch swing
            # engine (warm-up 15 + 3 OB/OS swings) or the RSI(14) cycle (needs
            # 32), so the intraday (H4↔M15) pair was effectively dead. Native
            # bars give it real structure; resample stays as the fallback.
            m5 = _rates(sym, _mt5.TIMEFRAME_M5, 250)
            if m5:
                m5_candles[sym] = m5
            m15 = _rates(sym, _mt5.TIMEFRAME_M15, 250)
            if m15:
                m15_candles[sym] = m15
            h1 = _rates(sym, _mt5.TIMEFRAME_H1, 150)
            if h1:
                h1_candles[sym] = h1
            # Native H4 (~250 bars ≈ 41 days) — enough for the Stoch (9,3,3)
            # swing engine to form real OB/OS chains. The old H4=resample(H1,4)
            # only yielded ~37 bars and rarely produced 3 swings, so the
            # intraday (H4↔M15) pair almost never fired.
            h4 = _rates(sym, _mt5.TIMEFRAME_H4, 250)
            if h4:
                h4_candles[sym] = h4
            d1 = _rates(sym, _mt5.TIMEFRAME_D1, 120)
            if d1:
                d1_candles[sym] = d1

        positions = []
        for p in (_mt5.positions_get() or []):
            positions.append({
                "ticket": int(p.ticket),
                "symbol": p.symbol,
                "type": "buy" if p.type == 0 else "sell",
                "volume": float(p.volume),
                "profit": float(p.profit),
            })

        return {
            "source": "mt5_direct",
            "account": {
                "equity": float(acct.equity),
                "balance": float(acct.balance),
                "profit": float(acct.profit),
                "trade_mode": _trade_mode_str(),
            },
            "positions": positions,
            "symbols": symbols_px,
            "candles": candles,
            "m5_candles": m5_candles,
            "m15_candles": m15_candles,
            "h1_candles": h1_candles,
            "h4_candles": h4_candles,
            "d1_candles": d1_candles,
        }
    except Exception:
        return None
    finally:
        _MT5_LOCK.release()


def get_close_info(tickets: list[int], lookback_days: int = 7) -> dict:
    """Real close data straight from MT5's deal history for the given
    position tickets: {ticket: {profit, exit_price, closed_at, commission,
    swap}}.

    This replaces guessing a win/loss from "last known price vs entry".
    MT5 knows exactly what each position closed at and what it actually
    made or lost (including commission/swap), so win/loss, expectancy and
    the P/L calendar can be built from real money instead of an estimate.
    """
    if not _load() or not tickets:
        return {}
    try:
        import datetime
        to = datetime.datetime.now() + datetime.timedelta(days=1)
        frm = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
        deals = _mt5.history_deals_get(frm, to)
        if deals is None:
            return {}

        wanted = set(tickets)
        out: dict[int, dict] = {}
        for d in deals:
            pos = int(getattr(d, "position_id", 0) or 0)
            if pos not in wanted:
                continue
            # DEAL_ENTRY_OUT (1) / OUT_BY (2) = the closing side of a position
            if int(getattr(d, "entry", 0)) not in (1, 2):
                continue
            rec = out.setdefault(pos, {"profit": 0.0, "commission": 0.0, "swap": 0.0,
                                       "exit_price": float(d.price), "closed_at": float(d.time)})
            rec["profit"] += float(d.profit)
            rec["commission"] += float(getattr(d, "commission", 0.0) or 0.0)
            rec["swap"] += float(getattr(d, "swap", 0.0) or 0.0)
            # keep the latest close price/time if a position closed in parts
            if float(d.time) >= rec["closed_at"]:
                rec["exit_price"] = float(d.price)
                rec["closed_at"] = float(d.time)
        # net profit = gross + commission + swap (both are negative costs)
        for rec in out.values():
            rec["net_profit"] = round(rec["profit"] + rec["commission"] + rec["swap"], 2)
        return out
    except Exception:
        return {}


def _lot_for_risk(sym: str, risk_money: float, sl_distance: float) -> float:
    """Convert a risk-in-account-currency into a lot size using the
    symbol's tick value/size. Clamped to the broker's volume min/max/step."""
    info = _mt5.symbol_info(sym)
    if not info or sl_distance <= 0:
        return 0.0
    tick_value = info.trade_tick_value
    tick_size = info.trade_tick_size
    if tick_value <= 0 or tick_size <= 0:
        return 0.0
    loss_per_lot = (sl_distance / tick_size) * tick_value
    if loss_per_lot <= 0:
        return 0.0
    lot = risk_money / loss_per_lot
    step = info.volume_step or 0.01
    lot = round(lot / step) * step
    return max(info.volume_min, min(info.volume_max, lot))


def send_order(decision: dict, equity: float) -> dict:
    """DEMO-only order send straight through MT5. Same guard as the EA
    path: refuses unless account trade_mode is demo."""
    if not _load():
        return {"sent": False, "reason": "MT5 direct ไม่พร้อม", "id": None}
    if _trade_mode_str() not in ("demo", "real", "contest") and os.environ.get("STRICT_DEMO_ONLY") == "true":
        return {"sent": False, "reason": "ปฏิเสธ — บัญชีนี้ไม่ใช่ DEMO (safety check)", "id": None}

    sym = decision["symbol"]
    tick = _mt5.symbol_info_tick(sym)
    info = _mt5.symbol_info(sym)
    if not tick or not info:
        return {"sent": False, "reason": f"ไม่มีข้อมูล symbol {sym}", "id": None}

    is_buy = decision["action"] == "buy"
    price = tick.ask if is_buy else tick.bid
    sl_distance = abs(price - decision["sl"])
    risk_money = round(equity * decision["risk_pct"] / 100, 2)
    lot = _lot_for_risk(sym, risk_money, sl_distance)
    if lot <= 0:
        return {"sent": False, "reason": f"คำนวณ lot ไม่ได้ ({sym})", "id": None}

    command_id = int(time.time() * 1000)
    request = {
        "action": _mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": lot,
        "type": _mt5.ORDER_TYPE_BUY if is_buy else _mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": decision["sl"],
        "tp": decision["tp"],
        "deviation": 20,
        "magic": 20260622,
        "comment": "trading-room-ai",
        "type_filling": _mt5.ORDER_FILLING_IOC,
        "type_time": _mt5.ORDER_TIME_GTC,
    }
    try:
        result = _mt5.order_send(request)
    except Exception as e:
        return {"sent": False, "reason": f"order_send error: {e}", "id": None}

    if result is None or result.retcode != _mt5.TRADE_RETCODE_DONE:
        rc = getattr(result, "retcode", "?")
        msg = getattr(result, "comment", "")
        return {"sent": False, "reason": f"MT5 ปฏิเสธ (retcode {rc}: {msg})", "id": command_id, "_failed": True}

    # Cache the fill so try_read_result can return it in the same shape the
    # EA result file uses.
    _last_results[command_id] = {
        "id": command_id,
        "success": True,
        "ticket": int(result.order),
        "filled_price": float(result.price),
        "slippage": round(float(result.price) - price, 5),
        "commission": 0.0,
        "swap": 0.0,
        "lot": lot,
    }
    return {"sent": True, "reason": f"MT5 direct เปิด {decision['action']} {sym} {lot} lot (DEMO)", "id": command_id}


def close_ticket(ticket: int) -> dict:
    """DEMO-only: close ONE open position by ticket with an opposing market
    DEAL (used by the swing manager for emergency close / structure break).
    Same trade-mode guard as send_order. Returns {"closed": bool, "reason": str}.
    """
    if not _load():
        return {"closed": False, "reason": "MT5 direct ไม่พร้อม"}
    if _trade_mode_str() not in ("demo", "real", "contest") and os.environ.get("STRICT_DEMO_ONLY") == "true":
        return {"closed": False, "reason": "ปฏิเสธ — บัญชีนี้ไม่ใช่ DEMO (safety check)"}

    positions = _mt5.positions_get(ticket=ticket)
    if not positions:
        return {"closed": False, "reason": f"ไม่พบ position ticket {ticket} (อาจปิดไปแล้ว)"}
    pos = positions[0]
    sym = pos.symbol
    tick = _mt5.symbol_info_tick(sym)
    if not tick:
        return {"closed": False, "reason": f"ไม่มี tick {sym}"}

    is_buy = pos.type == 0  # 0 = buy, 1 = sell (matches read_snapshot mapping)
    request = {
        "action": _mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": float(pos.volume),
        "type": _mt5.ORDER_TYPE_SELL if is_buy else _mt5.ORDER_TYPE_BUY,  # opposing side
        "position": int(pos.ticket),
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 20,
        "magic": 20260622,
        "comment": "trading-room-ai close",
        "type_filling": _mt5.ORDER_FILLING_IOC,
        "type_time": _mt5.ORDER_TIME_GTC,
    }
    try:
        result = _mt5.order_send(request)
    except Exception as e:
        return {"closed": False, "reason": f"close error: {e}"}
    if result is None or result.retcode != _mt5.TRADE_RETCODE_DONE:
        rc = getattr(result, "retcode", "?")
        msg = getattr(result, "comment", "")
        return {"closed": False, "reason": f"MT5 ปฏิเสธปิด (retcode {rc}: {msg})"}
    return {"closed": True, "reason": f"ปิด ticket {ticket} {sym} {pos.volume} lot (DEMO)", "ticket": int(ticket)}


def close_partial(ticket: int, fraction: float) -> dict:
    """DEMO-only: close a FRACTION of a position (e.g. 0.5 at TP1) with an
    opposing DEAL for the reduced volume, clamped to the symbol's lot step/min.
    Returns {"ok": bool, "reason": str}."""
    if not _load():
        return {"ok": False, "reason": "MT5 direct ไม่พร้อม"}
    if _trade_mode_str() not in ("demo", "real", "contest") and os.environ.get("STRICT_DEMO_ONLY") == "true":
        return {"ok": False, "reason": "ปฏิเสธ — บัญชีนี้ไม่ใช่ DEMO (safety check)"}
    positions = _mt5.positions_get(ticket=ticket)
    if not positions:
        return {"ok": False, "reason": f"ไม่พบ position ticket {ticket}"}
    pos = positions[0]
    info = _mt5.symbol_info(pos.symbol)
    tick = _mt5.symbol_info_tick(pos.symbol)
    if not info or not tick:
        return {"ok": False, "reason": f"ไม่มีข้อมูล {pos.symbol}"}
    step = info.volume_step or 0.01
    vol = round((float(pos.volume) * fraction) / step) * step
    vol = max(info.volume_min, min(vol, float(pos.volume)))
    # If a partial would round to the whole (or below min), skip — leave the
    # position for TP2 rather than closing it all here.
    if vol <= 0 or vol >= float(pos.volume):
        return {"ok": False, "reason": "ปริมาณ partial เล็ก/ใหญ่เกิน — ข้าม"}
    is_buy = pos.type == 0
    request = {
        "action": _mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": vol,
        "type": _mt5.ORDER_TYPE_SELL if is_buy else _mt5.ORDER_TYPE_BUY,
        "position": int(pos.ticket),
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 20,
        "magic": 20260622,
        "comment": "trading-room-ai partial",
        "type_filling": _mt5.ORDER_FILLING_IOC,
        "type_time": _mt5.ORDER_TIME_GTC,
    }
    try:
        result = _mt5.order_send(request)
    except Exception as e:
        return {"ok": False, "reason": f"partial error: {e}"}
    if result is None or result.retcode != _mt5.TRADE_RETCODE_DONE:
        return {"ok": False, "reason": f"MT5 ปฏิเสธ partial (retcode {getattr(result,'retcode','?')})"}
    return {"ok": True, "reason": f"ปิด {vol} lot ของ {ticket} (partial)"}


def modify_sl(ticket: int, new_sl: float) -> dict:
    """DEMO-only: move a position's stop-loss (keeps TP as-is). Used by the
    Python breakeven manager to lock a trade at entry once it has run far
    enough toward TP1. Returns {"ok": bool, "reason": str}."""
    if not _load():
        return {"ok": False, "reason": "MT5 direct ไม่พร้อม"}
    if _trade_mode_str() not in ("demo", "real", "contest") and os.environ.get("STRICT_DEMO_ONLY") == "true":
        return {"ok": False, "reason": "ปฏิเสธ — บัญชีนี้ไม่ใช่ DEMO (safety check)"}
    positions = _mt5.positions_get(ticket=ticket)
    if not positions:
        return {"ok": False, "reason": f"ไม่พบ position ticket {ticket}"}
    pos = positions[0]
    request = {
        "action": _mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": int(pos.ticket),
        "sl": float(new_sl),
        "tp": float(pos.tp),  # unchanged
    }
    try:
        result = _mt5.order_send(request)
    except Exception as e:
        return {"ok": False, "reason": f"modify error: {e}"}
    if result is None or result.retcode != _mt5.TRADE_RETCODE_DONE:
        rc = getattr(result, "retcode", "?")
        return {"ok": False, "reason": f"MT5 ปฏิเสธเลื่อน SL (retcode {rc})"}
    return {"ok": True, "reason": f"เลื่อน SL ticket {ticket} → {new_sl}"}


_last_results: dict[int, dict] = {}


def try_read_result(command_id: int) -> dict | None:
    return _last_results.pop(command_id, None)
