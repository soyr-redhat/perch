/* Perch — render loop. Vanilla on purpose: fetch + EventSource + DOM + xterm. */

const $ = (sel) => document.querySelector(sel);

const state = {
  snap: null,
  selected: null,
  quietOpen: localStorage.getItem("perch.quietOpen") === "1",
  connected: false,
  lastDetailKey: "",
  view: { kind: "agent" },   // {kind:'agent'} | {kind:'term', id}
  termsMap: {},              // id -> live xterm client
  hist: null,                // {agent, events, prompt, of} | null — prompt-jump view
};

const TERM_THEMES = {
  dark: { background: "#00000000", foreground: "#e8e8ea", cursor: "#52b588", selectionBackground: "rgba(82, 181, 136, 0.25)" },
  light: { background: "#00000000", foreground: "#2c2c2e", cursor: "#2e7d5b", selectionBackground: "rgba(46, 125, 91, 0.22)" },
};
const themeMode = () => (document.documentElement.dataset.theme === "light" ? "light" : "dark");
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem("perch.theme", mode);
  const btn = document.querySelector("#theme");
  if (btn) btn.textContent = mode === "light" ? "◐" : "◑";
  for (const o of Object.values(state.termsMap)) o.term.options.theme = TERM_THEMES[mode];
}

window.addEventListener("pywebviewready", () => document.body.classList.add("app"));

