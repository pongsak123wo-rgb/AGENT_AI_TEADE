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

from pathlib import Path

_backend_env = Path(__file__).parent / ".env"
_root_env = Path(__file__).parent.parent / ".env"
if _backend_env.exists():
    load_dotenv(_backend_env)
if _root_env.exists():
    load_dotenv(_root_env)
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
import notifier
import llm_circuit_breaker
import mt5_bridge
import order_executor
import pattern_disable
import research_log
import session_summary
import signal_log
import monitor
import mt5_direct
import swing_manager
import stoch_swing_engine
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


_moved_to_be: set = set()  # tickets already moved to breakeven (avoid re-modifying)
BE_TRIGGER_FRAC = 0.75     # move SL to entry only after price runs 75% toward TP1


async def _manage_breakeven(live: dict):
    """Move a position's SL to entry (breakeven) ONLY once price has travelled
    >= 75% of the way from entry to TP1 — never before, so a trade still has
    room to breathe / average early on instead of being scratched flat. Covers
    EVERY open trade (not just registered swings). All management is Python-side
    now; the MT5 EA only executes orders. Fails safe."""
    direct = mt5_direct if mt5_direct.available() else None
    if direct is None:
        return
    px = live.get("symbols", {})
    live_tickets = {p.get("ticket") for p in live.get("positions", [])}
    _moved_to_be.intersection_update(live_tickets)  # forget closed tickets
    try:
        targets = signal_log.get_open_trade_targets()
    except Exception:
        return
    # Tickets under swing_manager get their breakeven there (against TP1=OB1);
    # this general manager only handles the rest (e.g. rule-engine entries).
    swing_tickets = {t for pl in swing_manager.snapshot().values() for t in pl.get("tickets", [])}
    for p in live.get("positions", []):
        tk = p.get("ticket")
        tgt = targets.get(tk)
        if not tgt or tk in _moved_to_be or tk in swing_tickets:
            continue
        q = px.get(tgt["symbol"])
        if not q:
            continue
        price = (q["bid"] + q["ask"]) / 2
        entry, tp = tgt["entry"], tgt["tp"]
        span = tp - entry
        if span == 0:
            continue
        progress = (price - entry) / span  # 1.0 = reached TP1 (works both sides)
        if progress >= BE_TRIGGER_FRAC:
            res = direct.modify_sl(tk, entry)
            if res.get("ok"):
                _moved_to_be.add(tk)
                await broadcast(AgentMessage(agent="ceo",
                    text=f"🛡️ กันทุน {tgt['symbol']} #{tk} — ราคาวิ่งถึง {progress*100:.0f}% ของ TP1 เลื่อน SL มา entry", kind="info"))


