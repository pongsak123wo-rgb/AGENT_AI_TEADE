"""Risk configuration and portfolio-level risk tracking.

Numbers are user-configurable (different brokers/funds have different
rules) — there is no hardcoded prop-firm assumption here.
"""
from __future__ import annotations

import correlation_agent
import signal_log
from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5       # risk ต่อไม้ (% ของ equity)
    max_total_open_risk_pct: float = 3.0  # risk รวมสูงสุดของทุกไม้ที่เปิดพร้อมกัน
    daily_loss_limit_pct: float = 5.0     # ขาดทุนสะสมต่อวันสูงสุด
    max_total_drawdown_pct: float = 10.0  # ขาดทุนสะสมรวมสูงสุดจาก equity สูงสุด


@dataclass
class OpenPosition:
    symbol: str
    side: str
    risk_pct: float
    ticket: int | None = None
    estimated: bool = False


@dataclass
class PortfolioState:
    equity_start_of_day: float
    equity_peak: float
    equity: float
    daily_loss_used_pct: float = 0.0
    total_drawdown_pct: float = 0.0
    open_positions: list[OpenPosition] = field(default_factory=list)

    @property
    def total_open_risk_pct(self) -> float:
        return sum(p.risk_pct for p in self.open_positions)


class RiskManager:
    def __init__(self, config: RiskConfig, start_equity: float = 100_000.0):
        self.config = config
        self.state = PortfolioState(
            equity_start_of_day=start_equity,
            equity_peak=start_equity,
            equity=start_equity,
        )
        # ticket -> the risk_pct WE actually approved for that trade.
        # Populated the moment our own order fills (main.py calls
        # record_ticket_risk with the real MT5 ticket). Lets
        # sync_positions_from_mt5 tell apart "a trade we sized correctly"
        # from "a position we have no record of" (opened before this
        # process started, or manually) without guessing.
        self.ticket_risk_pct: dict[int, float] = {}

    def record_ticket_risk(self, ticket: int, risk_pct: float):
        self.ticket_risk_pct[ticket] = risk_pct

    def sync_positions_from_mt5(self, mt5_positions: list[dict]):
        """Rebuilds open_positions from the REAL position list MT5 just
        reported — the single source of truth — instead of trusting only
        our own in-memory open_position()/close_position() bookkeeping.

        Without this, a backend restart (or any position opened outside
        this exact process lifetime) becomes permanently invisible to the
        risk budget and the correlation veto, even though it's a real
        live position still affecting the account.
        """
        live_tickets = {p.get("ticket") for p in mt5_positions}
        self.ticket_risk_pct = {t: r for t, r in self.ticket_risk_pct.items() if t in live_tickets}

        new_positions = []
        for p in mt5_positions:
            ticket = p.get("ticket")
            known_risk_pct = self.ticket_risk_pct.get(ticket)
            new_positions.append(
                OpenPosition(
                    symbol=p["symbol"],
                    side=p["type"],
                    risk_pct=known_risk_pct if known_risk_pct is not None else self.config.risk_per_trade_pct,
                    ticket=ticket,
                    estimated=known_risk_pct is None,
                )
            )
        self.state.open_positions = new_positions

    def update_config(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.config, k) and v is not None:
                setattr(self.config, k, float(v))

    def sync_from_account(self, account: dict):
        """Pull real equity from the MT5 snapshot and recompute loss/drawdown."""
        equity = account["equity"]
        st = self.state
        st.equity = equity
        st.equity_peak = max(st.equity_peak, equity)
        st.daily_loss_used_pct = max(0.0, (st.equity_start_of_day - equity) / st.equity_start_of_day * 100)
        st.total_drawdown_pct = max(0.0, (st.equity_peak - equity) / st.equity_peak * 100)

    def evaluate(self, symbol: str, bias: str, spread: float | None = None, atr: float | None = None, mtf_confluence: str | None = None, ema_trend: str | None = None, rsi_state: str | None = None) -> dict:
        if bias == "none":
            return {"approved": False, "lot": 0.0, "reason": "ไม่มี setup ให้ประเมิน risk"}

        cfg = self.config
        st = self.state

        # RSI exhaustion gate — ห้ามขายเมื่อ RSI oversold (ตลาดอาจเด้ง),
        # ห้ามซื้อเมื่อ RSI overbought (ตลาดอาจร่วง)
        # ข้อมูลจริงพิสูจน์แล้ว: sell/oversold/down = 25% win rate (20 ไม้),
        # buy/overbought/up เคยเป็นปัญหาเดียวกัน — RSI exhaustion เป็น
        # สัญญาณห้ามไม่ใช่ confirmation
        if rsi_state == "oversold" and bias == "sell":
            return {
                "approved": False,
                "lot": 0.0,
                "reason": f"RSI gate — {symbol} RSI oversold ขณะ bias=sell ตลาดอาจเด้ง ไม่เข้าไม้",
            }
        if rsi_state == "overbought" and bias == "buy":
            return {
                "approved": False,
                "lot": 0.0,
                "reason": f"RSI gate — {symbol} RSI overbought ขณะ bias=buy ตลาดอาจร่วง ไม่เข้าไม้",
            }

        # Trend filter — only trade in the direction EMA confirms.
        # buy ต้องการ ema_trend="up", sell ต้องการ ema_trend="down"
        # ยกเว้น: ถ้า RSI overbought+sell หรือ oversold+buy = reversal setup ที่ข้อมูลยืนยันว่าใช้ได้
        if ema_trend is not None and ema_trend != "neutral":
            is_rsi_reversal = (rsi_state == "overbought" and bias == "sell") or (rsi_state == "oversold" and bias == "buy")
            trend_ok = (bias == "buy" and ema_trend == "up") or (bias == "sell" and ema_trend == "down")
            if not trend_ok and not is_rsi_reversal:
                return {
                    "approved": False,
                    "lot": 0.0,
                    "reason": f"กรองเทรน — {symbol} bias={bias} แต่ EMA trend={ema_trend} สวนทางกัน ไม่เข้าไม้",
                }

        # No-double check — ห้ามเบิ้ลไม้ในทิศทางเดิมของ symbol เดิม
        for pos in st.open_positions:
            if pos.symbol == symbol and pos.side == bias:
                return {
                    "approved": False,
                    "lot": 0.0,
                    "reason": f"ห้ามเบิ้ลไม้ — {symbol} มี {bias} เปิดอยู่แล้ว ticket #{pos.ticket}",
                }

        # Correlation veto — refuse to stack a directionally-equivalent bet
        # across correlated symbols (e.g. EURUSD buy + GBPUSD buy), even if
        # each individually passes the per-symbol risk checks below.
        corr = correlation_agent.check_correlation_risk(symbol, bias, st.open_positions)
        if corr["blocked"]:
            return {"approved": False, "lot": 0.0, "reason": corr["reason"]}

        # Spread gating — same idea as the EA's "block NEW entries if
        # spread > threshold": if the spread itself eats too much of the
        # ATR (the move you're trying to catch), the trade is paying away
        # its edge before it even starts.
        MAX_SPREAD_TO_ATR_RATIO = 0.5
        if spread is not None and atr is not None and atr > 0:
            spread_atr_ratio = spread / atr
            if spread_atr_ratio > MAX_SPREAD_TO_ATR_RATIO:
                return {
                    "approved": False,
                    "lot": 0.0,
                    "reason": (
                        f"Spread กว้างเกินไป ({spread} = {spread_atr_ratio:.0%} ของ ATR {atr}) "
                        f"— เลี่ยงเข้าไม้ตอนสเปรดกว้าง ({symbol})"
                    ),
                }

        if st.daily_loss_used_pct >= cfg.daily_loss_limit_pct:
            return {
                "approved": False,
                "lot": 0.0,
                "reason": f"Daily loss แตะ {st.daily_loss_used_pct:.2f}% ของ limit {cfg.daily_loss_limit_pct}% — ปฏิเสธทุก signal วันนี้",
            }

        if st.total_drawdown_pct >= cfg.max_total_drawdown_pct:
            return {
                "approved": False,
                "lot": 0.0,
                "reason": f"Total drawdown แตะ {st.total_drawdown_pct:.2f}% ของ limit {cfg.max_total_drawdown_pct}% — หยุดเทรดทั้งหมด",
            }

        projected_total_risk = st.total_open_risk_pct + cfg.risk_per_trade_pct
        if projected_total_risk > cfg.max_total_open_risk_pct:
            return {
                "approved": False,
                "lot": 0.0,
                "reason": (
                    f"Total open risk ตอนนี้ {st.total_open_risk_pct:.2f}% + ไม้ใหม่ {cfg.risk_per_trade_pct}% "
                    f"จะเกิน limit รวม {cfg.max_total_open_risk_pct}% — ปฏิเสธไม้นี้ ({symbol})"
                ),
            }

        # --- Dynamic Risk Sizing (Self-Learning) ---
        # Fetch the last 10 closed trades to calculate recent win rate
        try:
            recent_trades = [t for t in signal_log.recent(20) if t["status"] in ("win", "loss")][:10]
        except Exception:
            recent_trades = []
            
        recent_win_rate = 50.0
        risk_multiplier = 1.0
        
        if len(recent_trades) >= 5:
            wins = sum(1 for t in recent_trades if t["status"] == "win")
            recent_win_rate = (wins / len(recent_trades)) * 100
            
            # Anti-Martingale / Streak Learning Logic
            if recent_win_rate < 40.0:
                risk_multiplier = 0.5  # Cut risk in half during losing streaks
            elif recent_win_rate > 60.0:
                risk_multiplier = 1.2  # Slightly increase risk during winning streaks
                
        # --- Multi-Timeframe Sizing ---
        # "TF เล็กเข้าน้อย ใหญ่เข้าล็อตใหญ่": when the fast (small-TF-like)
        # structure and the macro/swing (big-TF-like) structure both agree
        # with this bias, conviction is genuinely higher — size up. When
        # only the fast structure fired without the bigger picture
        # confirming, that's the weaker "small TF only" case — size down.
        mtf_multiplier = 1.0
        mtf_label = "ไม่มีข้อมูล multi-timeframe"
        if mtf_confluence == "full":
            mtf_multiplier = 1.4
            mtf_label = "TF เล็ก+ใหญ่ยืนยันตรงกัน (full confluence) → ไม้ใหญ่ขึ้น"
        elif mtf_confluence == "fast_only":
            mtf_multiplier = 0.5
            mtf_label = "มีแค่ TF เล็กยืนยัน ภาพใหญ่ยังไม่ตาม → ไม้เล็กลง"
        elif mtf_confluence == "swing_only":
            mtf_multiplier = 0.8
            mtf_label = "ภาพใหญ่เอียงทางนี้แต่ TF เล็กยังไม่ break ตาม → ไม้ขนาดกลาง (ดักล่วงหน้า)"
        elif mtf_confluence == "none":
            mtf_multiplier = 0.7
            mtf_label = "ทั้งสอง TF ไม่ยืนยันทิศทางนี้ชัดเจน → ลดขนาดไม้ไว้ก่อน"

        actual_risk_pct = round(cfg.risk_per_trade_pct * risk_multiplier * mtf_multiplier, 2)
        # Ensure it never exceeds limits
        if st.total_open_risk_pct + actual_risk_pct > cfg.max_total_open_risk_pct:
             actual_risk_pct = cfg.max_total_open_risk_pct - st.total_open_risk_pct

        return {
            "approved": True,
            "lot": actual_risk_pct,
            "reason": (
                f"Risk ต่อไม้ปรับเป็น {actual_risk_pct}% (Base={cfg.risk_per_trade_pct}%, Recent Win Rate={recent_win_rate:.1f}%, {mtf_label}) "
                f"· Total open risk จะเป็น {st.total_open_risk_pct + actual_risk_pct:.2f}%/{cfg.max_total_open_risk_pct}% "
                f"· Daily loss {st.daily_loss_used_pct:.2f}%/{cfg.daily_loss_limit_pct}%"
            ),
        }

    def open_position(self, symbol: str, side: str, risk_pct: float | None = None):
        """`risk_pct` should be the actual approved risk for this trade
        (the `lot` field `evaluate()` returned) — not the static config
        value, since dynamic sizing can scale it down/up from that base.
        """
        self.state.open_positions.append(
            OpenPosition(symbol=symbol, side=side, risk_pct=risk_pct if risk_pct is not None else self.config.risk_per_trade_pct)
        )

    def close_position(self, symbol: str, side: str) -> bool:
        """Removes one matching open position so total_open_risk_pct
        reflects reality once a signal settles (win/loss) — without
        this, the risk budget only ever grows and eventually blocks all
        new trades even though the real MT5 positions closed long ago.
        """
        for i, p in enumerate(self.state.open_positions):
            if p.symbol == symbol and p.side == side:
                del self.state.open_positions[i]
                return True
        return False

    def snapshot(self) -> dict:
        return {
            "config": self.config.__dict__,
            "total_open_risk_pct": self.state.total_open_risk_pct,
            "daily_loss_used_pct": self.state.daily_loss_used_pct,
            "total_drawdown_pct": self.state.total_drawdown_pct,
            "open_positions": [p.__dict__ for p in self.state.open_positions],
        }
