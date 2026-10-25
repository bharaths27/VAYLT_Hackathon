/* ENGRAM Cortex console. Vanilla JS, no build step, no CDN. */
(function () {
  "use strict";

  // -------------------------------------------------------------- palette
  // Canvas cannot read CSS custom properties, so mirror the :root tokens into
  // JS constants once at startup. getComputedStyle is the authority; a missing
  // or empty computed value falls back to the hardcoded literal so canvas
  // rendering stays correct even fully offline (Requirements 3.5, 9.1, 10.1).
  const CSSVAR = (name, fallback) => {
    // In the browser, CSS is the authority. Under Node (dev/test import, no
    // document/getComputedStyle) fall back to the literal so the pure helpers
    // resolve the intended token values without a DOM.
    if (typeof document === "undefined" || typeof getComputedStyle === "undefined") return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  };
  const PALETTE = {
    steel:   CSSVAR("--steel", "#A9B4C2"),
    orange:  CSSVAR("--orange", "#FB8225"),
    sage:    CSSVAR("--sage", "#8FBF9A"),
    red:     CSSVAR("--red", "#D96A6A"),
    muted:   CSSVAR("--muted", "#8A9099"),
    text:    CSSVAR("--text", "#DADDE2"),
    labelBg: "rgba(22,23,26,0.72)", // --panel-ish, for overlay label plates
  };
  // Alpha-tint helper: turns a #RGB/#RRGGBB hex into an rgba() string for canvas.
  const rgba = (hex, a) => {
    let h = String(hex).trim().replace(/^#/, "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    const n = parseInt(h, 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return `rgba(${r},${g},${b},${a})`;
  };

  // ----------------------------------------------------- pure functions
  // These are the pure canvas/interaction helpers, factored out of the
  // drawing/DOM code so they can be imported and property-tested in isolation
  // (dev/test only — see the guarded module.exports at the bottom of this file;
  // the browser never evaluates it). Each is called from its original call
  // site below so runtime output stays byte-for-byte identical.

  // radar distance-to-radius scale (clamped at maxCm). Used inside toXY.
  const radiusFor = (cm, R, maxCm) => Math.min(cm, maxCm) / maxCm * R;
  // radar polar->screen mapping. 0deg points up; positive clockwise.
  const toXY = (deg, cm, cx, cy, R, maxCm) => {
    const a = (deg - 90) * Math.PI / 180, r = radiusFor(cm, R, maxCm);
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  };
  // radar sweep phase in [0,1): fraction of the 1.6 s sweep period at time t.
  const sweepAngle = (t) => (t / 1600) % 1;
  // radar obstacle point color by distance band.
  const pointColor = (cm) => (cm < 20 ? PALETTE.red : cm < 40 ? PALETTE.orange : PALETTE.sage);
  // overlay target-bracket color by lock state.
  const bracketColor = (locked) => (locked ? PALETTE.orange : PALETTE.steel);

  // overlay label/sub-label string builders (content only, no color/geometry).
  const overlayLabel = (tagId, name) =>
    `TAG ${tagId}${name ? " · " + name.toUpperCase() : ""}`;
  const overlaySub = (distanceCm, bearingDeg) =>
    distanceCm != null
      ? `${Math.round(distanceCm)} cm  ${bearingDeg != null ? (bearingDeg > 0 ? "R" : "L") + Math.abs(Math.round(bearingDeg)) + "°" : ""}`
      : "";
  // overlay bottom-left telemetry lines (content only).
  const overlayTelemetry = (robot) => {
    if (document.body.classList.contains("simple")) return [];
    const p = (robot && robot.pose) || {};
    return [
      `GRIP ${String((robot && robot.gripper) || "?").toUpperCase()}${robot && robot.holding_tag != null ? " · TAG " + robot.holding_tag : ""}`,
      p.x_cm != null ? `POS ${p.x_cm.toFixed(0)},${p.y_cm.toFixed(0)} cm  HDG ${Math.round(p.heading_deg)}°` : "",
      robot && robot.battery_v ? `BATT ${robot.battery_v.toFixed(2)} V` : "",
    ].filter(Boolean);
  };

  // hold-to-approve fill percent (0..100) for an elapsed hold of elapsedMs.
  const fillPercent = (elapsedMs) => Math.min(1, elapsedMs / 800) * 100;

  // scoreboard: whether a metric value should be animated (count-up) or shown
  // as-is. Only finite numbers animate; strings/null/NaN use the placeholder.
  const isAnimatableMetric = (v) => Number.isFinite(v);
  // scoreboard count-up value at tElapsed ms into a dur-ms ease-out-cubic
  // interpolation from start to end. Clamped to [start,end], lands on end.
  const countupValue = (start, end, tElapsed, duration) => {
    const p = Math.min(Math.max(tElapsed / duration, 0), 1);
    const ease = 1 - Math.pow(1 - p, 3);
    return start + (end - start) * ease;
  };

  // ----------------------------------------------------- Simple/Expert mode
  // Audience level of the console, persisted in localStorage (D5, Req 7).
  // Default is Simple for a first-time user (Req 7.2). Every localStorage read
  // and write is wrapped in try/catch so a blocked/unavailable store (iPad
  // private mode) never throws or breaks the UI, falling back to "simple"
  // (Req 7.5). These helpers are pure enough to import under Node for the
  // optional property test: setMode guards its DOM access so it is a no-op
  // storage round-trip when there is no document (dev/test import).
  const MODE_KEY = "fui.mode";
  function readMode() {
    try { return localStorage.getItem(MODE_KEY) || "simple"; }
    catch (e) { return "simple"; }
  }
  function setMode(m) {
    if (typeof document !== "undefined" && document.body) {
      document.body.classList.toggle("simple", m === "simple");
      document.body.classList.toggle("expert", m === "expert");
      // Reflect the current mode on the toggle control (label + pressed state)
      // so the affordance always shows the live mode (Req 7.1).
      const btn = document.getElementById("mode-toggle");
      if (btn) {
        btn.textContent = m === "expert" ? "Expert" : "Simple";
        btn.setAttribute("aria-pressed", m === "expert" ? "true" : "false");
        btn.title = m === "expert"
          ? "Expert view — tap for Simple"
          : "Simple view — tap for Expert";
      }
    }
    try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
  }

  // ----------------------------------------------------- icon step cards
  // Map an allowlisted primitive (server/plan.py) to a friendly Step_Card
  // {card, icon, verb} for the icon step cards (D2, Req 3). Keyed on the REAL
  // op names; search_tag gets its own Search card, distinct from Look and Turn.
  // Pure and total: equal inputs always return the same card, and ANY input
  // (unknown/empty/undefined op, any effect, any status) returns a defined
  // {card, icon, verb} without throwing — the map falls back op -> effect ->
  // generic "step". Icons are offline unicode glyphs (no image files).
  const OP_ICON = {
    observe:       { card: "Look",    icon: "👁", verb: "Looking" },
    detect_tag:    { card: "Look",    icon: "👁", verb: "Looking" },
    search_tag:    { card: "Search",  icon: "🔎", verb: "Searching" },
    drive_bounded: { card: "Drive",   icon: "➤", verb: "Driving" },
    approach_tag:  { card: "Drive",   icon: "➤", verb: "Driving" },
    turn_bounded:  { card: "Turn",    icon: "↻", verb: "Turning" },
    pick_tagged:   { card: "Grab",    icon: "✊", verb: "Grabbing" },
    gripper_close: { card: "Grab",    icon: "✊", verb: "Grabbing" },
    gripper_open:  { card: "Release", icon: "✋", verb: "Releasing" },
    place_at:      { card: "Place",   icon: "▣", verb: "Placing" },
    stop:          { card: "Stop",    icon: "■", verb: "Stopping" },
  };
  // effect-level fallback for an unrecognized future op: motion reads Drive-like,
  // gripper reads Grab-like, none reads Look-like (D2).
  const EFFECT_ICON = {
    motion:  { card: "Drive",   icon: "➤", verb: "Driving" },
    gripper: { card: "Grab",    icon: "✊", verb: "Grabbing" },
    none:    { card: "Look",    icon: "👁", verb: "Looking" },
  };
  const GENERIC_ICON = { card: "Step", icon: "•", verb: "Working" };
  function iconForStep(s) {
    const o = s || {};
    return OP_ICON[o.op] || EFFECT_ICON[o.effect] || GENERIC_ICON;
  }

  // --------------------------------------------------- dev/test-only export
  // Expose the pure helpers for Node-based property tests, then stop before any
  // DOM wiring. This branch is never taken in the browser (no CommonJS
  // `module`), so the offline console runs the full app below unchanged and
  // gains no runtime dependency (Requirement 3 unaffected).
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      radiusFor, toXY, sweepAngle, pointColor, bracketColor,
      overlayLabel, overlaySub, overlayTelemetry,
      fillPercent, isAnimatableMetric, countupValue,
      MODE_KEY, readMode, setMode,
      iconForStep,
    };
    return;
  }

  const $ = (s, r) => (r || document).querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtT = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);

  // -------------------------------------------------------------- reduced motion
  // Single source of truth for the operator's reduced-motion preference (D3,
  // Reqs 5.1-5.4, 9.6). REDUCED mirrors the media query and the `body.reduced`
  // class so CSS and the canvas frame loop react to live changes within one
  // tick, without a page reload (Req 5.4).
  const RM = matchMedia("(prefers-reduced-motion: reduce)");
  let REDUCED = RM.matches;
  document.body.classList.toggle("reduced", REDUCED);
  RM.addEventListener("change", (e) => {
    REDUCED = e.matches;
    document.body.classList.toggle("reduced", e.matches);
  });

  // Apply the stored (or default "simple") mode as early as possible. app.js is
  // loaded at the end of <body>, so setting body.simple/body.expert now — before
  // the boot sequence reveals #hud — means the HUD never flashes the wrong mode
  // before first paint (Req 7.6). setMode also syncs the #mode-toggle control.
  setMode(readMode());
  // The visible Simple/Expert toggle flips to the opposite mode and persists it
  // (Reqs 7.1, 7.5). Safety controls are untouched by the flip (Req 7.7).
  const modeToggle = $("#mode-toggle");
  if (modeToggle) {
    modeToggle.addEventListener("click", () => {
      setMode(readMode() === "expert" ? "simple" : "expert");
    });
  }

  const S = {
    ops: new Map(), focus: null, skills: [], runs: [], status: {}, robot: {}, bundle: null, parsed: null,
    ws: null, wsOk: false, stopped: false, knownSkillIds: new Set(), lastStepKey: "",
  };

  // -------------------------------------------------------------- API
  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    let j = null;
    try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error((j && j.detail) || r.statusText);
    return j;
  }

  // -------------------------------------------------------------- boot
  const SPOKES = $(".spokes");
  for (let i = 0; i < 10; i++) {
    const a = (i / 10) * Math.PI * 2;
    const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
    l.setAttribute("x1", 100 + Math.cos(a) * 32); l.setAttribute("y1", 100 + Math.sin(a) * 32);
    l.setAttribute("x2", 100 + Math.cos(a) * 52); l.setAttribute("y2", 100 + Math.sin(a) * 52);
    SPOKES.appendChild(l);
  }

  async function bootSequence() {
    try { FX.unlock(); VOICE.unlock(); FX.play("boot"); } catch (e) {}
    const btn = $("#engage"); if (btn) { btn.disabled = true; btn.hidden = true; }
    const log = $("#boot-log");
    let st = {};
    try { st = await api("GET", "/status"); } catch (e) {}
    S.status = st;
    const sha = (st.models || [])[0];
    const lines = [
      ["kernel", "ENGRAM cortex console v1"],
      ["upstream", st.upstream_online === false ? "<b>NONE · running fully local</b>" : st.upstream_online ? "<i>REACHABLE · unplug for demo</i>" : "checking"],
      ["robot", st.simulated ? "<i>SIMULATOR (not real hardware)</i>" : "<b>" + esc(st.robot_url) + "</b>"],
      ["memory", st.memory_url === "mock" ? "<i>sample SOP memory</i>" : "<b>" + esc(st.memory_url) + "</b>"],
      ["planner", st.llm ? "grammar → local llm (validated)" : "<b>deterministic grammar</b>"],
      ["voice out", st.tts && st.tts.piper_model ? "<b>piper local</b>" : "ipad on-device voice"],
      ["voice in", st.stt && st.stt.whisper_model ? "<b>whisper.cpp tiny.en</b>" : "<i>typed only</i>"],
      ["weights", sha ? "<b>" + esc(sha.file) + " " + sha.sha256.slice(0, 12) + "</b>" : "no model files hashed"],
      ["safety", "<b>allowlist · bounded args · lidar veto · hold-to-approve</b>"],
    ];
    log.innerHTML = "";
    for (const [k, v] of lines) {
      log.innerHTML += `> ${k.padEnd(10, " ")} ${v}\n`;
      FX.play("tick");
      await new Promise(r => setTimeout(r, 140));
    }
    log.innerHTML += "\n<b>ALL SYSTEMS NOMINAL</b>";
    await new Promise(r => setTimeout(r, 450));
    // Zoom the logo out from the exact middle of the screen, not from where it sits above the text.
    try {
      const rc = $("#boot .reactor").getBoundingClientRect();
      const dx = innerWidth / 2 - (rc.left + rc.width / 2), dy = innerHeight / 2 - (rc.top + rc.height / 2);
      $("#boot .reactor").style.setProperty("--dx", dx + "px");
      $("#boot .reactor").style.setProperty("--dy", dy + "px");
    } catch (e) {}
    $("#boot").classList.add("out");
    $("#hud").classList.remove("hidden");
    requestAnimationFrame(() => {
      $("#hud").classList.add("live");
      // The HUD is now live — the moment to offer the first-run tour (Req 8.1).
      // maybeTour() gates itself on the fui.toured flag so it shows at most once.
      setTimeout(maybeTour, 1400); // wait for the logo zoom-out to finish
    });
    FX.flash();
    VOICE.say(st.upstream_online === false ? "Cortex online. Running entirely offline." : "Cortex online.");
    setTimeout(() => $("#boot").remove(), 1500);
    startCamera();
    connect();
  }
  // Starts on its own (no ENGAGE tap). Browsers only allow sound after a touch or key,
  // so audio unlocks on the first tap or key press anywhere.
  const unlockAudio = () => { try { FX.unlock(); VOICE.unlock(); } catch (e) {} };
  document.addEventListener("pointerdown", unlockAudio, { once: true, capture: true });
  document.addEventListener("keydown", unlockAudio, { once: true, capture: true });
  // "Home" goes back to the ENGRAM Cortex front page (the Lovable site). Inside the
  // dashboard frame that is the page that embedded us; opened directly, it is port 8080.
  (function wireHome() {
    const a = $("#back-front"); if (!a) return;
    let home = location.protocol + "//" + location.hostname + ":8080/";
    try { if (window.top !== window && document.referrer) home = new URL(document.referrer).origin + "/"; } catch (e) {}
    a.href = home;
  })();
  bootSequence();

  // -------------------------------------------------------------- websocket
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/events`);
    S.ws = ws;
    ws.onopen = () => { S.wsOk = true; chip("link", "ok", "LOCAL"); };
    ws.onclose = () => { S.wsOk = false; chip("link", "bad", "LOST"); setTimeout(connect, 1200); };
    ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  }
  setInterval(() => { if (S.ws && S.wsOk) S.ws.send("ping"); }, 10000);

  function onEvent(e) {
    switch (e.type) {
      case "hello":
        S.status = e.status;
        applyStatus();
        (e.ops || []).forEach(o => S.ops.set(o.id, o));
        S.skills = e.skills || []; S.skills.forEach(s => S.knownSkillIds.add(s.id + s.status));
        S.runs = e.runs || [];
        (e.log || []).forEach(l => logEvent(l, true));
        const last = [...S.ops.values()].pop();
        if (last) S.focus = { kind: "op", id: last.id };
        renderPlan(); renderSkills(); renderScore();
        renderRightNow();
        break;
      case "state": onState(e.state, e.stopped); break;
      case "net": S.status.upstream_online = e.upstream_online; applyStatus(); break;
      case "heard": FX.typewrite($("#heard"), e.text); break;
      case "bundle": S.bundle = e.bundle; renderMemory(e.bundle, e.memory_ms); break;
      case "parsed": onParsed(e); break;
      case "operation": onOp(e.op); break;
      case "veto": FX.flash("red"); FX.play("veto"); FX.banner("LIDAR VETO", `obstacle at ${Math.round(e.lidar_min_cm)} cm · motion blocked`, "var(--red)"); break;
      case "stop": onStop(true); break;
      case "stop_clear": onStop(false); break;
      case "say": VOICE.say(e.text); break;
      case "skills": onSkills(e.skills); break;
      case "run": S.runs = e.runs; renderScore(); break;
      case "rule":
        FX.play("rule"); FX.flash("amber");
        FX.banner("RULE LEARNED", e.result && e.result.source_id ? "cited as " + e.result.source_id + " · no retraining" : "stored", "var(--amber)");
        if (e.result && e.result.structured === false) FX.toast("Stored as text. Add tag/zone in ＋RULE for it to drive plans.", "amber");
        break;
      case "prospective": FX.toast(`Scheduled: "${e.task.text}"`, "green"); FX.play("plan"); break;
      case "prospective_fired": FX.banner("TASK DUE", esc(e.task.text), "var(--green)"); FX.play("warn"); break;
    }
    if (e.type !== "state" && e.type !== "net" && e.type !== "hello") logEvent(e);
  }

  // -------------------------------------------------------------- status chips
  function chip(name, cls, text) {
    const c = $("#chip-" + name);
    if (!c) return;
    c.className = "chip " + (cls || "");
    c.querySelector("b").textContent = text;
  }
  function applyStatus() {
    const st = S.status;
    if (st.upstream_online === false) chip("net", "ok", "Offline ✓");
    else if (st.upstream_online) chip("net", "warn", "Online");
    const m = (st.models || [])[0];
    chip("model", m ? "ok" : "", m ? m.sha256.slice(0, 10) : "none");
    $("#chip-model").title = (st.models || []).map(x => `${x.file}\n${x.sha256}`).join("\n\n") || "No model files in models/";
    $("#chip-sim").classList.toggle("hidden", !st.simulated);
    $("#sim-ctl").classList.toggle("hidden", !st.simulated);
    if (!VOICE.micAvailable()) { $("#mic").classList.add("off"); $("#cmd-hint").textContent = window.isSecureContext ? "typed only" : "mic needs HTTPS · typed works"; }
  }

  // -------------------------------------------------------------- robot state
  function onState(st, stopped) {
    S.robot = st || {};
    if (!st.connected) chip("robot", "bad", "Not connected");
    else if (st.estop || stopped) chip("robot", "bad", "Stopped");
    else chip("robot", st.mode === "executing" ? "warn" : "ok", ((m0) => m0.charAt(0).toUpperCase() + m0.slice(1))(st.mode || "?"));
    const l = st.lidar_min_cm;
    if (l == null) chip("lidar", "", "n/a");
    else chip("lidar", st.veto ? "bad" : l < 40 ? "warn" : "ok", Math.round(l) + " cm" + (st.veto ? " · too close" : ""));
    $("#veto").classList.toggle("hidden", !st.veto);
    $("#radar-meta").textContent = l == null ? "no lidar data" : `nearest ${Math.round(l)} cm · veto < 20 cm`;
    // Sim-obstacle button label reflects the robot's actual veto state (fix 6):
    // if an obstacle is vetoing, the button offers to remove it, else to drop one.
    const sob = $("#sim-obst");
    if (sob) sob.textContent = st.veto ? "remove obstacle" : "drop obstacle";
    if ((st.estop || stopped) !== S.stopped) onStop(!!(st.estop || stopped), true);
    updateTape(st.pose && st.pose.heading_deg);
  }

  function onStop(on, silent) {
    S.stopped = on;
    document.body.classList.toggle("estop", on);
    $("#stop-clear").classList.toggle("hidden", !on);
    if (on && !silent) { VOICE.cancel(); FX.play("stop"); FX.glitch(); FX.flash("red"); FX.banner("E-STOP", "all motion halted · pending plans cancelled", "var(--red)"); }
    if (!on && !silent) { FX.play("approve"); FX.toast("E-STOP cleared. Re-issue commands to continue.", "green"); }
    // Re-render the plan so the hold-to-approve button's disabled/label state
    // tracks the latch (fix 7): disabled + "CLEAR E-STOP FIRST" while latched,
    // re-enabled once cleared.
    if (S.focus && S.focus.kind === "op") renderPlan();
  }

  // -------------------------------------------------------------- camera + overlay
  const img = $("#cam"), ov = $("#cam-overlay");
  let polling = false, frames = 0, lastFpsT = performance.now();
  function startCamera() {
    img.onerror = () => { if (!polling) { polling = true; poll(); } };
    img.onload = () => { $("#cam-msg").style.display = "none"; frames++; };
    img.src = "/video.mjpg?t=" + Date.now();
    setTimeout(() => { if (!frames && !polling) { polling = true; poll(); } }, 2500);
  }
  function poll() {
    const n = new Image();
    n.onload = () => { img.src = n.src; frames++; setTimeout(poll, 90); };
    n.onerror = () => { $("#cam-msg").style.display = ""; $("#cam-msg").textContent = "NO SIGNAL"; setTimeout(poll, 1000); };
    n.src = "/frame.jpg?t=" + Date.now();
  }
  setInterval(() => {
    const now = performance.now();
    const fps = frames / ((now - lastFpsT) / 1000);
    frames = 0; lastFpsT = now;
    $("#cam-meta").textContent = polling ? `${fps.toFixed(1)} fps · polling` : "MJPEG stream";
  }, 2000);

  const tape = $("#tape");
  (function buildTape() {
    let h = "";
    for (let d = -360; d <= 720; d += 15) {
      const v = ((d % 360) + 360) % 360;
      const lab = { 0: "N", 90: "E", 180: "S", 270: "W" }[v];
      h += `<span>${lab || (v % 45 === 0 ? v : "·")}</span>`;
    }
    tape.innerHTML = h;
  })();
  function updateTape(hd) {
    if (hd == null) return;
    const px = ((hd + 360) / 15) * 30;
    tape.style.transform = `translateX(${-px + tape.parentElement.clientWidth / 2 - 15}px)`;
  }

  function nameForTag(tag) {
    const b = S.bundle && (S.bundle.bindings || []).find(x => x.tag_id === tag);
    return b ? b.name : "";
  }
  function activeTag() {
    const op = S.focus && S.focus.kind === "op" && S.ops.get(S.focus.id);
    if (!op || op.status !== "EXECUTING") return null;
    const s = op.steps.find(x => x.status === "running");
    return s && s.args && s.args.tag_id != null ? s.args.tag_id : null;
  }
  function drawOverlay(t) {
    const W = ov.clientWidth, H = ov.clientHeight, dpr = window.devicePixelRatio || 1;
    if (ov.width !== Math.round(W * dpr) || ov.height !== Math.round(H * dpr)) { ov.width = Math.round(W * dpr); ov.height = Math.round(H * dpr); }
    const g = ov.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.shadowBlur = 0; // defensive: no glow on any overlay draw (Req 10.4)
    // fit 640x480 source into contain box
    const iw = img.naturalWidth || 640, ih = img.naturalHeight || 480;
    const sc = Math.min(W / iw, H / ih), ox = (W - iw * sc) / 2, oy = (H - ih * sc) / 2;
    // crosshair
    const cx = W / 2, cy = H / 2;
    g.strokeStyle = rgba(PALETTE.steel, .55); g.lineWidth = 1;
    g.beginPath();
    g.moveTo(cx - 30, cy); g.lineTo(cx - 10, cy); g.moveTo(cx + 10, cy); g.lineTo(cx + 30, cy);
    g.moveTo(cx, cy - 30); g.lineTo(cx, cy - 10); g.moveTo(cx, cy + 10); g.lineTo(cx, cy + 30);
    g.stroke();
    g.beginPath(); g.arc(cx, cy, 46, t / 900, t / 900 + 1.2); g.stroke();
    g.beginPath(); g.arc(cx, cy, 46, t / 900 + Math.PI, t / 900 + Math.PI + 1.2); g.stroke();
    // side ticks (fake pitch ladder for flavor)
    g.fillStyle = rgba(PALETTE.steel, .5); g.font = "9px ui-monospace, Menlo, monospace";
    for (let i = -3; i <= 3; i++) { const y = cy + i * 30; g.fillRect(18, y, i % 2 ? 6 : 12, 1); g.fillRect(W - 30, y, i % 2 ? 6 : 12, 1); }
    // detections
    const lock = activeTag();
    (S.robot.detections || []).forEach(d => {
      const [x, y, w, h] = d.bbox || [0, 0, 0, 0];
      const X = ox + x * sc, Y = oy + y * sc, Wd = w * sc, Hd = h * sc;
      const locked = lock === d.tag_id;
      const col = bracketColor(locked);
      const pad = locked ? 6 + Math.sin(t / 120) * 3 : 4;
      const L = Math.max(8, Wd * 0.25);
      g.strokeStyle = rgba(col, .95); g.lineWidth = locked ? 2 : 1.5;
      g.beginPath();
      [[X - pad, Y - pad, 1, 1], [X + Wd + pad, Y - pad, -1, 1], [X - pad, Y + Hd + pad, 1, -1], [X + Wd + pad, Y + Hd + pad, -1, -1]].forEach(([a, b, sx, sy]) => {
        g.moveTo(a, b + sy * L); g.lineTo(a, b); g.lineTo(a + sx * L, b);
      });
      g.stroke();
      if (locked) {
        g.save(); g.translate(X + Wd / 2, Y + Hd / 2); g.rotate(t / 400);
        g.setLineDash([6, 6]); g.beginPath(); g.arc(0, 0, Math.max(Wd, Hd) * 0.85, 0, Math.PI * 2); g.stroke(); g.restore();
      }
      const label = overlayLabel(d.tag_id, nameForTag(d.tag_id));
      const sub = overlaySub(d.distance_cm, d.bearing_deg);
      g.font = "600 10px ui-monospace, Menlo, monospace";
      const tw = Math.max(g.measureText(label).width, g.measureText(sub).width) + 10;
      g.fillStyle = PALETTE.labelBg; g.fillRect(X - pad, Y - pad - 28, tw, 24);
      g.fillStyle = locked ? PALETTE.orange : PALETTE.text; g.fillText(label, X - pad + 5, Y - pad - 17);
      g.fillStyle = PALETTE.muted; g.font = "9px ui-monospace, Menlo, monospace"; g.fillText(sub, X - pad + 5, Y - pad - 7);
      if (locked) { g.fillStyle = PALETTE.orange; g.font = "700 10px ui-monospace, Menlo, monospace"; g.fillText("◆ TARGET LOCK", X - pad, Y + Hd + pad + 14); }
    });
    // bottom-left telemetry
    g.fillStyle = rgba(PALETTE.steel, .75); g.font = "10px ui-monospace, Menlo, monospace";
    const tel = overlayTelemetry(S.robot);
    tel.forEach((s, i) => g.fillText(s, W - 210, H - 26 - (tel.length - 1 - i) * 13));
  }

  // -------------------------------------------------------------- mini map
  // Fixed Zone A / Zone B regions in world centimetres (D3, Req 1.1). The
  // frontend never receives zone coordinates from the server, so these are a
  // client-only layout constant matching the simulator world (zone A left-back,
  // zone B right-back of the robot origin). Rectangles are centred on
  // {x_cm,y_cm} with width w_cm / height h_cm.
  const ZONES = {
    A: { x_cm: -90, y_cm: 60, w_cm: 60, h_cm: 60, label: "A" },
    B: { x_cm: 90, y_cm: 60, w_cm: 60, h_cm: 60, label: "B" },
  };

  const mm = $("#minimap");
  // Where the running step is heading, for the dotted route hint. Returns
  // { tag } when the focused op's running step targets a tag, { zone } when it
  // targets a zone (e.g. place_at), or null. This is only a route hint — never
  // a claim the robot arrived (Req 10.3).
  function activeTarget() {
    const op = S.focus && S.focus.kind === "op" && S.ops.get(S.focus.id);
    if (!op || op.status !== "EXECUTING") return null;
    const s = (op.steps || []).find(x => x.status === "running");
    if (!s) return null;
    const a = s.args || {};
    if (a.tag_id != null) return { tag: a.tag_id };
    if (a.zone != null && ZONES[a.zone]) return { zone: a.zone };
    return null;
  }
  // Draw the top-down mini map from current S.robot state + ZONES only. Same
  // offline 2D-canvas approach as drawRadar: dpr handling, PALETTE/rgba colours,
  // shadowBlur 0 (no glow, Req 1.7). Assumes the common case (pose present,
  // detections may be non-empty); it is defensive enough not to throw when pose
  // is {} — the polished "position not reported" placeholder is task 8.
  function renderMiniMap(t) {
    if (!mm) return;
    const W = mm.clientWidth, H = mm.clientHeight, dpr = window.devicePixelRatio || 1;
    if (W === 0 || H === 0) return;
    if (mm.width !== Math.round(W * dpr) || mm.height !== Math.round(H * dpr)) { mm.width = Math.round(W * dpr); mm.height = Math.round(H * dpr); }
    const g = mm.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.shadowBlur = 0; // no glow on any mini-map draw (Req 1.7)

    const robot = S.robot || {};
    const pose = robot.pose || {};
    const hasPose = Number.isFinite(pose.x_cm) && Number.isFinite(pose.y_cm);
    const detections = Array.isArray(robot.detections) ? robot.detections : [];

    // -------- world->screen transform: fit robot origin + both zones + any
    // detected tag + a margin into the panel. World +y is "forward" (away from
    // the operator); screen y grows downward, so y is flipped.
    const pad = 16;
    const pts = [{ x: 0, y: 0 }];
    if (hasPose) pts.push({ x: pose.x_cm, y: pose.y_cm });
    Object.keys(ZONES).forEach(k => {
      const z = ZONES[k];
      pts.push({ x: z.x_cm - z.w_cm / 2, y: z.y_cm - z.h_cm / 2 });
      pts.push({ x: z.x_cm + z.w_cm / 2, y: z.y_cm + z.h_cm / 2 });
    });
    // include detected tag world positions so they are never clipped
    const heading = Number.isFinite(pose.heading_deg) ? pose.heading_deg : 0;
    const tagWorld = (d) => {
      const dist = Number(d.distance_cm), bearing = Number(d.bearing_deg);
      if (!hasPose || !Number.isFinite(dist) || !Number.isFinite(bearing)) return null;
      // bearing is relative to the robot heading; 0 = straight ahead. Heading
      // 0 faces +y. Convert to a world angle measured from +y, clockwise.
      const a = (heading + bearing) * Math.PI / 180;
      return { x: pose.x_cm + Math.sin(a) * dist, y: pose.y_cm + Math.cos(a) * dist };
    };
    detections.forEach(d => { const p = tagWorld(d); if (p) pts.push(p); });

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    pts.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); });
    const worldW = Math.max(1, maxX - minX), worldH = Math.max(1, maxY - minY);
    const sc = Math.min((W - pad * 2) / worldW, (H - pad * 2) / worldH);
    // centre the fitted scene in the panel
    const offX = (W - worldW * sc) / 2, offY = (H - worldH * sc) / 2;
    const sx = (x) => offX + (x - minX) * sc;
    const sy = (y) => offY + (maxY - y) * sc; // flip world +y to screen up

    // -------- fixed zone rects (muted outline, steel label)
    const roundRect = (x, y, w, h, r) => {
      const rr = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
      g.beginPath();
      g.moveTo(x + rr, y);
      g.arcTo(x + w, y, x + w, y + h, rr);
      g.arcTo(x + w, y + h, x, y + h, rr);
      g.arcTo(x, y + h, x, y, rr);
      g.arcTo(x, y, x + w, y, rr);
      g.closePath();
    };
    Object.keys(ZONES).forEach(k => {
      const z = ZONES[k];
      const x = sx(z.x_cm - z.w_cm / 2), y = sy(z.y_cm + z.h_cm / 2);
      const w = z.w_cm * sc, h = z.h_cm * sc;
      g.fillStyle = rgba(PALETTE.steel, .06);
      roundRect(x, y, w, h, 6); g.fill();
      g.strokeStyle = rgba(PALETTE.muted, .5); g.lineWidth = 1;
      roundRect(x, y, w, h, 6); g.stroke();
      g.fillStyle = rgba(PALETTE.steel, .85);
      g.font = "600 13px ui-monospace, Menlo, monospace";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(z.label, sx(z.x_cm), sy(z.y_cm));
      g.textAlign = "left"; g.textBaseline = "alphabetic";
    });

    const lock = activeTag();
    const target = activeTarget();

    // -------- detected tag world positions (dot + id/name)
    detections.forEach(d => {
      const p = tagWorld(d);
      if (!p) return;
      const X = sx(p.x), Y = sy(p.y);
      const isActive = lock != null && d.tag_id === lock;
      g.fillStyle = isActive ? PALETTE.orange : rgba(PALETTE.sage, .95);
      g.beginPath(); g.arc(X, Y, isActive ? 5 : 3.5, 0, Math.PI * 2); g.fill();
      const name = nameForTag(d.tag_id);
      const label = `${d.tag_id}${name ? " " + name : ""}`;
      g.fillStyle = isActive ? PALETTE.orange : rgba(PALETTE.steel, .8);
      g.font = "10px ui-monospace, Menlo, monospace";
      g.fillText(label, X + 7, Y + 3);
    });

    // -------- dotted route hint from robot to running target (orange dashes).
    // Static under REDUCED (frozen t); otherwise the dash offset animates.
    if (hasPose && target) {
      let tx = null, ty = null;
      if (target.tag != null) {
        const d = detections.find(x => x.tag_id === target.tag);
        const p = d ? tagWorld(d) : null;
        if (p) { tx = p.x; ty = p.y; }
      } else if (target.zone != null && ZONES[target.zone]) {
        tx = ZONES[target.zone].x_cm; ty = ZONES[target.zone].y_cm;
      }
      if (tx != null && ty != null) {
        g.save();
        g.strokeStyle = rgba(PALETTE.orange, .85); g.lineWidth = 1.5;
        g.setLineDash([5, 5]);
        g.lineDashOffset = -((t || 0) / 60) % 10;
        g.beginPath(); g.moveTo(sx(pose.x_cm), sy(pose.y_cm)); g.lineTo(sx(tx), sy(ty)); g.stroke();
        g.restore();
      }
    }

    // -------- robot triangle at pose, rotated by heading (steel). Heading 0
    // faces +y (screen up); positive heading rotates clockwise.
    if (hasPose) {
      g.save();
      g.translate(sx(pose.x_cm), sy(pose.y_cm));
      g.rotate(heading * Math.PI / 180);
      g.fillStyle = PALETTE.steel;
      g.beginPath();
      g.moveTo(0, -9); g.lineTo(-6, 6); g.lineTo(6, 6); g.closePath();
      g.fill();
      g.restore();
    }

    // -------- graceful degradation on sparse real-robot state (D3b, Req 1.5,
    // 1.6, 2.3, 2.4). Driven only from the fields the server actually sent;
    // nothing is simulated to fill a gap. Zone A/B above always render; here we
    // add calm, honest notes for the missing pieces. Static text, so reduced
    // motion is unaffected.
    const mmNote = (text, dy) => {
      g.save();
      g.fillStyle = rgba(PALETTE.muted, .9);
      g.font = "12px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(text, W / 2, H / 2 + (dy || 0));
      g.restore();
    };
    // WHEN pose is missing/non-numeric: do not place or rotate the robot (already
    // guarded by hasPose above); show a calm centered placeholder instead.
    if (!hasPose) mmNote("Position not reported", detections.length === 0 ? -8 : 0);
    // WHEN detections is empty (the real-robot case, camera frame already
    // annotated): one-line hint so the empty map does not look broken. Only
    // shown when there is no detection data — with sim detections present, none.
    if (detections.length === 0) mmNote("Tags are shown in the camera view", hasPose ? 0 : 10);

    // -------- panel meta line (counts only, from state the server sent)
    const meta = $("#minimap-meta");
    if (meta) meta.textContent = hasPose
      ? `${detections.length} tag${detections.length === 1 ? "" : "s"}`
      : "position pending";
  }

  // -------------------------------------------------------------- radar
  const rc = $("#radar");
  let sweepA = 0;
  function drawRadar(t) {
    const W = rc.clientWidth, H = rc.clientHeight, dpr = window.devicePixelRatio || 1;
    if (rc.width !== Math.round(W * dpr) || rc.height !== Math.round(H * dpr)) { rc.width = Math.round(W * dpr); rc.height = Math.round(H * dpr); }
    const g = rc.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H - 14, R = Math.min(W / 2 - 10, H - 24), maxCm = 250;
    const xy = (deg, cm) => toXY(deg, cm, cx, cy, R, maxCm);
    g.strokeStyle = rgba(PALETTE.steel, .18); g.lineWidth = 1;
    [50, 100, 150, 200, 250].forEach(cm => { g.beginPath(); g.arc(cx, cy, cm / maxCm * R, Math.PI, 2 * Math.PI); g.stroke(); });
    for (let d = -90; d <= 90; d += 30) { const [x, y] = xy(d, maxCm); g.beginPath(); g.moveTo(cx, cy); g.lineTo(x, y); g.stroke(); }
    g.fillStyle = PALETTE.muted; g.font = "9px ui-monospace, Menlo, monospace";
    [50, 100, 200].forEach(cm => g.fillText(cm + "", cx + 3, cy - cm / maxCm * R - 2));
    // veto zone
    g.fillStyle = rgba(PALETTE.red, .14); g.beginPath(); g.arc(cx, cy, 20 / maxCm * R, Math.PI, 2 * Math.PI); g.fill();
    // points: sectors are 10 deg each, index 0 = straight ahead, clockwise
    const sec = Array.isArray(S.robot.lidar_sectors) ? S.robot.lidar_sectors : [];

    // -------- graceful degradation when the robot reports no 360 sector data
    // (D3b, Req 2.1, 2.2, 2.4). The real robot may omit lidar_sectors; drawing
    // the animated sweep over an empty ring would look broken and imply sector
    // data we do not have. Instead show a calm centered note, skip the sweep and
    // per-sector points, but STILL surface the single nearest-distance + veto
    // state the robot does report (lidar_min_cm). Nothing is simulated. The
    // #radar-meta line ("nearest N cm" / "no lidar data") is owned by onState.
    if (sec.length === 0) {
      g.save();
      g.fillStyle = rgba(PALETTE.muted, .9);
      g.font = "12px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText("No range data on this robot", cx, cy - R / 2);
      const l = S.robot.lidar_min_cm;
      if (Number.isFinite(l)) {
        // single reported nearest distance: an arc at that radius + a readout,
        // honest about being one value, not a 360 scan.
        const rr = Math.min(l, maxCm) / maxCm * R;
        g.strokeStyle = rgba(S.robot.veto ? PALETTE.red : PALETTE.steel, .7); g.lineWidth = 1.5;
        g.beginPath(); g.arc(cx, cy, rr, Math.PI, 2 * Math.PI); g.stroke();
        g.fillStyle = S.robot.veto ? PALETTE.red : rgba(PALETTE.steel, .85);
        g.font = "11px ui-monospace, Menlo, monospace";
        g.fillText(`nearest ${Math.round(l)} cm${S.robot.veto ? " · veto" : ""}`, cx, cy - R / 2 + 18);
      }
      g.restore();
      // robot glyph (kept so the panel still reads as a sensor view)
      g.fillStyle = PALETTE.steel; g.beginPath(); g.moveTo(cx, cy - 9); g.lineTo(cx - 6, cy + 3); g.lineTo(cx + 6, cy + 3); g.closePath(); g.fill();
      return;
    }

    // sweep
    sweepA = sweepAngle(t);
    const sd = -90 + sweepA * 180;
    const grad = g.createLinearGradient(cx, cy, ...xy(sd, maxCm));
    grad.addColorStop(0, rgba(PALETTE.steel, 0)); grad.addColorStop(1, rgba(PALETTE.steel, .5));
    g.strokeStyle = grad; g.lineWidth = 2; g.beginPath(); g.moveTo(cx, cy); g.lineTo(...xy(sd, maxCm)); g.stroke();
    sec.forEach((cm, i) => {
      let deg = i * (360 / sec.length);
      if (deg > 180) deg -= 360;
      if (deg < -90 || deg > 90) return;
      const [x, y] = xy(deg, cm);
      const close = cm < 20;
      g.fillStyle = pointColor(cm);
      g.shadowBlur = 0;
      g.beginPath(); g.arc(x, y, close ? 4 : 2.6, 0, Math.PI * 2); g.fill();
    });
    // robot glyph
    g.fillStyle = PALETTE.steel; g.beginPath(); g.moveTo(cx, cy - 9); g.lineTo(cx - 6, cy + 3); g.lineTo(cx + 6, cy + 3); g.closePath(); g.fill();
  }

  // Under REDUCED motion, feed the draw functions a frozen constant time so the
  // radar sweep angle and the overlay's decorative rotation/pulse hold still,
  // while the loop keeps running and redraws current S.robot data (points,
  // brackets, telemetry) on every frame so new sim/robot state still updates
  // (Reqs 5.2, 5.3, 9.6). Non-reduced motion passes the real `t` unchanged, so
  // animated output stays pixel-identical.
  const FROZEN_T = 0;
  function frameLoop(t) {
    const dt = REDUCED ? FROZEN_T : t;
    // Separate guards: a hidden panel (e.g. Range in Simple mode) must not stop the others drawing.
    try { drawOverlay(dt); } catch (e) {}
    try { drawRadar(dt); } catch (e) {}
    try { renderMiniMap(dt); } catch (e) {}
    requestAnimationFrame(frameLoop);
  }
  requestAnimationFrame(frameLoop);

  // -------------------------------------------------------------- commands
  // Run a command as part of `turn` (the speech turn it came from), so any
  // speech it causes is credited to that turn.
  async function runCommand(text, source, turn) {
    try {
      return await VOICE.track(api("POST", "/commands", { text, source }), turn);
    } catch (e) { FX.toast("Command failed: " + e.message, "red"); }
  }
  async function send(text, source) {
    text = (text || "").trim();
    if (!text) return;
    FX.play("blip");
    $("#cmd").value = "";
    return runCommand(text, source || "typed", VOICE.turn()); // typed: the turn current when typed
  }
  $("#cmd-form").addEventListener("submit", (e) => { e.preventDefault(); send($("#cmd").value); $("#cmd").blur(); });
  $("#quick").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) send(b.dataset.cmd); });

  // hold to talk
  const mic = $("#mic");
  let recOn = false;
  async function micDown(e) {
    e.preventDefault();
    if (!VOICE.micAvailable()) { FX.toast(window.isSecureContext ? "Recording not supported here. Type instead." : "Mic needs HTTPS with the ENGRAM cert trusted. Typing still works.", "amber"); return; }
    // startRec silences the robot (new speech turn) and drops any older recorder
    // before the mic opens. It resolves false if released or replaced meanwhile.
    recOn = false; mic.classList.remove("rec");
    try {
      if (!(await VOICE.startRec($("#wave")))) return;
      recOn = true; mic.classList.add("rec"); FX.play("rec"); $("#cmd-hint").textContent = "listening… release to send";
    }
    catch (err) { FX.toast("Mic: " + err.message + ". Typing still works.", "amber"); }
  }
  async function micUp(e) {
    if (!recOn) { VOICE.cancelStart(); return; } // released while the mic was still opening
    e.preventDefault();
    recOn = false; mic.classList.remove("rec"); FX.play("blip");
    $("#cmd-hint").textContent = "transcribing locally…";
    const a = await VOICE.stopRec();
    if (!a || a.blob.size < 1500) { $("#cmd-hint").textContent = "too short · hold while speaking"; return; }
    try {
      // Runs in the turn this recording started; dropped if a newer one began meanwhile.
      const v = await VOICE.voiceCommand(a, (text, turn) => runCommand(text, "voice", turn));
      if (v.status === "unavailable") { FX.toast("Speech-to-text unavailable: type the command.", "amber"); $("#cmd-hint").textContent = "STT offline · typed works"; return; }
      if (v.status === "dropped") return; // a newer turn owns the mic and the hint now
      $("#cmd-hint").textContent = "heard via whisper.cpp (local)";
    } catch (err) { FX.toast("STT failed: " + err.message, "red"); }
  }
  mic.addEventListener("pointerdown", micDown);
  mic.addEventListener("pointerup", micUp);
  mic.addEventListener("pointercancel", micUp);
  mic.addEventListener("pointerleave", micUp);

  // -------------------------------------------------------------- STOP
  async function doStop() {
    onStop(true);
    try { await api("POST", "/stop"); } catch (e) { FX.toast("STOP request failed: use the robot's physical button!", "red"); }
  }
  $("#stop").addEventListener("pointerdown", (e) => { e.preventDefault(); doStop(); });
  $("#stop-clear").addEventListener("click", async () => { try { await api("POST", "/stop/clear"); } catch (e) { FX.toast(e.message, "red"); } });
  // Esc as a STOP shortcut requires two presses within 600 ms so a single stray
  // keypress can't halt the robot (fix 8). The physical STOP button and the
  // on-screen STOP are still single-action.
  let _escLast = 0;
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const now = performance.now();
    if (now - _escLast <= 600) { _escLast = 0; doStop(); }
    else { _escLast = now; FX.toast("Press Esc again to STOP", "amber"); }
  });

  // -------------------------------------------------------------- parsed / ops
  function onParsed(e) {
    S.parsed = e;
    if (e.kind === "abstain" || e.kind === "clarify") {
      S.focus = { kind: "parsed" };
      FX.play("abstain"); FX.flash(e.kind === "abstain" ? "red" : "amber");
      if (e.kind === "abstain") FX.glitch();
      renderPlan();
    }
    if (e.trace && S.bundle) renderMemory(S.bundle, null, e.trace);
  }

  function onOp(op) {
    const prev = S.ops.get(op.id);
    S.ops.set(op.id, op);
    if (!prev) {
      S.focus = { kind: "op", id: op.id };
      if (op.planner === "skill") { FX.play("skill"); FX.flash(); FX.banner("SKILL REUSED", "approved routine · planner bypassed", "var(--violet)"); }
      else FX.play("plan");
    } else if (prev.status !== op.status) {
      const st = op.status;
      if (st === "EXECUTING") { FX.play("approve"); FX.flash("green"); }
      if (st === "VERIFIED") { FX.play("success"); FX.flash("green"); FX.banner("VERIFIED", (op.verification ? "confirmed by operator" : "confirmed by sensor") + " · journaled", "var(--green)"); }
      if (st === "NEEDS_VERIFICATION") { FX.play("warn"); FX.toast("Sensors can't confirm the outcome. Verify on screen.", "amber"); }
      if (st === "FAILED" || st === "HALTED") { FX.play("reject"); FX.flash("red"); FX.toast(`Operation ${st.toLowerCase()}. Nothing is retried automatically.`, "red"); }
      if (st === "REJECTED") FX.play("reject");
      if (st === "EXPIRED") FX.toast("Approval window expired.", "amber");
    } else {
      const k = op.steps.map(s => s.status).join();
      if (k !== S.lastStepKey) FX.play("step");
      S.lastStepKey = k;
    }
    if (!S.focus || S.focus.kind !== "op" || S.focus.id === op.id) renderPlan();
    else renderHistoryOnly();
  }

  function renderHistoryOnly() { const h = $("#plan-body .history"); if (h) h.outerHTML = historyHTML(); }

  function historyHTML() {
    const ops = [...S.ops.values()].reverse().slice(0, 6);
    if (ops.length < 2) return "";
    return `<div class="history"><h5>RECENT</h5>${ops.map(o => `<div class="h" data-op="${o.id}"><span>${esc(o.goal)}</span><span class="tag status-${o.status}">${o.status.replace("_", " ")}</span></div>`).join("")}</div>`;
  }

  function stepHTML(s) {
    // Friendly icon + plain-word card name for this primitive (D2, Req 3). The
    // raw s.op is kept available as an Expert-only caption (.tech-caption) and
    // for screen readers (.dot aria-label), so beginners read the card while
    // experts keep the precise primitive.
    const ic = iconForStep(s);
    const args = Object.entries(s.args || {}).map(([k, v]) => `${k}=${v}`).join("  ");
    // Per-step progress cue that reads WITHOUT color (Req 3.5): a state glyph
    // driven only by server-reported status. The done "✓" is bound strictly to
    // status === "succeeded" — never optimistic (Req 10.3). Queued = "•",
    // running = "…", stopped/failed/vetoed = "✕".
    const stateCue = s.status === "succeeded" ? "✓"
      : s.status === "running" ? "…"
      : (s.status === "failed" || s.status === "vetoed" || s.status === "stopped" || s.status === "rejected") ? "✕"
      : "•";
    let res = "";
    if (s.result) {
      const r = s.result, pc = r.postcondition || {};
      const bits = [];
      if (r.reason) bits.push(esc(r.reason));
      if (pc.verified_by) bits.push(`verified by <b>${esc(pc.verified_by)}</b>`);
      if (r.duration_ms != null) bits.push(r.duration_ms + " ms");
      if (r.evidence_frame) bits.push(`<button class="cite" data-ev="${esc(r.evidence_frame)}">frame</button>`);
      res = `<div class="res">${bits.join(" · ")}</div>`;
    }
    return `<li class="step e-${s.effect} s-${s.status}" style="--i:${s.i}">
      <span class="dot" aria-label="${esc(ic.card)}">${ic.icon}</span>
      <div><div class="op"><b class="card-name">${esc(ic.card)}</b> <span class="step-state" aria-hidden="true">${stateCue}</span> <span class="op-raw mono tech-caption">${esc(s.op)}</span> <span class="args">${esc(args)}</span></div>
      <div class="expect">${esc(s.expect || s.label)}</div>${res}</div>
      <span class="st">${esc(s.status.toUpperCase())}</span></li>`;
  }

  // "Right now" plain-words banner (D4, Req 4). One friendly sentence derived
  // ONLY from server-reported op/step status + args — never a fabricated
  // progress or success (Req 4.3). Announced as a polite live region by the
  // #right-now aria-live markup, so updates are read without stealing focus
  // (Req 4.5). Called at the end of renderPlan() and once on boot so it stays
  // in sync with the plan on every operation/state change.
  const REST = "Ready when you are.";
  // Plain-words sentence for the currently running step, built from the step's
  // op + args (reusing the task-5 iconForStep verb). Returns null when there is
  // no describable running step so the caller can fall back to "Starting…".
  function sayStep(s) {
    if (!s) return null;
    const a = s.args || {};
    const tag = a.tag_id != null ? a.tag_id : null;
    const name = tag != null ? nameForTag(tag) : "";
    const target = tag != null ? (name ? `${name} (tag ${tag})` : `tag ${tag}`) : null;
    switch (s.op) {
      case "drive_bounded": {
        const d = Number(a.distance_cm);
        if (Number.isFinite(d)) return `Driving ${d < 0 ? "back" : "forward"} about ${Math.abs(d)} cm`;
        return "Driving";
      }
      case "turn_bounded": {
        const deg = Number(a.degrees);
        if (Number.isFinite(deg)) return `Turning ${deg < 0 ? "left" : "right"} ${Math.abs(deg)}°`;
        return "Turning";
      }
      case "search_tag":   return target ? `Turning to find ${target}` : "Turning to find a tag";
      case "detect_tag":
      case "observe":      return target ? `Looking for ${target}` : "Looking";
      case "approach_tag": return target ? `Driving to ${target}` : "Driving";
      case "pick_tagged":  return target ? `Grabbing ${target}` : "Grabbing";
      case "place_at":     return a.zone != null ? `Placing in zone ${a.zone}` : "Placing";
      default: {
        const verb = iconForStep(s).verb;
        return verb === "Working" ? "Working…" : verb;
      }
    }
  }
  function renderRightNow() {
    const el = document.getElementById("right-now");
    if (!el) return;
    let text = REST;
    if (S.focus && S.focus.kind === "parsed" && S.parsed) {
      text = S.parsed.kind === "clarify" ? "I need a bit more." : "I won't do that.";
    } else {
      const op = S.focus && S.focus.kind === "op" && S.ops.get(S.focus.id);
      if (op) {
        switch (op.status) {
          case "PENDING_APPROVAL":
            text = "Waiting for your approval.";
            break;
          case "EXECUTING": {
            const running = (op.steps || []).find(x => x.status === "running");
            const said = sayStep(running);
            text = said || "Starting…";
            break;
          }
          case "VERIFIED":
            // Success is shown ONLY on the server-sent VERIFIED status (Req 4.3).
            text = "Done.";
            break;
          case "NEEDS_VERIFICATION":
            text = "Please check the robot and confirm.";
            break;
          case "FAILED":
          case "HALTED":
          case "STOPPED": {
            const bad = (op.steps || []).find(x => ["vetoed", "failed", "stopped"].includes(x.status));
            const reason = bad && bad.result && bad.result.reason ? bad.result.reason : null;
            if (op.status === "STOPPED") text = "Stopped.";
            else if (op.status === "HALTED") text = reason ? `Stopped: ${reason}.` : "Stopped.";
            else text = reason ? `That didn't work: ${reason}.` : "That didn't work.";
            break;
          }
          case "REJECTED":
          case "EXPIRED":
          case "CANCELLED":
            text = "Nothing moved.";
            break;
        }
      }
    }
    el.textContent = text;
  }

  function renderPlan() {
    const body = $("#plan-body");
    if (S.focus && S.focus.kind === "parsed" && S.parsed) {
      const p = S.parsed, clar = p.kind === "clarify";
      const opts = (p.data && p.data.options) || [];
      body.innerHTML = `<div class="abst ${clar ? "clar" : ""}"><h3>${clar ? "I need a bit more" : "I won't do that"}</h3>
        <p>${esc(p.reason)}</p>
        ${opts.length ? `<div class="opts">${opts.map(o => `<span class="tag">${esc(o)}</span>`).join("")}</div>` : ""}
        ${(p.data && p.data.evidence_ids || []).map(id => `<button class="cite" data-ev="${esc(id)}">${esc(id)}</button>`).join("")}
        <p class="fine" style="color:var(--muted);margin-top:8px">Nothing was planned, and nothing moved.</p></div>` + historyHTML();
      $("#plan-actions").innerHTML = "";
      $("#plan-meta").textContent = clar ? "waiting on you" : "safe abstention";
      renderRightNow();
      return;
    }
    const op = S.focus && S.ops.get(S.focus.id);
    if (!op) { renderRightNow(); return; }
    const m = op.metrics || {};
    $("#plan-meta").textContent = `${op.id} · digest ${op.digest}`;
    const effects = [...new Set(op.steps.map(s => s.effect).filter(e => e !== "none"))];
    let actions = "";
    if (op.status === "PENDING_APPROVAL") {
      // E-STOP latched: approval must be impossible until the operator clears it.
      const latched = !!S.stopped;
      // The permission preview is collapsible (open by default) so on a short
      // iPad-landscape panel the plan steps below it stay visible (>=3 rows).
      actions = `<details class="perm" open><summary><h4>Before you approve</h4></summary><ul>
          <li>Physical effects: <b>${effects.length ? effects.join(", ") : "none"}</b></li>
          <li>${op.steps.length} allowlisted step${op.steps.length > 1 ? "s" : ""}, bounded args, LiDAR veto armed</li>
          <li>Grounded in: ${op.evidence_ids.length ? op.evidence_ids.map(id => `<button class="cite" data-ev="${esc(id)}">${esc(id)}</button>`).join("") : "<span class='missing'>operator instruction only</span>"}</li>
          ${op.missing && op.missing.length ? `<li class="missing">Missing evidence: ${op.missing.map(esc).join("; ")}</li>` : ""}
        </ul><div class="fine">Approving signs digest <b>${op.digest}</b>. AI confidence is not authority: you are.</div></details>
        <div class="approve-row">
          <svg class="ttl" viewBox="0 0 46 46"><circle class="bg" cx="23" cy="23" r="19"/><circle class="fg" id="ttl-fg" cx="23" cy="23" r="19" stroke-dasharray="119.4" stroke-dashoffset="0"/><text x="23" y="23" id="ttl-t">--</text></svg>
          <button class="hold" id="hold" data-op="${op.id}" data-digest="${op.digest}"${latched ? " disabled" : ""}><i class="fill"></i><span>${latched ? "CLEAR E-STOP FIRST" : "HOLD TO APPROVE"}</span></button>
          <button class="reject" id="reject" data-op="${op.id}">REJECT</button>
        </div>`;
    } else if (op.status === "NEEDS_VERIFICATION") {
      actions = `<div class="outcome warn">The robot did not report a sensor-verified postcondition. Look at the robot, then confirm honestly.</div>
        <div class="verify-row"><button class="v-ok" data-verify="1" data-op="${op.id}">✓ IT WORKED</button><button class="v-no" data-verify="0" data-op="${op.id}">✗ IT DIDN'T</button></div>`;
    } else if (op.status === "VERIFIED") {
      actions = `<div class="outcome good">✓ Verified by ${op.verification ? "operator" : "sensor"} · ${m.primitive_calls || 0} steps · ${m.exec_ms || 0} ms · journaled as episode</div>`;
    } else if (["FAILED", "HALTED", "STOPPED"].includes(op.status)) {
      // Surface the reason from the step that ended the run (e.g. the LiDAR
      // veto), and only offer operator verification when a physical primitive
      // actually executed. A LiDAR-veto HALT blocks motion *before* the
      // primitive runs, so the robot never moved -> no "it actually worked".
      const bad = op.steps.find(x => ["vetoed", "failed", "stopped"].includes(x.status));
      const vetoed = bad && (bad.status === "vetoed" || (bad.result && bad.result.by === "lidar"));
      const reason = bad && bad.result && bad.result.reason ? bad.result.reason : null;
      const ranPhysical = (m.primitive_calls || 0) > 0;
      // Verification is only meaningful if a physical primitive ran and the halt
      // was not a pure pre-motion veto. STOPPED (operator E-STOP) never offers it.
      const offerVerify = op.status !== "STOPPED" && !vetoed && ranPhysical;
      const lead = op.status === "HALTED" && reason ? `Stopped: ${esc(reason)}.`
                 : op.status === "HALTED" ? "Stopped before the robot moved."
                 : `✗ ${op.status}.` + (reason ? ` ${esc(reason)}.` : "");
      actions = `<div class="outcome bad">${lead} Remaining steps skipped. Uncertain effects are never replayed.</div>
        ${offerVerify ? `<div class="verify-row"><button class="v-ok" data-verify="1" data-op="${op.id}">ACTUALLY IT WORKED</button></div>` : ""}`;
    } else if (["REJECTED", "EXPIRED", "CANCELLED"].includes(op.status)) {
      actions = `<div class="outcome bad">${op.status}. Nothing moved.</div>`;
    }
    body.innerHTML = `
      <div class="op-goal">${esc(op.goal)}</div>
      <div class="op-sub">
        <span class="tag status-${op.status}">${op.status.replace(/_/g, " ")}</span>
        <span class="tag ${op.planner}">${op.planner === "skill" ? "◆ SKILL " + esc(op.skill_id) : op.planner === "llm" ? "LOCAL LLM (validated)" : "GRAMMAR"}</span>
        <span class="tag">plan ${m.plan_ms != null ? m.plan_ms : "?"} ms</span>
        <span class="tag">planner calls ${m.planner_calls != null ? m.planner_calls : 0}</span>
        <span class="tag">via ${esc(op.source)}</span>
      </div>
      <ol class="steps">${op.steps.map(stepHTML).join("")}</ol>${historyHTML()}`;
    $("#plan-actions").innerHTML = actions;
    wireHold();
    renderRightNow();
  }

  // hold-to-approve: deliberate 0.8 s press so a stray tap can't move a robot
  function wireHold() {
    const h = $("#hold");
    if (!h) return;
    // E-STOP latched (button disabled): do not wire the press so a stray tap
    // can never begin authorizing while motion is halted (fix 7).
    if (h.disabled) return;
    const fill = h.querySelector(".fill");
    let t0 = 0, raf = 0, done = false;
    const DUR = 800;
    const tick = () => {
      const pct = fillPercent(performance.now() - t0);
      fill.style.width = pct + "%";
      if (pct >= 100 && !done) { done = true; commit(); return; }
      if (pct < 100) raf = requestAnimationFrame(tick);
    };
    const down = (e) => { e.preventDefault(); done = false; t0 = performance.now(); FX.play("tick"); raf = requestAnimationFrame(tick); };
    const up = () => { cancelAnimationFrame(raf); if (!done) fill.style.width = "0"; };
    async function commit() {
      h.querySelector("span").textContent = "AUTHORIZING…";
      try { await api("POST", `/operations/${h.dataset.op}/approve`, { digest: h.dataset.digest }); }
      catch (e) { FX.toast("Approval refused: " + e.message, "red"); FX.play("reject"); fill.style.width = "0"; h.querySelector("span").textContent = "HOLD TO APPROVE"; }
    }
    h.addEventListener("pointerdown", down);
    h.addEventListener("pointerup", up);
    h.addEventListener("pointerleave", up);
    h.addEventListener("pointercancel", up);
  }
  setInterval(() => {
    const op = S.focus && S.ops.get(S.focus.id);
    const fg = $("#ttl-fg"), tt = $("#ttl-t");
    if (!op || !fg || op.status !== "PENDING_APPROVAL") return;
    const left = Math.max(0, op.expires_at - Date.now() / 1000), total = 90;
    fg.style.strokeDashoffset = 119.4 * (1 - left / total);
    tt.textContent = Math.ceil(left);
  }, 500);

  document.addEventListener("click", async (e) => {
    const r = e.target.closest("#reject");
    if (r) { try { await api("POST", `/operations/${r.dataset.op}/reject`); } catch (err) { FX.toast(err.message, "red"); } return; }
    const v = e.target.closest("[data-verify]");
    if (v) { try { await api("POST", `/operations/${v.dataset.op}/verify`, { ok: v.dataset.verify === "1" }); } catch (err) { FX.toast(err.message, "red"); } return; }
    const h = e.target.closest(".history .h");
    if (h) { S.focus = { kind: "op", id: h.dataset.op }; renderPlan(); return; }
    const c = e.target.closest("[data-ev]");
    if (c) { openEvidence(c.dataset.ev); return; }
    const sk = e.target.closest("[data-skill]");
    if (sk) { openSkill(sk.dataset.skill); return; }
    const sd = e.target.closest("[data-skill-decide]");
    if (sd) { decideSkill(sd.dataset.id, sd.dataset.skillDecide); return; }
  });

  // -------------------------------------------------------------- memory
  function renderMemory(b, ms, trace) {
    const facts = b.facts || [], cons = b.constraints || [], binds = b.bindings || [];
    $("#mem-meta").textContent = `${b.bundle_id || ""}${ms != null ? " · " + ms + " ms" : ""} · ${facts.length + cons.length} cited`;
    let i = 0;
    const consHTML = cons.slice(0, 4).map(c => `<div class="fact rule" style="--i:${i++}"><p>${esc(c.text)}</p>
        <button class="cite" data-ev="${esc(c.source_id)}">${esc(c.source_id)}</button>
        ${c.requires ? `<span class="tag">→ tag ${c.requires.tag_id} to zone ${esc(c.requires.zone)}</span>` : ""}
        ${c.supersedes ? `<span class="tag" style="color:var(--amber)">supersedes ${esc(c.supersedes)}</span>` : ""}</div>`).join("");
    const factHTML = facts.slice(0, 5).map(f => `<div class="fact" style="--i:${i++}"><p>${esc(f.text)}</p>
        <button class="cite" data-ev="${esc(f.source_id || f.id)}">${esc(f.source_id || f.id)}</button>
        ${f.freshness_s != null ? `<span class="tag">age ${fmtAge(f.freshness_s)}</span>` : ""}</div>`).join("");
    const bindHTML = binds.map(x => `<div class="bind"><b>${esc(x.name)}</b><span>tag ${x.tag_id}</span><button class="cite" data-ev="${esc(x.source_id)}">${esc(String(x.source_id).split("#")[0].slice(0, 14))}</button></div>`).join("");
    const tr = trace || (S.parsed && S.parsed.trace) || [];
    const miss = (b.missing_evidence || []).map(m => `<div class="missing">⚠ ${esc(m)}</div>`).join("");
    $("#mem-body").innerHTML =
      (consHTML ? `<div class="sec-h">RULES IN FORCE</div>${consHTML}` : "") +
      (factHTML ? `<div class="sec-h">RETRIEVED PASSAGES</div>${factHTML}` : "") +
      (bindHTML ? `<div class="sec-h">OBJECT BINDINGS</div>${bindHTML}` : "") + miss +
      (tr.length ? `<div class="sec-h">PARSER TRACE</div><ol class="trace">${tr.map(t => `<li>${esc(t)}</li>`).join("")}</ol>` : "");
  }
  function fmtAge(s) { return s < 60 ? s + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d"; }

  // -------------------------------------------------------------- drawer
  function openDrawer(title, html) {
    $("#drawer-title").textContent = title;
    $("#drawer-body").innerHTML = html;
    $("#drawer").classList.add("open"); $("#scrim").classList.add("on");
    FX.play("blip");
  }
  function closeDrawer() { $("#drawer").classList.remove("open"); $("#scrim").classList.remove("on"); }
  $("#drawer-x").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);

  // -------------------------------------------------------------- panel help
  // Per-panel "?" explainer (D7, Req 9). One friendly plain-language sentence
  // per primary panel, keyed by the button's data-help attribute. Presentation
  // only — no panel data or ids change (Req 9.4), fully offline (no network, no
  // new dependency). The copy is short, warm, and second person.
  const HELP_COPY = {
    cam:     "What the robot sees right now.",
    cmd:     "Type or hold the mic to tell the robot what to do.",
    plan:    "The steps the robot will take. Nothing moves until you approve.",
    memory:  "The rules and facts the robot used to make this plan.",
    minimap: "A top-down view of the robot, the zones, and any tags it sees.",
    range:   "How close the nearest obstacle is. Motion stops inside the red zone.",
    skills:  "Routines the robot learned from past runs. You approve each one.",
    timings: "How long real runs took — measured, not estimated.",
    log:     "A running list of everything that happened.",
  };
  // Reveal a dismissible inline explainer for one panel. The tip is a single
  // `.panel-tip` element appended inside that panel's `.panel-h`; its text is
  // real, accessible text (role="note" with an accessible label, Req 9.2/9.3).
  // Activating the same "?" again, or clicking elsewhere, dismisses it — so it
  // reads as a per-panel explainer, not a transient toast. No large motion on
  // reveal (simple show/hide; the global reduced-motion rule also covers it).
  function closePanelTips(except) {
    document.querySelectorAll(".panel-tip").forEach(t => { if (t !== except) t.remove(); });
    document.querySelectorAll('.panel-help[aria-expanded="true"]').forEach(b => {
      if (!except || b !== except._ownerBtn) b.setAttribute("aria-expanded", "false");
    });
  }
  function togglePanelHelp(btn) {
    const key = btn.getAttribute("data-help");
    const sentence = HELP_COPY[key];
    if (!sentence) return;
    const header = btn.closest(".panel-h");
    if (!header) return;
    const existing = header.querySelector(".panel-tip");
    // Second activation on an open tip closes it.
    if (existing) { closePanelTips(); return; }
    closePanelTips();
    const tip = document.createElement("div");
    tip.className = "panel-tip";
    tip.setAttribute("role", "note");
    tip.setAttribute("aria-label", btn.getAttribute("aria-label") || "Panel help");
    tip.textContent = sentence;
    tip._ownerBtn = btn;
    header.appendChild(tip);
    btn.setAttribute("aria-expanded", "true");
  }
  // Single delegated handler: a click on any `.panel-help` toggles its tip; a
  // click anywhere else dismisses open tips. Keyboard reach comes for free —
  // the "?" is a real <button>, so Enter/Space fire a native click (Req 9.3).
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".panel-help");
    if (btn) { e.stopPropagation(); togglePanelHelp(btn); return; }
    if (!e.target.closest(".panel-tip")) closePanelTips();
  });

  // -------------------------------------------------------------- first-run tour
  // Skippable 4-step coach-mark overlay (D6, Req 8). Shown once on the first
  // live HUD (gated by the fui.toured localStorage flag, read/written inside
  // try/catch so a blocked store never throws — Reqs 8.4, 8.5). Each step
  // positions a highlight ring over a real control and shows one/two plain
  // sentences. The overlay never blocks STOP: #tour-backdrop is
  // pointer-events:none and #stop sits above it, so STOP stays tappable
  // throughout (Req 8.7). Reduced motion collapses the animated sequence into a
  // single static, still-skippable tips card (Req 8.8). Fully offline — no
  // network, no new dependency.
  const TOUR_KEY = "fui.toured";
  // Each step: a title, plain copy, and a function returning the element to
  // highlight. The approve control (#hold) only exists during PENDING_APPROVAL,
  // so it falls back to the always-present .plan panel when absent (D6).
  const TOUR_STEPS = [
    {
      title: "The camera",
      body: "This is what the robot sees right now.",
      target: () => $(".cam") || $("#cam"),
    },
    {
      title: "Tell the robot",
      body: "Type here or hold the mic to tell the robot what to do.",
      target: () => $("#cmd"),
    },
    {
      title: "Approve a plan",
      body: "Plans wait here. Press and hold to approve — nothing moves until you do.",
      target: () => $("#hold") || $(".panel.plan"),
    },
    {
      title: "Stop any time",
      body: "Tap STOP any time to halt the robot. Esc twice does the same.",
      target: () => $("#stop"),
    },
  ];
  let tourStep = 0;

  function tourSeen() {
    try { return !!localStorage.getItem(TOUR_KEY); } catch (e) { return false; }
  }
  // Called once when the HUD goes live. Auto-starts only when the flag is unset
  // (Req 8.5); otherwise does nothing. The replay affordance can start it later
  // regardless of the flag (Req 8.6).
  function maybeTour() {
    if (tourSeen()) return;
    startTour();
  }

  // Position the highlight ring over `el`'s bounding box (getBoundingClientRect),
  // and place the card near it without covering STOP. Under REDUCED motion the
  // ring/card have no transition (handled in CSS), so this just snaps.
  function placeTourStep() {
    const tour = $("#tour"), ring = $("#tour-ring"), card = $("#tour-card");
    if (!tour || !ring || !card) return;
    const step = TOUR_STEPS[tourStep];
    const el = step && step.target();
    const title = $("#tour-title"), body = $("#tour-body"), count = $("#tour-count"), next = $("#tour-next");
    if (title) title.textContent = step.title;
    if (body) body.textContent = step.body;
    if (count) count.textContent = `${tourStep + 1} of ${TOUR_STEPS.length}`;
    if (next) next.textContent = tourStep === TOUR_STEPS.length - 1 ? "Done" : "Next";

    const vw = window.innerWidth, vh = window.innerHeight;
    if (!el) {
      // No target resolvable (shouldn't happen — every step has a stable
      // fallback). Hide the ring and center the card rather than throw.
      ring.style.display = "none";
      card.style.left = Math.round(vw / 2 - card.offsetWidth / 2) + "px";
      card.style.top = Math.round(vh / 2 - card.offsetHeight / 2) + "px";
      return;
    }
    const r = el.getBoundingClientRect();
    const pad = 6;
    ring.style.display = "block";
    ring.style.left = Math.round(r.left - pad) + "px";
    ring.style.top = Math.round(r.top - pad) + "px";
    ring.style.width = Math.round(r.width + pad * 2) + "px";
    ring.style.height = Math.round(r.height + pad * 2) + "px";

    // Place the card near the target, clamped to the viewport. Prefer below the
    // target; flip above if there isn't room. Keep it clear of the STOP button
    // (bottom-right) so the card never sits on top of STOP.
    const cw = card.offsetWidth || 280, ch = card.offsetHeight || 140;
    let left = r.left;
    let top = r.bottom + 12;
    if (top + ch > vh - 12) top = r.top - ch - 12;
    if (top < 12) top = 12;
    left = Math.max(12, Math.min(left, vw - cw - 12));
    card.style.left = Math.round(left) + "px";
    card.style.top = Math.round(top) + "px";
  }

  function showTourStep(i) {
    tourStep = Math.max(0, Math.min(i, TOUR_STEPS.length - 1));
    placeTourStep();
  }

  function startTour() {
    const tour = $("#tour");
    if (!tour) return;
    tourStep = 0;
    tour.classList.remove("hidden");
    tour.setAttribute("aria-hidden", "false");
    document.body.classList.add("touring");
    showTourStep(0);
  }

  function hideTour() {
    const tour = $("#tour");
    if (!tour) return;
    tour.classList.add("hidden");
    tour.setAttribute("aria-hidden", "true");
    document.body.classList.remove("touring");
  }

  // Completing or skipping both end the tour and set the fui.toured flag so it
  // does not auto-start again (Reqs 8.3, 8.4). The flag write is wrapped in
  // try/catch; a storage failure simply means the tour may reappear next boot.
  function endTour() {
    try { localStorage.setItem(TOUR_KEY, "1"); } catch (e) {}
    hideTour();
  }

  function tourNext() {
    if (tourStep >= TOUR_STEPS.length - 1) { endTour(); return; }
    showTourStep(tourStep + 1);
  }

  (function wireTour() {
    const next = $("#tour-next"), skip = $("#tour-skip"), replay = $("#tour-replay");
    if (next) next.addEventListener("click", tourNext);
    if (skip) skip.addEventListener("click", endTour);
    // Replay on demand, independent of the stored flag (Req 8.6).
    if (replay) replay.addEventListener("click", startTour);
    // Keep the ring/card aligned to live targets as the layout changes.
    const reflow = () => { if (document.body.classList.contains("touring")) placeTourStep(); };
    window.addEventListener("resize", reflow);
    window.addEventListener("scroll", reflow, true);
  })();

  async function openEvidence(id) {
    if (id.startsWith("frame:")) {
      openDrawer("EVIDENCE FRAME", `<div class="src-id">${esc(id)}</div><p>Camera frame captured right after the primitive finished.</p><img src="/evidence/${encodeURIComponent(id)}">`);
      return;
    }
    openDrawer("EVIDENCE", `<div class="src-id">${esc(id)}</div><p>Loading…</p>`);
    try {
      const ev = await api("GET", "/evidence/" + encodeURIComponent(id));
      const passage = typeof ev.passage === "string" ? `<blockquote>${esc(ev.passage)}</blockquote>` : `<pre>${esc(JSON.stringify(ev.passage || ev, null, 2))}</pre>`;
      $("#drawer-body").innerHTML = `<div class="src-id">${esc(ev.id || id)}</div><h3 style="margin:8px 0 0">${esc(ev.title || "Source")}</h3>
        ${ev.section ? `<div style="color:var(--muted)">section ${esc(ev.section)}</div>` : ""}${passage}
        ${ev.immutable ? `<span class="imm">IMMUTABLE SOURCE</span>` : ""}
        ${ev.added_at ? `<p style="color:var(--muted)">ingested ${new Date(ev.added_at * 1000).toLocaleString()}</p>` : ""}`;
    } catch (e) { $("#drawer-body").innerHTML = `<div class="src-id">${esc(id)}</div><p class="missing">No stored evidence for this ID (${esc(e.message)}).</p>`; }
  }

  $("#btn-teach").addEventListener("click", () => {
    openDrawer("TEACH A RULE", $("#teach-tpl").innerHTML);
    $("#teach-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { text: $("#t-text").value.trim() };
      if (!body.text) return;
      const tag = $("#t-tag").value.trim(); if (tag) body.tag_id = parseInt(tag, 10);
      const z = $("#t-zone").value; if (z) body.zone = z;
      const tr = $("#t-trig").value.trim(); if (tr) body.trigger = tr;
      try { await api("POST", "/rules", body); closeDrawer(); } catch (err) { FX.toast("Rule failed: " + err.message, "red"); }
    });
  });
  $("#btn-mute").addEventListener("click", (e) => {
    VOICE.muted = FX.muted = !VOICE.muted;
    if (VOICE.muted) VOICE.cancel();
    e.target.textContent = VOICE.muted ? "🔇" : "🔊";
  });
  $("#sim-obst").addEventListener("click", async (e) => {
    // Intent is derived from the live robot state (is a LiDAR veto active?),
    // not a local toggle, so the button can't drift out of sync with the robot
    // (fix 6). If an obstacle is currently vetoing, this removes it; else drops one.
    const obstaclePresent = !!(S.robot && S.robot.veto);
    try { await fetch("/sim/obstacle" + (obstaclePresent ? "" : "?cm=12"), { method: "POST" }); } catch (err) {}
    // The label is refreshed authoritatively by onState() from the next robot
    // state frame; no local flag is kept.
  });

  // -------------------------------------------------------------- skills
  function onSkills(list) {
    const fresh = (list || []).filter(s => s.status === "CANDIDATE" && !S.knownSkillIds.has(s.id + s.status));
    S.skills = list || [];
    S.skills.forEach(s => S.knownSkillIds.add(s.id + s.status));
    if (fresh.length) { FX.play("skill"); FX.flash(); FX.banner("NEW SKILL PROPOSED", "experience compiler · review required", "var(--violet)"); VOICE.say("I've compiled a reusable skill from that run. It needs your approval."); }
    renderSkills();
  }
  function renderSkills() {
    $("#skills-meta").textContent = `${S.skills.filter(s => s.status === "CANDIDATE").length} awaiting review`;
    if (!S.skills.length) return;
    $("#skills-body").innerHTML = S.skills.slice().reverse().map(s => `<div class="skill ${s.status}">
      <div class="sk-h"><b>${esc(s.name)} <span style="color:var(--muted)">v${s.version || 1}</span></b><span class="sk-s">${s.status}</span></div>
      <div class="sk-d">${(s.steps || []).map(x => x.op).join(" → ")}</div>
      <div class="sk-btns"><button class="sk-view" data-skill="${s.id}">REVIEW</button></div></div>`).join("");
  }
  function openSkill(id) {
    const s = S.skills.find(x => x.id === id);
    if (!s) return;
    const steps = (s.steps || []).map((x, i) => `<li class="step e-${({ approach_tag: "motion", place_at: "motion", pick_tagged: "gripper" })[x.op] || "none"} s-queued" style="--i:${i}"><span class="dot">${i + 1}</span><div><div class="op"><b>${esc(x.op)}</b> <span class="args">${esc(JSON.stringify(x.args || {}))}</span></div></div><span class="st">STEP</span></li>`).join("");
    openDrawer("SKILL REVIEW", `
      <div class="src-id">${esc(s.id)} · ${esc(s.created_by || "experience compiler")}</div>
      <h3 style="margin:8px 0">${esc(s.name)} <span class="tag">${s.status}</span></h3>
      <div class="sec-h">PARAMETERS</div><pre>${esc(JSON.stringify(s.params || {}, null, 2))}</pre>
      <div class="sec-h">Steps (allowlisted only)</div><ol class="steps">${steps}</ol>
      <div class="sec-h">PRECONDITIONS</div><ul>${(s.preconditions || []).map(p => `<li>${esc(p)}</li>`).join("")}</ul>
      <div class="sec-h">POSTCONDITIONS</div><ul>${(s.postconditions || []).map(p => `<li>${esc(p)}</li>`).join("")}</ul>
      <div class="sec-h">Where this came from</div>${(s.evidence_ids || []).map(e => `<button class="cite" data-ev="${esc(e)}">${esc(e)}</button>`).join("") || "<span class='missing'>no evidence ids</span>"}
      <div class="perm" style="margin-top:14px"><h4>EFFECT OF APPROVING</h4><ul><li>Future matching requests reuse this routine without planning.</li><li>Every run still needs your per-run approval and passes the safety gate.</li><li>You can revoke it at any time.</li></ul></div>
      ${s.status === "CANDIDATE" ? `<div class="verify-row"><button class="v-ok" data-skill-decide="approve" data-id="${s.id}">APPROVE SKILL</button><button class="v-no" data-skill-decide="reject" data-id="${s.id}">REJECT</button></div>` : ""}
      ${s.status === "APPROVED" ? `<div class="verify-row"><button class="v-no" data-skill-decide="revoke" data-id="${s.id}">REVOKE</button></div>` : ""}`);
  }
  async function decideSkill(id, d) {
    try { await api("POST", `/skills/${id}/${d}`); closeDrawer(); if (d === "approve") { FX.play("success"); FX.banner("SKILL APPROVED", "published to memory · reusable", "var(--green)"); } }
    catch (e) { FX.toast(e.message, "red"); }
  }

  // -------------------------------------------------------------- scoreboard
  function renderScore() {
    // Only compare runs where every step succeeded (fix 5). A vetoed/failed/
    // partial run is not a fair baseline for the skill-vs-planner comparison.
    const ok = S.runs.filter(r => r.all_steps_succeeded === true);
    // Compare the latest skill-reuse run against the planned run before it (same task family).
    const runs = ok.filter(r => r.signature && r.signature.includes("place_at"));
    const pool = runs.length ? runs : ok;
    if (!pool.length) return;
    let b = [...pool].reverse().find(r => r.planner === "skill") || null;
    let a = b ? [...pool].reverse().find(r => r.planner !== "skill" && r.ts < b.ts) : null;
    if (!a || !b) { b = pool.length > 1 ? pool[pool.length - 1] : null; a = pool.length > 1 ? pool[pool.length - 2] : pool[0]; }
    const rows = [["planner", r => r.planner + (r.skill_id ? " ◆" : "")], ["planner calls", r => r.planner_calls], ["plan ms", r => r.plan_ms],
      ["exec ms", r => r.exec_ms], ["total ms", r => r.total_ms], ["steps", r => r.primitive_calls], ["verified by", r => r.verified_by]];
    let h = `<div class="sc-grid"><span></span><span class="hd">RUN 1</span><span class="hd">RUN 2</span>`;
    // Per-cell raw values (not stringified) so the count-up post-pass can tell
    // numeric metrics apart from string/placeholder cells. Keeps the rendered
    // markup, run-comparison logic, and row set unchanged (Req 15.5).
    const cellVals = [];
    rows.forEach(([k, f]) => {
      const av = f(a);
      const bv = b ? f(b) : "—";
      cellVals.push(av, bv);
      h += `<span class="k">${k}</span><span class="v">${esc(av)}</span><span class="v">${esc(bv)}</span>`;
    });
    h += `</div>`;
    if (b && a.plan_ms != null && b.plan_ms != null) {
      const d = a.plan_ms - b.plan_ms;
      h += `<div class="delta">${b.planner === "skill" ? "◆ run 2 reused approved skill · " : ""}planning ${d >= 0 ? "−" + d : "+" + -d} ms vs run 1 (measured)</div>`;
    }
    $("#score-body").innerHTML = h;

    // Count-up post-pass (Req 15): animate finite numeric cells from their
    // previously displayed value (or 0) up to the server value; non-numeric or
    // missing cells keep the already-rendered placeholder (Req 15.6).
    const cells = $("#score-body").querySelectorAll(".v");
    cells.forEach((el, i) => countUp(el, cellVals[i]));
  }

  // Animate a scoreboard `.v` cell up to `to` over `dur` ms (200–400 band) with
  // ease-out cubic, landing exactly on the server value (Reqs 15.1–15.3, 15.7).
  function countUp(el, to, dur = 320) {
    // Use the module-level REDUCED flag (single source of truth, kept live by
    // the matchMedia `change` listener) so no count-up runs and the value is
    // rendered immediately under reduced motion (Req 15.4).
    const reduced = REDUCED;
    // Non-numeric/missing metrics skip animation and keep the placeholder (Req 15.6).
    if (!isAnimatableMetric(to)) { el._cu = cancelAnimationFrame(el._cu); return; }
    if (reduced) {
      el._cu = cancelAnimationFrame(el._cu);
      el.textContent = to; el.dataset.val = to; // display server value immediately
      return;
    }
    // A new value mid-flight cancels the in-progress animation and restarts (Req 15.7).
    cancelAnimationFrame(el._cu);
    const from = Number.isFinite(parseFloat(el.dataset.val)) ? parseFloat(el.dataset.val) : 0;
    const t0 = performance.now();
    const step = (now) => {
      const tElapsed = now - t0;
      const v = countupValue(from, to, tElapsed, dur); // shared ease-out-cubic helper
      if (tElapsed < dur) {
        el.textContent = Math.round(v);
        el._cu = requestAnimationFrame(step);
      } else {
        el.textContent = to; el.dataset.val = to; // land exactly on server value (Req 15.3)
      }
    };
    el._cu = requestAnimationFrame(step);
  }

  // -------------------------------------------------------------- log
  function logEvent(e, quiet) {
    const map = {
      heard: () => [`heard (${e.source}): "${e.text}"`],
      parsed: () => [`parse → ${e.kind}${e.reason ? ": " + e.reason : ""}`, e.kind === "abstain" ? "err" : e.kind === "clarify" ? "warn" : ""],
      operation: () => [`${e.op.id} ${e.op.status}`, e.op.status === "VERIFIED" ? "good" : /FAIL|HALT|STOP|REJ|EXP/.test(e.op.status) ? "err" : ""],
      veto: () => ["LiDAR veto, motion blocked", "err"],
      stop: () => [`E-STOP by ${e.by}`, "err"],
      stop_clear: () => ["E-STOP cleared", "warn"],
      rule: () => [`rule ingested ${e.result && e.result.source_id || ""}`, "good"],
      skills: () => ["skills updated"],
      episode: () => [`episode ${e.episode_id} journaled`, "good"],
      prospective: () => [`prospective task ${e.task.id}`],
      prospective_fired: () => [`prospective fired: ${e.task.text}`, "warn"],
      log: () => [e.msg, e.level === "error" ? "err" : e.level === "warn" ? "warn" : ""],
      say: () => [`🗣 ${e.text}`],
    };
    const f = map[e.type];
    if (!f) return;
    const [msg, cls] = f();
    if (e.type === "operation" && !quiet) {
      const lastLi = $("#log li");
      if (lastLi && lastLi.dataset.k === e.op.id + e.op.status) return;
    }
    const li = document.createElement("li");
    li.className = cls || "";
    if (e.type === "operation") li.dataset.k = e.op.id + e.op.status;
    li.innerHTML = `<span class="t">${fmtT(e.ts || Date.now() / 1000)}</span><span>${esc(msg)}</span>`;
    const ol = $("#log");
    ol.prepend(li);
    while (ol.children.length > 120) ol.lastChild.remove();
  }

  setInterval(() => { $("#clock").textContent = new Date().toTimeString().slice(0, 8); }, 1000);
})();
