"""Trading hour restrictions.

ห้ามเทรดในช่วงเวลาที่กำหนด (เวลาไทย UTC+7):
- 19:00–20:00 (1ทุ่ม–2ทุ่ม): liquidity ต่ำ, spread กว้าง, Asia session เปิดใหม่
"""
from __future__ import annotations

import datetime

UTC_OFFSET = datetime.timezone(datetime.timedelta(hours=7))

# รายการช่วงเวลาห้ามเทรด (ชั่วโมงเริ่ม, ชั่วโมงสิ้นสุด) เวลาไทย
BLOCKED_HOURS: list[tuple[int, int]] = [
    (19, 20),  # 1ทุ่ม–2ทุ่ม
]


def is_trading_allowed() -> tuple[bool, str | None]:
    """Returns (allowed, reason). reason is None when allowed."""
    now = datetime.datetime.now(UTC_OFFSET)
    h = now.hour
    for start, end in BLOCKED_HOURS:
        if start <= h < end:
            return False, f"ช่วงเวลาห้ามเทรด {start}:00–{end}:00 น. (เวลาไทย) — ปิดรับ signal ชั่วคราว"
    return True, None
