import asyncio
import json
import os
import secrets
import time
from collections import deque

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()

# Dashboard password. Set DASHBOARD_PASSWORD in .env to lock the API + WS
# so "anyone who knows the IP" can't read or command the system. Left
# empty = auth disabled (local dev convenience).
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def _token_ok(token: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    return secrets.compare_digest(token or "", DASHBOARD_PASSWORD)

import backtest_engine
import backtest_log
import decision_audit
import mt5_history_bridge
import kill_switch
import knowledge_base
import llm_circuit_breaker
import mt5_bridge
import order_executor
import pattern_disable
import research_log
import session_summary
import signal_log
import monitor
import mt5_direct
import mtf_engine
import smc_analysis
import trading_hours
import web_research
import zone_watch
from agents import AgentMessage, CEOAgent, DataAgent, RiskAgent, TechnicalAgent
from risk import RiskConfig, RiskManager

from pathlib import Path
from fastapi.staticfiles import StaticFiles

app = FastAPI()

_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_frontend_dir), html=True), name="ui")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request, call_next):
    # Let CORS preflight through untouched; guard everything else with the
    # token when a password is configured. Token comes from the
    # X-Auth-Token header (fetch) or ?token= query (fallback).
    if DASHBOARD_PASSWORD and request.method != "OPTIONS":
        token = request.headers.get("X-Auth-Token") or request.query_params.get("token", "")
        if not _token_ok(token):
            # This middleware runs OUTSIDE CORSMiddleware, so a bare 401 here
            # skips CORS and the browser blocks it — the login overlay never
            # sees the 401 and never appears (though non-browser clients like
            # the MCP server, which ignore CORS, work fine). Echo the CORS
            # headers manually so the browser accepts the 401 and shows login.
            origin = request.headers.get("origin", "*")
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Vary": "Origin",
                },
            )
    return await call_next(request)


@app.get("/auth/check")
def auth_check():
    # Reaching here means the middleware already accepted the token.
    return {"ok": True, "auth_required": bool(DASHBOARD_PASSWORD)}

clients: list[WebSocket] = []
recent_messages: deque = deque(maxlen=50)

# Mock symbol list — represents "every asset on the MT5 terminal".
# Swap for `mt5.symbols_get()` once real MT5 integration lands.
SYMBOLS = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2640,
    "USDJPY": 156.30,
    "XAUUSD": 2342.50,
    "EURJPY": 169.50,
    "GBPJPY": 197.60,
    "BTCUSD": 95000.0,
}

data_agent = DataAgent(symbols=SYMBOLS)
technical_agent = TechnicalAgent()
risk_manager = RiskManager(RiskConfig())
risk_agent = RiskAgent(risk_manager)
ceo_agent = CEOAgent()

symbol_cycle = list(SYMBOLS.keys())
cycle_index = 0
equity_baseline_set = False

# Latest zone-watch state per symbol (for the UI zone panel). Populated
# every cheap cycle even when no LLM call happens.
latest_zones: dict = {}


async def broadcast(message: AgentMessage):
    payload_dict = {"agent": message.agent, "text": message.text, "kind": message.kind, "data": message.data}
    recent_messages.append(payload_dict)
    payload = json.dumps(payload_dict)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


