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
import time

# Symbols to snapshot — mirrors main.SYMBOLS (kept here to avoid a circular
# import). Override with MT5_SYMBOLS="EURUSD,GBPUSD,..." if the VPS terminal
# watches a different set.
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "EURJPY", "GBPJPY"]

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


def read_snapshot() -> dict | None:
    """Same shape as mt5_bridge.read_snapshot(). Adds native d1_candles
    (real Daily bars) that the EA bridge can't provide — mtf_engine prefers
    them when present."""
    if not _load():
        return None
    try:
        acct = _mt5.account_info()
        if not acct:
            return None

        symbols_px, candles, h1_candles, d1_candles = {}, {}, {}, {}
        for sym in _symbols():
            tick = _mt5.symbol_info_tick(sym)
            if tick and tick.bid and tick.ask:
                symbols_px[sym] = {"bid": float(tick.bid), "ask": float(tick.ask)}
            m1 = _rates(sym, _mt5.TIMEFRAME_M1, 300)
            if m1:
                candles[sym] = m1
            h1 = _rates(sym, _mt5.TIMEFRAME_H1, 150)
            if h1:
                h1_candles[sym] = h1
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
            "h1_candles": h1_candles,
            "d1_candles": d1_candles,
        }
    except Exception:
        return None


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
    if _trade_mode_str() != "demo":
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


_last_results: dict[int, dict] = {}


def try_read_result(command_id: int) -> dict | None:
    return _last_results.pop(command_id, None)