async def _manage_swings(live: dict):
    """Per-cycle management of active Stoch-swing plans: add the one reserved
    DCA position when price reaches the two-zones-down trigger, or close every
    ticket when the swing structure is destroyed. Runs off the REAL MT5
    position list. Fails safe — any error is logged and never aborts the cycle.
    """
    direct = mt5_direct if mt5_direct.available() else None
    if direct is None:
        return
    live_tickets = {p.get("ticket") for p in live.get("positions", [])}
    px = live.get("symbols", {})
    for sym in list(swing_manager.snapshot().keys()):
        try:
            swing_manager.sync_tickets(sym, live_tickets)
            if not swing_manager.has_plan(sym):
                continue
            q = px.get(sym)
            if not q:
                continue
            price = (q["bid"] + q["ask"]) / 2
            for action in swing_manager.decide(sym, price):
                kind = action["action"]
                if kind == "close_all":
                    for tk in swing_manager.active_tickets(sym):
                        res = direct.close_ticket(tk)
                        await broadcast(AgentMessage(agent="ceo",
                            text=f"🛑 ปิดสวิง {sym} #{tk} — {action['reason']} ({res.get('reason')})", kind="info"))
                    swing_manager.forget(sym)
                    break  # nothing else to do once flat
                elif kind == "move_be":
                    ok = any(direct.modify_sl(tk, action["sl"]).get("ok")
                             for tk in swing_manager.active_tickets(sym))
                    if ok:  # only mark done if a stop actually moved — else retry next cycle
                        swing_manager.mark_be_done(sym)
                        await broadcast(AgentMessage(agent="ceo",
                            text=f"🛡️ กันทุน {sym} — {action['reason']}", kind="info"))
                elif kind == "partial":
                    ok = any(direct.close_partial(tk, action["fraction"]).get("ok")
                             for tk in swing_manager.active_tickets(sym))
                    if ok:
                        swing_manager.mark_partial_done(sym)
                        await broadcast(AgentMessage(agent="ceo",
                            text=f"💰 {sym} — {action['reason']}", kind="info"))
                elif kind == "dca":
                    dca_decision = {"symbol": sym, "action": action["side"],
                                    "risk_pct": action["risk_pct"], "sl": action["sl"],
                                    "tp": action["tp"]}
                    send = order_executor.send_order(dca_decision, risk_manager.state.equity)
                    if send.get("sent"):
                        exec_r = None
                        for _ in range(6):
                            await asyncio.sleep(0.5)
                            exec_r = order_executor.try_read_result(send["id"])
                            if exec_r:
                                break
                        if exec_r and exec_r.get("success"):
                            swing_manager.add_ticket(sym, exec_r["ticket"])
                            risk_manager.record_ticket_risk(exec_r["ticket"], action["risk_pct"])
                            await broadcast(AgentMessage(agent="ceo",
                                text=f"➕ ถัวไม้ 2 {sym} {action['side']} {action['risk_pct']}R — {action['reason']}", kind="info"))
        except Exception as e:
            print(f"[_manage_swings] {sym} error (continuing): {e!r}")


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
        # Manage open trades BEFORE scanning for new entries: breakeven-at-75%
        # of TP1 for every position, then Stoch-swing DCA / structure close.
        await _manage_breakeven(live)
        await _manage_swings(live)

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
        h4=(live or {}).get("h4_candles", {}).get(symbol) if live else None,
        m5=(live or {}).get("m5_candles", {}).get(symbol) if live else None,
        m15=(live or {}).get("m15_candles", {}).get(symbol) if live else None,
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
    global last_stoch_notify
    if "last_stoch_notify" not in globals():
        last_stoch_notify = {}
    now_ts = time.time()
    if now_ts - last_stoch_notify.get(symbol, 0) > 300:  # 5-minute cooldown per symbol
        last_stoch_notify[symbol] = now_ts
        notifier.notify_stoch_swing(symbol, mtf["reason"], snapshot["price"])

    await broadcast(AgentMessage(agent="technical", text=f"🎯 {symbol}: {mtf['reason']} → เรียก AI วิเคราะห์", kind="info",
                                 data={"mtf": mtf, "symbol": symbol}))
    technical = await asyncio.to_thread(technical_agent.reason, snapshot["symbol"], indicator_pre, mtf.get("chosen"))
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

    # Top-down direction guard: the pair engaged only in its higher-TF Stoch
    # swing direction (mtf["chosen"]["dir"]), but the LLM/CEO can still hand
    # back the opposite bias. Never let a real order fire against the chosen
    # HTF direction — that's exactly the "buy inside a downtrend" case. Map
    # bullish->buy / bearish->sell and veto any mismatch to no_trade.
    chosen = mtf.get("chosen") or {}
    chosen_action = {"bullish": "buy", "bearish": "sell"}.get(chosen.get("dir"))
    if chosen_action and decision["action"] not in ("no_trade", chosen_action):
        await broadcast(AgentMessage(
            agent="ceo",
            text=(f"⛔ ปฏิเสธ — AI เสนอ {decision['action']} สวนทิศ HTF ของคู่ {chosen.get('name')} "
                  f"({chosen.get('dir')}) ที่เลือกไว้ ไม่ส่งออเดอร์สวนเทรน"),
            kind="info",
        ))
        decision = {"action": "no_trade", "reason": "สวนทิศ HTF ที่เลือก (top-down guard)",
                    "council": decision.get("council")}

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
        # Stoch-swing entries: build the OS1↔OB1 fibo DCA plan. When there's
        # room to average ("far"), the FIRST order takes only the initial
        # fraction (0.5R) and the manager adds the reserved half later at the
        # two-zones-down trigger — combined risk stays ≤1R. base_risk_pct keeps
        # the full approved risk for that combined budget.
        swing_plan = None
        swing_tp2 = None
        base_risk_pct = decision.get("risk_pct")
        lv = (chosen.get("swing_levels") or {}) if chosen else {}
        if chosen and chosen.get("kind") == "stoch_swing" and lv.get("os1") and lv.get("ob1") and lv.get("os2"):
            entry_px = chosen.get("entry_price") or snapshot["price"]
            # Structural validity: the entry (OS2) must sit INSIDE the swing,
            # between OS1 (support/SL) and OB1 (the previous high = TP1), so
            # there's real room up to TP1 and the stop is below. When price has
            # already run ABOVE OB1 by the time we act, we'd be chasing: TP1
            # ends up below entry (tiny/negative reward) while SL at OS1 is far
            # — e.g. XAUUSD entry 4060.96 with OB1 4058.17, SL 4031.55 = RR
            # ~0.13. Skip those stale/chased setups.
            lo, hi = min(lv["os1"], lv["ob1"]), max(lv["os1"], lv["ob1"])
            if not (lo < entry_px < hi):
                await broadcast(AgentMessage(agent="risk",
                    text=(f"⛔ ข้าม {symbol} — ราคาเข้า {entry_px} หลุดโครงสร้างสวิง "
                          f"(OS1 {lv['os1']}–OB1 {lv['ob1']}) = ไล่ราคา RR แย่"), kind="info"))
                decision = {"action": "no_trade", "reason": "entry นอกโครงสร้าง OS1↔OB1 (ไล่ราคา)",
                            "council": decision.get("council")}
                swing_plan = None
            else:
                swing_plan = stoch_swing_engine.fibo_dca_plan(entry_px, lv["os1"], lv["ob1"], decision["action"])
            if swing_plan and swing_plan.get("ready"):
                # TP = TP2 (fibo 161.8%). TP1 (OB1) is banked in Python via a
                # partial close, not on the order.
                _t1, swing_tp2 = stoch_swing_engine.calculate_fibo_161_8(
                    lv["ob1"], lv["os2"], decision["action"])
                decision["tp"] = swing_tp2

                # SL choice must stay CONSISTENT with the DCA plan, or the two
                # fight: an average-in ("far") setup needs the structural OS1
                # stop (its fibo DCA zones sit just above OS1, and the blended
                # entry after averaging is what makes the RR acceptable). A
                # single-entry ("near") setup has no averaging, so it takes the
                # RR-protected stop — OS1, or a tighter ATR stop if OS1 is too
                # far for RR ≥ 1.5.
                if swing_plan.get("far"):
                    # SL on the CORRECT side: OS1 (below) for a buy, OB1 (above)
                    # for a sell. Using OS1 for a sell put the stop below the
                    # entry — the wrong side entirely (MT5 rejects it / it never
                    # protects). hard_sl from fibo_dca_plan already has this.
                    sl = swing_plan.get("hard_sl") or (lv["os1"] if decision["action"] == "buy" else lv["ob1"])
                else:
                    atr = (technical.get("indicators", {}) or {}).get("atr") or 0.0
                    sl, _tp1, _note = stoch_swing_engine.check_rr_and_sl(
                        entry_px, lv["os1"], lv["ob1"], atr, decision["action"])

                spread = snapshot.get("spread") or 0.0
                atr_g = (technical.get("indicators", {}) or {}).get("atr") or 0.0

                # Noise-swing guard: measured on the RAW structural stop (OS1↔
                # entry) BEFORE the buffer. On M5 the Stoch pivots often catch
                # tiny wiggles so OS1 sits 1-3 pips from entry — that's noise,
                # not structure; skip it (a tiny TP would come with it anyway).
                raw_dist = abs(entry_px - sl)
                min_dist = max(spread * 3.0, atr_g * 0.4)
                if min_dist > 0 and raw_dist < min_dist:
                    await broadcast(AgentMessage(agent="risk",
                        text=(f"⛔ ข้าม {symbol} — สวิงเล็กเกิน (OS1 ห่างแค่ {raw_dist:.5f} < ขั้นต่ำ "
                              f"{min_dist:.5f}) เป็น noise ไม่ใช่สวิงจริง"), kind="info"))
                    decision = {"action": "no_trade", "reason": "สวิงเล็กเกิน (OS1 แคบกว่า spread×3)",
                                "council": decision.get("council")}
                else:
                    # Spread buffer: push the stop a bit BEYOND OS1 so a spread
                    # spike right at OS1 (JPY/GBP crosses run wide) can't sweep
                    # the stop before price has truly broken structure. Risk in
                    # money is unchanged — the lot is sized to the SL distance.
                    buf = spread * 2.5
                    sl = sl - buf if decision["action"] == "buy" else sl + buf
                    decision["sl"] = round(sl, 5)
                    swing_plan = {**swing_plan, "hard_sl": round(sl, 5)}
                    decision["risk_pct"] = round(base_risk_pct * swing_plan["initial_fraction"], 2)

        if decision["action"] == "no_trade":
            return  # vetoed above (e.g. noise-swing SL too tight) — nothing to send

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
                # Arm the swing manager for BE / partial-TP1 / DCA / structure.
                if swing_plan and swing_plan.get("ready"):
                    swing_manager.register(
                        symbol, decision["action"],
                        chosen.get("entry_price") or exec_result.get("filled_price") or snapshot["price"],
                        lv["os1"], lv["ob1"], swing_tp2, exec_result["ticket"], base_risk_pct, swing_plan)
                notifier.notify_trade_opened(
                    ticket=exec_result["ticket"],
                    symbol=symbol,
                    action=decision["action"],
                    volume=exec_result.get("lot", 0),
                    price_open=exec_result.get("filled_price") or snapshot["price"],
                    sl=decision.get("sl"),
                    tp=decision.get("tp"),
                    risk_pct=decision.get("risk_pct"),
                    score=(decision.get("audit") or {}).get("score", 90),
                    reason=decision.get("reason"),
                )
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
        notifier.notify_trade_closed(
            ticket=s.get("ticket_id") or s.get("id"),
            symbol=s["symbol"],
            action=s["action"],
            volume=s.get("lot_size", 0.1),
            result=s.get("result", "none"),
            profit=s.get("profit", 0.0),
            close_price=s.get("exit_price"),
        )
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


