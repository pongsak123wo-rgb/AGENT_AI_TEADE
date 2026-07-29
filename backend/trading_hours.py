"""Trading hour restrictions.

ห้ามเทรดในช่วงเวลาที่กำหนด (เวลาไทย UTC+7):
- 19:00–20:00 (1ทุ่ม–2ทุ่ม): liquidity ต่ำ, spread กว้าง, Asia session เปิดใหม่
"""
from __future__ import annotations

import datetime

UTC_OFFSET = datetime.timezone(datetime.timedelta(hours=7))

def is_trading_allowed() -> tuple[bool, str | None]:
    """Returns (allowed, reason). Allows 24/7 trading across all sessions."""
    return True, None
