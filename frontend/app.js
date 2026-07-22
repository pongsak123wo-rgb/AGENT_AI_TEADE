const feed = document.getElementById("feed");
const decisionPanel = document.getElementById("decision-panel");
const history = document.getElementById("history");
const API = `http://${location.hostname || "localhost"}:8000`;
const WS_BASE = `ws://${location.hostname || "localhost"}:8000`;

// --- Auth: attach the stored token to every backend request ---
const AUTH_KEY = "trading_auth_token";
let authToken = localStorage.getItem(AUTH_KEY) || "";
const _origFetch = window.fetch.bind(window);
window.fetch = (url, opts = {}) => {
  if (typeof url === "string" && url.startsWith(API)) {
    opts.headers = { ...(opts.headers || {}), "X-Auth-Token": authToken };
  }
  return _origFetch(url, opts);
};

function showLogin(msg) {
  const ov = document.getElementById("login-overlay");
  if (ov) {
    ov.style.display = "flex";
    if (msg) document.getElementById("login-msg").textContent = msg;
  }
}
async function initAuth() {
  try {
    const res = await fetch(`${API}/auth/check`);
    if (res.status === 401) { showLogin("ต้องใส่รหัสผ่านเพื่อเข้าใช้งาน"); return false; }
    return true;
  } catch (e) { return true; } // backend down — let normal error handling show
}
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("login-btn");
  const inp = document.getElementById("login-input");
  if (btn && inp) {
    const submit = async () => {
      authToken = inp.value;
      const res = await fetch(`${API}/auth/check`).catch(() => null);
      if (res && res.ok) {
        localStorage.setItem(AUTH_KEY, authToken);
        location.reload();
      } else {
        document.getElementById("login-msg").textContent = "รหัสผ่านไม่ถูกต้อง ลองใหม่";
      }
    };
    btn.addEventListener("click", submit);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  }
  initAuth();
});

// --- Tab navigation ---
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});

const AVATARS = {
  technical: { bg: "#3a7bd5", color: "#f3e6cf", icon: "T" },
  risk: { bg: "#cf4444", color: "#f3e6cf", icon: "R" },
  ceo: { bg: "#7a5cc4", color: "#f3e6cf", icon: "C" },
};

const NAMES = {
  technical: "Technical Analysis",
  risk: "Risk Management",
  ceo: "CEO Agent",
};

function addChatMessage(msg, time) {
  const av = AVATARS[msg.agent] || { bg: "#333", icon: "?" };
  const row = document.createElement("div");
  row.className = "msg";
  row.innerHTML = `
    <div class="avatar" style="background:${av.bg}; color:${av.color}">${av.icon}</div>
    <div>
      <p class="who">${NAMES[msg.agent] || msg.agent} <span style="color:#666">${time}</span></p>
      <div class="bubble">${msg.text}</div>
    </div>
  `;
  feed.appendChild(row);
  feed.scrollTop = feed.scrollHeight;
}

function decisionCardHTML(decision) {
  const sideClass = decision.action === "buy" ? "buy" : "sell";
  return `
    <div class="decision-card ${sideClass}">
      <p class="side">${decision.action.toUpperCase()}</p>
      <p class="symbol">${decision.symbol}</p>
      <table>
        <tr><td>Entry</td><td>${decision.entry}</td></tr>
        <tr><td>SL</td><td>${decision.sl}</td></tr>
        <tr><td>TP</td><td>${decision.tp}</td></tr>
        <tr><td>Risk</td><td>${decision.risk_pct}%</td></tr>
      </table>
    </div>`;
}

function renderDecisionCard(decision, time, isHistory = false) {
  if (decision.action === "no_trade") {
    decisionPanel.innerHTML = `
      <div class="decision-card no_trade">
        <p class="side">NO TRADE</p>
        <p class="symbol" style="font-size:13px; color:#999; font-weight:400;">ยังไม่มี signal ที่ผ่านเกณฑ์ครบทุกฝ่าย</p>
      </div>`;
    return;
  }

  decisionPanel.innerHTML = decisionCardHTML(decision);

  const sideClass = decision.action === "buy" ? "buy" : "sell";
  const item = document.createElement("div");
  item.className = "history-item";
  item.innerHTML = `
    <span class="side-${sideClass}">${decision.action.toUpperCase()} ${decision.symbol}</span>
    <span>${time}</span>
  `;
  history.prepend(item);

  if (!isHistory) {
    showOrderModal(decision);
  }
}

// --- Order confirmation modal — pops up front-and-center on every real trade decision ---
const orderModal = document.getElementById("order-modal");
const modalCard = document.getElementById("modal-card");

function showOrderModal(decision) {
  modalCard.innerHTML = decisionCardHTML(decision);
  orderModal.classList.add("show");
}

function hideOrderModal() {
  orderModal.classList.remove("show");
}

document.getElementById("modal-approve").addEventListener("click", hideOrderModal);

function renderIndicatorsPanel(symbol, indicators) {
  const panel = document.getElementById("indicators-panel");
  if (!panel || !indicators) return;

  const sourceTag = indicators.ohlc_source === "real"
    ? '<span style="color:var(--green)">REAL candle</span>'
    : '<span style="color:var(--red)">synthetic</span>';

  const rows = [
    ["สินทรัพย์", symbol],
    ["แหล่งข้อมูล", sourceTag],
    ["RSI", `${indicators.rsi} (${indicators.rsi_state})`],
    ["EMA trend", indicators.ema_trend],
    ["Trend confluence", indicators.trend_confluence === null ? "-" : (indicators.trend_confluence ? "ตรงกัน" : "ขัดกัน")],
    ["MACD cross", indicators.macd_cross ?? "-"],
    ["Bollinger", indicators.bb_position],
    ["ATR", indicators.atr],
    ["Pin bar", indicators.pin_bar],
    ["Engulfing", indicators.engulfing],
  ];

  panel.innerHTML = rows
    .map(
      ([label, value]) => `
        <div class="meter-row"><span>${label}</span><span>${value}</span></div>`
    )
    .join("");
}