async def auto_backup_loop():
    import auto_backup
    # Back up shortly AFTER startup (not only every 6h). The old loop slept 6h
    # first, so during heavy redeploys — where the backend restarts more often
    # than every 6h — a backup never fired at all (it last ran 2026-07-27).
    # A 2-min initial delay lets startup settle, then it repeats every 6h.
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(auto_backup.push_backup_to_github)
        except Exception as e:
            print(f"[Backup Loop] Error: {e}")
        await asyncio.sleep(6 * 3600)  # then every 6 hours


@app.on_event("startup")
async def startup():
    asyncio.create_task(cycle_loop())
    asyncio.create_task(auto_backup_loop())


@app.post("/system/backup-now")
def trigger_backup():
    import auto_backup
    ok = auto_backup.push_backup_to_github()
    return {"ok": ok, "message": "Memory backed up and pushed to GitHub" if ok else "No changes or push failed"}


@app.get("/ai/level")
def get_ai_level():
    import ai_level
    return ai_level.get_ai_level_status()


@app.get("/intermarket/status")
def get_intermarket_status():
    import yahoo_finance
    return yahoo_finance.get_intermarket_status()


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


@app.get("/notifier/status")
def get_notifier_status():
    token, chat_id = notifier._get_config()
    return {
        "configured": bool(token and chat_id),
        "bot_token": f"{token[:8]}..." if token else None,
        "chat_id": chat_id,
    }


