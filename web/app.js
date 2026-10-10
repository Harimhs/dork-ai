"use strict";

const $ = (s, root = document) => root.querySelector(s);
const state = { convs: [], current: null, currentUpdated: 0, streaming: null, profile: null, status: null };
const myDevice = () => localStorage.getItem("dork-device-name") || "";

// ---------- icons (stroke, 24 grid) ----------
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h10"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5L18 7M9 7V4.5h6V7"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
  devices: '<rect x="2.5" y="4.5" width="14" height="10" rx="1.5"/><path d="M6.5 18.5h6"/><rect x="18" y="9" width="4" height="10.5" rx="1"/>',
  upload: '<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2"/><path d="M15.5 8.5V5.5A1.5 1.5 0 0 0 14 4H5.5A1.5 1.5 0 0 0 4 5.5V14a1.5 1.5 0 0 0 1.5 1.5h3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  logout: '<path d="M14 4h4.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H14M10 16.5 14.5 12 10 7.5M14.5 12H4"/>',
  download: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15"/>',
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/>',
  speaker: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  edit: '<path d="M14.5 5.5 18.5 9.5M4 20l1-4.5L15.5 5a1.4 1.4 0 0 1 2 0l1.5 1.5a1.4 1.4 0 0 1 0 2L8.5 19 4 20z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  settings: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  reactor: '<circle cx="12" cy="12" r="2.5"/><circle cx="12" cy="12" r="6.5" stroke-dasharray="3 2.2"/><circle cx="12" cy="12" r="9.5"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
function hydrateIcons(root = document) {
  root.querySelectorAll("i[data-icon]").forEach((el) => { el.outerHTML = icon(el.dataset.icon); });
}
hydrateIcons();

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/login") && !path.startsWith("/api/pair/verify")) { showLock(); throw new Error("locked"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

// ---------- text ----------
// Emotion cues Dork adds to spoken replies, e.g. [laughs] — shown as a quiet tag, performed by the voice.
const VOICE_CUE = /\[((?:laugh|chuckl|giggl|sigh|excited|happy|cheerful|thinking|hmm|annoyed|irritated|angry|sad|softly|whisper|surprised|teasing|serious|warmly)[^\]]{0,30})\]/gi;
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\[(\d{1,2})\](?!\()/g, '<span class="cite" data-n="$1">$1</span>')
    .replace(VOICE_CUE, '<span class="cue">$1</span>');  // [laughs] → a quiet "laughs" tag
}
function blocks(text) {
  const out = []; let list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const line of text.split("\n")) {
    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) { flushPara(); flushList(); out.push(`<h3>${inline(m[2])}</h3>`); continue; }
    if ((m = line.match(/^\s*([-*•]|\d+[.)])\s+(.*)/))) {
      flushPara();
      const tag = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(m[2]); continue;
    }
    flushList(); para.push(line);
  }
  flushPara(); flushList();
  return out.join("");
}
function md(src) {
  return src.split("```").map((part, i) => {
    if (i % 2 === 0) return blocks(part);
    const nl = part.indexOf("\n");
    return `<pre><code>${esc(nl >= 0 ? part.slice(nl + 1) : part)}</code></pre>`;
  }).join("");
}
function linkCitations(el, sources) {
  const byN = Object.fromEntries((sources || []).map((s) => [String(s.n), s]));
  if (sources?.length) {
    // In a web-sourced answer, links the model wrote itself are often guessed: keep only
    // links to pages Dork actually read; the rest become plain text (the [n] chips are the real links).
    const real = new Set(sources.map((s) => s.url.replace(/\/$/, "")));
    el.querySelectorAll("a:not(.cite)").forEach((a) => {
      if (!real.has(a.href.replace(/\/$/, ""))) a.replaceWith(document.createTextNode(a.textContent));
    });
  }
  el.querySelectorAll("span.cite").forEach((c) => {
    const s = byN[c.dataset.n];
    if (!s) return;
    const a = document.createElement("a");
    a.className = "cite"; a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.textContent = c.textContent; a.title = `${s.title}\n${s.site} · ${s.age}`;
    c.replaceWith(a);
  });
}
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
};
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
function shortWhen(t) {
  const d = new Date(t * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days <= 0) return clock(t);
  if (days === 1) return "yday";
  if (days < 7) return d.toLocaleDateString([], { weekday: "short" }).toLowerCase();
  return d.toLocaleDateString([], { day: "numeric", month: "short" }).toLowerCase();
}