async def run_cycle():
    global cycle_index, equity_baseline_set
    symbol = symbol_cycle[cycle_index % len(symbol_cycle)]
    cycle_index += 1

    live = mt5_bridge.read_snapshot()
    if live:
        if not equity_baseline_set:
            risk_manager.state.equity_start_of_day = live["account"]["equity"]
            risk_manager.state.equity_peak = live["account"]["equity"]
            equity_baseline_set = True
        risk_manager.sync_from_account(live["account"])
        risk_manager.sync_positions_from_mt5(live["positions"])
        tripped = kill_switch.auto_trip_if_needed(risk_manager)
        if tripped:
            await broadcast(AgentMessage(agent="ceo", text=f"Kill switch ตัดอัตโนมัติ — {tripped}", kind="info"))

    snapshot = data_agent.tick(symbol)
    await broadcast(data_agent.report(snapshot))
    await asyncio.sleep(0.5)

    # PHASE 1 (cheap, no LLM): compute indicators + SMC + Elliott every
    # cycle. This is pure math — costs no tokens.
    indicator_pre = await asyncio.to_thread(technical_agent.compute, snapshot)
    if not indicator_pre.get("ready"):
        await broadcast(AgentMessage(agent="technical", text="ข้อมูลราคายังไม่พอคำนวณ indicator (ต้องมีอย่างน้อย 20 จุด)", kind="info"))
        return

    # PHASE 1b (cheap): multi-timeframe read — pair HTF structure/zones with
    # LTF entry (H1↔M5, H4↔M15) and read trend from H1/H4/D1. Only escalate
    # to the LLM when an LTF price reaches an HTF zone that agrees with the
    # trend consensus. Everything here is pure math (resampling + SMC).
    mtf = mtf_engine.analyze(
        snapshot.get("candles"), snapshot.get("h1_candles"),
        snapshot["price"], indicator_pre.get("atr"),
        d1=(live or {}).get("d1_candles", {}).get(symbol) if live else None,
    )
    latest_zones[symbol] = {"mtf": mtf, "price": snapshot["price"], "at": time.time()}

    if not mtf["engage"]:
        # Not at any pair's zone → stay in watch mode, no LLM. Free cycle.
        trend_txt = f"เทรน {mtf['trend']['overall']} (H1={mtf['trend']['per_tf'].get('H1')}/H4={mtf['trend']['per_tf'].get('H4')}/D1={mtf['trend']['per_tf'].get('D1')})"
        await broadcast(AgentMessage(agent="technical", text=f"👁 เฝ้าโซน {symbol}: {mtf['reason']} · {trend_txt}", kind="info",
                                     data={"mtf": mtf, "symbol": symbol}))
        return

    # PHASE 2 (paid): an LTF reached an HTF zone aligned with trend — worth
    # asking the LLM now. This is where tokens get spent.
    await broadcast(AgentMessage(agent="technical", text=f"🎯 {symbol}: {mtf['reason']} → เรียก AI วิเคราะห์", kind="info",
                                 data={"mtf": mtf, "symbol": symbol}))
    technical = await asyncio.to_thread(technical_agent.reason, snapshot["symbol"], indicator_pre)
    await broadcast(technical_agent.report(technical))
    await asyncio.sleep(0.5)

    indicators = technical.get("indicators", {})
    mtf_confluence = smc_analysis.classify_mtf_confluence(indicators.get("smc"), technical["bias"])
    structure_event = (indicators.get("smc") or {}).get("structure_event")

    # Trading hours veto — ห้ามเทรด 1ทุ่ม–2ทุ่ม (19:00–20:00 เวลาไทย)
    hours_ok, hours_reason = trading_hours.is_trading_allowed()
    if not hours_ok:
        await broadcast(AgentMessage(agent="risk", text=hours_reason, kind="info"))
        risk = {"approved": False, "lot": 0.0, "reason": hours_reason}
    else:
        pattern_disabled = (
            pattern_disable.check(symbol, technical["bias"], indicators.get("rsi_state"), indicators.get("ema_trend"))
            or pattern_disable.check_structure(symbol, structure_event, mtf_confluence)
        )
        if pattern_disabled:
            await broadcast(AgentMessage(agent="risk", text=f"Setup นี้ถูกปิดใช้งานชั่วคราว — {pattern_disabled['reason']}", kind="info"))
            risk = {"approved": False, "lot": 0.0, "reason": pattern_disabled["reason"]}
        else:
            risk = risk_agent.evaluate(
                symbol,
                technical["bias"],
                spread=snapshot.get("spread"),
                atr=indicators.get("atr"),
                mtf_confluence=mtf_confluence,
                ema_trend=indicators.get("ema_trend"),
                rsi_state=indicators.get("rsi_state"),
                price=snapshot["price"],
            )
    await broadcast(risk_agent.report(risk))
    await asyncio.sleep(0.5)

    decision = await asyncio.to_thread(ceo_agent.decide, technical, {}, risk, snapshot)
    await broadcast(ceo_agent.report(decision))

    # Transparent, code-computed factor sheet for this decision — never a
    # pure LLM black box. Built from the real numbers, deterministic.
    if decision["action"] != "no_trade":
        audit = decision_audit.build(technical["bias"], indicators, mtf, risk, symbol=symbol)
        decision["audit"] = audit
        factor_lines = " | ".join(
            f"{'✅' if f['stance'] == 'for' else '❌' if f['stance'] == 'against' else '➖'} {f['name']}: {f['detail']}"
            for f in audit["factors"]
        )
        await broadcast(AgentMessage(
            agent="ceo",
            text=f"📋 ใบตรวจสอบการตัดสินใจ ({audit['summary']}) — {factor_lines}",
            kind="info",
            data={"audit": audit, "symbol": symbol},
        ))

    if decision["action"] != "no_trade" and risk["approved"] and not kill_switch.is_enabled():
        await broadcast(
            AgentMessage(agent="ceo", text=f"Signal ผ่านทุกเกณฑ์แต่ Kill Switch ปิดอยู่ — ไม่ส่งออเดอร์ ({kill_switch.status()['tripped_reason']})", kind="info")
        )
    elif decision["action"] != "no_trade" and risk["approved"]:
        # Send the order FIRST. A signal is only persisted to signals.db
        # once it becomes a REAL trade (a confirmed MT5 fill with a ticket).
        # Previously we logged before sending, so orders that never executed
        # — e.g. US30/NAS100 which aren't in Market Watch, or any mock-data
        # cycle — still got recorded and then "settled" on synthetic prices,
        # polluting every win-rate / expectancy / calendar stat with trades
        # that never actually happened.
        send_result = order_executor.send_order(decision, risk_manager.state.equity)
        await broadcast(AgentMessage(agent="ceo", text=send_result["reason"], kind="info"))

        if send_result["sent"]:
            exec_result = None
            for _ in range(6):
                await asyncio.sleep(0.5)
                exec_result = order_executor.try_read_result(send_result["id"])
                if exec_result:
                    break
            if exec_result and exec_result["success"]:
                # Confirmed real fill — NOW record it as a real trade.
                risk_manager.open_position(symbol, decision["action"], risk_pct=decision["risk_pct"])
                signal_id = signal_log.log_signal(decision, indicators=technical.get("indicators"), reason=technical.get("reason"), mtf_confluence=mtf_confluence)
                signal_log.record_execution(
                    signal_id,
                    slippage=exec_result.get("slippage", 0),
                    commission=exec_result.get("commission", 0),
                    swap=exec_result.get("swap", 0),
                    filled_price=exec_result.get("filled_price", 0),
                )
                risk_manager.record_ticket_risk(exec_result["ticket"], decision["risk_pct"])
                signal_log.set_ticket(signal_id, exec_result["ticket"])
                await broadcast(
                    AgentMessage(
                        agent="ceo",
                        text=(
                            f"MT5 เปิดออเดอร์สำเร็จ — ticket #{exec_result['ticket']} "
                            f"(slippage {exec_result.get('slippage', 0):.5f}, ค่าคอม {exec_result.get('commission', 0)})"
                        ),
                        kind="info",
                    )
                )
            elif exec_result:
                # Broker rejected — this was never a real trade, so nothing
                # is logged and it won't count toward any statistic.
                await broadcast(
                    AgentMessage(agent="ceo", text=f"MT5 ปฏิเสธคำสั่ง: {exec_result.get('message', exec_result.get('reason', '?'))} — ไม่นับเป็นไม้ (ไม่บันทึก)", kind="info")
                )
            else:
                await broadcast(
                    AgentMessage(agent="ceo", text="ไม่ได้รับผลตอบจาก EA/MT5 — ไม่นับเป็นไม้ (ไม่บันทึก)", kind="info")
                )
        else:
            # Order not sent at all (not a demo account, MT5 offline, mock
            # data) — nothing logged. send_result reason already broadcast.
            await broadcast(
                AgentMessage(agent="ceo", text="ไม่ได้ส่งออเดอร์จริง — ไม่นับเป็นไม้ (เก็บสถิติเฉพาะไม้ที่เข้า MT5 จริง)", kind="info")
            )

    # Settlement, best source first:
    # 1) REAL closed-deal P/L from MT5 (exact money, commission+swap included)
    # 2) price hit SL/TP as recorded
    # 3) ticket vanished from MT5 → estimate from last price (last resort)
    # Re-read the snapshot FIRST: `live` above was captured at the start of
    # this cycle, before any order was placed, so a brand-new ticket looked
    # "already gone from MT5" and got closed instantly at its entry price.
    fresh = mt5_bridge.read_snapshot()
    live_tickets = {p["ticket"] for p in fresh["positions"]} if fresh else set()

    # Settle ONLY against real broker prices. data_agent.prices is seeded
    # with mock values and a symbol keeps its seed until it ticks in this
    # process, so using it settled live trades against a fake price (an
    # open EURJPY at 186 was closed as a loss against the 169.50 seed).
    real_prices: dict[str, float] = {}
    if fresh:
        for sym, px in (fresh.get("symbols") or {}).items():
            if px.get("bid") and px.get("ask"):
                real_prices[sym] = (px["bid"] + px["ask"]) / 2

    settled = []
    open_tickets = signal_log.get_open_tickets()
    if open_tickets and fresh:
        try:
            close_info = mt5_direct.get_close_info(open_tickets)
            # live_tickets guard: a partial close (the EA's TP1/trailing
            # management) also writes a closing deal, so only settle tickets
            # that are genuinely gone from MT5's open-position list.
            settled += signal_log.settle_by_real_deals(close_info, live_tickets)
        except Exception:
            pass

    if real_prices:
        settled += signal_log.check_open_signals(real_prices)
        settled += signal_log.check_settled_by_ticket(real_prices, live_tickets)
    for s in settled:
        risk_manager.close_position(s["symbol"], s["action"])
        if s["result"] == "win":
            result_th = "ชนะ"
        elif s["result"] == "loss":
            result_th = "แพ้"
        else:
            result_th = "เสมอ (breakeven)"
        if s.get("profit") is not None:
            result_th += f" · P/L จริง {s['profit']:+.2f}"
        await broadcast(
            AgentMessage(
                agent="ceo",
                text=f"Signal #{s['id']} {s['symbol']} ปิดแล้ว — {result_th}",
                kind="info",
            )
        )

    research = await asyncio.to_thread(web_research.run_if_due)
    if research and "skipped" not in research:
        total_chunks = sum(n for n in research.values() if isinstance(n, int))
        topics_text = ", ".join(research.keys())
        await broadcast(
            AgentMessage(
                agent="technical",
                text=f"ค้นคว้าเอง {len(research)} เรื่อง ({topics_text}) ได้ความรู้ใหม่ {total_chunks} chunks เข้า RAG",
                kind="info",
            )
        )