@app.post("/notifier/test")
def send_test_notification():
    ok, detail = notifier.send_telegram_sync(
        "🚀 <b>Trading Room AI — Telegram Test Alert</b>\n\n"
        "✅ การเชื่อมต่อระบบแจ้งเตือน Telegram สำเร็จเรียบร้อยแล้ว!\n"
        "พร้อมรับการแจ้งเตือนเปิด/ปิดไม้ออเดอร์เข้ามือถือทันทีครับ 📱💎"
    )
    return {"ok": ok, "detail": detail, "message": "ส่งข้อความทดสอบสำเร็จ!" if ok else f"ส่งข้อความไม่สำเร็จ: {detail}"}


import stoch_swing_engine


@app.get("/stoch-swings/status")
def get_stoch_swings_status(symbol: str = "XAUUSD"):
    sym = symbol.upper()
    candles = None

    # Try MT5 Direct
    if mt5_direct.available():
        snap = mt5_direct.read_snapshot()
        if snap and "candles" in snap and sym in snap["candles"]:
            candles = snap["candles"][sym]

    # Try MT5 EA Bridge
    if not candles:
        snap = mt5_bridge.read_snapshot()
        if snap and "candles" in snap and sym in snap["candles"]:
            candles = snap["candles"][sym]

    # Fallback to DataAgent
    if not candles:
        tick_data = data_agent.tick(sym)
        candles = tick_data.get("candles")

    highs, lows, closes = [], [], []
    if isinstance(candles, dict):
        highs = [float(x) for x in candles.get("h", [])]
        lows = [float(x) for x in candles.get("l", [])]
        closes = [float(x) for x in candles.get("c", [])]
    elif isinstance(candles, list):
        for bar in candles:
            if isinstance(bar, dict):
                highs.append(float(bar.get("high") or bar.get("h") or 0))
                lows.append(float(bar.get("low") or bar.get("l") or 0))
                closes.append(float(bar.get("close") or bar.get("c") or 0))

    min_len = min(len(highs), len(lows), len(closes))
    if min_len >= 15:
        highs = highs[:min_len]
        lows = lows[:min_len]
        closes = closes[:min_len]
    else:
        px = float(data_agent.prices.get(sym) or (1.138 if "USD" in sym and "XAU" not in sym and "BTC" not in sym else 2650.0))
        import math
        closes = [px * (1.0 + 0.0015 * math.sin(i * 0.25)) for i in range(60)]
        highs = [p * 1.0008 for p in closes]
        lows = [p * 0.9992 for p in closes]

    try:
        swings = stoch_swing_engine.detect_stoch_swings(highs, lows, closes)
    except Exception as err:
        swings = {
            "ready": False,
            "reason": f"คำนวณสวิง Stoch (9,3,3) ขัดข้อง ({err})",
            "latest_k": "-",
            "latest_d": "-",
            "uptrend": {"valid": False, "l1_price": None, "h1_price": None, "l2_price": None},
            "downtrend": {"valid": False, "h1_price": None, "l1_price": None, "h2_price": None}
        }
    return {
        "symbol": sym,
        "engine": "Stochastic (9,3,3) + RSI (14) Dual-Side Engine",
        "rules": [
            "OB (>80) & OS (<20) Strict Zone Swings",
            "Uptrend Chain: OS1 (L1) -> OB1 (H1) -> OS2 (L2) with L2 > L1",
            "Downtrend Chain: OB1 (H1) -> OS1 (L1) -> OB2 (H2) with H2 < H1",
            "Major Support anchored at OS1 (L1), Major Resistance at OB1 (H1)",
            "Multi-Bar Engulfing Confirmation (Buy & Sell)",
            "TP1: Previous High (H1) / Low (L1), TP2: Fibo Extension 161.8%",
            "Emergency DCA Layer 2: Fibo 38.2% drop",
            "Emergency Close: OB2 with H2 < H1",
            "Hard Cut Loss: Price breaks OS1 (L3 < L1)",
            "MTF AI Selection: [H1+M5] vs [H4+M15]"
        ],
        "stoch_analysis": swings
    }


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


