// Conversation mic for the assistant window: while Dork is awake it listens here, with the browser's echo
// cancellation on, so Dork's own voice (coming out of the speakers) is removed from what the mic hears.
// The moment Hari starts talking, onSpeechStart fires (Dork stops mid-sentence); when he pauses, the phrase
// is handed over as a 16 kHz WAV. No wake word needed during a conversation.
//
//   const mic = createConvMic(audioCtx, { onSpeechStart, onUtterance(wavBase64), isPlaying, onLevel })
//   await mic.start(); mic.stop();

const RATE = 16000;
const FRAME_MS = 43;          // one ScriptProcessor block at 48 kHz (2048 samples)
const START_FRAMES = 3;       // ~130 ms of voice = Hari has started talking
const END_MS = 800;           // this much quiet = he's finished the phrase
const PRE_ROLL_MS = 400;      // keep a little audio from before speech was detected
const MAX_MS = 15000;

export function createConvMic(ctx, { onSpeechStart, onUtterance, isPlaying = () => false, onLevel = () => {} }) {
  let stream = null, src = null, proc = null, active = false;
  let floor = -60, voiced = 0, inSpeech = false, quietMs = 0, speechMs = 0;
  let pre = [], pcm = [];

  function downsample(buf, from) {
    if (from === RATE) return Float32Array.from(buf);
    const ratio = from / RATE, out = new Float32Array(Math.floor(buf.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const a = Math.floor(i * ratio), b = Math.min(buf.length, Math.floor((i + 1) * ratio));
      let s = 0; for (let j = a; j < b; j++) s += buf[j];
      out[i] = s / Math.max(1, b - a);
    }
    return out;
  }

  function wavBase64(chunks) {
    const n = chunks.reduce((k, c) => k + c.length, 0);
    const view = new DataView(new ArrayBuffer(44 + n * 2));
    const w = (o, s) => [...s].forEach((ch, i) => view.setUint8(o + i, ch.charCodeAt(0)));
    w(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); w(36, "data"); view.setUint32(40, n * 2, true);
    let o = 44;
    for (const c of chunks) for (const x of c) { view.setInt16(o, Math.max(-1, Math.min(1, x)) * 0x7fff, true); o += 2; }
    let bin = ""; const bytes = new Uint8Array(view.buffer);
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  function frame(e) {
    if (!active) return;
    const x = e.inputBuffer.getChannelData(0);
    let s = 0; for (const v of x) s += v * v;
    const db = 10 * Math.log10(s / x.length + 1e-12);
    onLevel(Math.max(0, Math.min(1, (db + 60) / 45)));
    const chunk = downsample(x, ctx.sampleRate);
    // While Dork is talking, whatever echo slips past the canceller must not count as Hari: higher bar.
    const bar = Math.max(floor + (isPlaying() ? 18 : 11), -50);
    if (!inSpeech) {
      pre.push(chunk);
      while (pre.length * FRAME_MS > PRE_ROLL_MS) pre.shift();
      if (db < floor + 6) floor = floor * 0.97 + db * 0.03;  // slow-moving background level
      voiced = db > bar ? voiced + 1 : 0;
      if (voiced >= START_FRAMES) {
        inSpeech = true; quietMs = 0; speechMs = voiced * FRAME_MS;
        pcm = [...pre]; pre = [];
        onSpeechStart();
      }
      return;
    }
    pcm.push(chunk);
    speechMs += FRAME_MS;
    quietMs = db < floor + 6 ? quietMs + FRAME_MS : 0;
    if (quietMs >= END_MS || speechMs >= MAX_MS) {
      inSpeech = false; voiced = 0;
      const phrase = pcm; pcm = [];
      if (speechMs - quietMs > 350) onUtterance(wavBase64(phrase));  // ignore coughs and clicks
    }
  }

  return {
    get active() { return active; },
    async start() {
      if (active) return;
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      src = ctx.createMediaStreamSource(stream);
      proc = ctx.createScriptProcessor(2048, 1, 1);
      proc.onaudioprocess = frame;
      src.connect(proc); proc.connect(ctx.destination);  // (silent: the processor writes nothing)
      floor = -60; voiced = 0; inSpeech = false; pre = []; pcm = [];
      active = true;
    },
    stop() {
      active = false;
      try { proc && proc.disconnect(); src && src.disconnect(); } catch {}
      stream && stream.getTracks().forEach((t) => t.stop());
      stream = src = proc = null;
    },
  };
}
