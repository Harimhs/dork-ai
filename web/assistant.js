// Dork assistant mode. The page is Dork's face: it follows the server's events (state, mic level, captions,
// what to say, what to open) and plays Dork's voice. Listening happens on the PC itself (dork/assistant.py);
// on a phone or another browser the mic button records here and sends the phrase to Dork.

import { createReactor } from "./reactor2d.js?v=1";  // 2D: smooth on integrated graphics (reactor.js = the 3D one)
import { createConvMic } from "./convmic.js?v=1";

const $ = (s) => document.querySelector(s);
const desk = window.dorkDesktop || null;                  // set by the desktop app's preload
const params = new URLSearchParams(location.search);
const HOST = !!desk || params.get("host") === "1";        // this window speaks for the PC's own ears

// ---------- icons ----------
const ICONS = {
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/>',
  keyboard: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10"/>',
  chat: '<path d="M4.5 5.5h15v10h-9l-4 3.5v-3.5h-2z"/>',
  send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  minus: '<path d="M5 12h14"/>',
};
document.querySelectorAll("i[data-icon]").forEach((el) => {
  el.outerHTML = `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[el.dataset.icon] || ""}</svg>`;
});

// ---------- reactor ----------
const reactor = createReactor($("#reactor"));
if (desk?.onVisible) desk.onVisible((v) => reactor.setPaused(!v));
const LABEL = { sleeping: "STANDBY", wake: "ONLINE", awake: "ONLINE", listening: "LISTENING", thinking: "PROCESSING",
  speaking: "SPEAKING", error: "FAULT" };
let uiState = "sleeping";
function show(state) {
  uiState = state;
  reactor.setState(state);
  document.body.className = state === "wake" ? "awake" : state;
  $("#state-label").textContent = LABEL[state] || state.toUpperCase();
}
setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }, 1000);