@app.post("/cost-guard/sync")
def sync_cost_guard(real_thb: float):
    """Align the guard's running total with Google's real invoice figure."""
    import cost_guard
    cost_guard.sync_spent(real_thb)
    return cost_guard.status()


@app.get("/monitor/status")
def get_monitor_status():
    return monitor.status()


@app.get("/swing-manager/status")
def get_swing_manager_status():
    """Active Stoch-swing plans currently managed for DCA / structure close."""
    return {"active_plans": swing_manager.snapshot()}


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


@app.get("/allin/status")
def get_allin_status(symbol: str = "XAUUSD"):
    try:
        import allin_patterns
        import yahoo_finance
        import cot_report

        yf = yahoo_finance.get_intermarket_status()
        cot = cot_report.get_bias(symbol)
        cot_dict = cot if (isinstance(cot, dict) and cot.get("available")) else {"bias": "neutral"}
        
        # Build sample confluence calculation
        smc = {"ready": True, "structure_event": "BOS", "zone_touch": True}
        indicators = {"rsi_state": "oversold", "ema_trend": "bullish", "candlestick_flaws": {"has_flaw": False}}
        mfi = {"state": "oversold", "mfi": 28.5}
        flaws = {"has_flaw": False}

        score_res = allin_patterns.calculate_confluence_score(smc, indicators, mfi, flaws, yf, cot_dict, symbol)
        return {
            "status": "active",
            "symbol": symbol,
            "engine": "ALLIN Confluence 100-Point Engine",
            "scoring": score_res,
            "rules": [
                "Candlestick Flaws Detection (Body Engulf without Wick Cover = Trap)",
                "MFI Money Flow Index Confirmation",
                "RSI 14-Candle Base Accumulation Rule",
                "Gold RSI H4 34.05 Oversold Threshold",
                "Dynamic Asset Buffer Math (XAUUSD=400 points)"
            ]
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


@app.get("/llm/reset-cooldowns")
def reset_llm_cooldowns():
    import llm_circuit_breaker
    llm_circuit_breaker.reset_cooldowns()
    return {"status": "reset", "message": "All LLM circuit breakers reset to active."}


@app.get("/signals/matrix-stats")
@app.get("/signals/matrix")
def get_signal_matrix_stats():
    return signal_log.get_rsi_ema_matrix()


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
