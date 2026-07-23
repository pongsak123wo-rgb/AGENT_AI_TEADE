#!/usr/bin/env python3
"""MCP server for the Trading Room AI system — zero dependencies.

Lets an MCP client (Claude Desktop / Claude Code) inspect the live trading
system in plain language: what zones each symbol is watching, why a trade
was or wasn't taken, what the real P/L is, whether anything is broken.

MCP is just JSON-RPC 2.0 over stdio, so this is implemented against the
standard library alone — no `mcp` package needed. It talks to the running
backend's REST API (default http://localhost:8000).

Register it with an MCP client, e.g. in claude_desktop_config.json:

    {
      "mcpServers": {
        "trading-room": {
          "command": "python",
          "args": ["C:/Users/Pong/Desktop/AgentAI/mcp_trading_server.py"]
        }
      }
    }
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("TRADING_API", "http://localhost:8000")
TIMEOUT = 20
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------- helpers
def _get(path: str) -> dict | list:
    """GET a backend endpoint, returning parsed JSON or an error dict."""
    url = f"{API}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = os.environ.get("DASHBOARD_PASSWORD")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"error": f"เชื่อมต่อ backend ไม่ได้ ({url}): {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _text(obj) -> dict:
    """Wrap a value as an MCP text content result."""
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}]}


# ---------------------------------------------------------------- tools
def t_system_status(_args: dict) -> dict:
    """Overall health: cycle heartbeat, MT5 link, LLM availability, alerts."""
    mon = _get("/monitor/status")
    acct = _get("/account")
    if isinstance(mon, dict) and mon.get("error"):
        return _text(mon)
    return _text({
        "ok": mon.get("ok"),
        "alerts": mon.get("alerts"),
        "cycle": mon.get("cycle"),
        "mt5": mon.get("mt5"),
        "llm": mon.get("llm"),
        "kill_switch": mon.get("kill_switch"),
        "account": acct.get("account") if isinstance(acct, dict) else None,
        "open_positions": len(acct.get("positions", [])) if isinstance(acct, dict) else None,
    })


def t_watched_zones(args: dict) -> dict:
    """What each symbol is waiting for: multi-TF zones, trend, engage state."""
    zones = _get("/zones")
    if isinstance(zones, dict) and zones.get("error"):
        return _text(zones)
    symbol = args.get("symbol")
    out = {}
    for sym, d in (zones or {}).items():
        if symbol and sym.upper() != symbol.upper():
            continue
        mtf = d.get("mtf", {})
        out[sym] = {
            "price": d.get("price"),
            "trend": mtf.get("trend", {}).get("overall"),
            "trend_per_tf": mtf.get("trend", {}).get("per_tf"),
            "engage_now": mtf.get("engage"),
            "reason": mtf.get("reason"),
            "pairs": [
                {
                    "entry_tf": p.get("entry_tf"),
                    "structure_tf": p.get("structure_tf"),
                    "fired": p.get("fired"),
                    "zones": [
                        {"kind": z.get("kind"), "dir": z.get("dir"),
                         "low": z.get("low"), "high": z.get("high")}
                        for z in (p.get("zones") or [])
                    ],
                }
                for p in (mtf.get("pairs") or [])
            ],
        }
    return _text(out or {"note": "ยังไม่มีข้อมูลโซน (รอ cycle แรก)"})


def t_positions(_args: dict) -> dict:
    """Real open positions from MT5 plus the current risk budget."""
    acct = _get("/account")
    risk = _get("/risk")
    return _text({
        "live": acct.get("live") if isinstance(acct, dict) else None,
        "source": acct.get("source") if isinstance(acct, dict) else None,
        "account": acct.get("account") if isinstance(acct, dict) else None,
        "positions": acct.get("positions") if isinstance(acct, dict) else None,
        "risk_config": risk.get("config") if isinstance(risk, dict) else None,
        "total_open_risk_pct": risk.get("total_open_risk_pct") if isinstance(risk, dict) else None,
    })


def t_performance(_args: dict) -> dict:
    """Real-trade-only performance: win rate, expectancy, per-symbol, daily P/L."""
    return _text({
        "stats": _get("/signals/stats"),
        "expectancy": _get("/signals/expectancy"),
        "by_symbol": _get("/signals/symbol-expectancy"),
        "daily_pnl": _get("/signals/daily-pnl"),
    })


def t_trade_journal(args: dict) -> dict:
    """Per-trade detail: levels, result, R-multiple, real P/L, entry reason."""
    limit = int(args.get("limit", 20))
    return _text(_get(f"/signals/journal?limit={limit}"))


def t_agent_feed(args: dict) -> dict:
    """Recent agent conversation — what Technical/Risk/CEO actually said."""
    limit = int(args.get("limit", 30))
    feed = _get("/feed/recent")
    if isinstance(feed, dict) and feed.get("error"):
        return _text(feed)
    rows = [{"agent": m.get("agent"), "text": m.get("text")} for m in (feed or [])][-limit:]
    return _text(rows)


def t_why_no_trade(_args: dict) -> dict:
    """Diagnose why no position is being opened right now."""
    feed = _get("/feed/recent")
    mon = _get("/monitor/status")
    zones = _get("/zones")
    blocks: list[str] = []
    for m in (feed or []):
        if not isinstance(m, dict):
            continue
        t = m.get("text", "")
        if m.get("agent") in ("risk", "ceo") and ("ไม่ออก" in t or "veto" in t or "ไม่เข้าไม้" in t or "ห้าม" in t):
            blocks.append(t[:160])
    engaged = [s for s, d in (zones or {}).items() if d.get("mtf", {}).get("engage")]
    return _text({
        "system_ok": mon.get("ok") if isinstance(mon, dict) else None,
        "llm_usable": mon.get("llm", {}).get("usable") if isinstance(mon, dict) else None,
        "llm_cooldown": mon.get("llm", {}).get("cooldown") if isinstance(mon, dict) else None,
        "symbols_at_a_zone_now": engaged,
        "recent_block_reasons": blocks[-8:] or ["ไม่พบเหตุผลบล็อกล่าสุด"],
        "hint": "ถ้า llm_usable ว่าง = ไม่มีสมองตัดสินใจ; ถ้า symbols_at_a_zone_now ว่าง = ราคายังไม่ถึงโซน",
    })


def t_cost(_args: dict) -> dict:
    """Gemini spend against the monthly budget cap."""
    return _text({"cost_guard": _get("/cost-guard/status"),
                  "llm_circuit_breaker": _get("/llm-circuit-breaker/status")})


TOOLS = [
    ("get_system_status", "สถานะระบบโดยรวม (cycle, MT5, LLM, alert, บัญชี)", {}, t_system_status),
    ("get_watched_zones", "โซนที่แต่ละสินทรัพย์เฝ้าอยู่ + เทรน multi-TF + แตะโซนหรือยัง",
     {"symbol": {"type": "string", "description": "กรองเฉพาะ symbol เช่น XAUUSD (ไม่ใส่ = ทุกตัว)"}}, t_watched_zones),
    ("get_positions", "ไม้ที่เปิดอยู่จริงใน MT5 + งบความเสี่ยง", {}, t_positions),
    ("get_performance", "ผลงานจริง: win rate, expectancy, แยกสินทรัพย์, P/L รายวัน", {}, t_performance),
    ("get_trade_journal", "รายละเอียดไม้ล่าสุด (entry/SL/TP/ผล/R/P-L/เหตุผล)",
     {"limit": {"type": "integer", "description": "จำนวนไม้ (ค่าเริ่มต้น 20)"}}, t_trade_journal),
    ("get_agent_feed", "บทสนทนา agent ล่าสุด (Technical/Risk/CEO พูดอะไร)",
     {"limit": {"type": "integer", "description": "จำนวนข้อความ (ค่าเริ่มต้น 30)"}}, t_agent_feed),
    ("diagnose_no_trade", "วิเคราะห์ว่าทำไมตอนนี้ยังไม่เข้าไม้", {}, t_why_no_trade),
    ("get_llm_cost", "ค่าใช้จ่าย Gemini เทียบงบเดือน + สถานะ provider", {}, t_cost),
]
HANDLERS = {name: fn for name, _d, _s, fn in TOOLS}


def _tool_list() -> list[dict]:
    return [
        {
            "name": name,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": []},
        }
        for name, desc, props, _fn in TOOLS
    ]


# ---------------------------------------------------------------- JSON-RPC
def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "trading-room", "version": "1.0.0"},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None  # notification — no reply
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": fn(args)}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"tool error: {e!r}"}],
                               "isError": True}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
