"""Swing-trade position manager (Stoch-swing strategy, Layer 5b).

Tracks each active Stoch-swing entry's plan — OS1 (hard SL / major support),
OB1 (major resistance / TP1), the DCA trigger from the OS1↔OB1 fibo grid — and
each cycle decides ONE of:
  • add the single allowed DCA position (price dropped two fibo zones), or
  • close everything (structure broken: price through OS1, or a fresh OB below
    OB1 = failed higher high).

Combined risk stays ≤ 1R: a "far" setup opens 0.5R now and reserves 0.5R for
the DCA; a "near" setup opens the full 1.0R and never averages.

decide() is PURE logic (no MT5 calls) so it can be unit-tested. State lives in
memory, keyed by symbol; a restart simply forgets in-flight DCA arming (the
positions themselves still carry their own SL in MT5), which is safe.
"""
from __future__ import annotations

import stoch_swing_engine

# symbol -> active plan dict
_plans: dict[str, dict] = {}


def register(symbol: str, side: str, entry_price: float, os1: float, ob1: float,
             ticket: int | None, base_risk_pct: float, plan: dict) -> None:
    """Record a freshly-opened swing entry. `plan` is a fibo_dca_plan() result;
    `base_risk_pct` is the FULL (1R) risk approved for this setup."""
    _plans[symbol] = {
        "side": side,
        "entry": entry_price,
        "os1": os1,
        "ob1": ob1,
        "dca_trigger": plan.get("dca_trigger"),
        "dca_armed": bool(plan.get("far")),
        "hard_sl": plan.get("hard_sl"),
        "base_risk_pct": base_risk_pct,
        "max_positions": plan.get("max_positions", 1),
        "tickets": [ticket] if ticket else [],
    }


def has_plan(symbol: str) -> bool:
    return symbol in _plans


def forget(symbol: str) -> None:
    _plans.pop(symbol, None)


def sync_tickets(symbol: str, live_tickets: set) -> None:
    """Drop tickets that MT5 no longer reports (closed by SL/TP). When none of
    the plan's tickets remain, the swing is over → forget it."""
    pl = _plans.get(symbol)
    if not pl:
        return
    pl["tickets"] = [t for t in pl["tickets"] if t in live_tickets]
    if not pl["tickets"]:
        forget(symbol)


def add_ticket(symbol: str, ticket: int) -> None:
    """Record a DCA fill and disarm further averaging (max one DCA)."""
    pl = _plans.get(symbol)
    if pl and ticket:
        pl["tickets"].append(ticket)
        pl["dca_armed"] = False


def active_tickets(symbol: str) -> list[int]:
    pl = _plans.get(symbol)
    return list(pl["tickets"]) if pl else []


def decide(symbol: str, price: float, latest_ob_high: float | None = None) -> dict:
    """Pure decision for one cycle. Returns one of:
      {"action": "none"}
      {"action": "close_all", "reason": str}
      {"action": "dca", "side": str, "risk_pct": float, "sl": float, "reason": str}
    Structure break takes priority over a DCA add.
    """
    pl = _plans.get(symbol)
    if not pl:
        return {"action": "none"}
    side = pl["side"]

    # 1) Structure destroyed → close all.
    brk = stoch_swing_engine.check_structure_broken(
        price, pl["os1"], pl["ob1"], latest_ob_high, side)
    if brk["broken"]:
        return {"action": "close_all", "reason": brk["reason"]}

    # 2) DCA add — only if armed, under the position cap, and price reached the
    #    two-zones-down trigger in the adverse direction.
    if pl["dca_armed"] and pl["dca_trigger"] is not None and len(pl["tickets"]) < pl["max_positions"]:
        hit = (price <= pl["dca_trigger"]) if side == "buy" else (price >= pl["dca_trigger"])
        if hit:
            return {
                "action": "dca",
                "side": side,
                "risk_pct": round(pl["base_risk_pct"] * 0.5, 2),
                "sl": pl["hard_sl"],
                "reason": f"ราคาถึงโซนถัว {pl['dca_trigger']} → ถัวไม้ 2 (0.5R, รวมยังคง ≤1R)",
            }

    return {"action": "none"}


def snapshot() -> dict:
    """For diagnostics / an endpoint — the current active swing plans."""
    return {s: {k: v for k, v in pl.items()} for s, pl in _plans.items()}
