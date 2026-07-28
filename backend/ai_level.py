"""AI Level & XP Evolution System

Calculates local XP, Level, Title, and Badges for the Trading Room AI
using empirical data from `signals.db` and research logs with ZERO API cost.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import signal_log

DB_PATH = Path(__file__).parent / "signals.db"

LEVEL_TIERS = [
    {"level": 1, "title": "Novice Analyst", "badge": "🟩", "min_xp": 0, "max_xp": 100},
    {"level": 2, "title": "SMC Zone Apprentice", "badge": "🟦", "min_xp": 100, "max_xp": 300},
    {"level": 3, "title": "Risk Guardian Specialist", "badge": "🟪", "min_xp": 300, "max_xp": 700},
    {"level": 4, "title": "Quantitative Master", "badge": "🟨", "min_xp": 700, "max_xp": 1500},
    {"level": 5, "title": "Grandmaster Trading AI", "badge": "👑", "min_xp": 1500, "max_xp": 999999},
]


def get_ai_level_status() -> dict:
    """Computes empirical XP, Level, Progress, and Unlocked Badges."""
    stats = signal_log.get_stats()
    closed_trades = stats.get("wins", 0) + stats.get("losses", 0) + stats.get("breakeven", 0)
    wins = stats.get("wins", 0)
    win_rate = stats.get("win_rate_pct", 0.0)

    # Count research logs from DB or research table
    research_count = 0
    try:
        if DB_PATH.exists():
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                res = conn.execute("SELECT COUNT(*) FROM research_log").fetchone()
                if res:
                    research_count = res[0]
    except Exception:
        pass

    # Calculate XP
    xp = (closed_trades * 10) + (wins * 25) + (research_count * 15)

    # Base baseline XP from scanning cycles so fresh installs show progress
    total_cycles = 0
    try:
        if DB_PATH.exists():
            with sqlite3.connect(DB_PATH) as conn:
                res = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
                if res:
                    total_cycles = res[0]
    except Exception:
        pass

    xp += min(500, total_cycles * 2)

    # Determine Level Tier
    current_tier = LEVEL_TIERS[0]
    next_tier = LEVEL_TIERS[1]
    for i, tier in enumerate(LEVEL_TIERS):
        if xp >= tier["min_xp"]:
            current_tier = tier
            next_tier = LEVEL_TIERS[i + 1] if i + 1 < len(LEVEL_TIERS) else tier

    level_span = max(1, next_tier["min_xp"] - current_tier["min_xp"])
    xp_in_level = max(0, xp - current_tier["min_xp"])
    progress_pct = round(min(100.0, (xp_in_level / level_span) * 100.0), 1)

    # Compute Badges
    badges = [
        {
            "id": "smc_master",
            "name": "SMC Master",
            "icon": "🎯",
            "unlocked": closed_trades >= 10 or total_cycles >= 15,
            "desc": "ผ่านการสแกนและวิเคราะห์โครงสร้าง SMC",
        },
        {
            "id": "risk_guardian",
            "name": "Risk Guardian",
            "icon": "🛡️",
            "unlocked": True,
            "desc": "ระบบคุมความเสี่ยง Veto ล็อกไม้สุ่มเสี่ยง 100%",
        },
        {
            "id": "deep_researcher",
            "name": "Deep Researcher",
            "icon": "📚",
            "unlocked": research_count >= 5 or total_cycles >= 20,
            "desc": "ออกวิจัยบทเรียนเทรดบนเว็บเข้า ChromaDB",
        },
        {
            "id": "prop_firm_ready",
            "name": "Prop Firm Ready",
            "icon": "👑",
            "unlocked": win_rate >= 45.0 and closed_trades >= 10,
            "desc": "สถิติ Win Rate และ RR พร้อมสอบพอร์ตกองทุน",
        },
    ]

    return {
        "level": current_tier["level"],
        "title": current_tier["title"],
        "badge": current_tier["badge"],
        "xp": xp,
        "current_level_min_xp": current_tier["min_xp"],
        "next_level_xp": next_tier["min_xp"],
        "progress_pct": progress_pct,
        "closed_trades": closed_trades,
        "wins": wins,
        "research_count": research_count,
        "badges": badges,
    }


if __name__ == "__main__":
    st = get_ai_level_status()
    print(f"AI Level {st['level']} - {st['title']} ({st['xp']} XP, {st['progress_pct']}%)")