// ---------- lock / boot ----------
// A new device connects with a 6-digit code shown on a signed-in device; the master key is the fallback.
let pairRequest = null;
function lockMode(mode) {  // "connect" | "code" | "key"
  for (const [m, id] of [["connect", "#lock-connect"], ["code", "#lock-code"], ["key", "#lock-form"]]) {
    $(id).classList.toggle("hidden", m !== mode);
  }
  $("#lock-switch").textContent = mode === "key" ? "Connect with a code instead" : "Use master key instead";
  $("#lock-error").textContent = "";
  ($(mode === "key" ? "#lock-key" : mode === "code" ? "#lock-code-input" : "#lock-device")).focus();
}
function showLock() {
  $("#app").classList.add("hidden");
  $("#lock").classList.remove("hidden");
  $("#retry").classList.add("hidden");
  $("#lock-switch").classList.remove("hidden");
  lockMode(pairRequest ? "code" : "connect");
}
const deviceName = () => $("#lock-device").value.trim() || "Device";
$("#lock-connect").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    pairRequest = await api("/api/pair/request", { method: "POST", body: { device_name: deviceName() } });
    localStorage.setItem("dork-device-name", deviceName());
    $("#lock-code-input").value = "";
    lockMode("code");
  } catch (err) { $("#lock-error").textContent = err.message; }
});
$("#lock-code").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#lock-error").textContent = "";
  try {
    await api("/api/pair/verify", { method: "POST", body: { request_id: pairRequest.request_id, code: $("#lock-code-input").value } });
    pairRequest = null;
    boot();
  } catch (err) {
    $("#lock-error").textContent = err.message;
    if (/expired|start again|new one/i.test(err.message)) { pairRequest = null; setTimeout(() => lockMode("connect"), 1800); }
  }
});
$("#lock-restart").onclick = () => { pairRequest = null; lockMode("connect"); };
$("#lock-switch").onclick = () => lockMode($("#lock-form").classList.contains("hidden") ? "key" : (pairRequest ? "code" : "connect"));
async function boot() {
  try {
    const me = await api("/api/auth/me");
    state.profile = me.profile;
    $("#lock").classList.add("hidden");
    $("#app").classList.remove("hidden");
    await Promise.all([loadConvs(), refreshStatus()]);
    // Links from the voice assistant: /?conv=<id> (open that thread), /?settings=1 or =memory
    const q = new URLSearchParams(location.search);
    if (location.search) history.replaceState(null, "", "/");
    if (q.get("conv") && state.convs.some((c) => c.id === q.get("conv"))) await openConv(q.get("conv"));
    else newChat();  // every visit starts fresh; earlier threads are one click (or Ctrl+K) away
    if (q.get("settings")) { openPanel("#settings-panel"); showTab(q.get("settings") === "1" ? "profile" : q.get("settings")); }
  } catch (e) {
    if (e.message === "locked") return;
    // Installed app opened while the Dork PC is off / unreachable: say so instead of a blank screen.
    $("#app").classList.add("hidden");
    $("#lock").classList.remove("hidden");
    for (const id of ["#lock-connect", "#lock-code", "#lock-form", "#lock-switch"]) $(id).classList.add("hidden");
    $("#lock-error").textContent = "Can't reach Dork. Check the Dork PC is on and awake, and Tailscale is connected on this device.";
    $("#retry").classList.remove("hidden");
  }
}
$("#retry").onclick = () => { $("#retry").classList.add("hidden"); $("#lock-error").textContent = ""; boot(); };
$("#lock-device").value = myDevice() || (/Android|iPhone|iPad/i.test(navigator.userAgent) ? "Phone" : "Laptop");
$("#lock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#lock-error").textContent = "";
  const device_name = deviceName();
  try {
    await api("/api/auth/login", { method: "POST", body: { key: $("#lock-key").value, device_name } });
    localStorage.setItem("dork-device-name", device_name);
    $("#lock-key").value = "";
    boot();
  } catch (err) { $("#lock-error").textContent = err.message; }
});

// ---------- threads ----------
async function loadConvs() { state.convs = await api("/api/conversations"); renderConvs(); }
function renderConvs() {
  const nav = $("#conv-list"); nav.innerHTML = "";
  if (!state.convs.length) nav.innerHTML = `<p class="hint" style="padding:8px 10px">No threads yet.</p>`;
  for (const c of state.convs) {
    const el = document.createElement("div");
    el.className = "conv" + (c.id === state.current?.id ? " active" : "");
    el.innerHTML = `<span class="t"></span><span class="when"></span><button class="del" title="Delete thread">${icon("trash")}</button>`;
    $(".t", el).textContent = c.title;
    $(".when", el).textContent = shortWhen(c.updated_at);
    el.onclick = () => { openConv(c.id); closeNav(); };
    $(".del", el).onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`Delete "${c.title}"?`)) return;
      await api(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (state.current?.id === c.id) newChat();
      loadConvs();
    };
    nav.appendChild(el);
  }
}
async function openConv(id) {
  const conv = await api(`/api/conversations/${id}`);
  state.current = conv; state.currentUpdated = conv.updated_at;
  $("#conv-title").textContent = conv.title;
  renderMessages(conv.messages);
  renderConvs();
}
function newChat() {
  state.current = null; state.currentUpdated = 0;
  $("#conv-title").textContent = "New thread";
  renderMessages([]); renderConvs(); closeNav();
  $("#input").focus();
}
$("#new-chat").onclick = newChat;
$("#new-chat-top").onclick = newChat;
$("#conv-title").onclick = async () => {
  if (!state.current) return;
  const title = prompt("Rename thread", state.current.title);
  if (!title) return;
  await api(`/api/conversations/${state.current.id}`, { method: "PATCH", body: { title } });
  state.current.title = title; $("#conv-title").textContent = title; loadConvs();
};

