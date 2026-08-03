"""Swing-trade position manager (Stoch-swing strategy).

One manager, in Python, owns the whole life of a Stoch-swing trade — the MT5
EA only executes orders. Each cycle it decides, in priority order:

  1. close_all   — structure destroyed (price broke OS1, or a fresh OB below
                   OB1 = failed higher high)
  2. move_be     — price ran >= 75% of the way from entry to TP1 (OB1): lock
                   the stop at entry. NOT before, so the trade keeps room to
                   breathe / average early on.
  3. partial     — price reached TP1 (OB1 = the real previous high): bank half
                   the position; the rest runs to TP2 (fibo 161.8%).
  4. dca         — price dropped two fibo zones against us: add the one
                   reserved 0.5R (combined risk stays <= 1R).

decide() is pure logic (no MT5 calls) and returns a LIST of actions for the
cycle to execute. State is in memory, keyed by symbol.
"""
from __future__ import annotations

import stoch_swing_engine

BE_TRIGGER_FRAC = 0.75   # move SL to entry only after price runs 75% toward TP1
PARTIAL_FRAC = 0.5       # close half the position at TP1

_plans: dict[str, dict] = {}


def register(symbol: str, side: str, entry_price: float, os1: float, ob1: float,
             tp2: float, ticket: int | None, base_risk_pct: float, plan: dict) -> None:
    """Record a freshly-opened swing entry. `plan` is a fibo_dca_plan() result;
    `base_risk_pct` is the FULL (1R) risk approved. ob1 = TP1, tp2 = fibo 161.8%."""
    _plans[symbol] = {
        "side": side, "entry": entry_price, "os1": os1, "ob1": ob1, "tp2": tp2,
        "dca_trigger": plan.get("dca_trigger"),
        "dca_armed": bool(plan.get("far")),
        "hard_sl": plan.get("hard_sl"),
        "base_risk_pct": base_risk_pct,
        "max_positions": plan.get("max_positions", 1),
        "tickets": [ticket] if ticket else [],
        "be_done": False,
        "partial_done": False,
    }


def has_plan(symbol: str) -> bool:
    return symbol in _plans


def forget(symbol: str) -> None:
    _plans.pop(symbol, None)


def sync_tickets(symbol: str, live_tickets: set) -> None:
    pl = _plans.get(symbol)
    if not pl:
        return
    pl["tickets"] = [t for t in pl["tickets"] if t in live_tickets]
    if not pl["tickets"]:
        forget(symbol)


def add_ticket(symbol: str, ticket: int) -> None:
    pl = _plans.get(symbol)
    if pl and ticket:
        pl["tickets"].append(ticket)
        pl["dca_armed"] = False  # one DCA only


def mark_be_done(symbol: str) -> None:
    if symbol in _plans:
        _plans[symbol]["be_done"] = True


def mark_partial_done(symbol: str) -> None:
    if symbol in _plans:
        _plans[symbol]["partial_done"] = True


def active_tickets(symbol: str) -> list[int]:
    pl = _plans.get(symbol)
    return list(pl["tickets"]) if pl else []


def _reached(price: float, target: float, entry: float, side: str) -> bool:
    """Has price reached `target` in the favourable direction?"""
    return price >= target if side == "buy" else price <= target


def decide(symbol: str, price: float, latest_ob_high: float | None = None) -> list[dict]:
    """Return the list of actions to run this cycle (may be empty)."""
    pl = _plans.get(symbol)
    if not pl:
        return []
    side = pl["side"]

    # 1) Structure destroyed → close everything (highest priority, alone).
    brk = stoch_swing_engine.check_structure_broken(
        price, pl["os1"], pl["ob1"], latest_ob_high, side)
    if brk["broken"]:
        return [{"action": "close_all", "reason": brk["reason"]}]

    actions = []
    entry, ob1 = pl["entry"], pl["ob1"]
    span = ob1 - entry  # to TP1; sign carries direction

    # 2) Breakeven at >= 75% of the way to TP1.
    if not pl["be_done"] and span != 0:
        progress = (price - entry) / span
        if progress >= BE_TRIGGER_FRAC:
            actions.append({"action": "move_be", "sl": round(entry, 5),
                            "reason": f"ราคาถึง {progress*100:.0f}% ของ TP1 → กันทุน (SL→entry)"})

    # 3) Partial close at TP1 (OB1 = real previous high).
    if not pl["partial_done"] and _reached(price, ob1, entry, side):
        actions.append({"action": "partial", "fraction": PARTIAL_FRAC,
                        "reason": f"ถึง TP1 (OB1 {ob1}) → ปิด {int(PARTIAL_FRAC*100)}% ที่เหลือวิ่ง TP2 {pl['tp2']}"})

    # 4) DCA add on an adverse move to the two-zones-down trigger.
    if pl["dca_armed"] and pl["dca_trigger"] is not None and len(pl["tickets"]) < pl["max_positions"]:
        hit = (price <= pl["dca_trigger"]) if side == "buy" else (price >= pl["dca_trigger"])
        if hit:
            actions.append({"action": "dca", "side": side,
                            "risk_pct": round(pl["base_risk_pct"] * 0.5, 2),
                            "sl": pl["hard_sl"], "tp": pl["tp2"],
                            "reason": f"ราคาถึงโซนถัว {pl['dca_trigger']} → ถัวไม้ 2 (0.5R)"})
    return actions


def snapshot() -> dict:
    return {s: dict(pl) for s, pl in _plans.items()}
