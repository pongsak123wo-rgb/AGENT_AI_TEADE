"""Break-even awareness: how far price has to move before a trade is
actually profitable after spread + slippage (commission tracked
separately since it's in account currency, not price units — merging
the two would need lot/tick-value math we don't have in Python without
querying MT5 directly, so it's reported alongside instead of folded in).

This is what "understanding spread and break-even, not just trading
like the EA" means concretely: the CEO sees real measured costs for
this symbol and judges whether the TP is even worth taking, instead of
assuming every pip of TP is pure profit.
"""
from __future__ import annotations

import signal_log


def breakeven_info(symbol: str, action: str, entry: float, sl: float, tp: float, spread: float) -> dict:
    cost = signal_log.get_cost_stats(symbol)
    avg_slippage = abs(cost.get("avg_slippage", 0) or 0)
    avg_commission = cost.get("avg_commission", 0) or 0
    samples = cost.get("samples", 0)

    # Round-trip price cost: spread is paid once on entry (the bid/ask gap
    # already reflects exit cost too on most brokers' execution model),
    # plus the average slippage actually observed on this symbol so far.
    breakeven_distance = round(abs(spread) + avg_slippage, 6)
    tp_distance = round(abs(tp - entry), 6)
    sl_distance = round(abs(sl - entry), 6)

    # Require TP to clear break-even by a safety margin, not just barely.
    safety_margin = 1.5
    tp_covers_costs = tp_distance > breakeven_distance * safety_margin

    return {
        "spread": round(spread, 6),
        "avg_slippage": avg_slippage,
        "avg_commission_per_trade": avg_commission,
        "cost_samples": samples,
        "breakeven_distance": breakeven_distance,
        "tp_distance": tp_distance,
        "sl_distance": sl_distance,
        "tp_covers_costs": tp_covers_costs,
    }
