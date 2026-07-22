"""Real economic calendar via the free ForexFactory community XML feed
(no API key needed — Finnhub's calendar endpoint turned out to require
a paid plan, this is the free fallback that actually has impact levels).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Currencies relevant to the symbols this system trades.
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "XAU", "AUD", "CAD", "CHF"}


def _parse_event_time(date_str: str, time_str: str) -> datetime | None:
    if not time_str or "Day" in time_str or "Tentative" in time_str:
        return None
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
    except ValueError:
        return None

def _get_adaptive_window(title: str, default_window: int = 30) -> int:
    """News Agent Learning: Adapt safe window based on news historical volatility."""
    title_lower = title.lower()
    if "nfp" in title_lower or "non-farm" in title_lower:
        return 60
    if "cpi" in title_lower or "inflation" in title_lower:
        return 45
    if "fomc" in title_lower or "rate decision" in title_lower:
        return 120
    return default_window


def get_upcoming_high_impact(window_minutes: int = 30) -> list[dict]:
    """Returns high-impact events for watched currencies within the next
    `window_minutes`, plus any from the last 5 minutes (just happened)."""
    response = requests.get(FEED_URL, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    now = datetime.now()
    upcoming = []

    for event in root.findall("event"):
        impact = event.findtext("impact", "")
        if impact != "High":
            continue
        country = event.findtext("country", "")
        if country not in WATCHED_CURRENCIES:
            continue

        event_time = _parse_event_time(event.findtext("date", ""), event.findtext("time", ""))
        if event_time is None:
            continue
            
        title = event.findtext("title", "")
        adaptive_window = _get_adaptive_window(title, window_minutes)

        delta = (event_time - now).total_seconds() / 60
        if -10 <= delta <= adaptive_window:
            upcoming.append(
                {
                    "title": title,
                    "country": country,
                    "minutes_away": round(delta),
                    "safe_window_used": adaptive_window
                }
            )

    return upcoming