async def cycle_loop():
    # Runs continuously — NOT gated on a connected browser. On a 24/7 VPS
    # nobody keeps the dashboard open, so gating on `clients` used to freeze
    # the whole system whenever the last tab closed. The zone-watch gate
    # keeps this cheap: most cycles never touch the LLM. broadcast() handles
    # zero connected clients fine (it just iterates an empty list).
    #
    # Each cycle is wrapped so a single failure (bad tick, transient MT5
    # read, LLM hiccup) logs and continues instead of killing the loop.
    while True:
        try:
            await run_cycle()
            monitor.record_cycle(symbol_cycle[(cycle_index - 1) % len(symbol_cycle)])
        except Exception as e:
            monitor.record_error(repr(e))
            print(f"[cycle_loop] error (continuing): {e!r}")
        await asyncio.sleep(5)


@app.on_event("startup")
async def startup():
    asyncio.create_task(cycle_loop())


@app.get("/feed/recent")
def get_recent_feed():
    return list(recent_messages)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Browsers can't set headers on a WebSocket, so the token rides in the
    # query string (?token=). Reject before accepting if it's wrong.
    if DASHBOARD_PASSWORD and not _token_ok(ws.query_params.get("token", "")):
        await ws.close(code=1008)  # policy violation
        return

    await ws.accept()
    was_empty = len(clients) == 0
    clients.append(ws)

    if was_empty:
        last_summary_at = session_summary.get_last_summary_at()
        now = time.time()
        if now - last_summary_at > session_summary.MIN_GAP_SEC:
            summary_text = session_summary.build_summary(last_summary_at)
            if summary_text:
                await broadcast(AgentMessage(agent="ceo", text=summary_text, kind="info"))
            session_summary.set_last_summary_at(now)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.remove(ws)