// ---------- transcript ----------
function greeting() {
  const h = new Date().getHours();
  const part = h < 5 ? "Up late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  const name = state.profile?.call_me || state.profile?.name;
  return name ? `${part}, ${name}.` : `${part}.`;
}
// ---------- new-thread greeting: time-of-day sky + a line Dork writes from recent context ----------
function partOfDay() {
  const h = new Date().getHours();
  return h < 5 ? "night" : h < 12 ? "morning" : h < 16 ? "noon" : h < 21 ? "evening" : "night";
}
const SKY = {
  morning: `<line class="horizon" x1="4" y1="40" x2="92" y2="40"/>
    <g class="sun rise"><circle cx="34" cy="30" r="8"/>
      <g class="rays"><line x1="34" y1="14" x2="34" y2="10"/><line x1="22" y1="19" x2="19.5" y2="16.5"/><line x1="46" y1="19" x2="48.5" y2="16.5"/></g></g>`,
  noon: `<line class="horizon" x1="4" y1="40" x2="92" y2="40"/>
    <g class="sun"><circle cx="48" cy="20" r="8"/>
      <g class="rays spin">${[0, 45, 90, 135, 180, 225, 270, 315].map((a) => `<line x1="48" y1="7" x2="48" y2="4" transform="rotate(${a} 48 20)"/>`).join("")}</g></g>`,
  evening: `<clipPath id="above"><rect x="0" y="0" width="96" height="40"/></clipPath>
    <g clip-path="url(#above)"><circle class="sun set" cx="64" cy="34" r="9"/></g>
    <line class="horizon" x1="4" y1="40" x2="92" y2="40"/>
    <line class="cloud" x1="14" y1="24" x2="30" y2="24"/><line class="cloud c2" x1="20" y1="30" x2="40" y2="30"/>`,
  night: `<mask id="crescent"><rect width="96" height="48" fill="white"/><circle cx="66" cy="15" r="8" fill="black"/></mask>
    <circle class="moon" cx="61" cy="19" r="9" mask="url(#crescent)"/>
    ${[[18, 12, 0], [32, 26, 0.8], [80, 30, 1.6], [42, 8, 2.4]].map(([x, y, d]) => `<circle class="star" cx="${x}" cy="${y}" r="1.4" style="animation-delay:${d}s"/>`).join("")}
    <line class="horizon" x1="4" y1="40" x2="92" y2="40"/>`,
};
function animateLine(el, text) {
  el.innerHTML = text.split(" ").map((w, i) => `<span style="animation-delay:${i * 45}ms">${esc(w)}</span>`).join(" ");
}
let greetTimer = null;
async function renderBrief() {
  const part = partOfDay();
  $("#messages").innerHTML = `
    <div class="brief">
      <svg class="sky ${part}" viewBox="0 0 96 48" aria-hidden="true">${SKY[part]}</svg>
      <h1 class="greet"></h1>
      <div class="suggest"></div>
    </div>`;
  animateLine($(".greet"), greeting());
  clearTimeout(greetTimer);
  let tries = 0;
  const load = async () => {
    if (!$("#messages .brief")) return;
    try {
      const g = await api("/api/greeting");
      if (g.line && $(".greet") && $(".greet").dataset.line !== g.line) {
        $(".greet").dataset.line = g.line;
        animateLine($(".greet"), g.line);
      }
      renderSuggestions(g.suggestions || []);
      if (g.pending && ++tries < 10) greetTimer = setTimeout(load, 4000);
    } catch {}
  };
  load();
}
function renderSuggestions(list) {
  const box = $("#messages .suggest");
  if (!box || box.childElementCount === list.length) return;
  box.innerHTML = "";
  list.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "chip"; b.textContent = s; b.style.animationDelay = `${300 + i * 80}ms`;
    b.onclick = () => send(s);
    box.appendChild(b);
  });
}
function renderMessages(msgs) {
  $("#messages").innerHTML = "";
  $(".main").classList.toggle("empty", !msgs.length);
  if (!msgs.length) { renderBrief(); return; }
  for (const m of msgs) {
    if (m.role === "user") addUser(m.content, m.device, m.created_at, m.id);
    else fillDork(addDork(m.created_at), m.content, m.meta);
  }
  scrollDown(true);
}
function turnShell(kind, name, when, extra) {
  $("#messages .brief")?.remove();
  $(".main").classList.remove("empty");
  const el = document.createElement("div");
  el.className = `turn ${kind}`;
  el.innerHTML = `<div class="who"><span class="name">${name}</span><span class="meta"></span></div><div class="body"></div>`;
  $(".meta", el).textContent = [clock(when), extra].filter(Boolean).join(" · ");
  $("#messages").appendChild(el);
  return el;
}
function addUser(content, from, when = Date.now() / 1000, id = null) {
  const el = turnShell("user", "YOU", when, from && from !== myDevice() ? from.toLowerCase() : "");
  $(".body", el).innerHTML = `<div class="content"></div>
    <div class="actions"><button class="btn-text act-edit">${icon("edit")}Edit</button><button class="btn-text act-copy">${icon("copy")}Copy</button></div>`;
  $(".content", el).textContent = content;
  if (id) el.dataset.id = id;
  $(".act-copy", el).onclick = (e) => copyText(content, e.currentTarget);
  $(".act-edit", el).onclick = () => editTurn(el, content);
  return el;
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch { return; }
  btn.innerHTML = `${icon("check")}Copied`;
  setTimeout(() => { btn.innerHTML = `${icon("copy")}Copy`; }, 1400);
}
// Edit a message: Dork drops it and everything after it, then answers the new version.
function editTurn(el, original) {
  if (state.streaming || !el.dataset.id) return;
  const body = $(".body", el);
  body.innerHTML = `<textarea class="edit-box" rows="2"></textarea>
    <div class="actions show"><button class="btn-text act-cancel">Cancel</button><button class="btn btn-primary btn-sm act-save">Send</button></div>`;
  const box = $(".edit-box", body);
  box.value = original; box.focus();
  const fit = () => { box.style.height = "auto"; box.style.height = box.scrollHeight + "px"; };
  fit(); box.oninput = fit;
  const cancel = () => { const fresh = addUser(original, null, Date.now() / 1000, el.dataset.id); el.replaceWith(fresh); };
  $(".act-cancel", body).onclick = cancel;
  const save = async () => {
    const text = box.value.trim();
    if (!text || text === original) return cancel();
    await api(`/api/conversations/${state.current.id}/truncate`, { method: "POST", body: { from_message_id: Number(el.dataset.id) } });
    let n = el; while (n) { const next = n.nextElementSibling; n.remove(); n = next; }
    send(text);
  };
  $(".act-save", body).onclick = save;
  box.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
    if (e.key === "Escape") cancel();
  };
}
function addDork(when = Date.now() / 1000) {
  const el = turnShell("dork", "DORK", when, "");
  $(".body", el).innerHTML = `<div class="log hidden"></div><div class="content"></div><div class="refs hidden"></div><div class="actions"></div>`;
  return el;
}
function renderRefs(el, sources) {
  const box = $(".refs", el);
  if (!sources?.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden"); box.innerHTML = "";
  for (const s of sources) {
    const a = document.createElement("a");
    a.className = "ref"; a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = s.title;
    a.innerHTML = `<span class="n"></span><span class="t"><b></b><span></span></span><span class="age"></span>`;
    $(".n", a).textContent = s.n;
    $(".t b", a).textContent = s.site;
    $(".t span", a).textContent = s.title;
    $(".age", a).textContent = s.age;
    if (/min ago| h ago/.test(s.age)) $(".age", a).classList.add("fresh");
    box.appendChild(a);
  }
}
function fillDork(el, content, meta) {
  const c = $(".content", el);
  c.innerHTML = md(content);
  linkCitations(c, meta?.sources);
  renderRefs(el, meta?.sources);
  if (meta?.sources?.length) $(".who .meta", el).textContent += ` · web`;
  const act = $(".actions", el);
  act.innerHTML = `<button class="btn-text act-copy">${icon("copy")}Copy</button><button class="btn-text act-listen">${icon("speaker")}Listen</button>`;
  $(".act-copy", act).onclick = (e) => copyText(content, e.currentTarget);
  $(".act-listen", act).onclick = () => speakReply(content);
}
function scrollDown(force) {
  const box = $("#messages");
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 140) box.scrollTop = box.scrollHeight;
}
// ---------- web lookups (a per-device setting) ----------
function webMode() { try { return localStorage.getItem("dork-web") || "auto"; } catch { return "auto"; } }
document.querySelectorAll('input[name="web"]').forEach((r) => {
  r.checked = r.value === webMode();
  r.onchange = () => { try { localStorage.setItem("dork-web", r.value); } catch {} };
});

