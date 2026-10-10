// Dork's reactor, 2D edition: the same face and states as the 3D one, drawn with a plain canvas so it stays
// smooth on integrated graphics. Glows are pre-rendered once; each frame only composites a few shapes.
//
//   createReactor(canvas) → { setState(name), setLevel(0..1), setSpectrum(Uint8Array|null), pulse(), setPaused(b), dispose() }
//
// States: sleeping · wake · awake · listening · thinking · speaking · error. Every parameter eases toward the
// current state's target, so changes are smooth, never hard cuts.

const AMBER = [233, 162, 59], CYAN = [46, 200, 255], WHITE = [244, 251, 255], RED = [255, 77, 77];

const LOOK = {
  sleeping:  { energy: 0.25, spin: 0.12, mix: 0.0,  core: 0.8,  open: 0.15, sweep: 0, bars: 0 },
  wake:      { energy: 1.3,  spin: 2.4,  mix: 0.85, core: 1.18, open: 1.0,  sweep: 0, bars: 0.4 },
  awake:     { energy: 0.75, spin: 0.5,  mix: 0.6,  core: 1.0,  open: 1.0,  sweep: 0, bars: 0.15 },
  listening: { energy: 0.9,  spin: 0.6,  mix: 0.95, core: 1.03, open: 1.0,  sweep: 0, bars: 1 },
  thinking:  { energy: 0.95, spin: 2.2,  mix: 0.35, core: 0.96, open: 1.0,  sweep: 1, bars: 0.2 },
  speaking:  { energy: 1.0,  spin: 0.8,  mix: 0.7,  core: 1.05, open: 1.0,  sweep: 0, bars: 1 },
  error:     { energy: 0.7,  spin: 0.25, mix: -1,   core: 0.9,  open: 0.6,  sweep: 0, bars: 0 },
};

const lerp = (a, b, t) => a + (b - a) * t;
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
const mixc = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];

// A soft round glow, drawn once and reused as a sprite every frame (much cheaper than shadowBlur).
function glowSprite(color, size = 256) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, rgba(color, 1));
  grad.addColorStop(0.25, rgba(color, 0.55));
  grad.addColorStop(0.6, rgba(color, 0.12));
  grad.addColorStop(1, rgba(color, 0));
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return c;
}

