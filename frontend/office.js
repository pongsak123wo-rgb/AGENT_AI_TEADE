/* Pokémon-style top-down office scene.
 * Agents wander at their desks; when one "speaks", it walks across the
 * room to the CEO, faces them, shows a speech bubble, then walks home.
 * Exposes window.OfficeScene.speak(agent, text).
 */
(function () {
  const TILE = 16;
  const COLS = 18, ROWS = 10;
  const W = COLS * TILE, H = ROWS * TILE;

  const canvas = document.getElementById("office-canvas");
  const bubbleLayer = document.getElementById("office-bubbles");
  if (!canvas) return;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;

  const COLORS = {
    technical: "#3a7bd5",
    risk: "#ef5350",
    ceo: "#9d7cff",
  };
  const NAMES = { technical: "Technical", risk: "Risk", ceo: "CEO" };

  // Desk / home positions in tile coords (x, y = the tile the char stands on)
  const HOMES = {
    ceo: { x: 8, y: 3, face: "down" },
    technical: { x: 3, y: 7, face: "down" },
    risk: { x: 14, y: 7, face: "down" },
  };
  // Where a visitor stands to talk to the CEO (just below CEO desk)
  const MEET = { technical: { x: 7, y: 5 }, risk: { x: 9, y: 5 } };

  // Desks drawn as furniture (tileX, tileY, w, h in tiles)
  const DESKS = [
    { x: 7, y: 2, w: 3, h: 1 },   // CEO desk (top center)
    { x: 2, y: 8, w: 3, h: 1 },   // Technical desk
    { x: 13, y: 8, w: 3, h: 1 },  // Risk desk
  ];

  // Pre-generated candlestick series for the wall screens / monitors —
  // fixed once so the charts don't flicker every frame. Each candle is
  // normalized 0..1: {o, c, hi, lo, up}.
  function makeCandles(n, seed) {
    let s = seed, price = 0.5;
    const rnd = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
    const out = [];
    for (let i = 0; i < n; i++) {
      const o = price;
      price = Math.max(0.08, Math.min(0.92, price + (rnd() - 0.48) * 0.22));
      const c = price;
      const hi = Math.min(0.96, Math.max(o, c) + rnd() * 0.08);
      const lo = Math.max(0.04, Math.min(o, c) - rnd() * 0.08);
      out.push({ o, c, hi, lo, up: c >= o });
    }
    return out;
  }
  const WALL_CANDLES = makeCandles(26, 12345);
  const DESK_CANDLES = makeCandles(10, 777);

  function drawCandles(x, y, w, h, candles) {
    const step = w / candles.length;
    const cw = Math.max(1, step - 1);
    candles.forEach((k, i) => {
      const cx = x + i * step;
      const col = k.up ? "#26a69a" : "#ef5350";
      ctx.fillStyle = col;
      // wick
      ctx.fillRect(Math.round(cx + cw / 2), Math.round(y + (1 - k.hi) * h), 1, Math.max(1, Math.round((k.hi - k.lo) * h)));
      // body
      const bt = Math.min(k.o, k.c), bb = Math.max(k.o, k.c);
      ctx.fillRect(Math.round(cx), Math.round(y + (1 - bb) * h), Math.round(cw), Math.max(1, Math.round((bb - bt) * h)));
    });
  }

  function makeChar(agent) {
    const h = HOMES[agent];
    return {
      agent,
      cx: h.x, cy: h.y,          // continuous tile position
      face: h.face,
      path: [],                  // queued waypoint tiles
      moving: false,
      frame: 0,
      stepAccum: 0,
      state: "idle",             // idle | toMeet | talking | toHome
      bubble: null,
      bubbleUntil: 0,
      wanderAt: performance.now() + Math.random() * 3000,
      talkUntil: 0,
    };
  }

  const chars = {
    ceo: makeChar("ceo"),
    technical: makeChar("technical"),
    risk: makeChar("risk"),
  };

  // Build an L-shaped path (horizontal then vertical) of tile waypoints.
  function pathTo(c, tx, ty) {
    const wp = [];
    let x = Math.round(c.cx), y = Math.round(c.cy);
    const sx = tx > x ? 1 : -1;
    while (x !== tx) { x += sx; wp.push({ x, y }); }
    const sy = ty > y ? 1 : -1;
    while (y !== ty) { y += sy; wp.push({ x, y }); }
    return wp;
  }

  function setFace(c, tx, ty) {
    const dx = tx - c.cx, dy = ty - c.cy;
    if (Math.abs(dx) > Math.abs(dy)) c.face = dx > 0 ? "right" : "left";
    else if (dy !== 0) c.face = dy > 0 ? "down" : "up";
  }

  const SPEED = 3.2; // tiles per second

  function update(dt) {
    const now = performance.now();
    for (const agent in chars) {
      const c = chars[agent];

      // idle wander (small hops near home) when nothing to do
      if (c.state === "idle" && now > c.wanderAt && c.path.length === 0) {
        const h = HOMES[agent];
        const nx = h.x + (Math.floor(Math.random() * 3) - 1);
        const ny = h.y + (Math.floor(Math.random() * 3) - 1);
        if (nx >= 1 && nx < COLS - 1 && ny >= 4 && ny < ROWS - 1) {
          c.path = pathTo(c, nx, ny);
        }
        c.wanderAt = now + 2500 + Math.random() * 3500;
      }

      // move along path
      if (c.path.length > 0) {
        c.moving = true;
        const wp = c.path[0];
        setFace(c, wp.x, wp.y);
        const step = SPEED * dt;
        const dx = wp.x - c.cx, dy = wp.y - c.cy;
        const dist = Math.hypot(dx, dy);
        if (dist <= step) {
          c.cx = wp.x; c.cy = wp.y;
          c.path.shift();
        } else {
          c.cx += (dx / dist) * step;
          c.cy += (dy / dist) * step;
        }
        c.stepAccum += step;
        if (c.stepAccum >= 0.5) { c.frame ^= 1; c.stepAccum = 0; }
      } else {
        c.moving = false;
        c.frame = 0;
      }

      // arrival transitions
      if (!c.moving && c.path.length === 0) {
        if (c.state === "toMeet") {
          c.face = "up"; // face the CEO
          c.state = "talking";
        } else if (c.state === "toHome") {
          c.face = HOMES[agent].face;
          c.state = "idle";
        } else if (c.state === "talking" && now > c.talkUntil) {
          const h = HOMES[agent];
          c.path = pathTo(c, h.x, h.y);
          c.state = "toHome";
        }
      }

      // bubble expiry
      if (c.bubble && now > c.bubbleUntil) c.bubble = null;
    }
  }

  // ---------- drawing ----------
  const WALL_H = 34;  // back-wall height in px (holds the big screens)

  function drawFloor() {
    // ---- office carpet floor ----
    for (let y = 0; y < ROWS; y++) {
      for (let x = 0; x < COLS; x++) {
        ctx.fillStyle = (x + y) % 2 === 0 ? "#1e222c" : "#191d26";
        ctx.fillRect(x * TILE, y * TILE, TILE, TILE);
      }
    }
    // faint carpet grid lines
    ctx.strokeStyle = "rgba(255,255,255,0.03)";
    for (let x = 0; x <= COLS; x++) { ctx.beginPath(); ctx.moveTo(x * TILE, WALL_H); ctx.lineTo(x * TILE, H); ctx.stroke(); }

    // ---- back wall ----
    ctx.fillStyle = "#12151d";
    ctx.fillRect(0, 0, W, WALL_H);
    ctx.fillStyle = "#0d1016";
    ctx.fillRect(0, WALL_H - 3, W, 3);           // wall/floor skirting shadow

    // ---- big central market screen (candlestick chart) ----
    const bw = 120, bx = (W - bw) / 2, by = 4, bh = 24;
    ctx.fillStyle = "#0a0d13"; ctx.fillRect(bx - 2, by - 2, bw + 4, bh + 4);   // bezel
    ctx.fillStyle = "#0e141c"; ctx.fillRect(bx, by, bw, bh);                   // screen
    // grid on screen
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(bx, by + i * bh / 4); ctx.lineTo(bx + bw, by + i * bh / 4); ctx.stroke(); }
    drawCandles(bx + 3, by + 2, bw - 6, bh - 4, WALL_CANDLES);
    // "LIVE" dot
    ctx.fillStyle = "#26a69a"; ctx.fillRect(bx + 3, by + 2, 2, 2);

    // ---- side ticker screens ----
    const drawTicker = (tx, up) => {
      ctx.fillStyle = "#0a0d13"; ctx.fillRect(tx - 1, 5, 34, 22);
      ctx.fillStyle = "#0e141c"; ctx.fillRect(tx, 6, 32, 20);
      ctx.fillStyle = up ? "#26a69a" : "#ef5350";
      ctx.font = "6px monospace"; ctx.textAlign = "left";
      ctx.fillText(up ? "▲ BUY" : "▼ SELL", tx + 2, 13);
      // mini bars
      for (let i = 0; i < 6; i++) { ctx.fillRect(tx + 3 + i * 5, 22 - (i % 3) * 3, 3, (i % 3) * 3 + 2); }
    };
    drawTicker(6, true);
    drawTicker(W - 39, false);

    // ---- wall clock ----
    ctx.fillStyle = "#2a2f3d"; ctx.beginPath(); ctx.arc(W / 2, WALL_H - 6, 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#0a0d13"; ctx.beginPath(); ctx.arc(W / 2, WALL_H - 6, 3, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#7fd4ff"; ctx.beginPath(); ctx.moveTo(W / 2, WALL_H - 6); ctx.lineTo(W / 2, WALL_H - 8); ctx.stroke();

    // ---- potted plant (bottom-left corner) ----
    ctx.fillStyle = "#3a2f22"; ctx.fillRect(4, H - 12, 8, 8);
    ctx.fillStyle = "#2e7d32"; ctx.beginPath(); ctx.arc(8, H - 14, 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#388e3c"; ctx.beginPath(); ctx.arc(6, H - 16, 3, 0, Math.PI * 2); ctx.fill();
  }

  function drawDesk(d) {
    const px = d.x * TILE, py = d.y * TILE;
    const w = d.w * TILE;
    // desk top
    ctx.fillStyle = "#2b3140"; ctx.fillRect(px, py, w, d.h * TILE);
    ctx.fillStyle = "#343b4c"; ctx.fillRect(px, py, w, 4);        // front edge highlight

    // Desks up against the back wall (the CEO's) use the big wall screen as
    // their display — drawing pop-up monitors there would clash with it.
    const againstWall = py - 8 < WALL_H;
    if (!againstWall) {
      const drawMon = (mx) => {
        ctx.fillStyle = "#05070b"; ctx.fillRect(mx, py - 8, 13, 10);     // bezel
        ctx.fillStyle = "#0e141c"; ctx.fillRect(mx + 1, py - 7, 11, 7);  // screen
        drawCandles(mx + 1, py - 7, 11, 7, DESK_CANDLES);
        ctx.fillStyle = "#1a1f2b"; ctx.fillRect(mx + 5, py + 2, 3, 2);   // stand
      };
      drawMon(px + 2);
      if (w >= 40) drawMon(px + w - 15);
    }
    // keyboard
    ctx.fillStyle = "#151922"; ctx.fillRect(px + w / 2 - 6, py + 5, 12, 4);
  }

  // Per-agent pixel-art look — same body, distinct hair + accessory so
  // each teammate is recognizable at a glance.
  const LOOK = {
    technical: { hair: "#4a3a24", glasses: "#7fd4ff" },
    risk:      { hair: "#241a12", cap: "#ef5350" },
    ceo:       { hair: "#1a1410", tie: "#f0b90b" },
  };

  function drawChar(c) {
    const px = Math.round(c.cx * TILE + TILE / 2);
    const py = Math.round(c.cy * TILE + TILE / 2);
    const shirt = COLORS[c.agent];
    const look = LOOK[c.agent] || {};
    const skin = "#f0c090";
    const skinShade = "#d9a878";
    const hair = look.hair || "#2a2016";
    const pants = "#2b3040";
    const shoe = "#15181f";
    const R = (x, y, w, h, col) => { ctx.fillStyle = col; ctx.fillRect(px + x, y, w, h); };

    const bob = c.moving && c.frame ? -1 : 0;
    const topY = py - 11 + bob;   // top of head
    const up = c.face === "up";
    const side = c.face === "left" || c.face === "right";
    const dir = c.face === "left" ? -1 : 1;

    // soft shadow
    ctx.fillStyle = "rgba(0,0,0,0.3)";
    ctx.fillRect(px - 5, py + 5, 10, 3);

    // ---- legs + shoes (walk cycle) ----
    const swing = c.moving ? (c.frame ? 1 : -1) : 0;
    R(-3, topY + 15, 2, 2, pants); R(1, topY + 15, 2, 2, pants);          // thighs
    R(-3 - (swing < 0 ? 1 : 0), topY + 17, 3, 2, shoe);                   // left shoe
    R(1 + (swing > 0 ? 1 : 0), topY + 17, 3, 2, shoe);                    // right shoe

    // ---- body / shirt ----
    R(-4, topY + 9, 8, 6, shirt);
    R(-4, topY + 9, 8, 1, "rgba(255,255,255,0.15)");                      // shoulder highlight
    // arms
    R(-5, topY + 9, 1, 4, shirt); R(4, topY + 9, 1, 4, shirt);
    R(-5, topY + 12, 1, 1, skin); R(4, topY + 12, 1, 1, skin);            // hands
    // CEO tie
    if (look.tie) { R(0, topY + 9, 1, 5, look.tie); R(-1, topY + 9, 3, 1, "#fff"); }

    // ---- head ----
    R(-4, topY + 3, 8, 6, skin);                                         // face box
    R(3 * dir, topY + 4, 1, 4, skinShade);                               // cheek shade on side
    // hair
    R(-4, topY, 8, 3, hair);
    R(-4, topY + 3, 1, 2, hair); R(3, topY + 3, 1, 2, hair);             // side hair
    if (up) R(-4, topY + 3, 8, 5, hair);                                 // back of head when facing up

    // ---- face features ----
    if (!up) {
      const eye = "#20150a";
      if (side) {
        R(dir > 0 ? 2 : 1, topY + 5, 1, 2, eye);                         // single side eye
      } else {
        R(-2, topY + 5, 1, 2, eye); R(1, topY + 5, 1, 2, eye);           // two eyes
        R(-1, topY + 8, 2, 1, skinShade);                                // mouth hint
      }
      // Technical glasses
      if (look.glasses && !up) {
        if (side) R(dir > 0 ? 1 : 1, topY + 5, 2, 1, look.glasses);
        else { R(-2, topY + 5, 5, 1, look.glasses); }
      }
    }
    // Risk cap (over hair)
    if (look.cap) { R(-4, topY - 1, 8, 2, look.cap); R(-4, topY + 1, 8, 1, "rgba(0,0,0,0.25)"); }

    // ---- name tag ----
    ctx.fillStyle = shirt;
    ctx.font = "5px monospace";
    ctx.textAlign = "center";
    ctx.fillText(NAMES[c.agent], px, py + 12);
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    drawFloor();
    DESKS.forEach(drawDesk);
    // draw back-to-front by y
    Object.values(chars).sort((a, b) => a.cy - b.cy).forEach(drawChar);
  }

  // ---------- speech bubbles (DOM overlay for crisp Thai text) ----------
  const bubbleEls = {};
  function ensureBubble(agent) {
    if (!bubbleEls[agent]) {
      const el = document.createElement("div");
      el.className = "office-bubble";
      el.style.borderColor = COLORS[agent];
      bubbleLayer.appendChild(el);
      bubbleEls[agent] = el;
    }
    return bubbleEls[agent];
  }

  function positionBubbles() {
    if (!bubbleLayer) return;
    const scaleX = bubbleLayer.clientWidth / W;
    const scaleY = bubbleLayer.clientHeight / H;
    for (const agent in chars) {
      const c = chars[agent];
      if (!c.bubble) { if (bubbleEls[agent]) bubbleEls[agent].style.display = "none"; continue; }
      const el = ensureBubble(agent);
      el.style.display = "block";
      el.textContent = c.bubble;
      const px = (c.cx * TILE + TILE / 2) * scaleX;
      const py = (c.cy * TILE - 6) * scaleY;
      el.style.left = px + "px";
      el.style.top = py + "px";
    }
  }

  // ---------- public API ----------
  function speak(agent, text) {
    const c = chars[agent];
    if (!c) return;
    const short = text.length > 90 ? text.slice(0, 90) + "…" : text;
    c.bubble = short;
    c.bubbleUntil = performance.now() + 5500;

    if (agent === "ceo") {
      // CEO speaks from their desk; visitors already there just listen
      return;
    }
    // Technical / Risk walk over to the CEO to report
    const m = MEET[agent];
    c.path = pathTo(c, m.x, m.y);
    c.state = "toMeet";
    c.talkUntil = performance.now() + 5000;
    // CEO turns to face the visitor
    chars.ceo.face = "down";
  }

  window.OfficeScene = { speak };

  // ---------- loop ----------
  // Driven by setInterval (not requestAnimationFrame) on purpose: rAF is
  // paused whenever the page isn't "visible" (e.g. inside a preview pane
  // or a background tab), which would freeze the whole scene. setInterval
  // keeps ticking regardless, so the office always animates.
  let last = performance.now();
  function tick() {
    const now = performance.now();
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    update(dt);
    draw();
    positionBubbles();
  }
  setInterval(tick, 33); // ~30fps
})();