// ---------- sending ----------
const input = $("#input");
const grow = () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; };
input.addEventListener("input", grow);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); $("#composer").requestSubmit(); }
});
function setSending(on) {
  const b = $("#send");
  b.innerHTML = icon(on ? "stop" : "arrowUp");
  b.classList.toggle("stop", on);
  b.setAttribute("aria-label", on ? "Stop" : "Send");
  $("#logo-cursor").classList.toggle("busy", on);
}
setSending(false);

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.streaming) { state.streaming.abort(); return; }
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; grow();
  send(text);
});

async function send(text, opts = {}) {
  if (state.streaming) return;
  if (opts.voice) voiceState("thinking");
  try {
    if (!state.current) state.current = await api("/api/conversations", { method: "POST", body: {} });
  } catch (err) { if (err.message !== "locked") alert(err.message); return; }

  const convId = state.current.id;
  const userEl = addUser(text, myDevice());
  const el = addDork();
  const content = $(".content", el), log = $(".log", el);
  content.classList.add("typing");
  scrollDown(true);

  const ctrl = new AbortController();
  state.streaming = ctrl; setSending(true);
  let reply = "", meta = null, steps = 0;
  const started = performance.now();

  const step = (label) => {
    log.classList.remove("hidden");
    log.querySelectorAll(".spinner").forEach((s) => { s.outerHTML = icon("check"); });
    log.insertAdjacentHTML("beforeend", `<div class="line"><span class="spinner"></span><span></span></div>`);
    log.lastElementChild.lastElementChild.textContent = label.toLowerCase();
    steps++; scrollDown();
  };
  // Keep each tool action visible (like a work log); just stop the spinners.
  const tickLog = () => log.querySelectorAll(".spinner").forEach((s) => { s.outerHTML = icon("check"); });
  const settleLog = () => {
    if (!steps) return;
    tickLog();
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    const n = meta?.sources?.length || 0;
    log.insertAdjacentHTML("beforeend", `<div class="line"><span></span></div>`);
    log.lastElementChild.lastElementChild.textContent = n ? `${n} source${n > 1 ? "s" : ""} read · ${secs}s` : `nothing useful found · ${secs}s`;
    steps = 0;
  };

  try {
    const res = await fetch(`/api/conversations/${convId}/chat`, {
      method: "POST", credentials: "same-origin", signal: ctrl.signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text, web: webMode(), voice: !!opts.voice }),
    });
    if (res.status === 401) { showLock(); return; }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "status") step(ev.text);
        else if (ev.type === "sources") { meta = { sources: ev.sources, took: ev.took }; renderRefs(el, ev.sources); }
        else if (ev.type === "meta" && ev.user_id) userEl.dataset.id = ev.user_id;
        else if (ev.type === "token") {
          tickLog();
          reply += ev.text; content.innerHTML = md(reply); linkCitations(content, meta?.sources); scrollDown();
        } else if (ev.type === "error") throw new Error(ev.error);
      }
    }
    settleLog();
    fillDork(el, reply, meta);
    afterReply(reply, opts);
  } catch (err) {
    settleLog();
    if (err.name !== "AbortError") {
      el.classList.add("error");
      content.innerHTML = md(reply) + `<p>${esc(err.message)}</p>`;
    } else if (reply) fillDork(el, reply, meta);
  } finally {
    content.classList.remove("typing");
    state.streaming = null; setSending(false);
    if (opts.voice && voice.state === "thinking") voiceState("idle");
    await Promise.all([loadConvs().catch(() => {}), refreshStatus()]);
    const conv = state.convs.find((c) => c.id === convId);
    if (conv && state.current?.id === convId) {
      state.currentUpdated = conv.updated_at; state.current.title = conv.title;
      $("#conv-title").textContent = conv.title;
    }
  }
}

// ---------- cross-device sync ----------
async function sync() {
  if (document.hidden || state.streaming || $("#app").classList.contains("hidden")) return;
  try {
    await loadConvs();
    const cur = state.current && state.convs.find((c) => c.id === state.current.id);
    if (state.current && !cur) newChat();
    else if (cur && cur.updated_at !== state.currentUpdated) await openConv(cur.id);
  } catch {}
}
setInterval(sync, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    $("#about").textContent = `Dork ${s.version.replace("nano-", "nano ")}`;
    if ($("#messages .brief")) renderBrief();
  } catch {}
}
setInterval(refreshStatus, 30000);

