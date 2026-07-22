"""Multi-agent pipeline for the trading assistant.

DataAgent reads real prices/candles from the MT5 EA via mt5_bridge when
the EA snapshot is fresh, and only falls back to a random-walk mock
when no live snapshot is available (EA not running, terminal closed).
"""
from __future__ import annotations

import random
import time
from collections import defaultdict
from dataclasses import dataclass, field

import ceo_council
import elliott_wave
import llm_analysis
import mt5_bridge
import news_calendar
import news_sentiment
import smc_analysis
from indicators import compute_snapshot

# Which currencies a symbol's sentiment depends on — gold/indices are
# priced in USD with no second leg, so only USD sentiment applies there.
SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "XAUUSD": ["USD"],
    "EURJPY": ["EUR", "JPY"],
    "GBPJPY": ["GBP", "JPY"],
}


@dataclass
class AgentMessage:
    agent: str
    text: str
    kind: str = "info"  # info | decision
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class DataAgent:
    """Tracks live price for every symbol available on the MT5 terminal.

    `prices` mocks what MT5's symbol list + tick feed would give us —
    swap `tick()` for a real MT5 call later without touching callers.
    """

    def __init__(self, symbols: dict[str, float]):
        self.prices = dict(symbols)
        self.history: dict[str, list[float]] = defaultdict(list)

    def tick(self, symbol: str) -> dict:
        live = mt5_bridge.read_snapshot()
        live_price = live["symbols"].get(symbol) if live else None
        candles = live.get("candles", {}).get(symbol) if live else None
        h1_candles = live.get("h1_candles", {}).get(symbol) if live else None

        if live_price:
            price = round((live_price["bid"] + live_price["ask"]) / 2, 5)
            self.prices[symbol] = price
            spread = round(live_price["ask"] - live_price["bid"], 6)
        else:
            step = self.prices[symbol] * random.uniform(-0.0015, 0.0015)
            self.prices[symbol] = round(self.prices[symbol] + step, 5)
            spread = round(self.prices[symbol] * 0.0001, 6)  # rough mock spread, not real

        self.history[symbol].append(self.prices[symbol])
        self.history[symbol] = self.history[symbol][-100:]
        return {
            "symbol": symbol,
            "price": self.prices[symbol],
            "history": self.history[symbol],
            "live": live_price is not None,
            "candles": candles,
            "h1_candles": h1_candles,
            "spread": spread,
        }

    def report(self, snapshot: dict) -> AgentMessage:
        tag = "LIVE" if snapshot["live"] else "mock"
        return AgentMessage(
            agent="data",
            text=f"[{tag}] {snapshot['symbol']} ราคาล่าสุด {snapshot['price']}",
        )


class TechnicalAgent:
    """Computes real indicators from price history, then asks the LLM
    (with RAG context from ingested PDFs) to judge if there's a setup.
    """

    def compute(self, snapshot: dict) -> dict:
        """Cheap phase — indicators + SMC + Elliott, ALL pure math, no LLM.
        Runs every cycle. The zone-watch gate reads this to decide whether
        it's worth escalating to the (paid) LLM reasoning in reason()."""
        candles = snapshot.get("candles")
        if candles and len(candles.get("c", [])) >= 20:
            # Real M1 candles available — use their closes as the price
            # series too, so indicators and OHLC patterns are computed
            # from the same consistent window of real bars.
            indicator_snapshot = compute_snapshot(candles["c"], ohlc=candles)
        else:
            indicator_snapshot = compute_snapshot(snapshot["history"])

        if not indicator_snapshot["ready"]:
            return indicator_snapshot

        # SMC and Elliott Wave both need real OHLC, and read far more
        # meaningfully off H1 bars than 60 minutes of M1 (not enough
        # history for structure/wave counting to mean anything) — prefer
        # h1_candles when the EA exported them, fall back to M1 candles,
        # then to "not ready" when neither is available.
        h1_candles = snapshot.get("h1_candles")
        structure_source = h1_candles if h1_candles and len(h1_candles.get("c", [])) >= 20 else candles
        if structure_source and len(structure_source.get("c", [])) >= 20:
            indicator_snapshot["smc"] = smc_analysis.analyze_smc(structure_source)
            indicator_snapshot["elliott_wave"] = elliott_wave.analyze_elliott(structure_source["c"])
            indicator_snapshot["structure_timeframe"] = "H1" if structure_source is h1_candles else "M1"
        else:
            indicator_snapshot["smc"] = {"ready": False, "reason": "ไม่มี candle จริง"}
            indicator_snapshot["elliott_wave"] = {"ready": False, "reason": "ไม่มี candle จริง"}
            indicator_snapshot["structure_timeframe"] = None

        return indicator_snapshot

    def reason(self, symbol: str, indicator_snapshot: dict) -> dict:
        """Paid phase — hand the precomputed indicators to the LLM. Only
        called once the zone-watch gate says price is at a meaningful
        level, so this (token-spending) call fires far less often."""
        return llm_analysis.analyze(symbol, indicator_snapshot)

    def analyze(self, snapshot: dict) -> dict:
        """Full pipeline (compute + reason). Kept for callers/tests that
        want the old one-shot behavior (e.g. debug_cycle)."""
        indicator_snapshot = self.compute(snapshot)
        if not indicator_snapshot["ready"]:
            return {
                "bias": "none",
                "confidence": 0,
                "reason": "ข้อมูลราคายังไม่พอคำนวณ indicator (ต้องมีอย่างน้อย 20 จุด)",
            }
        return self.reason(snapshot["symbol"], indicator_snapshot)

    def report(self, analysis: dict) -> AgentMessage:
        if analysis["bias"] == "none":
            text = analysis["reason"]
        else:
            text = f"{analysis['reason']} — confidence {analysis['confidence']}%"

        knowledge_note = analysis.get("knowledge_note")
        if knowledge_note:
            cite_tag = "ใช้ตำรา" if analysis.get("knowledge_cited") else "ไม่มีตำราตรง"
            text += f" | {cite_tag}: {knowledge_note}"

        return AgentMessage(agent="technical", text=text, data=analysis)