function addMessage(msg, isHistory = false) {
  const time = new Date().toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" });
  addChatMessage(msg, time);
  if (!isHistory) {
    showSpeech(msg.agent, msg.text, msg.kind);
  }
  if (msg.kind === "decision") {
    renderDecisionCard(msg.data, time, isHistory);
  }
  if (msg.agent === "technical" && msg.data && msg.data.indicators) {
    renderIndicatorsPanel(msg.data.indicators.symbol || "-", msg.data.indicators);
  }
}

async function loadRecentFeed() {
  try {
    const res = await fetch(`${API}/feed/recent`);
    const messages = await res.json();
    messages.forEach((m) => addMessage(m, true));
  } catch (e) {
    // backend not reachable yet — live messages will fill in once connected
  }
}

loadRecentFeed();

// --- Walking office characters (Pokémon-style canvas scene in office.js) ---
// The office scene handles its own animation loop; app.js just tells it
// which agent is "speaking" so that character walks over to the CEO and
// shows a bubble. Only technical/risk/ceo have avatars in the scene.
function showSpeech(agent, text, kind) {
  if (window.OfficeScene && ["technical", "risk", "ceo"].includes(agent)) {
    // Walk to the CEO only on a real action — a zone was hit (🎯), a
    // decision/order (📋 / kind=decision), or the CEO speaking. Routine
    // "👁 เฝ้าโซน" watch reports stay at the desk (walk=false).
    const isAction = kind === "decision" || agent === "ceo" || /🎯|📋|MT5|ออเดอร์|ticket/.test(text);
    window.OfficeScene.speak(agent, text, isAction);
  }
}

function connect() {
  const url = `${WS_BASE}/ws${authToken ? "?token=" + encodeURIComponent(authToken) : ""}`;
  const ws = new WebSocket(url);
  ws.onmessage = (event) => addMessage(JSON.parse(event.data));
  ws.onclose = (e) => {
    // 1008 = server rejected the token → prompt for login instead of looping
    if (e.code === 1008) { showLogin("รหัสผ่านไม่ถูกต้องหรือหมดอายุ"); return; }
    setTimeout(connect, 2000);
  };
}

connect();

const riskInputs = {
  risk_per_trade_pct: document.getElementById("risk-per-trade"),
  max_total_open_risk_pct: document.getElementById("risk-total-open"),
  daily_loss_limit_pct: document.getElementById("risk-daily"),
  max_total_drawdown_pct: document.getElementById("risk-drawdown"),
};

function renderRiskSnapshot(snap) {
  // Skip whichever field the user currently has focused/is typing into —
  // otherwise the 5s poll below overwrites their in-progress edit before
  // they can finish typing, making the field look "stuck"/uneditable.
  if (document.activeElement !== riskInputs.risk_per_trade_pct) {
    riskInputs.risk_per_trade_pct.value = snap.config.risk_per_trade_pct;
  }
  if (document.activeElement !== riskInputs.max_total_open_risk_pct) {
    riskInputs.max_total_open_risk_pct.value = snap.config.max_total_open_risk_pct;
  }
  if (document.activeElement !== riskInputs.daily_loss_limit_pct) {
    riskInputs.daily_loss_limit_pct.value = snap.config.daily_loss_limit_pct;
  }
  if (document.activeElement !== riskInputs.max_total_drawdown_pct) {
    riskInputs.max_total_drawdown_pct.value = snap.config.max_total_drawdown_pct;
  }

  const openPct = snap.total_open_risk_pct;
  const openLimit = snap.config.max_total_open_risk_pct;
  document.getElementById("meter-open").textContent = `${openPct.toFixed(2)}% / ${openLimit}%`;
  document.getElementById("meter-open-bar").style.width = `${Math.min(100, (openPct / openLimit) * 100)}%`;

  const dailyPct = snap.daily_loss_used_pct;
  const dailyLimit = snap.config.daily_loss_limit_pct;
  document.getElementById("meter-daily").textContent = `${dailyPct.toFixed(2)}% / ${dailyLimit}%`;
  document.getElementById("meter-daily-bar").style.width = `${Math.min(100, (dailyPct / dailyLimit) * 100)}%`;
}

async function loadRisk() {
  const res = await fetch(`${API}/risk`);
  renderRiskSnapshot(await res.json());
}