// ---------- pairing: show a new device's code here ----------
const pairSeen = new Set();
let pairShown = null;
async function checkPairing() {
  if ($("#app").classList.contains("hidden")) return;
  let pending = [];
  try { pending = await api("/api/pair/pending"); } catch { return; }
  renderPairList(pending);
  const fresh = pending.find((p) => !pairSeen.has(p.id));
  if (pairShown && !pending.some((p) => p.id === pairShown.id)) hidePairModal();
  if (fresh) {
    pairSeen.add(fresh.id);
    showPairModal(fresh);
    try {
      if (window.Notification && Notification.permission === "granted") {
        new Notification(`${fresh.name} wants to connect`, { body: `Code: ${fresh.code.slice(0, 3)} ${fresh.code.slice(3)}`, tag: fresh.id });
      }
    } catch {}
  } else if (pairShown) {
    const p = pending.find((x) => x.id === pairShown.id);
    if (p) $("#pair-expiry").textContent = `expires in ${Math.ceil(p.expires_in / 60)} min`;
  }
}
function showPairModal(p) {
  pairShown = p;
  $("#pair-name").textContent = `${p.name} wants to connect`;
  $("#pair-code").textContent = `${p.code.slice(0, 3)} ${p.code.slice(3)}`;
  $("#pair-expiry").textContent = `expires in ${Math.ceil(p.expires_in / 60)} min`;
  $("#pair-modal").classList.remove("hidden");
}
function hidePairModal() { pairShown = null; $("#pair-modal").classList.add("hidden"); }
$("#pair-hide").onclick = hidePairModal;
$("#pair-deny").onclick = async () => {
  if (pairShown) await api(`/api/pair/${pairShown.id}`, { method: "DELETE" }).catch(() => {});
  hidePairModal(); checkPairing();
};
function renderPairList(pending) {
  const list = $("#pair-list");
  list.innerHTML = pending.length ? "" : `<p class="hint">No devices waiting.</p>`;
  for (const p of pending) {
    const el = document.createElement("div"); el.className = "item";
    el.innerHTML = `<div class="body"><div class="c"></div><div class="sub"></div></div><span class="pair-code sm mono"></span>`;
    $(".c", el).textContent = p.name;
    $(".sub", el).textContent = `waiting · expires in ${Math.ceil(p.expires_in / 60)} min`;
    $(".pair-code", el).textContent = `${p.code.slice(0, 3)} ${p.code.slice(3)}`;
    list.appendChild(el);
  }
}
setInterval(checkPairing, 3000);
try { if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } catch {}

// ---------- nav (mobile) ----------
const closeNav = () => $("#app").classList.remove("nav-open");
$("#menu").onclick = () => $("#app").classList.add("nav-open");
$("#scrim").onclick = closeNav;

// ---------- settings ----------
function openPanel(id) { $(id).classList.remove("hidden"); closeNav(); }
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== name));
  if (name === "profile") loadProfile();
  if (name === "memory") loadMemories();
  if (name === "devices") { loadDevices(); checkPairing(); }
  if (name === "security") refreshStatus();
}
document.querySelectorAll(".tabs button").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
$("#open-settings").onclick = () => { openPanel("#settings-panel"); showTab("profile"); };
document.querySelectorAll(".sheet").forEach((p) => {
  p.addEventListener("click", (e) => { if (e.target === p || e.target.closest(".close")) p.classList.add("hidden"); });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll(".sheet:not(.hidden)").forEach((p) => p.classList.add("hidden"));
});

// profile
function showGathered(p) {
  $("#pf-gathered").innerHTML = p.gathered
    ? md(p.gathered) + `<div class="stamp">updated ${ago(p.gathered_at)}</div>`
    : `<p class="hint">Nothing yet. Chat a little, or upload something about you.</p>`;
}
async function loadProfile() {
  const p = await api("/api/profile");
  state.profile = p;
  $("#pf-name").value = p.name; $("#pf-call").value = p.call_me; $("#pf-about").value = p.about;
  $("#pf-learn").checked = p.learn_from_chats;
  $("#pf-msg").textContent = "";
  showGathered(p);
}
$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    state.profile = await api("/api/profile", { method: "PUT", body: {
      name: $("#pf-name").value, call_me: $("#pf-call").value, about: $("#pf-about").value, learn_from_chats: $("#pf-learn").checked,
    } });
    $("#pf-msg").textContent = "Saved";
  } catch (err) { $("#pf-msg").textContent = err.message; }
});
$("#pf-refresh").onclick = async () => {
  $("#pf-gathered").innerHTML = `<p class="hint">Rewriting…</p>`;
  try { showGathered(await api("/api/profile/refresh", { method: "POST" })); }
  catch (err) { $("#pf-gathered").textContent = err.message; }
};
$("#pf-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const msg = $("#pf-upload-msg");
  if (file.size > 10 * 1024 * 1024) { msg.textContent = "That file is over 10 MB."; return; }
  msg.textContent = `Reading ${file.name}…`;
  const data_b64 = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result.split(",")[1]); r.onerror = reject; r.readAsDataURL(file);
  });
  try {
    const res = await api("/api/profile/upload", { method: "POST", body: { filename: file.name, data_b64 } });
    msg.textContent = `Learned ${res.learned} thing${res.learned === 1 ? "" : "s"} from ${file.name}`;
    showGathered(res.profile); loadMemories();
  } catch (err) { msg.textContent = err.message; }
});