class NewsAgent:
    """Checks the real ForexFactory economic calendar, but only twice a
    day (every 12h) and caches the result — news cycles don't shift fast
    enough to justify checking every analysis cycle.
    """

    CHECK_INTERVAL_SEC = 12 * 60 * 60

    def __init__(self):
        self._cached: dict | None = None
        self._last_check: float = 0.0

    def check(self, symbol: str | None = None) -> dict:
        now = time.time()
        if self._cached is not None and (now - self._last_check) < self.CHECK_INTERVAL_SEC:
            cached = dict(self._cached)
        else:
            try:
                events = news_calendar.get_upcoming_high_impact(window_minutes=30)
                safe = len(events) == 0
                cached = {"safe": safe, "events": events, "checked_at": now}
            except Exception as e:
                # If the feed is unreachable, fail safe: treat as unsafe so we
                # don't trade blind, but say why in the chat.
                cached = {"safe": False, "events": [], "checked_at": now, "error": str(e)}
            self._cached = cached
            self._last_check = now

        # Sentiment is interpretation, not avoidance — cached separately per
        # currency inside news_sentiment.py, so this is cheap to call every
        # cycle even though the calendar check above is cached for 12h.
        sentiment = {}
        if symbol:
            for currency in SYMBOL_CURRENCIES.get(symbol, []):
                try:
                    sentiment[currency] = news_sentiment.get_sentiment(currency)
                except Exception as e:
                    sentiment[currency] = {"sentiment": "neutral", "reason": f"error: {e}"}
        cached["sentiment"] = sentiment
        return cached

    def report(self, news: dict) -> AgentMessage:
        if news.get("error"):
            base = f"ดึง economic calendar ไม่ได้ ({news['error'][:60]}) — เลี่ยงการเทรดไว้ก่อนเพื่อความปลอดภัย"
        elif news["safe"]:
            base = "ไม่มี high-impact news ใน 30 นาทีข้างหน้า ปลอดภัยที่จะเข้า"
        else:
            names = ", ".join(f"{e['title']} (อีก {e['minutes_away']} นาที, ห้ามเทรด {e['safe_window_used']}m)" for e in news["events"])
            base = f"มี high-impact news ใกล้เข้ามา: {names} — แนะนำเลี่ยงการเข้าเทรด"
        age_min = (time.time() - news["checked_at"]) / 60
        suffix = " (ผลจากการเช็คครั้งล่าสุด)" if age_min > 1 else " (เช็คใหม่)"

        sentiment = news.get("sentiment") or {}
        if sentiment:
            sentiment_text = ", ".join(f"{cur}={s['sentiment']}" for cur, s in sentiment.items())
            suffix += f" | sentiment: {sentiment_text}"

        return AgentMessage(agent="news", text=base + suffix, data=news)


class RiskAgent:
    """Thin wrapper around RiskManager so the agent pipeline stays uniform."""

    def __init__(self, risk_manager):
        self.risk_manager = risk_manager

    def evaluate(self, symbol: str, bias: str, spread: float | None = None, atr: float | None = None, mtf_confluence: str | None = None) -> dict:
        return self.risk_manager.evaluate(symbol, bias, spread=spread, atr=atr, mtf_confluence=mtf_confluence)

    def report(self, risk: dict) -> AgentMessage:
        return AgentMessage(agent="risk", text=risk["reason"], data=risk)


class CEOAgent:
    """Three-provider council (Gemini, Groq, Cerebras, with a local
    Ollama fallback when a cloud provider errors) votes on every signal.

    Risk Agent veto is checked before any vote — no council result can
    override a risk rejection. Approval threshold is self-adjusting
    (45-65%) based on the system's recent real win rate, not a fixed 2/3.
    """

    def decide(self, technical: dict, news: dict, risk: dict, snapshot: dict) -> dict:
        council = ceo_council.decide(technical, news, risk, snapshot)
        if not council["approved"] or not news["safe"]:
            return {"action": "no_trade", "council": council}

        price = snapshot["price"]
        atr = technical.get("indicators", {}).get("atr", price * 0.001)
        sl_dist = atr * 1.5
        tp_dist = atr * 3.0
        sl = price - sl_dist if technical["bias"] == "buy" else price + sl_dist
        tp = price + tp_dist if technical["bias"] == "buy" else price - tp_dist
        return {
            "action": technical["bias"],
            "symbol": snapshot["symbol"],
            "entry": price,
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "risk_pct": risk["lot"],
            "council": council,
        }

    def report(self, decision: dict) -> AgentMessage:
        council = decision.get("council", {})
        if decision["action"] == "no_trade":
            return AgentMessage(agent="ceo", text=f"ไม่ออก signal — {council.get('reason', 'ไม่ผ่านเกณฑ์')}", kind="info")
        votes_text = ", ".join(
            f"{v['provider']}={v.get('vote')}" + (" (เปลี่ยนใจ!)" if v.get("changed_mind") else "")
            for v in council.get("votes", [])
        )
        text = (
            f"เปิด {decision['action'].upper()} {decision['symbol']} — "
            f"Entry {decision['entry']} · SL {decision['sl']} · TP {decision['tp']} · Risk {decision['risk_pct']}% "
            f"· โหวต: {votes_text}"
        )
        return AgentMessage(agent="ceo", text=text, kind="decision", data=decision)
