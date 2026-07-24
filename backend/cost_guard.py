"""Gemini spend guard — a code-side hard cap so the bill can't run past a
monthly budget even if Google's own quota isn't set.

Two layers protect the bill:
  1. Google Cloud quota limit — YOU set this in the console (token/day cap).
  2. This module — the app itself tracks estimated Gemini spend and, once
     it reaches the monthly budget, stops calling Gemini (falls back to the
     free Groq/Cerebras providers instead). Survives restarts (persisted)
     and resets at the start of each calendar month.

Budget is in THB via GEMINI_MONTHLY_BUDGET_THB (default 550). Cost is
estimated from token counts using Gemini 2.0 Flash pricing:
    input  $0.10 / 1M tokens
    output $0.40 / 1M tokens
converted at USD_TO_THB. Token counts come from the API's usage metadata
when available, else a chars/4 estimate — good enough for budgeting.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

_STATE_PATH = Path(__file__).parent / "cost_guard_state.json"

# Pricing MUST match the model in gemini.py. Default is gemini-2.0-flash
# ($0.10 in / $0.40 out) — a cheap, non-"thinking" model. If GEMINI_MODEL is
# switched to a pricier/thinking flash (e.g. 3.x, which bills hidden
# reasoning as output), raise these to match or the guard will undercount.
USD_TO_THB = 35.0
IN_PRICE_PER_TOKEN = 0.10 / 1_000_000    # gemini-2.0-flash input
OUT_PRICE_PER_TOKEN = 0.40 / 1_000_000   # gemini-2.0-flash output


def _budget_thb() -> float:
    try:
        return float(os.environ.get("GEMINI_MONTHLY_BUDGET_THB", "550"))
    except ValueError:
        return 550.0


def _this_month() -> str:
    return datetime.datetime.now().strftime("%Y-%m")


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        d = {}
    if d.get("month") != _this_month():
        d = {"month": _this_month(), "spent_thb": 0.0, "calls": 0, "today_date": _today(), "today_spent_thb": 0.0}
    if d.get("today_date") != _today():
        d["today_date"] = _today()
        d["today_spent_thb"] = 0.0
    return d


def _save(d: dict):
    try:
        _STATE_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def can_spend() -> bool:
    """True if both daily spend (< 10 THB) and monthly spend (< 350 THB) are under budget."""
    d = _load()
    daily_budget = float(os.environ.get("GEMINI_DAILY_BUDGET_THB", "10.0"))
    monthly_budget = float(os.environ.get("GEMINI_MONTHLY_BUDGET_THB", "350.0"))
    return d.get("today_spent_thb", 0.0) < daily_budget and d.get("spent_thb", 0.0) < monthly_budget


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def record(in_tokens: int, out_tokens: int):
    """Add one Gemini call's estimated cost to today's and this month's running total."""
    d = _load()
    cost_usd = in_tokens * IN_PRICE_PER_TOKEN + out_tokens * OUT_PRICE_PER_TOKEN
    cost_thb = round(cost_usd * USD_TO_THB, 4)
    d["spent_thb"] = round(d.get("spent_thb", 0.0) + cost_thb, 4)
    d["today_spent_thb"] = round(d.get("today_spent_thb", 0.0) + cost_thb, 4)
    d["calls"] = d.get("calls", 0) + 1
    _save(d)


def sync_spent(real_thb: float):
    d = _load()
    d["spent_thb"] = round(float(real_thb), 2)
    _save(d)
    return d["spent_thb"]


def status() -> dict:
    d = _load()
    daily_b = float(os.environ.get("GEMINI_DAILY_BUDGET_THB", "10.0"))
    monthly_b = float(os.environ.get("GEMINI_MONTHLY_BUDGET_THB", "350.0"))
    return {
        "month": d["month"],
        "today_date": d.get("today_date", _today()),
        "today_spent_thb": round(d.get("today_spent_thb", 0.0), 2),
        "daily_budget_thb": daily_b,
        "spent_thb": round(d["spent_thb"], 2),
        "monthly_budget_thb": monthly_b,
        "remaining_thb": round(monthly_b - d["spent_thb"], 2),
        "calls": d.get("calls", 0),
        "over_budget": d.get("today_spent_thb", 0.0) >= daily_b or d["spent_thb"] >= monthly_b,
        "used_pct": round(d["spent_thb"] / monthly_b * 100, 1) if monthly_b else 0,
    }