// memories
// The memory tree: You › …, People › Family › Sister (Priya), Interests › Games › Valorant, …
const openBranches = new Set(["You", "People", "Interests", "Work & Studies"]);
async function loadMemories() {
  const [mems, tree] = await Promise.all([api("/api/memories"), api("/api/memory-tree")]);
  $("#mem-count").textContent = `${mems.length} remembered`;
  $("#memory-paths").innerHTML = [...new Set([...tree.roots, ...tree.paths])].map((p) => `<option value="${esc(p.replaceAll("/", " / "))}">`).join("");
  // Build nested nodes from the paths.
  const root = { name: "", kids: new Map(), items: [] };
  for (const r of tree.roots) root.kids.set(r, { name: r, kids: new Map(), items: [] });
  for (const m of mems) {
    let node = root;
    for (const part of (m.path || "Life").split("/")) {
      if (!node.kids.has(part)) node.kids.set(part, { name: part, kids: new Map(), items: [] });
      node = node.kids.get(part);
    }
    node.items.push(m);
  }
  const count = (n) => n.items.length + [...n.kids.values()].reduce((a, k) => a + count(k), 0);
  const render = (node, trail) => {
    const wrap = document.createElement("div");
    for (const kid of node.kids.values()) {
      const n = count(kid), key = trail ? `${trail}/${kid.name}` : kid.name;
      const d = document.createElement("details");
      d.className = "branch" + (n ? "" : " empty");
      d.open = openBranches.has(key);
      d.ontoggle = () => (d.open ? openBranches.add(key) : openBranches.delete(key));
      d.innerHTML = `<summary>${icon("chev")}<span class="bn"></span><span class="bc mono">${n || ""}</span></summary>`;
      $(".bn", d).textContent = kid.name;
      const inner = render(kid, key);
      for (const m of kid.items) inner.appendChild(memoryLeaf(m));
      d.appendChild(inner);
      wrap.appendChild(d);
    }
    wrap.className = "branch-kids";
    return wrap;
  };
  const list = $("#memory-list");
  list.innerHTML = "";
  list.appendChild(render(root, ""));
  if (!mems.length) list.insertAdjacentHTML("beforeend", `<p class="hint">Nothing yet. Dork fills this in as you chat.</p>`);
}
function memoryLeaf(m) {
  const el = document.createElement("div"); el.className = "leaf";
  el.innerHTML = `<span class="c"></span><span class="leaf-actions">
    <button class="icon-btn sm" title="Move to another branch">${icon("edit")}</button>
    <button class="icon-btn sm" title="Forget">${icon("trash")}</button></span>`;
  $(".c", el).textContent = m.content;
  const [moveBtn, delBtn] = el.querySelectorAll("button");
  moveBtn.onclick = async () => {
    const to = prompt("Move to which branch? e.g. People / Family / Sister (Priya)", (m.path || "").replaceAll("/", " / "));
    if (to && to.trim()) { await api(`/api/memories/${m.id}`, { method: "PATCH", body: { path: to } }); loadMemories(); }
  };
  delBtn.onclick = async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); loadMemories(); };
  return el;
}
$("#memory-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/memories", { method: "POST", body: { content: $("#memory-input").value, path: $("#memory-path").value } });
  $("#memory-input").value = ""; loadMemories();
});

// ---------- quick switcher (Ctrl+K) ----------
function openSwitcher() {
  $("#switcher").classList.remove("hidden");
  const q = $("#switcher-input"); q.value = ""; renderSwitcher(); q.focus();
}
function closeSwitcher() { $("#switcher").classList.add("hidden"); }
let switchIndex = 0;
function switcherItems() {
  const q = $("#switcher-input").value.trim().toLowerCase();
  const actions = [
    { label: "New thread", hint: "start fresh", run: newChat },
    { label: "Settings", hint: "profile, memory, devices", run: () => $("#open-settings").click() },
    { label: "What Dork remembers", hint: "memory tree", run: () => { $("#open-settings").click(); showTab("memory"); } },
  ];
  const threads = state.convs.map((c) => ({ label: c.title, hint: shortWhen(c.updated_at), run: () => openConv(c.id) }));
  return [...actions, ...threads].filter((i) => !q || i.label.toLowerCase().includes(q)).slice(0, 9);
}
function renderSwitcher() {
  const items = switcherItems();
  switchIndex = Math.min(switchIndex, Math.max(0, items.length - 1));
  const list = $("#switcher-list"); list.innerHTML = "";
  items.forEach((it, i) => {
    const b = document.createElement("button");
    b.className = "sw-item" + (i === switchIndex ? " active" : "");
    b.innerHTML = `<span></span><span class="mono"></span>`;
    b.firstChild.textContent = it.label; b.lastChild.textContent = it.hint;
    b.onclick = () => { closeSwitcher(); it.run(); };
    list.appendChild(b);
  });
  if (!items.length) list.innerHTML = `<p class="hint sw-none">No matches.</p>`;
}
$("#switcher-input").addEventListener("input", () => { switchIndex = 0; renderSwitcher(); });
$("#switcher-input").addEventListener("keydown", (e) => {
  const items = switcherItems();
  if (e.key === "ArrowDown") { switchIndex = (switchIndex + 1) % Math.max(1, items.length); renderSwitcher(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { switchIndex = (switchIndex - 1 + items.length) % Math.max(1, items.length); renderSwitcher(); e.preventDefault(); }
  else if (e.key === "Enter" && items[switchIndex]) { closeSwitcher(); items[switchIndex].run(); }
  else if (e.key === "Escape") closeSwitcher();
});
$("#switcher").addEventListener("click", (e) => { if (e.target.id === "switcher") closeSwitcher(); });
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !$("#app").classList.contains("hidden")) {
    e.preventDefault();
    $("#switcher").classList.contains("hidden") ? openSwitcher() : closeSwitcher();
  }
});
$("#open-switcher").onclick = openSwitcher;

// devices
async function loadDevices() {
  const list = $("#device-list"); list.innerHTML = "";
  for (const d of await api("/api/devices")) {
    const el = document.createElement("div"); el.className = "item";
    el.innerHTML = `<div class="body"><div class="c"></div><div class="sub"></div></div>`;
    $(".c", el).textContent = d.name + (d.current ? "  (this device)" : "");
    $(".sub", el).textContent = `last active ${ago(d.last_seen)} · connected ${new Date(d.created_at * 1000).toLocaleDateString()}`;
    if (!d.current) {
      const b = document.createElement("button"); b.className = "icon-btn"; b.title = "Remove device"; b.innerHTML = icon("x");
      b.onclick = async () => {
        if (!confirm(`Remove "${d.name}"? It will need a new code to connect again.`)) return;
        await api(`/api/devices/${d.id}`, { method: "DELETE" }); loadDevices();
      };
      el.appendChild(b);
    }
    list.appendChild(el);
  }
}
$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#key-msg").textContent = "";
  try {
    await api("/api/auth/change-key", { method: "POST", body: { current: $("#key-current").value, new: $("#key-new").value } });
    $("#key-current").value = $("#key-new").value = "";
    $("#settings-panel").classList.add("hidden");
    showLock();
  } catch (err) { $("#key-msg").textContent = err.message; }
});
$("#logout").onclick = async () => {
  if (!confirm("Disconnect this device? It will need a new code to connect again.")) return;
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  $("#settings-panel").classList.add("hidden"); showLock();
};