// ---------- captions ----------
let capTimer = 0;
function caption(el, text, words = false) {
  clearTimeout(capTimer);
  el.classList.remove("fade");
  if (words) {
    el.innerHTML = "";
    text.split(/\s+/).forEach((w, i) => {
      const s = document.createElement("span");
      s.className = "w"; s.textContent = w + " "; s.style.animationDelay = `${i * 55}ms`;
      el.appendChild(s);
    });
  } else el.textContent = text;
  capTimer = setTimeout(() => ["#cap-you", "#cap-dork", "#cap-status"].forEach((s) => $(s).classList.add("fade")), 14000);
}
const clean = (t) => t.replace(/\[(\d{1,2}|[a-z ]+)\]\s*/gi, "").replace(/\s+([.,!?])/g, "$1").replace(/[*_`#]/g, "").trim();

// ---------- API ----------
async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET", credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { location.href = "/"; throw new Error("locked"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

// ---------- Dork's voice: a queue of sentences, fetched ahead, played through an analyser ----------
const audio = $("#voice-out");
let actx = null, outAnalyser = null, micAnalyser = null;
function audioCtx() {
  if (!actx) {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    const src = actx.createMediaElementSource(audio);
    outAnalyser = actx.createAnalyser(); outAnalyser.fftSize = 256; outAnalyser.smoothingTimeConstant = 0.7;
    src.connect(outAnalyser); outAnalyser.connect(actx.destination);
  }
  if (actx.state === "suspended") actx.resume().catch(() => {});
  return actx;
}
const queue = [];          // [{text, url: Promise<string|null>}]
let playing = false, epoch = 0;
function speak(text) {
  const t = clean(text);
  if (!t) return;
  const e = epoch;
  const url = api("/api/voice/speak", { text: t, whole: false })
    .then(async (r) => URL.createObjectURL(await r.blob())).catch(() => null);
  queue.push({ text: t, url, e });
  if (!playing) next();
}
async function next() {
  const item = queue.shift();
  if (!item) {
    if (playing) { playing = false; api("/api/assistant/spoken", { playing: false }).catch(() => {}); if (uiState === "speaking") show("awake"); }
    return;
  }
  if (item.e !== epoch) return next();
  if (!playing) { playing = true; api("/api/assistant/spoken", { playing: true }).catch(() => {}); }
  show("speaking");
  const url = await item.url;
  if (!url || item.e !== epoch) return next();
  try { audioCtx(); } catch {}
  audio.src = url;
  audio.onended = () => { URL.revokeObjectURL(url); next(); };
  audio.onerror = () => next();
  try { await audio.play(); } catch { next(); }
}
// a soft two-note chime: "got it" the instant a phrase is heard (the brain may take a few seconds)
function chime(up = true) {
  try {
    const ctx = audioCtx(), t = ctx.currentTime;
    [[up ? 660 : 880, 0], [up ? 990 : 660, 0.09]].forEach(([f, d]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0, t + d); g.gain.linearRampToValueAtTime(0.07, t + d + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.22);
      o.connect(g); g.connect(ctx.destination); o.start(t + d); o.stop(t + d + 0.25);
    });
  } catch {}
}
// ---------- conversation mic (desktop app): echo-cancelled, so Hari can talk over Dork and be heard ----------
let convMic = null, bargedIn = false, micBeat = 0;
async function startConvMic() {
  if (!HOST || (convMic && convMic.active)) return;
  try {
    convMic = convMic || createConvMic(audioCtx(), {
      isPlaying: () => playing,
      onLevel: (v) => { if (!playing) micLevel = v; },
      onSpeechStart: () => {
        if (playing) { bargedIn = true; hush(); }  // Hari started talking: Dork stops mid-sentence
        show("listening");
      },
      onUtterance: async (b64) => {
        show("thinking");
        try {
          const r = await api("/api/assistant/utterance", { audio_b64: b64, mime: "audio/wav", client });
          if (!r.heard && uiState === "thinking") show("awake");
        } catch { show("awake"); }
      },
    });
    await convMic.start();
    api("/api/assistant/hostmic", { on: true }).catch(() => {});
    clearInterval(micBeat);
    micBeat = setInterval(() => convMic && convMic.active && api("/api/assistant/hostmic", { on: true }).catch(() => {}), 4000);
  } catch (e) { console.warn("conversation mic unavailable", e); }
}
function stopConvMic() {
  clearInterval(micBeat);
  if (convMic && convMic.active) { convMic.stop(); api("/api/assistant/hostmic", { on: false }).catch(() => {}); }
}

function hush() {
  epoch++; queue.length = 0;
  audio.pause(); audio.removeAttribute("src");
  if (playing) { playing = false; api("/api/assistant/spoken", { playing: false }).catch(() => {}); }
}

// recording state (talking from this device; see below)
let recording = false, rec = null, stream = null, chunks = [], heardSpeech = false, levelRaf = 0;

// reactor follows Dork's voice (speaking) or the mic (listening)
const spec = new Uint8Array(128);
let micLevel = 0;
(function meter() {
  requestAnimationFrame(meter);
  if (playing && outAnalyser) {
    outAnalyser.getByteFrequencyData(spec);
    let sum = 0; for (let i = 2; i < 48; i++) sum += spec[i];
    reactor.setLevel(Math.min(1, sum / 46 / 150));
    reactor.setSpectrum(spec);
  } else if (recording && micAnalyser) {
    micAnalyser.getByteFrequencyData(spec);
    reactor.setSpectrum(spec);
    reactor.setLevel(micLevel);
  } else {
    reactor.setSpectrum(null);
    reactor.setLevel(uiState === "sleeping" ? 0 : micLevel);
  }
})();

// ---------- events from Dork ----------
let client = "", es = null, retry = 1000;
function connect() {
  es = new EventSource(`/api/assistant/events${HOST ? "?host=true" : ""}`, { withCredentials: true });
  es.onopen = () => { retry = 1000; setLink(true); };
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch {} };
  es.onerror = () => {
    setLink(false); es.close();
    // signed out? the API call sends us to the sign-in page
    api("/api/assistant/state").catch(() => {});
    setTimeout(connect, retry); retry = Math.min(retry * 2, 15000);
  };
}
function setLink(ok) { const v = $("#st-link"); v.textContent = ok ? "live" : "reconnecting"; v.className = "v " + (ok ? "ok" : "warn"); }
function status(s) {
  const ears = $("#st-ears");
  ears.textContent = s.ears ? "on" : (s.ears_error ? "no mic" : "off");
  ears.className = "v " + (s.ears ? "ok" : "warn");
  const owner = s.hearing?.enrolled?.find((p) => p.role === "owner");
  const v = $("#st-voice");
  v.textContent = owner ? `${owner.name} enrolled` : "not enrolled";
  v.className = "v " + (owner ? "ok" : "warn");
  setGuard(s.locked);
}
function setGuard(on) { const g = $("#st-guard"); g.textContent = on ? "on" : "off"; g.className = "v " + (on ? "warn" : ""); }

let silenceTimer = 0;
function onEvent(ev) {
  switch (ev.type) {
    case "hello":
      client = ev.client; status(ev); show(ev.state === "sleeping" ? "sleeping" : "awake");
      if (ev.state !== "sleeping") startConvMic();
      break;
    case "state":
      setGuard(ev.locked);
      if (ev.state === "awake" && uiState === "sleeping") show("wake");
      else if (ev.state === "speaking" && !playing) { /* the UI decides speaking by playback */ }
      else if (!(playing && ev.state === "awake")) show(ev.state);
      if (ev.state === "sleeping") { hush(); stopRecording(true); stopConvMic(); }
      else startConvMic();
      break;
    case "level":
      micLevel = ev.v * (ev.speech ? 1 : 0.4);
      if (ev.speech && uiState === "awake") show("listening");
      clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => { if (uiState === "listening") show("awake"); micLevel = 0; }, 900);
      break;
    case "heard":
      bargedIn = false;
      chime(true); show("thinking");
      caption($("#cap-you"), ev.text);
      $("#cap-dork").textContent = ""; $("#cap-status").textContent = "";
      reactor.pulse();
      break;
    case "status":
      caption($("#cap-status"), ev.text);
      break;
    case "say":
      if (bargedIn) break;  // Hari talked over this answer: drop the rest of it
      caption($("#cap-dork"), clean(ev.text), true);
      speak(ev.text);
      break;
    case "reply":
      $("#cap-status").textContent = "";
      break;
    case "stop_speaking":
      hush(); show("listening");
      break;
    case "enroll":
      showEnroll(ev);
      break;
    case "ui":
      uiAction(ev);
      break;
  }
}

function uiAction(ev) {
  const go = (url) => (desk ? desk.openChat(url) : window.open(url, "_blank"));
  if (ev.do === "show") { desk?.show(); if (uiState === "sleeping") show("wake"); }
  else if (ev.do === "hide") { setTimeout(() => { if (!playing) desk?.hide(); else audio.addEventListener("ended", () => !playing && desk?.hide(), { once: true }); }, 1200); show("sleeping"); }
  else if (ev.do === "open_chat") go(`/?conv=${encodeURIComponent(ev.conv || "")}`);
  else if (ev.do === "open_settings") go("/?settings=1");
  else if (ev.do === "show_memories") go("/?settings=memory");
}

function showEnroll(ev) {
  const box = $("#enroll");
  box.classList.toggle("hidden", ev.step >= ev.of);
  $("#enroll-name").textContent = (ev.name || "").toUpperCase();
  $("#enroll-steps").innerHTML = Array.from({ length: ev.of }, (_, i) => `<span class="${i < ev.step ? "done" : ""}"></span>`).join("");
}

// ---------- talking from this device (phone / browser): record a phrase, send it ----------
async function startRecording() {
  if (recording) return;
  hush();
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch { caption($("#cap-status"), "Microphone blocked"); return; }
  const ctx = audioCtx();
  const src = ctx.createMediaStreamSource(stream);
  micAnalyser = ctx.createAnalyser(); micAnalyser.fftSize = 256;
  src.connect(micAnalyser);
  chunks = []; heardSpeech = false;
  rec = new MediaRecorder(stream);
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = send;
  rec.start(250);
  recording = true;
  $("#btn-mic").classList.add("rec");
  show("listening");
  const buf = new Float32Array(micAnalyser.fftSize);
  const started = performance.now();
  let quietSince = performance.now();
  const tick = () => {
    if (!recording) return;
    levelRaf = requestAnimationFrame(tick);
    micAnalyser.getFloatTimeDomainData(buf);
    let s = 0; for (const x of buf) s += x * x;
    const db = 20 * Math.log10(Math.sqrt(s / buf.length) + 1e-6);
    micLevel = Math.max(0, Math.min(1, (db + 55) / 40));
    if (db > -42) { heardSpeech = true; quietSince = performance.now(); }
    // stop after a pause (or 15 s)
    if ((heardSpeech && performance.now() - quietSince > 1100) || performance.now() - started > 15000
      || (!heardSpeech && performance.now() - started > 6000)) stopRecording(false);
  };
  tick();
}
function stopRecording(discard) {
  if (!recording) return;
  recording = false;
  cancelAnimationFrame(levelRaf);
  $("#btn-mic").classList.remove("rec");
  if (discard) chunks = [];
  try { rec.stop(); } catch {}
  stream?.getTracks().forEach((t) => t.stop());
  micLevel = 0;
}
async function send() {
  if (!chunks.length || !heardSpeech) { if (uiState === "listening") show("awake"); return; }
  const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
  show("thinking");
  const b64 = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(r.result.split(",")[1]); r.readAsDataURL(blob); });
  try {
    const r = await api("/api/assistant/utterance", { audio_b64: b64, mime: blob.type, client });
    if (!r.heard) { caption($("#cap-status"), "Didn't catch that"); show("awake"); }
  } catch (e) { caption($("#cap-status"), e.message); show("error"); setTimeout(() => show("awake"), 2500); }
}

$("#btn-mic").onclick = () => { audioCtx(); recording ? stopRecording(false) : startRecording(); };
$("#reactor").addEventListener("click", () => { audioCtx(); if (uiState === "sleeping") api("/api/assistant/wake", { client }).catch(() => {}); });
$("#btn-type").onclick = () => { $("#type-form").classList.toggle("hidden"); $("#type-input").focus(); };
$("#type-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  audioCtx();
  const text = $("#type-input").value.trim();
  if (!text) return;
  $("#type-input").value = "";
  hush();
  caption($("#cap-you"), text);
  show("thinking");
  try { await api("/api/assistant/utterance", { text, client }); } catch (err) { caption($("#cap-status"), err.message); }
});
$("#btn-chat").onclick = async () => {
  const s = await api("/api/assistant/state").catch(() => null);
  uiAction({ do: "open_chat", conv: s?.voice_conversation });
};
$("#btn-hide").onclick = () => { hush(); api("/api/assistant/sleep", {}).catch(() => {}); desk ? desk.hide() : show("sleeping"); };
$("#btn-full").onclick = () => (desk ? desk.toggleFull() : (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()));
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") { if (e.key === "Escape") $("#type-form").classList.add("hidden"); return; }
  if (e.key === "Escape") $("#btn-hide").click();
  if (e.key.toLowerCase() === "f") $("#btn-full").click();
  if (e.code === "Space" && $("#btn-mic")) { e.preventDefault(); $("#btn-mic").click(); }
});
// On the PC, Dork listens through the PC's own microphone ("Dork…"): no buttons, just the reactor.
// On a phone or another browser the dock stays, since its mic is the only way to talk to Dork from there.
if (HOST) document.querySelector(".dock").remove();

show("sleeping");
api("/api/assistant/state").then(status).catch(() => {});
connect();
