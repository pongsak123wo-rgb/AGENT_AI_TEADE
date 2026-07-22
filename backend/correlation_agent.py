"""Checks correlation risk across currently-open positions before
approving a new one. Without this, the system can stack EURUSD-buy +
GBPUSD-buy and call it "diversified" when it's really one leveraged bet
on USD weakness twice over.

Correlation coefficients below are static approximations (typical
historical ranges for these instrument pairs), not computed from this
account's own price history — there isn't enough stored historical
price data yet to compute a real rolling correlation. Treat these as a
reasonable first filter, not ground truth; revisit if/when real
historical OHLC is persisted long-term.
"""
from __future__ import annotations

# (symbol_a, symbol_b) -> approximate correlation coefficient (-1 to 1).
# Order doesn't matter; both directions are checked.
CORRELATION_MAP = {
    ("EURUSD", "GBPUSD"): 0.85,
    ("EURUSD", "USDJPY"): -0.55,
    ("GBPUSD", "USDJPY"): -0.45,
    ("US30", "NAS100"): 0.85,
    ("XAUUSD", "EURUSD"): 0.35,
    ("XAUUSD", "USDJPY"): -0.40,
}

HIGH_CORRELATION_THRESHOLD = 0.7


def _lookup_correlation(symbol_a: str, symbol_b: str) -> float:
    if symbol_a == symbol_b:
        return 1.0
    return CORRELATION_MAP.get((symbol_a, symbol_b)) or CORRELATION_MAP.get((symbol_b, symbol_a)) or 0.0


def check_correlation_risk(symbol: str, action: str, open_positions: list) -> dict:
    """open_positions: list of objects/dicts with .symbol and .side (or ["symbol"]/["side"]).
    Returns {"blocked": bool, "reason": str, "correlated_with": [...]}.
    """
    conflicts = []
    for pos in open_positions:
        pos_symbol = pos.symbol if hasattr(pos, "symbol") else pos["symbol"]
        pos_side = pos.side if hasattr(pos, "side") else pos["side"]
        if pos_symbol == symbol:
            continue  # same-symbol stacking is governed by total_open_risk_pct already

        corr = _lookup_correlation(symbol, pos_symbol)
        if abs(corr) < HIGH_CORRELATION_THRESHOLD:
            continue

        # Positive correlation + same direction = doubling the same bet.
        # Negative correlation + opposite direction = also doubling the same bet
        # (e.g. EURUSD buy + USDJPY sell are both "USD weakens" bets).
        same_direction = action == pos_side
        doubling_up = (corr > 0 and same_direction) or (corr < 0 and not same_direction)

        if doubling_up:
            conflicts.append(
                {"symbol": pos_symbol, "side": pos_side, "correlation": corr}
            )

    if conflicts:
        names = ", ".join(f"{c['symbol']} ({c['side']}, corr={c['correlation']:+.2f})" for c in conflicts)
        return {
            "blocked": True,
            "reason": f"{symbol} {action} ไปทางเดียวกับไม้ที่เปิดอยู่แล้วซึ่ง correlate สูง: {names} — เสี่ยงเดิมพันซ้ำซ้อนโดยไม่ได้ตั้งใจ",
            "correlated_with": conflicts,
        }

    return {"blocked": False, "reason": "ไม่มี correlation conflict กับไม้ที่เปิดอยู่", "correlated_with": []}