// ---------- voice: talk to Dork, hear Dork ----------
// Listening: the browser's own speech recognition where it exists (Chrome, Edge, Android), otherwise
// (the desktop app) record the mic and let Dork transcribe it. Speaking: everyday sentences are fetched
// one by one (the next is prepared while the current one plays); replies with emotion cues go to the
// expressive voice in one piece.
const isElectron = /Electron/i.test(navigator.userAgent);
const Recognition = !isElectron && (window.SpeechRecognition || window.webkitSpeechRecognition);
const vpref = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };
const vset = (k, v) => { try { localStorage.setItem(k, v); } catch {} };
const voice = { state: "idle", audio: new Audio(), queue: [], token: 0, rec: null, recorder: null, ctx: null, analyser: null, raf: 0 };

function voiceState(s, label) {
  voice.state = s;
  document.body.dataset.voice = s;
  const bar = $("#voice-bar");
  bar.classList.toggle("hidden", s === "idle");
  $("#voice-state").textContent = label || { listening: "Listening…", thinking: "Thinking…", speaking: "Speaking…" }[s] || "";
  $("#mic").classList.toggle("on", s === "listening");
  if (s === "idle") setLevel(0);
}
// Say what happened instead of silently closing (e.g. nothing heard), then fade back to idle.
function voiceNotice(message) {
  voiceState("notice", message);
  clearTimeout(voice.noticeTimer);
  voice.noticeTimer = setTimeout(() => { if (voice.state === "notice") voiceState("idle"); }, 4500);
}
function setLevel(v) { document.documentElement.style.setProperty("--level", Math.min(1, v).toFixed(3)); }

// Drive the mic ring from an audio level (mic while listening, Dork's voice while speaking).
function meter(source) {
  cancelAnimationFrame(voice.raf);
  if (!voice.ctx) return;
  const an = voice.ctx.createAnalyser(); an.fftSize = 512;
  source.connect(an);
  const buf = new Uint8Array(an.fftSize);
  const tick = () => {
    an.getByteTimeDomainData(buf);
    let sum = 0; for (const b of buf) sum += (b - 128) ** 2;
    setLevel(Math.sqrt(sum / buf.length) / 40);
    voice.raf = requestAnimationFrame(tick);
  };
  tick();
  return an;
}
function audioCtx() {
  if (!voice.ctx) {
    voice.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = voice.ctx.createMediaElementSource(voice.audio);
    src.connect(voice.ctx.destination);
    voice.outSource = src;
  }
  if (voice.ctx.state === "suspended") voice.ctx.resume();
  return voice.ctx;
}

// ----- speaking
function spokenSentences(text) {
  return text.replace(/```[\s\S]*?```/g, " ").split(/\n+/)
    .flatMap((p) => p.split(/(?<=[.!?…।])\s+/)).map((s) => s.trim()).filter((s) => /[\p{L}\p{N}]/u.test(s));
}
async function fetchSpeech(text, whole) {
  const res = await fetch("/api/voice/speak", { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, whole }) });
  if (!res.ok) return null;
  return URL.createObjectURL(await res.blob());
}
function playUrl(url, token) {
  return new Promise((resolve) => {
    if (token !== voice.token) return resolve();
    audioCtx(); meter(voice.outSource);
    voice.audio.src = url;
    voice.audio.onended = voice.audio.onerror = () => { URL.revokeObjectURL(url); resolve(); };
    voice.audio.play().catch(resolve);
  });
}
async function speakReply(text) {
  stopSpeaking();
  const token = ++voice.token;
  const expressive = VOICE_CUE.test(text); VOICE_CUE.lastIndex = 0;
  voiceState("speaking");
  try {
    if (expressive && text.length < 1200) {
      const url = await fetchSpeech(text, true);
      if (url) await playUrl(url, token);
    } else {
      const parts = spokenSentences(text);
      let next = parts.length ? fetchSpeech(parts[0], false) : null;
      for (let i = 0; i < parts.length && token === voice.token; i++) {
        const url = await next;
        next = i + 1 < parts.length ? fetchSpeech(parts[i + 1], false) : null;  // prepare the next while this plays
        if (url) await playUrl(url, token);
      }
    }
  } catch {}
  if (token === voice.token) { cancelAnimationFrame(voice.raf); voiceState("idle"); }
  return token === voice.token;
}
function stopSpeaking() {
  voice.token++;
  try { voice.audio.pause(); } catch {}
  cancelAnimationFrame(voice.raf);
  if (voice.state === "speaking") voiceState("idle");
}
async function afterReply(reply, opts) {
  const mode = vpref("dork-speak", "voice");
  if (!reply || mode === "never" || (mode === "voice" && !opts.voice)) { if (opts.voice) voiceState("idle"); return; }
  const finished = await speakReply(reply);
  if (finished && opts.voice && vpref("dork-continuous", "1") === "1") startListening(true);
}