class RiskConfigUpdate(BaseModel):
    risk_per_trade_pct: float | None = None
    max_total_open_risk_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    max_total_drawdown_pct: float | None = None
    max_concurrent_positions: int | None = None


@app.get("/risk")
def get_risk():
    return risk_manager.snapshot()


@app.put("/risk")
def update_risk(update: RiskConfigUpdate):
    risk_manager.update_config(**update.model_dump())
    return risk_manager.snapshot()


@app.post("/risk/reset-positions")
def reset_positions():
    risk_manager.state.open_positions.clear()
    return risk_manager.snapshot()


@app.get("/kill-switch")
def get_kill_switch():
    return kill_switch.status()


@app.post("/kill-switch/enable")
def enable_kill_switch():
    kill_switch.enable()
    return kill_switch.status()


@app.post("/kill-switch/disable")
def disable_kill_switch():
    kill_switch.disable("ปิดโดยผู้ใช้")
    return kill_switch.status()


@app.get("/health")
def get_health():
    return kill_switch.check_health()


@app.get("/pattern-disable/status")
def get_pattern_disable_status():
    return pattern_disable.status()


@app.get("/llm-circuit-breaker/status")
def get_llm_circuit_breaker_status():
    return llm_circuit_breaker.status()


@app.get("/cost-guard/status")
def get_cost_guard_status():
    import cost_guard
    return cost_guard.status()


@app.get("/monitor/status")
def get_monitor_status():
    return monitor.status()


@app.get("/collect-mode")
def get_collect_mode():
    return {"enabled": os.environ.get("COLLECT_MODE") == "1"}


