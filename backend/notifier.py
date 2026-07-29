"""Telegram Push Notification Engine for Trading Room AI.

Sends non-blocking real-time mobile notifications via Telegram Bot API when:
1. An MT5 trade is opened (with entry, SL, TP, risk %, and ALLIN score).
2. An MT5 trade is closed (with Win/Loss status, profit $, and duration).
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

_backend_env = Path(__file__).parent / ".env"
if _backend_env.exists():
    load_dotenv(_backend_env)


def _get_config() -> tuple[str | None, str | None]:
    _e1 = Path(__file__).parent / ".env"
    _e2 = Path(__file__).parent.parent / ".env"
    if _e1.exists():
        load_dotenv(_e1)
    if _e2.exists():
        load_dotenv(_e2)
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token if token else None, chat_id if chat_id else None


def send_telegram_sync(text: str) -> tuple[bool, str]:
    """Send text message synchronously via Telegram Bot API."""
    token, chat_id = _get_config()
    if not token or not chat_id:
        msg = f"Telegram not configured (token={bool(token)}, chat_id={bool(chat_id)})"
        print(f"[Notifier] {msg}")
        return False, msg

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        import ssl
        ctx = ssl._create_unverified_context()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            ok = res_json.get("ok", False)
            return ok, "Success" if ok else f"Telegram API error: {res_json}"
    except Exception as e:
        msg = f"HTTP Error: {e}"
        print(f"[Notifier] Failed to send Telegram message: {e}")
        return False, msg


def send_telegram_async(text: str) -> None:
    """Send message in a non-blocking background thread."""
    t = threading.Thread(target=send_telegram_sync, args=(text,), daemon=True)
    t.start()


def notify_trade_opened(
    ticket: int | str,
    symbol: str,
    action: str,
    volume: float,
    price_open: float,
    sl: float | None = None,
    tp: float | None = None,
    risk_pct: float | None = None,
    score: int | None = None,
    reason: str | None = None,
    major_support: float | None = None,
    major_resistance: float | None = None,
) -> None:
    """Send rich Telegram alert for new open trade with Major Support & Resistance levels."""
    action_upper = str(action).upper()
    icon = "🟢 <b>[BUY ORDER]</b>" if "BUY" in action_upper else "🔴 <b>[SELL ORDER]</b>"
    sl_str = f"{sl:,.5f}" if sl else "N/A"
    tp_str = f"{tp:,.5f}" if tp else "N/A"
    risk_str = f"{risk_pct:.2f}%" if risk_pct is not None else "N/A"
    score_str = f"{score}/100" if score is not None else "N/A"
    sup_str = f"{major_support:,.5f}" if major_support else "N/A"
    res_str = f"{major_resistance:,.5f}" if major_resistance else "N/A"

    msg = (
        f"{icon} <b>เปิดออเดอร์ใหม่เข้า MT5 สำเร็จ!</b>\n\n"
        f"📌 <b>สินทรัพย์:</b> #{symbol}\n"
        f"🎫 <b>Ticket:</b> #{ticket}\n"
        f"📊 <b>ประเภท:</b> {action_upper} {volume:.2f} Lot\n"
        f"💵 <b>ราคาเข้า (Entry):</b> <code>{price_open:,.5f}</code>\n"
        f"🛡️ <b>แนวรับสำคัญ (OS1 Support):</b> <code>{sup_str}</code>\n"
        f"🎯 <b>แนวต้านสำคัญ (OB1 Resistance):</b> <code>{res_str}</code>\n"
        f"🛑 <b>Stop Loss (SL):</b> <code>{sl_str}</code>\n"
        f"🎯 <b>Take Profit (TP):</b> <code>{tp_str}</code>\n"
        f"🛡️ <b>ความเสี่ยง (Risk):</b> {risk_str}\n"
        f"🔥 <b>ALLIN Score:</b> {score_str}\n"
    )
    if reason:
        msg += f"\n💡 <b>เหตุผล AI:</b> {reason[:120]}"

    send_telegram_async(msg)


def notify_trade_closed(
    ticket: int | str,
    symbol: str,
    action: str,
    volume: float,
    result: str,
    profit: float,
    close_price: float | None = None,
) -> None:
    """Send rich Telegram alert when a trade is closed."""
    res_lower = str(result).lower()
    if "win" in res_lower or profit > 0:
        icon = "🎉 <b>[WIN / PROFIT]</b>"
        p_str = f"<b>+${profit:,.2f} USD</b> 🟢"
    elif "loss" in res_lower or profit < 0:
        icon = "🔴 <b>[LOSS / STOP LOSS]</b>"
        p_str = f"<b>-${abs(profit):,.2f} USD</b> 🔻"
    else:
        icon = "⚪ <b>[BREAKEVEN]</b>"
        p_str = f"<b>$0.00 USD</b>"

    close_str = f"<code>{close_price:,.5f}</code>" if close_price else "N/A"

    msg = (
        f"{icon} <b>ปิดออเดอร์เรียบร้อยแล้ว!</b>\n\n"
        f"📌 <b>สินทรัพย์:</b> #{symbol}\n"
        f"🎫 <b>Ticket:</b> #{ticket}\n"
        f"📊 <b>ประเภท:</b> {str(action).upper()} {volume:.2f} Lot\n"
        f"🔚 <b>ราคาปิด:</b> {close_str}\n"
        f"💰 <b>กำไร/ขาดทุน (P/L):</b> {p_str}\n"
    )

    send_telegram_async(msg)


def notify_stoch_swing(symbol: str, reason: str, price: float | None = None) -> None:
    """Send rich Telegram alert when Stoch (9,3,3) Swing / Zone setup fires."""
    is_buy = "ขึ้น" in reason or "BUY" in reason.upper() or "l2 > l1" in reason.lower()
    icon = "🌊 🟢 <b>[STOCH SWING / SETUP FIRED]</b>" if is_buy else "🌊 🔴 <b>[STOCH SWING / SETUP FIRED]</b>"
    p_str = f"<code>{price:,.5f}</code>" if price else "N/A"

    msg = (
        f"{icon}\n\n"
        f"📌 <b>สินทรัพย์:</b> #{symbol}\n"
        f"💵 <b>ราคาปัจจุบัน:</b> {p_str}\n"
        f"💡 <b>รายละเอียด:</b> {reason}\n\n"
        f"🤖 <b>สถานะ AI:</b> กำลังดึงสมองกล AI ประเมินอนุมัติยิงไม้ออเดอร์เข้า MT5..."
    )
    send_telegram_async(msg)