// ----- listening
async function startListening(auto = false) {
  stopSpeaking();
  if (voice.state === "listening") return stopListening();
  audioCtx();
  if (Recognition) return listenBrowser(auto);
  return listenRecorder(auto);
}
function stopListening(send = true) {
  voice.manualStop = send; voice.cancelled = !send;  // a tap while listening means "I'm done — send it", even if it sounded quiet
  try { voice.rec?.stop(); } catch {}
  try { if (voice.recorder?.state === "recording") voice.recorder.stop(); } catch {}
}
function listenBrowser(auto) {
  const rec = new Recognition();
  voice.rec = rec;
  rec.lang = vpref("dork-hear", "en-IN");
  rec.interimResults = true; rec.continuous = false;
  let final = "";
  rec.onresult = (e) => {
    let interim = "";
    for (const r of e.results) (r.isFinal ? (final = r[0].transcript) : (interim += r[0].transcript));
    input.value = final || interim; grow();
  };
  let failed = "";
  rec.onerror = (e) => {
    failed = { "no-speech": auto ? "Stopped listening. Tap the mic whenever you want to talk." : "I didn't hear anything. Try again a little closer.",
               "not-allowed": "Microphone access is blocked for Dork in this browser.",
               "network": "Speech recognition needs internet here.", "aborted": "" }[e.error] ?? `Voice input failed (${e.error}).`;
  };
  rec.onend = () => {
    voice.rec = null;
    const text = (final || input.value).trim();
    if (voice.cancelled) { voice.cancelled = false; input.value = ""; grow(); return voiceState("idle"); }
    if (text) { input.value = ""; grow(); send(text, { voice: true }); }
    else if (failed) voiceNotice(failed);
    else voiceState("idle");
  };
  voice.manualStop = false;
  voiceState("listening", "Listening… tap the mic again when you're done");
  rec.start(250);
}
async function listenRecorder(auto) {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    ...(vpref("dork-mic", "") ? { deviceId: { exact: vpref("dork-mic", "") } } : {}) } }); }
  catch { voiceState("idle"); alert("Dork couldn't use the microphone."); return; }
  const rec = new MediaRecorder(stream);
  voice.recorder = rec;
  const chunks = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  // Voice-activity detection that adapts to the room: measure the background for ~0.6 s, then "speech" is
  // clearly louder than that. Stop ~1.8 s after you finish (≥0.4 s of speech heard), or at 30 s.
  const an = meter(voice.ctx.createMediaStreamSource(stream));
  const buf = new Uint8Array(512);
  const started = performance.now();
  let noise = null, calib = [], speechMs = 0, quietSince = 0, peak = 0;
  const rms = () => { an.getByteTimeDomainData(buf); let s = 0; for (const b of buf) s += (b - 128) ** 2; return Math.sqrt(s / buf.length); };
  const vad = setInterval(() => {
    const level = rms(), now = performance.now();
    peak = Math.max(peak, level);
    if (now - started < 600) { calib.push(level); return; }
    if (noise === null) noise = calib.length ? calib.reduce((a, b) => a + b, 0) / calib.length : 1;
    const loud = level > Math.max(noise * 2.5, noise + 0.8);  // quiet laptop mics: speech can peak at ~3 over a 0.6 room
    if (loud) { speechMs += 100; quietSince = now; }
    else if (!loud && noise !== null && level < noise * 1.3) noise = noise * 0.97 + level * 0.03;  // follow the room slowly
    const spoke = speechMs >= 400;
    if ((spoke && now - quietSince > 1800) || now - started > 45000 || (!spoke && now - started > 25000)) rec.stop();
  }, 100);
  rec.onstop = async () => {
    clearInterval(vad); cancelAnimationFrame(voice.raf);
    stream.getTracks().forEach((t) => t.stop());
    voice.recorder = null;
    console.info(`[dork voice] noise ${noise?.toFixed(1)} peak ${peak.toFixed(1)} speech ${speechMs}ms`);
    const manual = voice.manualStop; voice.manualStop = false;
    if (voice.cancelled) { voice.cancelled = false; return; }
    if (!manual && (speechMs < 400 || !chunks.length)) {
      return voiceNotice(auto ? "Stopped listening. Tap the mic whenever you want to talk."
                              : "I didn't hear anything. Tap the mic and talk a little closer.");
    }
    voiceState("thinking", "Understanding…");
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    const b64 = await new Promise((r) => { const f = new FileReader(); f.onload = () => r(f.result.split(",")[1]); f.readAsDataURL(blob); });
    try {
      const { text } = await api("/api/voice/hear", { method: "POST", body: { audio_b64: b64, mime: blob.type } });
      if (text) send(text, { voice: true }); else voiceNotice("I couldn't make out the words. Tap the mic to try again.");
    } catch (err) { if (err.message !== "locked") voiceNotice(err.message); }
  };
  voice.manualStop = false;
  voiceState("listening", "Listening… tap the mic again when you're done");
  rec.start();
}

$("#mic").onclick = () => (voice.state === "speaking" ? stopSpeaking() : startListening());
$("#voice-stop").onclick = () => { stopListening(false); stopSpeaking(); voiceState("idle"); };
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.code === "Space") { e.preventDefault(); startListening(); }
  else if (e.key === "Escape" && voice.state !== "idle") { stopListening(false); stopSpeaking(); voiceState("idle"); }
});

// Settings › Voice
document.querySelectorAll('input[name="speak"]').forEach((r) => {
  r.checked = r.value === vpref("dork-speak", "voice");
  r.onchange = () => vset("dork-speak", r.value);
});
document.querySelectorAll('input[name="hear"]').forEach((r) => {
  r.checked = r.value === vpref("dork-hear", "en-IN");
  r.onchange = () => vset("dork-hear", r.value);
});
async function loadMics() {
  try {
    const list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput" && d.deviceId !== "default" && d.deviceId !== "communications");
    const sel = $("#vc-mic");
    sel.innerHTML = `<option value="">System default</option>` + list.map((d) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Microphone")}</option>`).join("");
    sel.value = list.some((d) => d.deviceId === vpref("dork-mic", "")) ? vpref("dork-mic", "") : "";
  } catch {}
}
$("#vc-mic").onchange = (e) => vset("dork-mic", e.target.value);
document.querySelector('.tabs button[data-tab="voice"]').addEventListener("click", loadMics);
$("#vc-continuous").checked = vpref("dork-continuous", "1") === "1";
$("#vc-continuous").onchange = (e) => vset("dork-continuous", e.target.checked ? "1" : "0");

// ---------- app mode (installable on every device) ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
let installPrompt = null;
addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  $("#install-app").classList.remove("hidden");
});
if (isIOS && !standalone) $("#install-app").classList.remove("hidden");  // iOS has no install prompt
$("#install-app").onclick = async () => {
  if (installPrompt) {
    installPrompt.prompt();
    if ((await installPrompt.userChoice).outcome === "accepted") $("#install-app").classList.add("hidden");
    installPrompt = null;
  } else if (isIOS) {
    alert("To install Dork: tap the Share button in Safari, then \"Add to Home Screen\".");
  } else {
    alert("Use your browser menu: \"Install app\" or \"Add to Home screen\".\nOn phones this needs Dork's https:// address.");
  }
};
addEventListener("appinstalled", () => $("#install-app").classList.add("hidden"));
if (standalone) document.documentElement.classList.add("standalone");

boot();