export function createReactor(canvas) {
  const ctx = canvas.getContext("2d", { alpha: true, desynchronized: true });
  const sprites = { amber: glowSprite(AMBER), cyan: glowSprite(CYAN), white: glowSprite(WHITE), red: glowSprite(RED) };
  const cur = { ...LOOK.sleeping };
  let target = LOOK.sleeping, state = "sleeping";
  let level = 0, shownLevel = 0, spectrum = null, burst = 0, paused = false, raf = 0;
  let rot = 0, sweepAng = 0, last = performance.now(), W = 0, H = 0, dpr = 1;
  const bars = new Float32Array(48);

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);  // sharp enough, without 4× the pixels on hi-DPI screens
    const r = canvas.getBoundingClientRect();
    W = Math.max(1, r.width); H = Math.max(1, r.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();

  function frame(now) {
    raf = 0;
    if (paused) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const k = 1 - Math.pow(0.002, dt);  // ease ~0.4 s toward the state's look
    for (const key in cur) cur[key] = lerp(cur[key], target[key], k);
    shownLevel = lerp(shownLevel, level, 1 - Math.pow(0.0005, dt));
    burst = Math.max(0, burst - dt * 1.6);
    rot += dt * cur.spin;
    sweepAng += dt * 3.2;

    const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.34;
    const e = cur.energy + shownLevel * 0.6 + burst * 0.8;
    const hue = cur.mix < 0 ? RED : mixc(AMBER, CYAN, Math.max(0, cur.mix) * 0.55);
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";

    // ambient glow
    const g = R * (2.2 + shownLevel * 0.5 + burst * 0.6);
    ctx.globalAlpha = Math.min(1, 0.18 + e * 0.22);
    ctx.drawImage(cur.mix < 0 ? sprites.red : sprites.amber, cx - g, cy - g, g * 2, g * 2);

    // outer segmented ring
    const segs = 12, open = cur.open;
    ctx.lineCap = "round";
    ctx.lineWidth = R * 0.06;
    for (let i = 0; i < segs; i++) {
      const a0 = rot + (i / segs) * Math.PI * 2, span = (Math.PI * 2 / segs) * 0.68 * open;
      ctx.strokeStyle = rgba(hue, 0.35 + 0.5 * Math.min(1, e));
      ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.12, a0, a0 + span); ctx.stroke();
    }
    // fine counter-rotating hairlines
    ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(CYAN, 0.18 + 0.25 * cur.mix);
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.92, -rot * 1.4, -rot * 1.4 + Math.PI * 1.6); ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.32, rot * 0.7, rot * 0.7 + Math.PI * 1.3); ctx.stroke();

    // thinking: a scanning sweep
    if (cur.sweep > 0.02) {
      ctx.globalAlpha = cur.sweep;
      const grad = ctx.createConicGradient ? ctx.createConicGradient(sweepAng, cx, cy) : null;
      if (grad) {
        grad.addColorStop(0, rgba(CYAN, 0.35)); grad.addColorStop(0.12, rgba(CYAN, 0)); grad.addColorStop(1, rgba(CYAN, 0));
        ctx.fillStyle = grad;
        ctx.beginPath(); ctx.arc(cx, cy, R * 1.45, 0, Math.PI * 2); ctx.arc(cx, cy, R * 0.95, 0, Math.PI * 2, true); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // listening / speaking: a ring of bars that follows the sound
    if (cur.bars > 0.02) {
      const n = bars.length;
      for (let i = 0; i < n; i++) {
        const v = spectrum ? spectrum[Math.floor((i / n) * spectrum.length * 0.6)] / 255 : shownLevel * (0.6 + 0.4 * Math.sin(now / 180 + i));
        bars[i] = lerp(bars[i], v, 0.35);
      }
      ctx.lineWidth = Math.max(1.5, R * 0.025);
      ctx.strokeStyle = rgba(hue, 0.55 * cur.bars);
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const a = (i / n) * Math.PI * 2 - Math.PI / 2, r0 = R * 0.78, r1 = r0 + R * (0.04 + bars[i] * 0.32) * cur.bars;
        ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
        ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      }
      ctx.stroke();
    }

    // core
    const cr = R * 0.42 * cur.core * (1 + shownLevel * 0.18 + burst * 0.25);
    ctx.globalAlpha = Math.min(1, 0.55 + e * 0.35);
    ctx.drawImage(cur.mix > 0.5 ? sprites.cyan : sprites.amber, cx - cr * 1.6, cy - cr * 1.6, cr * 3.2, cr * 3.2);
    ctx.globalAlpha = Math.min(1, 0.6 + e * 0.3);
    ctx.drawImage(sprites.white, cx - cr * 0.7, cy - cr * 0.7, cr * 1.4, cr * 1.4);
    ctx.globalAlpha = 1;
    ctx.lineWidth = R * 0.035;
    ctx.strokeStyle = rgba(hue, 0.85);
    ctx.beginPath(); ctx.arc(cx, cy, cr * 0.95, 0, Math.PI * 2); ctx.stroke();
    ctx.globalCompositeOperation = "source-over";

    // keep animating while there's motion; when fully asleep and still, draw at ~12 fps to save power
    if (state === "sleeping" && shownLevel < 0.01 && burst === 0 && Math.abs(cur.energy - target.energy) < 0.01) {
      setTimeout(() => { if (!raf && !paused) raf = requestAnimationFrame(frame); }, 80);
    } else {
      raf = requestAnimationFrame(frame);
    }
  }
  raf = requestAnimationFrame(frame);

  const kick = () => { if (!raf && !paused) { last = performance.now(); raf = requestAnimationFrame(frame); } };
  return {
    setState(name) {
      state = LOOK[name] ? name : "awake";
      target = LOOK[state];
      if (name === "wake") burst = 1;
      kick();
    },
    setLevel(v) { level = Math.max(0, Math.min(1, v || 0)); kick(); },
    setSpectrum(s) { spectrum = s; },
    pulse() { burst = Math.max(burst, 0.6); kick(); },
    setPaused(p) { paused = !!p; if (paused && raf) { cancelAnimationFrame(raf); raf = 0; } else kick(); },
    dispose() { paused = true; if (raf) cancelAnimationFrame(raf); ro.disconnect(); },
  };
}
