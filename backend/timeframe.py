"""Timeframe resampling — build higher-TF candles from a base stream.

The EA exports M1 (1-minute) and H1 (1-hour) candles. From those we can
aggregate the timeframes the multi-timeframe engine needs without asking
the EA for more separate streams:

    M5  = 5  × M1        M15 = 15 × M1
    H4  = 4  × H1        D1  = 24 × H1

Aggregation rule per group of `factor` bars (standard OHLC):
    open  = first bar's open
    high  = max of highs
    low   = min of lows
    close = last bar's close

Groups are aligned from the most recent bar backwards, dropping the
oldest incomplete remainder, so the newest aggregated bar reflects the
freshest complete-ish group.
"""
from __future__ import annotations


def resample(candles: dict | None, factor: int) -> dict | None:
    """candles: {"o","h","l","c"} oldest-first. Returns the same shape at
    `factor`× the timeframe, or None if there isn't at least one full group."""
    if not candles or factor <= 1:
        return candles
    o, h, l, c = candles.get("o"), candles.get("h"), candles.get("l"), candles.get("c")
    if not c or len(c) < factor:
        return None

    n = len(c)
    start = n % factor  # drop the oldest incomplete remainder
    out = {"o": [], "h": [], "l": [], "c": []}
    i = start
    while i + factor <= n:
        seg_h = h[i:i + factor]
        seg_l = l[i:i + factor]
        out["o"].append(o[i])
        out["h"].append(max(seg_h))
        out["l"].append(min(seg_l))
        out["c"].append(c[i + factor - 1])
        i += factor
    return out if out["c"] else None