document.getElementById("risk-save").addEventListener("click", async () => {
  const body = {};
  for (const [key, el] of Object.entries(riskInputs)) {
    body[key] = parseFloat(el.value);
  }
  const res = await fetch(`${API}/risk`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  renderRiskSnapshot(await res.json());
});

loadRisk();
setInterval(loadRisk, 5000);

// --- Kill switch + health ---
async function loadKillSwitch() {
  const panel = document.getElementById("kill-switch-panel");
  const btn = document.getElementById("kill-switch-toggle");
  try {
    const [ksRes, healthRes] = await Promise.all([fetch(`${API}/kill-switch`), fetch(`${API}/health`)]);
    const ks = await ksRes.json();
    const health = await healthRes.json();

    btn.textContent = ks.enabled ? "ปิด Auto-Trade (Kill Switch)" : "เปิด Auto-Trade กลับมา";
    btn.dataset.enabled = ks.enabled;

    const rows = [
      ["Auto-Trade", ks.enabled ? '<span style="color:var(--green)">เปิดอยู่</span>' : '<span style="color:var(--red)">ปิดอยู่</span>'],
      ["เหตุผลที่ตัด", ks.tripped_reason || "-"],
      ["MT5 Snapshot", health.mt5_snapshot.detail],
      ["บัญชี Demo", health.account_mode.is_demo ? "ใช่" : "ไม่ใช่ / ไม่ทราบ"],
    ];
    panel.innerHTML = rows.map(([label, value]) => `<div class="meter-row"><span>${label}</span><span>${value}</span></div>`).join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

document.getElementById("kill-switch-toggle").addEventListener("click", async () => {
  const btn = document.getElementById("kill-switch-toggle");
  const enabled = btn.dataset.enabled === "true";
  const endpoint = enabled ? "disable" : "enable";
  await fetch(`${API}/kill-switch/${endpoint}`, { method: "POST" });
  loadKillSwitch();
});

loadKillSwitch();
setInterval(loadKillSwitch, 5000);


// --- Account summary + open positions (real MT5 data) ---
function fmt(n) {
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderAccount(data) {
  const liveEl = document.getElementById("m-live");
  if (!data.live || !data.account) {
    document.getElementById("m-equity").textContent = "--";
    document.getElementById("m-balance").textContent = "--";
    document.getElementById("m-profit").textContent = "--";
    document.getElementById("m-positions-count").textContent = "--";
    liveEl.textContent = "MOCK";
    liveEl.className = "metric-value negative";
    document.getElementById("positions-list").innerHTML =
      '<p class="placeholder">ไม่มีไม้เปิดอยู่ หรือยังไม่เชื่อมต่อ MT5</p>';
    return;
  }

  const acc = data.account;
  document.getElementById("m-equity").textContent = fmt(acc.equity);
  document.getElementById("m-balance").textContent = fmt(acc.balance);

  const profitEl = document.getElementById("m-profit");
  profitEl.textContent = fmt(acc.profit);
  profitEl.className = "metric-value " + (acc.profit >= 0 ? "positive" : "negative");

  document.getElementById("m-positions-count").textContent = data.positions.length;
  liveEl.textContent = "LIVE";
  liveEl.className = "metric-value positive";

  const list = document.getElementById("positions-list");
  if (data.positions.length === 0) {
    list.innerHTML = '<p class="placeholder">ไม่มีไม้เปิดอยู่ตอนนี้</p>';
    return;
  }
  list.innerHTML = data.positions
    .map((p) => {
      const profitClass = p.profit >= 0 ? "profit-positive" : "profit-negative";
      return `
        <div class="position-row">
          <span>${p.symbol}</span>
          <span class="type-${p.type}">${p.type.toUpperCase()}</span>
          <span>${p.volume} lot</span>
          <span class="${profitClass}">${fmt(p.profit)}</span>
        </div>`;
    })
    .join("");
}

async function loadAccount() {
  try {
    const res = await fetch(`${API}/account`);
    renderAccount(await res.json());
  } catch (e) {
    renderAccount({ live: false, account: null, positions: [] });
  }
}

loadAccount();
setInterval(loadAccount, 3000);

// --- Win rate (from the system's own signal log) ---
async function loadWinrate() {
  try {
    const res = await fetch(`${API}/signals/stats`);
    const stats = await res.json();
    const valueEl = document.getElementById("winrate-value");
    const detailEl = document.getElementById("winrate-detail");

    const beText = stats.breakeven ? ` · breakeven ${stats.breakeven} (ไม่นับชนะ/แพ้)` : "";
    if (stats.win_rate_pct === null) {
      valueEl.textContent = "--";
      valueEl.className = "winrate-big";
      detailEl.textContent = stats.breakeven
        ? `ยังไม่มีไม้ชนะ/แพ้จริง · breakeven ${stats.breakeven} · เปิดอยู่ ${stats.open} (นับเฉพาะไม้ที่เข้า MT5 จริง)`
        : `ยังไม่มีไม้จริงที่ปิดผล (เปิดอยู่ ${stats.open})`;
    } else {
      valueEl.textContent = `${stats.win_rate_pct}%`;
      valueEl.className = "winrate-big " + (stats.win_rate_pct >= 50 ? "positive" : "negative");
      detailEl.textContent = `ชนะ ${stats.win} / แพ้ ${stats.loss} (ปิดแล้ว ${stats.closed}) · เปิดอยู่ ${stats.open}${beText}`;
    }

    const dirEl = document.getElementById("direction-stats");
    if (dirEl) {
      dirEl.innerHTML = `
        <div style="background:#1a4a2e; border:2px solid #3aa65a; padding:6px 12px; text-align:center;">
          <div style="color:#3aa65a; font-size:10px;">BUY</div>
          <div style="font-size:16px; color:#f3e6cf;">${stats.buy_count ?? 0}</div>
        </div>
        <div style="background:#4a1a1a; border:2px solid #cf4444; padding:6px 12px; text-align:center;">
          <div style="color:#cf4444; font-size:10px;">SELL</div>
          <div style="font-size:16px; color:#f3e6cf;">${stats.sell_count ?? 0}</div>
        </div>
        <div style="background:#2a2a3a; border:2px solid #7a5cc4; padding:6px 12px; text-align:center;">
          <div style="color:#7a5cc4; font-size:10px;">TOTAL</div>
          <div style="font-size:16px; color:#f3e6cf;">${stats.total ?? 0}</div>
        </div>`;
    }

    // daily stats
    const dailyPanel = document.getElementById("daily-stats-panel");
    if (dailyPanel && stats.daily && stats.daily.length > 0) {
      dailyPanel.innerHTML = stats.daily.slice(0, 7).map(d => `
        <div class="meter-row">
          <span>${d.date}</span>
          <span>${d.total} ไม้ &nbsp;
            <span style="color:#3aa65a">B:${d.buy}</span> /
            <span style="color:#cf4444">S:${d.sell}</span>
          </span>
        </div>`).join("");
    }
  } catch (e) {
    // backend not reachable — leave last known value
  }
}

loadWinrate();
setInterval(loadWinrate, 5000);

// --- Expectancy / R-multiple — win rate alone can't show whether the
// average win is even bigger than the average loss. ---
async function loadExpectancy() {
  const panel = document.getElementById("expectancy-panel");
  try {
    const res = await fetch(`${API}/signals/expectancy`);
    const data = await res.json();
    if (data.samples === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มีไม้จริงที่ปิดผล</p>';
      return;
    }
    const expClass = data.expectancy_r >= 0 ? "positive" : "negative";
    const avgWin = data.avg_win_r != null ? `+${data.avg_win_r}R (${data.win_count} ไม้)` : "— (ยังไม่มีไม้ชนะ)";
    const avgLoss = data.avg_loss_r != null ? `${data.avg_loss_r}R (${data.loss_count} ไม้)` : "— (ยังไม่มีไม้แพ้)";
    panel.innerHTML = `
      <div class="meter-row"><span>Expectancy</span><span class="${expClass}">${data.expectancy_r}R / ไม้</span></div>
      <div class="meter-row"><span>กำไรเฉลี่ยไม้ชนะ</span><span>${avgWin}</span></div>
      <div class="meter-row"><span>ขาดทุนเฉลี่ยไม้แพ้</span><span>${avgLoss}</span></div>
      <div class="meter-row"><span>ตัวอย่างที่ใช้คำนวณ</span><span>${data.samples}${data.excluded_outliers ? ` (ตัดข้อมูลผิดปกติ ${data.excluded_outliers})` : ""}</span></div>
    `;
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

loadExpectancy();
setInterval(loadExpectancy, 10000);

// --- Self-learning panels: provider accuracy, real trading costs, ML status ---
async function loadProviderAccuracy() {
  const panel = document.getElementById("provider-accuracy-panel");
  try {
    const res = await fetch(`${API}/signals/provider-accuracy`);
    const scores = await res.json();
    const entries = Object.entries(scores);
    if (entries.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มี signal ที่ปิดผล</p>';
      return;
    }
    panel.innerHTML = entries
      .map(([provider, score]) => {
        const pct = (score * 100).toFixed(1);
        return `
          <div class="meter-row"><span>${provider}</span><span>${pct}%</span></div>
          <div class="meter-bar"><div class="meter-fill" style="width:${pct}%"></div></div>`;
      })
      .join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

async function loadCostStats() {
  const panel = document.getElementById("cost-stats-panel");
  try {
    const res = await fetch(`${API}/signals/costs`);
    const data = await res.json();
    if (!data.results || data.results.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มีไม้ที่ execute สำเร็จ</p>';
      return;
    }
    panel.innerHTML = data.results
      .map(
        (r) => `
          <div class="position-row" style="grid-template-columns:1fr 1fr 1fr;">
            <span>${r.symbol}</span>
            <span>slip ${r.avg_slippage}</span>
            <span>คอม ${r.avg_commission}</span>
          </div>`
      )
      .join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

async function loadMlStatus() {
  const panel = document.getElementById("ml-status-panel");
  try {
    const res = await fetch(`${API}/ml/status`);
    const data = await res.json();
    if (data.status === "trained") {
      const cvText = data.cv_accuracy !== null && data.cv_accuracy !== undefined
        ? `CV accuracy ${data.cv_accuracy}% (${data.cv_folds}-fold, ของจริง)`
        : `CV ทำไม่ได้ (ข้อมูลน้อย/ไม่สมดุล)`;
      panel.innerHTML = `<p style="margin:0; font-size:9px;">เทรนแล้วจาก ${data.samples} signal · ${cvText} · train accuracy ${data.train_accuracy}% (อย่าเชื่อตัวนี้ มัก overfit)</p>`;
    } else {
      panel.innerHTML = `<p class="placeholder">ยังเทรนไม่ได้ (${data.status}${data.samples !== undefined ? ", มี " + data.samples + " ไม้" : ""})</p>`;
    }
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

async function loadResearchLog() {
  const panel = document.getElementById("research-log-panel");
  try {
    const res = await fetch(`${API}/research/log`);
    const rows = await res.json();
    if (!rows || rows.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่เคยค้นคว้าเอง</p>';
      return;
    }
    panel.innerHTML = rows
      .map((r) => {
        const time = new Date(r.created_at * 1000).toLocaleString("th-TH", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" });
        const wr = r.win_rate_at_time !== null ? `win rate ขณะนั้น ${r.win_rate_at_time}%` : "จากไม้ที่แพ้จริง";
        const chunks = r.chunks_ingested > 0
          ? `<span style="color:var(--green)">+${r.chunks_ingested} chunks</span>`
          : `<span style="color:#888">ไม่พบผลค้นหา</span>`;
        return `
          <div style="border-bottom:1px solid #2a2a2a; padding:6px 0;">
            <div style="font-size:10px; color:#999;">${time} · ${wr}</div>
            <div style="font-size:11px;"><b>${r.topic_key}</b></div>
            <div style="font-size:10px; color:#bbb;">${r.reason}</div>
            <div style="font-size:10px;">${chunks}</div>
          </div>`;
      })
      .join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

loadProviderAccuracy();
loadCostStats();
loadMlStatus();
loadResearchLog();
setInterval(loadProviderAccuracy, 10000);
setInterval(loadCostStats, 10000);
setInterval(loadMlStatus, 15000);
setInterval(loadResearchLog, 20000);

// --- Backtest tab ---
async function loadBacktestSummary() {
  const panel = document.getElementById("backtest-summary-panel");
  try {
    const res = await fetch(`${API}/backtest/summary`);
    const data = await res.json();
    const entries = Object.entries(data);
    if (entries.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล — กดรัน Backtest ก่อน</p>';
      return;
    }
    panel.innerHTML = entries
      .map(([symbol, s]) => {
        const total = s.win + s.loss;
        const wr = total > 0 ? ((s.win / total) * 100).toFixed(1) : "-";
        const wrClass = total > 0 && s.win / total >= 0.5 ? "positive" : "negative";
        return `
          <div class="meter-row"><span>${symbol}</span><span class="${wrClass}">${wr}% (W${s.win}/L${s.loss}, no_hit ${s.no_hit})</span></div>`;
      })
      .join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

async function loadBacktestStructurePatterns() {
  const panel = document.getElementById("backtest-structure-panel");
  try {
    const res = await fetch(`${API}/backtest/structure-patterns`);
    const rows = await res.json();
    if (!rows || rows.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>';
      return;
    }
    panel.innerHTML = rows
      .map((r) => {
        const wrClass = r.win_rate_pct >= 50 ? "positive" : "negative";
        return `
          <div class="meter-row">
            <span>structure=${r.structure_event}, mtf=${r.mtf_confluence}</span>
            <span class="${wrClass}">${r.win_rate_pct}% (${r.samples} ไม้)</span>
          </div>`;
      })
      .join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

document.getElementById("backtest-run-btn").addEventListener("click", async () => {
  const statusEl = document.getElementById("backtest-status");
  const period = document.getElementById("backtest-period").value || "90d";
  const requireMtf = document.getElementById("backtest-mtf-filter").checked;
  const source = document.getElementById("backtest-source").value;
  statusEl.textContent = "กำลังรัน backtest ทุกสินทรัพย์ (อาจใช้เวลาสักครู่)...";
  try {
    const res = await fetch(
      `${API}/backtest/run-batch?period=${encodeURIComponent(period)}&interval=1h${requireMtf ? "&require_mtf_confluence=true" : ""}&source=${source}`,
      { method: "POST" }
    );
    const data = await res.json();
    const errors = Object.entries(data).filter(([, r]) => r.error);
    if (errors.length > 0) {
      statusEl.textContent = `รันเสร็จแต่มีปัญหา: ${errors[0][1].error}`;
    } else {
      statusEl.textContent = `รันเสร็จแล้ว (${period}, source: ${source}, mtf filter: ${requireMtf ? "เปิด" : "ปิด"})`;
    }
    loadBacktestSummary();
    loadBacktestStructurePatterns();
  } catch (e) {
    statusEl.textContent = "รันไม่สำเร็จ — เชื่อมต่อ backend ไม่ได้";
  }
});

async function loadMt5HistoryStatus() {
  const panel = document.getElementById("mt5-history-status-panel");
  try {
    const res = await fetch(`${API}/backtest/mt5-history-status`);
    const data = await res.json();
    if (!data.available) {
      panel.innerHTML = `<p class="placeholder">${data.reason}</p>`;
      return;
    }
    const rows = Object.entries(data.symbols).map(
      ([sym, d]) => `<div class="meter-row"><span>${sym}</span><span>H1: ${d.h1_bars} bars, M1: ${d.m1_bars} bars</span></div>`
    );
    panel.innerHTML = `<p style="font-size:10px; margin:0 0 6px;">Export ล่าสุด: ${data.exported_at}</p>` + rows.join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

loadMt5HistoryStatus();
setInterval(loadMt5HistoryStatus, 15000);

loadBacktestSummary();
loadBacktestStructurePatterns();

// --- วิเคราะห์ไม้ tab ---
function wrColor(pct) {
  if (pct === null) return "#888";
  return pct >= 50 ? "var(--green)" : "var(--red)";
}

async function loadDirectionStats() {
  const summaryPanel = document.getElementById("dir-summary-panel");
  const symbolPanel  = document.getElementById("dir-symbol-panel");
  const dailyPanel   = document.getElementById("dir-daily-panel");
  if (!summaryPanel) return;
  try {
    const res  = await fetch(`${API}/signals/direction-stats`);
    const data = await res.json();

    // --- ภาพรวม Buy vs Sell ---
    summaryPanel.innerHTML = data.direction.map(d => {
      const wr   = d.win_rate_pct !== null ? `${d.win_rate_pct}%` : "--";
      const col  = wrColor(d.win_rate_pct);
      const side = d.action === "buy" ? "BUY" : "SELL";
      const bg   = d.action === "buy" ? "#1a3a1a" : "#3a1a1a";
      const border = d.action === "buy" ? "var(--green)" : "var(--red)";
      return `
        <div style="border:2px solid ${border}; background:${bg}; padding:12px; margin-bottom:10px;">
          <div style="font-size:14px; color:${border}; margin-bottom:8px;">${side}</div>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:10px;">
            <div>ทั้งหมด</div><div>${d.win + d.loss + d.breakeven} ไม้</div>
            <div>ชนะ</div><div style="color:var(--green)">${d.win}</div>
            <div>แพ้</div><div style="color:var(--red)">${d.loss}</div>
            <div>Breakeven</div><div>${d.breakeven}</div>
            <div>Win Rate</div><div style="color:${col}; font-size:13px;">${wr}</div>
          </div>
        </div>`;
    }).join("");

    // --- แยกตาม Symbol (BUY vs SELL คู่กัน) ---
    if (data.by_symbol.length === 0) {
      symbolPanel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>';
    } else {
      // จัดกลุ่มตาม symbol
      const bySymMap = {};
      data.by_symbol.forEach(r => {
        if (!bySymMap[r.symbol]) bySymMap[r.symbol] = { buy: null, sell: null };
        bySymMap[r.symbol][r.action] = r;
      });
      const symbols = Object.keys(bySymMap).sort();

      const empty = { win: 0, loss: 0, breakeven: 0, closed: 0, win_rate_pct: null };
      const cell = (r, dir) => {
        if (!r) return `<td style="color:#555">-</td><td style="color:#555">-</td><td style="color:#555">-</td><td style="color:#555">-</td>`;
        const wr = r.win_rate_pct !== null ? `${r.win_rate_pct}%` : "--";
        const col = wrColor(r.win_rate_pct);
        const dirCol = dir === "buy" ? "var(--green)" : "var(--red)";
        return `
          <td style="color:${dirCol}">${r.win + r.loss + r.breakeven}</td>
          <td style="color:var(--green)">${r.win}</td>
          <td style="color:var(--red)">${r.loss}</td>
          <td style="color:${col};font-weight:bold">${wr}</td>`;
      };

      symbolPanel.innerHTML = `
        <table style="width:100%;border-collapse:collapse;font-size:9px;">
          <thead>
            <tr style="color:#888;border-bottom:1px solid #444;">
              <th style="text-align:left;padding:4px 6px;">Symbol</th>
              <th colspan="4" style="color:var(--green);text-align:center;padding:4px;">── BUY ──</th>
              <th colspan="4" style="color:var(--red);text-align:center;padding:4px;">── SELL ──</th>
            </tr>
            <tr style="color:#666;border-bottom:1px solid #333;font-size:8px;">
              <th></th>
              <th style="padding:2px 4px;">ไม้</th><th>W</th><th>L</th><th>Win%</th>
              <th style="padding:2px 4px;">ไม้</th><th>W</th><th>L</th><th>Win%</th>
            </tr>
          </thead>
          <tbody>` +
        symbols.map(sym => {
          const b = bySymMap[sym].buy;
          const s = bySymMap[sym].sell;
          const totalBuy  = b ? b.win + b.loss + b.breakeven : 0;
          const totalSell = s ? s.win + s.loss + s.breakeven : 0;
          return `
            <tr style="border-bottom:1px solid #2a2a2a;">
              <td style="padding:5px 6px;font-size:10px;">${sym}</td>
              ${cell(b, "buy")}
              ${cell(s, "sell")}
            </tr>`;
        }).join("") +
        `</tbody></table>`;
    }

    // --- รายวัน Buy vs Sell win rate ---
    if (data.daily.length === 0) {
      dailyPanel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>';
    } else {
      dailyPanel.innerHTML = `
        <div class="position-row" style="font-size:9px; color:#888; grid-template-columns:90px 1fr 1fr;">
          <span>วันที่</span><span style="color:var(--green)">BUY (W/L/BE)</span><span style="color:var(--red)">SELL (W/L/BE)</span>
        </div>` +
        data.daily.map(d => {
          const bwr = d.buy.win_rate  !== null ? `${d.buy.win_rate}%`  : "--";
          const swr = d.sell.win_rate !== null ? `${d.sell.win_rate}%` : "--";
          const bcol = wrColor(d.buy.win_rate);
          const scol = wrColor(d.sell.win_rate);
          return `
            <div class="position-row" style="grid-template-columns:90px 1fr 1fr;">
              <span>${d.date}</span>
              <span style="color:${bcol}">${bwr} &nbsp;(${d.buy.win}/${d.buy.loss}/${d.buy.be})</span>
              <span style="color:${scol}">${swr} &nbsp;(${d.sell.win}/${d.sell.loss}/${d.sell.be})</span>
            </div>`;
        }).join("");
    }

  } catch (e) {
    summaryPanel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}

loadDirectionStats();
setInterval(loadDirectionStats, 10000);

// --- Multi-TF Zone Watch panel ---
const KIND_TH = {
  order_block: "Order Block", fvg: "FVG", equal_level: "Liquidity",
  support: "แนวรับ", resistance: "แนวต้าน",
};
const TREND_TH = { bullish: "ขึ้น", bearish: "ลง", ranging: "ออกข้าง", mixed: "ผสม", unknown: "-" };
function trendColor(t) {
  return t === "bullish" ? "var(--green-bright)" : t === "bearish" ? "var(--red-bright)" : "var(--text-dim)";
}
async function loadZones() {
  const panel = document.getElementById("zones-panel");
  if (!panel) return;
  try {
    const res = await fetch(`${API}/zones`);
    const data = await res.json();
    const symbols = Object.keys(data);
    if (symbols.length === 0) {
      panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูลโซน (รอ cycle แรก)</p>';
      return;
    }
    panel.innerHTML = symbols.map((sym) => {
      const s = data[sym];
      const mtf = s.mtf || {};
      const engaged = mtf.engage;
      const statusColor = engaged ? "var(--green-bright)" : "var(--text-dim)";
      const statusIcon = engaged ? "🎯 เรียก AI" : "👁 เฝ้าดู (ฟรี)";

      // trend row (H1 / H4 / D1)
      const per = mtf.trend?.per_tf || {};
      const trendChips = ["H1", "H4", "D1"].map((tf) =>
        `<span style="font-size:9px;color:${trendColor(per[tf])};margin-right:8px;">${tf}: ${TREND_TH[per[tf]] || "-"}</span>`
      ).join("");
      const overall = mtf.trend?.overall;

      // per-pair zones
      const pairsHtml = (mtf.pairs || []).map((p) => {
        const hitKeys = new Set((p.zones_hit || []).map((z) => `${z.kind}-${z.low}`));
        const zoneRows = (p.zones || []).slice(0, 5).map((z) => {
          const hit = hitKeys.has(`${z.kind}-${z.low}`);
          const dirCol = z.dir === "bullish" ? "var(--green)" : z.dir === "bearish" ? "var(--red)" : "var(--text-dim)";
          const range = z.low === z.high ? `${z.low}` : `${z.low}–${z.high}`;
          return `<div class="meter-row" style="${hit ? 'background:rgba(14,203,129,0.14);border-radius:4px;padding:1px 4px;' : ''}">
              <span style="font-size:10px;">${KIND_TH[z.kind] || z.kind}</span>
              <span style="color:${dirCol};font-size:10px;">${range}${hit ? " ⚡" : ""}</span>
            </div>`;
        }).join("") || '<div class="meter-row"><span class="placeholder" style="font-size:9px;">ยังไม่พบโซน (ต้องมี candle ครบ)</span></div>';
        return `
          <div style="margin:6px 0; padding:6px; background:var(--panel-2); border-radius:8px; ${p.fired ? 'border:1px solid var(--green);' : 'border:1px solid var(--border-soft);'}">
            <div style="font-size:10px; color:var(--text); margin-bottom:4px;">
              <strong>${p.entry_tf}</strong> <span style="color:var(--text-dim)">คู่กับ</span> <strong>${p.structure_tf}</strong>
              ${p.fired ? '<span style="color:var(--green-bright)"> ⚡ FIRED</span>' : ''}
              <span style="color:#5a606e;font-size:8px;float:right;">${p.bars?.[p.structure_tf]||0}/${p.bars?.[p.entry_tf]||0} bars</span>
            </div>
            ${zoneRows}
          </div>`;
      }).join("");

      return `
        <div style="margin-bottom:14px; border-bottom:1px solid var(--border); padding-bottom:10px;">
          <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
            <strong style="color:var(--text)">${sym} <span style="color:${trendColor(overall)};font-size:10px;">▸ เทรน ${TREND_TH[overall] || "-"}</span></strong>
            <span style="color:${statusColor}; font-size:10px;">${statusIcon}</span>
          </div>
          <div style="margin-bottom:4px;">${trendChips}</div>
          <div style="font-size:9px; color:var(--text-dim); margin-bottom:4px;">ราคา ${s.price} · ${mtf.reason || ""}</div>
          ${pairsHtml}
        </div>`;
    }).join("");
  } catch (e) {
    panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>';
  }
}
loadZones();
setInterval(loadZones, 4000);

// --- P/L Calendar (trading-journal style: 7 days + WEEKLY column) ---
let calPnlMap = {};   // "YYYY-MM-DD" -> {net_r, wins, losses, trades}
let calMonth = new Date();  // currently displayed month
const CAL_WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
  "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"];

function fmtR(v) { return `${v > 0 ? "+" : ""}${v.toFixed(2)}R`; }

function renderCalendar() {
  const days = document.getElementById("cal-days");
  const wd = document.getElementById("cal-weekdays");
  const title = document.getElementById("cal-title");
  const totalBadge = document.getElementById("cal-total");
  if (!days) return;

  wd.innerHTML = CAL_WEEKDAYS.map((d) => `<div class="cal-weekday">${d}</div>`).join("")
    + `<div class="cal-weekday weekly">WEEKLY</div>`;

  const y = calMonth.getFullYear(), m = calMonth.getMonth();
  title.textContent = `${TH_MONTHS[m]} ${y}`;

  const first = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

  // build a flat list of day-slots (leading blanks + days), then chunk to weeks
  const slots = [];
  for (let i = 0; i < first; i++) slots.push(null);
  for (let d = 1; d <= daysInMonth; d++) slots.push(d);
  while (slots.length % 7 !== 0) slots.push(null);

  let monthR = 0, monthTrades = 0;
  let html = "";

  for (let w = 0; w < slots.length / 7; w++) {
    let weekR = 0, weekDays = 0;
    for (let i = 0; i < 7; i++) {
      const d = slots[w * 7 + i];
      if (d === null) { html += `<div class="cal-cell empty"></div>`; continue; }
      const key = `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
      const rec = calPnlMap[key];
      let cls = "cal-cell";
      let body;
      if (rec) {
        // green = net win, red = net loss, neutral "be" = traded but flat
        cls += rec.net_r > 0.005 ? " win" : rec.net_r < -0.005 ? " loss" : " be";
        const sub = `W${rec.wins} L${rec.losses}${rec.breakeven ? " BE" + rec.breakeven : ""}`;
        body = `<div class="cal-body"><div class="cal-r">${fmtR(rec.net_r)}</div>
                  <div class="cal-sub">${sub}</div></div>`;
        monthR += rec.net_r; monthTrades += rec.trades;
        weekR += rec.net_r; weekDays++;
      } else {
        body = `<div class="cal-body"><div class="cal-none">ไม่มีไม้</div></div>`;
      }
      if (key === todayStr) cls += " today";
      const badge = rec ? `<span class="cal-badge">📄 ${rec.trades}</span>` : "";
      html += `<div class="${cls}">
          <div class="cal-top"><span class="cal-date">${d}</span>${badge}</div>
          ${body}
        </div>`;
    }
    // weekly summary cell
    const wcls = weekR > 0 ? "win" : weekR < 0 ? "loss" : "";
    html += `<div class="cal-week">
        <div class="cal-week-label">WEEKLY</div>
        <div class="cal-week-r ${wcls}">${weekDays ? fmtR(weekR) : "0R"}</div>
        <div class="cal-week-days">${weekDays} วัน</div>
      </div>`;
  }
  days.innerHTML = html;

  const cls = monthR > 0 ? "win" : monthR < 0 ? "loss" : "";
  totalBadge.className = "cal-total-badge " + cls;
  totalBadge.textContent = `${fmtR(monthR)} · ${monthTrades} ไม้`;
}

async function loadCalendar() {
  try {
    const res = await fetch(`${API}/signals/daily-pnl`);
    const rows = await res.json();
    calPnlMap = {};
    rows.forEach((r) => { calPnlMap[r.date] = r; });
    renderCalendar();
  } catch (e) { /* backend down */ }
}

document.addEventListener("DOMContentLoaded", () => {
  const prev = document.getElementById("cal-prev");
  const next = document.getElementById("cal-next");
  const todayBtn = document.getElementById("cal-today");
  if (prev) prev.addEventListener("click", () => { calMonth.setMonth(calMonth.getMonth() - 1); renderCalendar(); });
  if (next) next.addEventListener("click", () => { calMonth.setMonth(calMonth.getMonth() + 1); renderCalendar(); });
  if (todayBtn) todayBtn.addEventListener("click", () => { calMonth = new Date(); renderCalendar(); });
});
loadCalendar();
setInterval(loadCalendar, 15000);

// --- Expectancy รายสินทรัพย์ ---
async function loadSymbolExpectancy() {
  const panel = document.getElementById("sym-expectancy-panel");
  if (!panel) return;
  try {
    const res  = await fetch(`${API}/signals/symbol-expectancy`);
    const data = await res.json();
    if (!data.length) { panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>'; return; }
    panel.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:9px;">
        <thead>
          <tr style="color:#888;border-bottom:1px solid #444;">
            <th style="text-align:left;padding:3px 6px;">Symbol</th>
            <th>Exp(R)</th><th>AvgW</th><th>AvgL</th><th>W</th><th>L</th><th>n</th>
          </tr>
        </thead><tbody>` +
      data.map(r => {
        const col = r.expectancy_r >= 0 ? "var(--green)" : "var(--red)";
        return `<tr style="border-bottom:1px solid #222;">
          <td style="padding:4px 6px;">${r.symbol}</td>
          <td style="color:${col};font-weight:bold;text-align:center;">${r.expectancy_r}R</td>
          <td style="color:var(--green);text-align:center;">${r.avg_win_r !== null ? "+"+r.avg_win_r+"R" : "-"}</td>
          <td style="color:var(--red);text-align:center;">${r.avg_loss_r !== null ? r.avg_loss_r+"R" : "-"}</td>
          <td style="color:var(--green);text-align:center;">${r.win_count}</td>
          <td style="color:var(--red);text-align:center;">${r.loss_count}</td>
          <td style="color:#888;text-align:center;">${r.samples}</td>
        </tr>`;
      }).join("") +
      `</tbody></table>`;
  } catch(e) { panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>'; }
}

// --- Win Rate รายชั่วโมง ---
async function loadHourlyStats() {
  const panel = document.getElementById("hourly-panel");
  if (!panel) return;
  try {
    const res  = await fetch(`${API}/signals/hourly-stats`);
    const data = await res.json();
    const active = data.filter(h => h.total > 0);
    if (!active.length) { panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>'; return; }
    const maxTotal = Math.max(...active.map(h => h.total));
    panel.innerHTML = active.map(h => {
      const wr  = h.win_rate_pct !== null ? `${h.win_rate_pct}%` : "--";
      const col = wrColor(h.win_rate_pct);
      const barW = Math.round((h.total / maxTotal) * 100);
      const label = `${String(h.hour).padStart(2,"0")}:00`;
      return `
        <div style="display:grid;grid-template-columns:44px 1fr 52px 40px;gap:4px;align-items:center;margin-bottom:3px;font-size:9px;">
          <span style="color:#aaa;">${label}</span>
          <div style="background:#333;height:8px;border-radius:2px;overflow:hidden;">
            <div style="background:${col};width:${barW}%;height:100%;"></div>
          </div>
          <span style="color:#888;text-align:right;">W${h.win}/L${h.loss}</span>
          <span style="color:${col};text-align:right;font-weight:bold;">${wr}</span>
        </div>`;
    }).join("");
  } catch(e) { panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>'; }
}

// --- RSI × EMA Pattern Matrix ---
async function loadRsiEmaMatrix() {
  const panel = document.getElementById("rsi-ema-matrix-panel");
  if (!panel) return;
  try {
    const res  = await fetch(`${API}/signals/rsi-ema-matrix`);
    const data = await res.json();
    if (!data.cells || !data.cells.length) { panel.innerHTML = '<p class="placeholder">ยังไม่มีข้อมูล</p>'; return; }

    const RSI_STATES = ["oversold","neutral","overbought"];
    const EMA_TRENDS = ["down","neutral","up"];
    const ACTIONS    = ["buy","sell"];

    let html = "";
    for (const action of ACTIONS) {
      const cells = data.cells.filter(c => c.action === action);
      const dirCol = action === "buy" ? "var(--green)" : "var(--red)";
      html += `<div style="margin-bottom:14px;">
        <div style="color:${dirCol};font-size:10px;margin-bottom:6px;">${action.toUpperCase()}</div>
        <table style="border-collapse:collapse;font-size:9px;width:100%;">
          <thead><tr>
            <th style="color:#666;padding:3px 6px;text-align:left;">RSI \\ EMA</th>
            ${EMA_TRENDS.map(e => `<th style="color:#888;padding:3px 8px;">${e}</th>`).join("")}
          </tr></thead><tbody>`;
      for (const rsi of RSI_STATES) {
        html += `<tr><td style="color:#aaa;padding:3px 6px;">${rsi}</td>`;
        for (const ema of EMA_TRENDS) {
          const cell = cells.find(c => c.rsi_state === rsi && c.ema_trend === ema);
          if (!cell) {
            html += `<td style="text-align:center;color:#444;padding:4px 8px;">-</td>`;
          } else {
            const wr  = cell.win_rate_pct;
            const col = wr >= 55 ? "#1a5c1a" : wr >= 45 ? "#3a3a1a" : "#5c1a1a";
            const txt = wrColor(wr);
            html += `<td style="background:${col};text-align:center;padding:4px 8px;border:1px solid #333;">
              <div style="color:${txt};font-weight:bold;">${wr}%</div>
              <div style="color:#666;font-size:8px;">${cell.samples}ไม้</div>
            </td>`;
          }
        }
        html += `</tr>`;
      }
      html += `</tbody></table></div>`;
    }
    panel.innerHTML = html;
  } catch(e) { panel.innerHTML = '<p class="placeholder">เชื่อมต่อ backend ไม่ได้</p>'; }
}

loadSymbolExpectancy();
loadHourlyStats();
loadRsiEmaMatrix();
setInterval(loadSymbolExpectancy, 15000);
setInterval(loadHourlyStats, 15000);
setInterval(loadRsiEmaMatrix, 15000);