/* ---------- helpers ---------- */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function rel(ts) {
  if (!ts) return "—";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 5) return "now";
  if (s < 60) return `${Math.floor(s)}s`;
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h`;
  return `${Math.floor(h / 24)}d`;
}

function ago(ts) {
  const r = rel(ts);
  return r === "now" ? "just now" : r + " ago";
}

const tokens = (n) =>
  n == null ? null : n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);

const folder = (cwd) => (cwd ? cwd.replace(/[/\\]+$/, "").split(/[/\\]/).pop() : null);

const harness = (id) =>
  (state.snap?.harnesses || []).find((w) => w.id === id) ||
  (state.snap?.watching || []).find((w) => w.id === id) ||
  { id, name: id, color: "#0a84ff" };

/* Collapse runs of consecutive tool calls into one line: "write ×3" */
function compress(tail) {
  const evs = [];
  for (const e of tail || []) {
    const last = evs[evs.length - 1];
    if (e.who === "tool" && last && last.who === "tool") {
      last.names.push(e.text);
      last.ts = e.ts;
    } else {
      evs.push({ ...e, names: e.who === "tool" ? [e.text] : null });
    }
  }
  for (const e of evs) {
    if (!e.names) continue;
    const same = e.names.every((n) => n === e.names[0]);
    e.text = same && e.names.length > 1
      ? `${e.names[0]} ×${e.names.length}`
      : e.names.join(" · ");
  }
  return evs;
}

/* ---------- data ---------- */

function apply(snap) {
  state.snap = snap;
  const agents = snap.agents || [];
  if (!agents.some((a) => a.id === state.selected)) {
    const live = agents.find((a) => a.state !== "quiet");
    state.selected = (live || agents[0] || {}).id || null;
    state.lastDetailKey = "";
  }
  // drop local terminals the server forgot
  const ids = new Set((snap.terms || []).map((t) => t.id));
  for (const id of Object.keys(state.termsMap)) {
    if (!ids.has(id)) disposeTerm(id);
  }
  render();
}

async function boot() {
  try {
    apply(await (await fetch("/api/snapshot")).json());
  } catch { /* eventsource below retries the stream */ }
  const es = new EventSource("/api/events");
  es.onopen = () => { state.connected = true; renderConn(); };
  es.onerror = () => { state.connected = false; renderConn(); };
  es.onmessage = (e) => apply(JSON.parse(e.data));
}

/* ---------- top chrome ---------- */

function render() {
  if (!state.snap) return;
  renderConn();
  renderPill();
  renderSidebar();
  renderDetail();
  renderWatchbar();
}

function renderConn() {
  $("#conn").className = "conn " + (state.connected ? "on" : "off");
  $("#conn").title = state.connected ? "live" : "reconnecting…";
}

function renderPill() {
  const agents = state.snap.agents || [];
  const live = agents.filter((a) => a.state !== "quiet").length;
  const hot = agents.some((a) => a.state === "working");
  const pill = $("#live-pill");
  pill.textContent = live ? `${live} live` : "quiet";
  pill.classList.toggle("hot", hot);
}

/* ---------- sidebar ---------- */

function row(a) {
  const h = harness(a.harness);
  const dim = a.state === "quiet" ? " dim" : "";
  const sel = a.id === state.selected && state.view.kind === "agent" ? " sel" : "";
  return `<div class="row${dim}${sel}" style="--hc:${h.color}" data-id="${esc(a.id)}" tabindex="0" role="option" aria-selected="${!!sel}">
    <div class="row-top">
      <span class="dot ${a.state}"></span>
      <span class="row-title">${esc(a.title)}</span>
      <span class="row-time" data-ts="${esc(a.updated || a.mtime)}">${rel(a.updated || a.mtime)}</span>
    </div>
    <div class="row-sub">
      <span class="chip">${esc(h.name)}</span>
      ${a.cwd ? `<span class="row-folder">${esc(folder(a.cwd))}</span>` : ""}
    </div>
  </div>`;
}

function renderSidebar() {
  const agents = state.snap.agents || [];
  const live = agents.filter((a) => a.state !== "quiet");
  const quiet = agents.filter((a) => a.state === "quiet");
  const canSpawn = (state.snap.harnesses || []).some((h) => h.canSpawn);

  let html = `<div class="section-label">Live${canSpawn ? `<span class="plus" id="plus" title="new agent">＋</span>` : ""}</div>`;
  html += live.length
    ? live.map(row).join("")
    : `<div class="empty-note">No agents up. Start one anywhere — or press ＋. It lands here within seconds.</div>`;

  if (quiet.length) {
    const closed = state.quietOpen ? "" : " closed";
    html += `<div class="section-label toggle${closed}" id="quiet-toggle">
        <span class="chev">▾</span> Quiet · ${quiet.length}</div>`;
    if (state.quietOpen) html += quiet.map(row).join("");
  }

  const sb = $("#sidebar");
  sb.innerHTML = html;
  sb.querySelectorAll(".row").forEach((el) =>
    el.addEventListener("click", () => {
      state.selected = el.dataset.id;
      state.view = { kind: "agent" };
      state.hist = null;
      state.lastDetailKey = "";
      renderSidebar();
      renderDetail(true);
    }));
  $("#quiet-toggle")?.addEventListener("click", () => {
    state.quietOpen = !state.quietOpen;
    localStorage.setItem("perch.quietOpen", state.quietOpen ? "1" : "0");
    renderSidebar();
  });
  $("#plus")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openSpawnPop(e.currentTarget);
  });
}

/* ---------- detail: tabs + agent + terminal ---------- */

function tabsHtml() {
  const terms = state.snap.terms || [];
  if (!terms.length) return "";
  const tabs = [
    `<div class="tab${state.view.kind === "agent" ? " on" : ""}" data-view="agent">Activity</div>`,
  ];
  for (const t of terms) {
    const on = state.view.kind === "term" && state.view.id === t.id;
    tabs.push(`<div class="tab${on ? " on" : ""}" data-term="${t.id}" style="--tc:${t.color}">
      <span class="dot" style="background:${t.color}"></span>${esc(t.name)} · ${esc(folder(t.cwd) || "~")}
      ${t.alive ? "" : `<span class="dead">exited</span>`}
      <span class="x" data-kill="${t.id}" title="close terminal">×</span>
    </div>`);
  }
  return `<div class="tabs">${tabs.join("")}</div>`;
}

function bindTabs() {
  document.querySelectorAll(".tab[data-view]").forEach((el) =>
    el.addEventListener("click", () => {
      state.view = { kind: "agent" };
      state.lastDetailKey = "";
      renderDetail(true);
    }));
  document.querySelectorAll(".tab[data-term]").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.dataset.kill) return;
      state.view = { kind: "term", id: el.dataset.term };
      state.lastDetailKey = "";
      renderDetail(true);
    }));
  document.querySelectorAll("[data-kill]").forEach((el) =>
    el.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = el.dataset.kill;
      await fetch("/api/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      disposeTerm(id);
      const idx = (state.snap.terms || []).findIndex((t) => t.id === id);
      if (idx >= 0) state.snap.terms.splice(idx, 1);
      if (state.view.kind === "term" && state.view.id === id) state.view = { kind: "agent" };
      state.lastDetailKey = "";
      render();
    }));
}

function renderDetail(force) {
  const detail = $("#detail");
  const terms = state.snap.terms || [];
  let termInfo = null;
  if (state.view.kind === "term") {
    termInfo = terms.find((t) => t.id === state.view.id);
    if (!termInfo) state.view = { kind: "agent" };
  }
  const agent = (state.snap.agents || []).find((x) => x.id === state.selected);
  const hist = agent && state.hist && state.hist.agent === agent.id ? state.hist : null;
  const pend = agent && !hist ? (state.snap.pending || {})[agent.id] || [] : [];
  const key = JSON.stringify({ v: state.view, a: agent, t: terms, s: state.connected, p: pend, h: hist });
  if (!force && key === state.lastDetailKey) return;
  state.lastDetailKey = key;

  // a re-render every snapshot must not steal the reply box mid-keystroke
  const prevReply = detail.querySelector("#reply");
  const replyVal = prevReply ? prevReply.value : "";
  const replyFocused = document.activeElement === prevReply;

  if (state.view.kind === "term" && termInfo) {
    detail.innerHTML = tabsHtml() + `
      <div class="termwrap">
        ${termInfo.alive ? "" : `<div class="term-banner">process exited${termInfo.exit != null ? " · code " + termInfo.exit : ""} — close the tab with ×</div>`}
        <div class="termmount"></div>
      </div>`;
    bindTabs();
    const obj = termClient(termInfo.id);
    const mount = detail.querySelector(".termmount");
    mount.appendChild(obj.el);
    if (!obj.opened) {
      obj.term.open(obj.el);
      obj.opened = true;
    }
    requestAnimationFrame(() => {
      try { obj.fit.fit(); obj.term.focus(); } catch { /* hidden */ }
    });
    return;
  }

  if (!agent) {
    detail.innerHTML = tabsHtml() + `<div class="detail-empty">
      <span class="dot working"></span>
      <h2>Nothing selected</h2>
      <p>Pick an agent on the left to watch it work.</p>
    </div>`;
    bindTabs();
    return;
  }

  const h = harness(agent.harness);
  const label = { working: "working", waiting: "waiting", quiet: "quiet" }[agent.state];
  const chips = [
    agent.cwd && ["cwd", agent.cwd],
    agent.model && ["model", agent.model],
    tokens(agent.tokens) && ["tokens", tokens(agent.tokens)],
    agent.started && ["started", ago(agent.started)],
    ["active", ago(agent.updated || agent.mtime)],
  ].filter(Boolean);
  const canResume = h.canResume && agent.state === "quiet";

  detail.innerHTML = tabsHtml() + `
    <div class="detail-head" style="--hc:${h.color}">
      <div class="detail-title">${esc(agent.title)}</div>
      <div class="status-pill ${agent.state}"><span class="dot ${agent.state}"></span>${label}</div>
      ${canResume ? `<button class="resume" id="resume" title="open this session in a live terminal">Resume in terminal</button>` : ""}
    </div>
    <div class="meta">
      ${chips.map(([k, v]) => `<span class="meta-chip"><span class="k">${k}</span><span class="v">${esc(v)}</span></span>`).join("")}
    </div>
    <div class="feed-label">Activity</div>
    ${hist ? `<div class="histbar">viewing prompt ${hist.prompt + 1} of ${hist.of}<button class="histx" id="histx">back to live</button></div>` : ""}
    <div class="feedwrap">
    <div class="feed" style="--hc:${h.color}">
      ${(hist ? hist.events : compress(agent.tail)).map((e) => `
        <div class="ev ${esc(e.who)}">
          <span class="dot"></span>
          <span class="ev-text">${esc(e.text)}</span>
          <span class="ev-time" data-ts="${esc(e.ts || "")}">${e.ts ? rel(e.ts) : ""}</span>
        </div>`).join("") || `<div class="empty-note">No recorded activity yet.</div>`}
      ${pend.map((p) => `
        <div class="ev user pending">
          <span class="dot"></span>
          <span class="ev-text">${esc(p.text)}${p.error
            ? `<span class="perr">✕ ${esc(p.error)}</span>`
            : `<span class="psub">sending…</span>`}</span>
          <span class="ev-time">${rel(p.ts)}</span>
        </div>`).join("")}
    </div>
    <div class="rail" id="rail"></div>
    </div>
    ${h.canMessage ? `
    <div class="replybar">
      <input class="reply" id="reply" placeholder="Reply to ${esc(h.name)}…" spellcheck="false" autocomplete="off">
      <button class="send" id="send">Send</button>
    </div>` : ""}`;
  bindTabs();

  const feed = detail.querySelector(".feed");
  if (!hist) feed.scrollTop = feed.scrollHeight;
  buildRail(agent);
  $("#histx")?.addEventListener("click", () => {
    state.hist = null;
    state.lastDetailKey = "";
    renderDetail(true);
  });

  if (canResume) {
    $("#resume").addEventListener("click", () =>
      spawn(agent.harness, agent.cwd || "", agent.id.split(":").slice(1).join(":")));
  }

  const reply = $("#reply");
  if (reply) {
    const send = () => {
      const text = reply.value.trim();
      if (!text) return;
      reply.value = "";
      fetch("/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: agent.id, text }),
      }).then((r) => r.json()).then((d) => { if (d.error) toast(d.error); });
    };
    reply.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
      e.stopPropagation();
    });
    $("#send").addEventListener("click", send);
    if (replyFocused) {
      reply.value = replyVal;
      reply.focus();
      reply.setSelectionRange(replyVal.length, replyVal.length);
    } else if (replyVal) {
      reply.value = replyVal;
    }
  }
}

function toast(msg) {
  const note = document.createElement("div");
  note.className = "toast";
  note.textContent = msg;
  document.body.appendChild(note);
  setTimeout(() => note.remove(), 5000);
}

function syncSummary(r) {
  if (r.error) return "sync failed: " + r.error;
  const s = r.skills || {}, m = r.mcp || {};
  const linked = (s.linked || []).length
    ? s.linked.map((l) => `${l.skill} → ${l.into}`).join(", ")
    : "skills already unified";
  const mcp = m.found
    ? `mcp: ${m.found} server(s) unified`
    : "mcp: nothing configured yet — add a server to any harness, then sync";
  const conf = (s.conflicts?.length || 0) + (m.conflicts?.length || 0);
  return `${linked} · ${mcp}${conf ? ` · ${conf} conflict(s) skipped` : ""}`;
}

/* prompt-jump rail: one tick per prompt in the whole session; click opens
   that prompt's slice of history. Codex-style navigator. */
function buildRail(agent) {
  const rail = document.querySelector("#rail");
  if (!rail) return;
  rail.innerHTML = "";
  const prompts = agent?.prompts || [];
  if (prompts.length < 2) return;
  prompts.forEach((p, i) => {
    const tick = document.createElement("div");
    tick.className = "tick" + (state.hist && state.hist.agent === agent.id && state.hist.prompt === i ? " on" : "");
    tick.style.top = (i / (prompts.length - 1)) * 100 + "%";
    tick.title = p.text;
    tick.addEventListener("click", () => loadHistory(agent, i));
    rail.appendChild(tick);
  });
}

async function loadHistory(agent, i) {
  const r = await fetch(`/api/history?agent=${encodeURIComponent(agent.id)}&prompt=${i}`);
  const d = await r.json();
  if (d.error) { toast(d.error); return; }
  state.hist = { agent: agent.id, events: d.events, prompt: d.prompt, of: d.of };
  state.lastDetailKey = "";
  renderDetail(true);
}

/* ---------- terminals ---------- */

function termClient(id) {
  if (state.termsMap[id]) return state.termsMap[id];
  const el = document.createElement("div");
  el.className = "termel";
  const term = new Terminal({
    fontFamily: 'ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace',
    fontSize: 12.5,
    cursorBlink: true,
    allowTransparency: true,
    scrollback: 4000,
        theme: TERM_THEMES[themeMode()],
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);

  const obj = { term, fit, el, ws: null, opened: false };
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/term/${id}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (obj.opened) ws.send(JSON.stringify({ type: "resize", cols: obj.term.cols, rows: obj.term.rows }));
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      try {
        if (JSON.parse(e.data).type === "exit") renderDetail(true);
      } catch { /* not control */ }
      return;
    }
    term.write(new Uint8Array(e.data));
    if (!obj.cleaned) {
      // first paint came from a backlog drawn at the PTY's spawn size; refit and
      // let alt-screen TUIs repaint clean at the real size
      obj.cleaned = true;
      setTimeout(() => {
        try { obj.fit.fit(); } catch { /* hidden */ }
        if (obj.term.buffer.active.type === "alternate") obj.term.clear();
      }, 250);
    }
  };
  term.onData((d) => { if (ws.readyState === 1) ws.send(d); });
  term.onResize(({ cols, rows }) => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows }));
  });
  obj.ws = ws;
  state.termsMap[id] = obj;
  return obj;
}

function disposeTerm(id) {
  const obj = state.termsMap[id];
  if (!obj) return;
  try { obj.ws && obj.ws.close(); } catch { /* gone */ }
  try { obj.term.dispose(); } catch { /* gone */ }
  delete state.termsMap[id];
}

/* refit the visible terminal whenever the layout shifts */
new ResizeObserver(() => {
  buildRail(document.querySelector(".feed"));
  if (state.view.kind !== "term") return;
  const obj = state.termsMap[state.view.id];
  if (obj && obj.opened) {
    try { obj.fit.fit(); } catch { /* hidden */ }
  }
}).observe(document.body);

/* ---------- spawn popover ---------- */

function defaultCwd(hid) {
  const a = (state.snap.agents || []).find((x) => x.harness === hid && x.cwd);
  return a ? a.cwd : "";
}

function closePop() {
  $("#pop")?.remove();
}

function openSpawnPop(anchor) {
  closePop();
  const hs = (state.snap.harnesses || []).filter((h) => h.canSpawn);
  if (!hs.length) return;
  const rect = anchor.getBoundingClientRect();
  const pop = document.createElement("div");
  pop.className = "pop";
  pop.id = "pop";
  pop.innerHTML = `
    <div class="pop-title">New agent</div>
    ${hs.map((h, i) => `
      <label class="pop-h">
        <input type="radio" name="ph" value="${h.id}"${i === 0 ? " checked" : ""}>
        <span class="dot" style="background:${h.color}"></span>${esc(h.name)}
      </label>`).join("")}
    <input class="pop-cwd" id="pop-cwd" placeholder="working folder" spellcheck="false">
    <button class="pop-go" id="pop-go">Launch</button>`;
  document.body.appendChild(pop);
  pop.style.left = Math.min(rect.left, innerWidth - 262) + "px";
  pop.style.top = rect.bottom + 8 + "px";

  const cwd = $("#pop-cwd");
  const picked = () => pop.querySelector("input[name=ph]:checked").value;
  cwd.value = defaultCwd(picked());
  pop.querySelectorAll("input[name=ph]").forEach((r) =>
    r.addEventListener("change", () => { cwd.value = defaultCwd(picked()); }));
  $("#pop-go").addEventListener("click", () => {
    closePop();
    spawn(picked(), cwd.value.trim(), null);
  });
  cwd.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { closePop(); spawn(picked(), cwd.value.trim(), null); }
    e.stopPropagation();
  });
  setTimeout(() => document.addEventListener("click", function h(ev) {
    if (!pop.contains(ev.target)) { closePop(); document.removeEventListener("click", h); }
  }), 0);
}

async function spawn(hid, cwd, session) {
  const r = await fetch("/api/spawn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ harness: hid, cwd, session }),
  });
  const d = await r.json();
  if (d.term) {
    state.snap.terms = state.snap.terms || [];
    if (!state.snap.terms.some((t) => t.id === d.term.id)) state.snap.terms.push(d.term);
    state.view = { kind: "term", id: d.term.id };
    state.lastDetailKey = "";
    render();
  } else {
    toast(d.error || "could not launch");
  }
}

/* ---------- watchbar ---------- */

function renderWatchbar() {
  const items = (state.snap.watching || [])
    .map((w) => {
      const bits = [];
      if (w.files) bits.push(`${w.files} session${w.files === 1 ? "" : "s"}`);
      if (w.processes) bits.push(`${w.processes} proc${w.processes === 1 ? "" : "s"}`);
      return `<span class="watch"><span class="dot" style="background:${w.color}"></span>${esc(w.name)}<span class="n">${bits.join(" · ") || "—"}</span></span>`;
    })
    .join("");
  $("#watchbar").innerHTML =
    items + `<span class="spacer"></span><button class="syncbtn" id="sync" title="share skills and MCP servers across every harness">⇅ sync</button><span class="hint">add any harness via sources.json</span>`;
  $("#sync")?.addEventListener("click", async () => {
    const d = await (await fetch("/api/sync", { method: "POST" })).json();
    toast(syncSummary(d));
  });
}

/* ---------- settings sheet ---------- */

const patchSettings = (body) =>
  fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

async function openSettings() {
  const d = await (await fetch("/api/settings")).json();
  if (d.error) { toast(d.error); return; }
  const s = d.settings;
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `
  <div class="sheet" role="dialog" aria-label="Settings">
    <div class="sheet-head"><span>Settings</span><button class="sheet-x" id="sheet-x">×</button></div>
    <div class="sheet-body">
      <div class="set-group">Appearance</div>
      <div class="set-card">
        <div class="set-row"><span>Theme <span class="set-sub">this browser</span></span>
          <span class="seg" id="set-theme">
            <button data-th="dark" class="${themeMode() === "dark" ? "on" : ""}">Dark</button>
            <button data-th="light" class="${themeMode() === "light" ? "on" : ""}">Light</button>
          </span>
        </div>
      </div>
      <div class="set-group">Watching</div>
      <div class="set-card">
        <div class="set-row"><span>Keep silent sessions listed</span>
          <span class="numwrap"><input type="number" id="set-quiet" min="0.5" max="30" step="0.5" value="${s.watching.quietDays}"> days</span>
        </div>
      </div>
      <div class="set-group">Harnesses</div>
      <div class="set-card">
        ${d.harnesses.map((h) => `
        <div class="set-row">
          <span class="set-h"><span class="dot" style="background:${h.color}"></span>${esc(h.name)}
            <span class="set-sub">${h.installed ? esc(h.patterns[0] || "") : "cli not found"}</span></span>
          <span class="switch${h.enabled ? " on" : ""}" data-h="${h.id}" role="switch" aria-checked="${h.enabled}"></span>
        </div>`).join("")}
      </div>
      <div class="set-group">Sharing</div>
      <div class="set-card">
        <div class="set-row"><span>Sync skills</span><span class="switch${s.sharing.skills ? " on" : ""}" data-sh="skills"></span></div>
        <div class="set-row"><span>Sync MCP servers</span><span class="switch${s.sharing.mcp ? " on" : ""}" data-sh="mcp"></span></div>
        <div class="set-row"><span>Sync on launch</span><span class="switch${s.sharing.autoSync ? " on" : ""}" data-sh="autoSync"></span></div>
        <div class="set-row"><span>Push skills into</span>
          <span class="seg" id="set-targets">
            ${["claude", "codex"].map((t) => `<button data-tg="${t}" class="${s.sharing.targets.includes(t) ? "on" : ""}">${t}</button>`).join("")}
          </span>
        </div>
        <div class="set-row"><span></span><button class="pop-go" id="set-sync">Sync now</button></div>
        <div class="set-note">${d.lastSync
          ? `last sync ${ago(d.lastSync.ts)} · ${d.lastSync.skills.linked.length} skill(s) linked · ${d.lastSync.mcp.found} mcp server(s)`
          : "never synced yet"}</div>
      </div>
      ${d.mcp.length ? `
      <div class="set-group">MCP servers · union</div>
      <div class="set-card">
        ${d.mcp.map((m) => `
        <div class="set-row"><span class="set-h"><span class="dot" style="background:var(--accent)"></span>${esc(m.name)}
          <span class="set-sub">${m.transport} · from ${esc(m.origin)}</span></span>
          <span class="set-sub">${m.presentIn.join(" · ")}</span></div>`).join("")}
      </div>` : ""}
      <div class="set-files">${esc(d.files.settings)}<br>${esc(d.files.sources)}</div>
    </div>
  </div>`;
  document.body.appendChild(ov);

  const close = () => { ov.remove(); document.removeEventListener("keydown", escClose); };
  const escClose = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", escClose);
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  ov.querySelector("#sheet-x").addEventListener("click", close);

  ov.querySelectorAll("#set-theme button").forEach((b) =>
    b.addEventListener("click", () => {
      applyTheme(b.dataset.th);
      ov.querySelectorAll("#set-theme button").forEach((x) => x.classList.toggle("on", x === b));
    }));

  ov.querySelector("#set-quiet").addEventListener("change", (e) =>
    patchSettings({ watching: { quietDays: parseFloat(e.target.value) } }));

  ov.querySelectorAll(".switch").forEach((sw) =>
    sw.addEventListener("click", () => {
      sw.classList.toggle("on");
      const on = sw.classList.contains("on");
      sw.setAttribute("aria-checked", on);
      if (sw.dataset.h) {
        const disabled = [...ov.querySelectorAll(".switch[data-h]")]
          .filter((x) => !x.classList.contains("on")).map((x) => x.dataset.h);
        patchSettings({ harnesses: { disabled } });
      } else if (sw.dataset.sh) {
        patchSettings({ sharing: { [sw.dataset.sh]: on } });
      }
    }));

  ov.querySelectorAll("#set-targets button").forEach((b) =>
    b.addEventListener("click", () => {
      b.classList.toggle("on");
      const targets = [...ov.querySelectorAll("#set-targets button.on")].map((x) => x.dataset.tg);
      patchSettings({ sharing: { targets } });
    }));

  ov.querySelector("#set-sync").addEventListener("click", async () => {
    const r = await (await fetch("/api/sync", { method: "POST" })).json();
    toast(syncSummary(r));
    close();
  });
}

/* ---------- keyboard ---------- */

addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePop();
  if (state.view.kind === "term") return;          // terminal owns the keys
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const agents = (state.snap?.agents || []).filter(
    (a) => a.state !== "quiet" || state.quietOpen);
  if (!agents.length) return;
  e.preventDefault();
  const i = agents.findIndex((a) => a.id === state.selected);
  const next = agents[Math.min(agents.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))];
  if (next && next.id !== state.selected) {
    state.selected = next.id;
    state.view = { kind: "agent" };
    state.hist = null;
    state.lastDetailKey = "";
    renderSidebar();
    renderDetail(true);
  }
});

/* ---------- clock ---------- */

setInterval(() => {
  document.querySelectorAll("[data-ts]").forEach((el) => {
    const ts = el.dataset.ts;
    if (ts) el.textContent = rel(ts.includes("-") ? ts : Number(ts));
  });
}, 10000);

document.querySelector("#theme").addEventListener("click", () =>
  applyTheme(themeMode() === "light" ? "dark" : "light"));
document.querySelector("#gear").addEventListener("click", openSettings);
applyTheme(themeMode());

boot();
