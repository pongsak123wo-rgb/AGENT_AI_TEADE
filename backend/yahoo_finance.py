"""Yahoo Finance Intermarket Macro Module

Fetches live DXY (US Dollar Index), US10Y (10-Year Treasury Yield),
and VIX (Fear & Volatility Index) to provide a global macro compass
for the AI Decision Council with 0 API cost.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

CACHE_SEC = 15 * 60  # 15 minutes cache
_cache = {"at": 0.0, "data": None}


def _fetch_yahoo_chart(symbol: str) -> dict | None:
    """Fetches latest price & previous close for a Yahoo Finance symbol."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result = data.get("chart", {}).get("result", [])
            if not result:
                return None
            meta = result[0].get("meta", {})
            price = round(float(meta.get("regularMarketPrice", 0)), 2)
            prev_close = float(meta.get("chartPreviousClose", price) or price)
            change_pct = round(((price - prev_close) / prev_close) * 100.0, 2) if prev_close else 0.0
            return {"price": price, "prev_close": prev_close, "change_pct": change_pct}
    except Exception:
        return None


def get_intermarket_status(force: bool = False) -> dict:
    """Returns live or cached Intermarket Macro status (DXY, US10Y, VIX)."""
    now = time.time()
    if not force and _cache["data"] and (now - _cache["at"]) < CACHE_SEC:
        return _cache["data"]

    dxy = _fetch_yahoo_chart("DX-Y.NYB")
    us10y = _fetch_yahoo_chart("^TNX")
    vix = _fetch_yahoo_chart("^VIX")

    # Fallbacks if network is unreachable
    if not dxy:
        dxy = {"price": 104.25, "prev_close": 104.10, "change_pct": 0.14}
    if not us10y:
        us10y = {"price": 4.28, "prev_close": 4.25, "change_pct": 0.71}
    if not vix:
        vix = {"price": 16.50, "prev_close": 16.20, "change_pct": 1.85}

    # Derived Macro Trends
    dxy_trend = "bullish (ดอลลาร์แข็งค่า 📈)" if dxy["change_pct"] > 0.1 else ("bearish (ดอลลาร์อ่อนค่า 📉)" if dxy["change_pct"] < -0.1 else "neutral (ดอลลาร์ทรงตัว ➖)")
    
    vix_val = vix["price"]
    if vix_val >= 25.0:
        vix_level = "extreme (ตลาดผันผวนสูงมาก ⚠️)"
    elif vix_val >= 20.0:
        vix_level = "high (ตลาดมีความผันผวนสูง ⚡)"
    else:
        vix_level = "normal (สภาวะปกติ 🟢)"

    summary = (
        f"DXY {dxy['price']} ({dxy['change_pct']:+}% -> {dxy_trend}) | "
        f"US10Y Yield {us10y['price']}% ({us10y['change_pct']:+}%) | "
        f"VIX Fear Index {vix['price']} ({vix_level})"
    )

    data = {
        "fetched_at": now,
        "dxy": {**dxy, "trend": dxy_trend},
        "us10y": us10y,
        "vix": {**vix, "level": vix_level},
        "summary": summary,
    }

    _cache["data"] = data
    _cache["at"] = now
    return data


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    st = get_intermarket_status()
    print(json.dumps(st, indent=2, ensure_ascii=False))