@app.post("/collect-mode")
def set_collect_mode(on: bool = True):
    """Toggle data-collection mode at runtime (no restart, no VPS edit).
    ON lowers the CEO approval bar so more trades go through to feed the
    learning mechanisms; turn OFF once enough real trades are collected."""
    os.environ["COLLECT_MODE"] = "1" if on else "0"
    return {"enabled": on}


@app.get("/cot/status")
def get_cot_status():
    """Large-speculator positioning per symbol from the CFTC's weekly COT."""
    import cot_report
    return cot_report.status()


@app.get("/backtest/run")
def run_backtest(symbol: str = "EURUSD", period: str = "60d", interval: str = "1h", source: str = "yahoo"):
    return backtest_engine.run_backtest(symbol, period=period, interval=interval, source=source)


@app.post("/backtest/run-batch")
def run_backtest_batch(period: str = "90d", interval: str = "1h", require_mtf_confluence: bool = False, source: str = "yahoo"):
    return backtest_engine.run_backtest_batch(period=period, interval=interval, require_mtf_confluence=require_mtf_confluence, source=source)


@app.get("/backtest/mt5-history-status")
def get_mt5_history_status():
    return mt5_history_bridge.status()


@app.get("/backtest/structure-patterns")
def get_backtest_structure_patterns():
    return backtest_log.get_structure_patterns()


@app.get("/backtest/summary")
def get_backtest_summary():
    return backtest_log.summary()


@app.post("/backtest/v2")
def run_backtest_v2(entry_tf: str = "M15", max_bars: int = 800,
                    max_concurrent: int = 6, symbol: str | None = None):
    """Portfolio backtest that replays the LIVE pipeline — zones, multi-TF,
    adaptive SL/TP, risk gates, correlation veto, a shared position cap and
    real costs (spread + commission + swap), on one simulated account."""
    import backtest_v2
    syms = [symbol] if symbol else None
    return backtest_v2.run_portfolio(syms, entry_tf=entry_tf, max_bars=max_bars,
                                     max_concurrent=max_concurrent)


@app.get("/account")
def get_account():
    live = mt5_bridge.read_snapshot()
    if not live:
        return {"live": False, "account": None, "positions": [], "source": None}
    # "mt5_direct" = MetaTrader5 Python package; absent = EA file bridge
    source = live.get("source", "ea_file")
    return {"live": True, "account": live["account"], "positions": live["positions"], "source": source}


@app.get("/signals/stats")
def get_signal_stats():
    return signal_log.get_stats()


@app.get("/signals/expectancy")
def get_signal_expectancy(symbol: str | None = None):
    return signal_log.get_expectancy(symbol=symbol)


@app.get("/signals/recent")
def get_recent_signals():
    return signal_log.recent()


@app.get("/symbols")
def get_symbols():
    return {"symbols": list(SYMBOLS.keys())}


@app.get("/zones")
def get_zones():
    return latest_zones


@app.get("/signals/direction-stats")
def get_direction_stats():
    return signal_log.get_direction_stats()


@app.get("/signals/hourly-stats")
def get_hourly_stats():
    return signal_log.get_hourly_stats()


@app.get("/signals/daily-pnl")
def get_daily_pnl():
    return signal_log.get_daily_pnl()


@app.get("/signals/equity-curve")
def get_equity_curve():
    return signal_log.get_equity_curve()


@app.get("/signals/journal")
def get_trade_journal(limit: int = 50):
    return signal_log.get_trade_journal(limit=limit)


@app.get("/signals/symbol-expectancy")
def get_symbol_expectancy():
    return signal_log.get_symbol_expectancy_all()


@app.get("/signals/rsi-ema-matrix")
def get_rsi_ema_matrix():
    return signal_log.get_rsi_ema_matrix()


@app.get("/signals/patterns")
def get_signal_patterns():
    return signal_log.get_learned_patterns()


@app.get("/signals/structure-patterns")
def get_signal_structure_patterns():
    return signal_log.get_structure_patterns()


@app.get("/signals/costs")
def get_signal_costs():
    return signal_log.get_cost_stats()


@app.get("/signals/provider-accuracy")
def get_provider_accuracy():
    return signal_log.get_provider_accuracy()


@app.get("/ml/status")
def get_ml_status():
    import ml_model

    return ml_model.train_model()


@app.get("/research/log")
def get_research_log():
    return research_log.recent()


@app.get("/knowledge/status")
def knowledge_status():
    return knowledge_base.status()


@app.post("/knowledge/ingest")
def knowledge_ingest():
    return knowledge_base.ingest_all()
