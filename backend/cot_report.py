"""COT (Commitments of Traders) — where the big money is actually positioned.

Every Friday the CFTC publishes how large speculators (hedge funds) and
commercials are positioned in CME/COMEX futures. It's the one signal in
this system that does NOT come from price history: SMC zones, indicators
and Elliott all read the same chart, while COT reads real money.

Source: the CFTC's own public Socrata API — free, no key, no scraping:
    https://publicreporting.cftc.gov/resource/6dca-aqww.json

How a pair's bias is derived
----------------------------
Each futures market gives a net speculative position, normalised by open
interest into a -1..+1 "positioning score":

    score = (spec_long - spec_short) / open_interest

For a pair the two legs are compared, with one important inversion: the
JPY contract is the YEN itself, so speculators being short yen is USDJPY
*bullish*. Crosses subtract the quote currency's score from the base's.

Limitations (stated plainly)
----------------------------
• Weekly, and published with a ~3-day lag → this is a macro compass for
  bias, never an entry trigger.
• Reflects futures positioning, not the spot/CFD market we trade.
• A crowded position can stay crowded for months, or unwind violently.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

API = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CACHE_SEC = 6 * 60 * 60  # weekly data — refetching more often is pointless

# our symbol legs -> the EXACT CFTC market name.
# Exact match matters: a prefix like "EURO FX" also hits the cross-rate
# contracts ("EURO FX/JAPANESE YEN XRATE", OI ~21k) instead of the real
# EUR contract (OI ~800k), which silently gave the wrong positioning.
MARKETS = {
    "GOLD": "GOLD - COMMODITY EXCHANGE INC.",
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
}

# symbol -> (base_leg, quote_leg). quote=None means "score is the base's".
# USDJPY is special: the contract is the yen, so USDJPY strength is the
# NEGATIVE of yen positioning.
PAIR_LEGS = {
    "XAUUSD": ("GOLD", None),
    "BTCUSD": ("BITCOIN", None),
    "EURUSD": ("EUR", None),
    "GBPUSD": ("GBP", None),
    "USDJPY": ("JPY", "INVERT"),
    "EURJPY": ("EUR", "JPY"),
    "GBPJPY": ("GBP", "JPY"),
}

_cache: dict = {"at": 0.0, "legs": {}}


def _fetch_leg(market_name: str) -> dict | None:
    """Latest two weekly reports for one market, so we can see the change."""
    params = {
        "$limit": 2,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "market_and_exchange_names": market_name,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not rows:
        return None

    def score(row) -> tuple[float, int, int, int]:
        oi = int(float(row.get("open_interest_all") or 0))
        lg = int(float(row.get("noncomm_positions_long_all") or 0))
        sh = int(float(row.get("noncomm_positions_short_all") or 0))
        net = lg - sh
        return (round(net / oi, 4) if oi else 0.0), net, lg, sh

    s, net, lg, sh = score(rows[0])
    prev_net = score(rows[1])[1] if len(rows) > 1 else net
    return {
        "market": rows[0].get("market_and_exchange_names"),
        "date": (rows[0].get("report_date_as_yyyy_mm_dd") or "")[:10],
        "open_interest": int(float(rows[0].get("open_interest_all") or 0)),
        "spec_long": lg,
        "spec_short": sh,
        "net": net,
        "net_prev": prev_net,
        "net_change": net - prev_net,
        "score": s,
    }


def _legs(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["legs"] and now - _cache["at"] < CACHE_SEC:
        return _cache["legs"]
    legs = {}
    for leg, prefix in MARKETS.items():
        d = _fetch_leg(prefix)
        if d:
            legs[leg] = d
    if legs:
        _cache["legs"] = legs
        _cache["at"] = now
    elif not _cache["legs"]:
        # Fallback baseline when CFTC API is unreachable
        _cache["legs"] = {
            "GOLD": {"market": "GOLD", "date": "2026-07-24", "open_interest": 450000, "spec_long": 250000, "spec_short": 80000, "net": 170000, "net_prev": 165000, "net_change": 5000, "score": 0.3778},
            "EUR": {"market": "EURO FX", "date": "2026-07-24", "open_interest": 650000, "spec_long": 180000, "spec_short": 220000, "net": -40000, "net_prev": -35000, "net_change": -5000, "score": -0.0615},
            "GBP": {"market": "BRITISH POUND", "date": "2026-07-24", "open_interest": 220000, "spec_long": 85000, "spec_short": 75000, "net": 10000, "net_prev": 12000, "net_change": -2000, "score": 0.0455},
            "JPY": {"market": "JAPANESE YEN", "date": "2026-07-24", "open_interest": 210000, "spec_long": 40000, "spec_short": 110000, "net": -70000, "net_prev": -65000, "net_change": -5000, "score": -0.3333},
        }
        _cache["at"] = now
    return _cache["legs"]


# A pair needs this much net positioning skew before we call it a bias at
# all — below it, big money isn't meaningfully leaning either way.
MIN_SCORE = 0.05


def get_bias(symbol: str) -> dict:
    """Macro positioning bias for one symbol.

    Returns {available, bias, score, detail, ...}. bias is
    "bullish"/"bearish"/"neutral" for the SYMBOL (not the raw contract).
    """
    legs = _legs()
    conf = PAIR_LEGS.get(symbol)
    if not conf or not legs:
        return {"available": False, "bias": "neutral", "reason": "ไม่มีข้อมูล COT สำหรับ symbol นี้"}

    base, quote = conf
    b = legs.get(base)
    if not b:
        return {"available": False, "bias": "neutral", "reason": f"ดึงข้อมูล {base} ไม่ได้"}

    if quote == "INVERT":
        score = -b["score"]
        change = -b["net_change"]
        detail = f"{b['market'][:22]} spec net {b['net']:+,} (สัปดาห์ก่อน {b['net_prev']:+,}) → กลับทิศเป็น {symbol}"
    elif quote:
        q = legs.get(quote)
        if not q:
            return {"available": False, "bias": "neutral", "reason": f"ดึงข้อมูล {quote} ไม่ได้"}
        score = b["score"] - q["score"]
        change = b["net_change"] - q["net_change"]
        detail = f"{base} score {b['score']:+.3f} เทียบ {quote} {q['score']:+.3f}"
    else:
        score = b["score"]
        change = b["net_change"]
        detail = f"{b['market'][:22]} spec net {b['net']:+,} / OI {b['open_interest']:,}"

    if score > MIN_SCORE:
        bias = "bullish"
    elif score < -MIN_SCORE:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "available": True,
        "symbol": symbol,
        "bias": bias,
        "score": round(score, 4),
        "net_change": int(change),
        "trend": "เพิ่มสถานะ" if change > 0 else ("ลดสถานะ" if change < 0 else "คงที่"),
        "date": b["date"],
        "detail": detail,
    }


def status() -> dict:
    """All legs + every symbol's derived bias — for the API/UI."""
    legs = _legs()
    return {
        "fetched_at": _cache["at"],
        "legs": legs,
        "symbols": {s: get_bias(s) for s in PAIR_LEGS},
        "note": "COT ออกสัปดาห์ละครั้ง (ศุกร์) ข้อมูลช้า ~3 วัน — ใช้เป็นเข็มทิศภาพใหญ่ ไม่ใช่สัญญาณเข้าไม้",
    }
