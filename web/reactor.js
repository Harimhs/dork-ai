// Dork's reactor: a real-time 3D arc reactor (Three.js + bloom). It's Dork's face in assistant mode.
//
//   createReactor(canvas) → { setState(name), setLevel(0..1), setSpectrum(Uint8Array|null), pulse(), dispose() }
//
// States: sleeping · wake · awake · listening · thinking · speaking · error. Every visual parameter eases
// toward the target of the current state, so changes are smooth, never hard cuts.

import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const AMBER = new THREE.Color("#e9a23b");
const CYAN = new THREE.Color("#2ec8ff");
const WHITE = new THREE.Color("#f4fbff");
const RED = new THREE.Color("#ff4d4d");

// target look per state
const LOOK = {
  sleeping:  { energy: 0.22, spin: 0.15, hue: 0.0, bloom: 0.35, core: 0.78, open: 0.0, sweep: 0, burst: 0, ticks: 0.35 },
  wake:      { energy: 1.25, spin: 2.8,  hue: 0.85, bloom: 1.25, core: 1.15, open: 1.0, sweep: 0, burst: 1, ticks: 1 },
  awake:     { energy: 0.75, spin: 0.55, hue: 0.65, bloom: 0.7, core: 1.0, open: 1.0, sweep: 0, burst: 0, ticks: 0.9 },
  listening: { energy: 0.9,  spin: 0.7,  hue: 0.95, bloom: 0.8, core: 1.02, open: 1.0, sweep: 0, burst: 0, ticks: 1 },
  thinking:  { energy: 0.95, spin: 2.4,  hue: 0.35, bloom: 0.8, core: 0.95, open: 1.0, sweep: 1, burst: 0, ticks: 1 },
  speaking:  { energy: 1.0,  spin: 0.9,  hue: 0.7,  bloom: 0.85, core: 1.05, open: 1.0, sweep: 0, burst: 0, ticks: 1 },
  error:     { energy: 0.7,  spin: 0.3,  hue: -1,   bloom: 0.75, core: 0.9, open: 0.6, sweep: 0, burst: 0, ticks: 0.6 },
};

const NOISE = /* glsl */ `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy; i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx; vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_); vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy;
  vec4 h=1.0-abs(x)-abs(y); vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

function webglOK(canvas) {
  try { return !!(canvas.getContext("webgl2") || canvas.getContext("webgl")); } catch { return false; }
}

export function createReactor(canvas, opts = {}) {
  const probe = document.createElement("canvas");
  if (!webglOK(probe)) return createFallback(canvas);

  const small = Math.min(innerWidth, innerHeight) < 600 || matchMedia("(pointer: coarse)").matches;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: !small, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, small ? 1 : 1.5));
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.NoToneMapping;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
  camera.position.set(0, 0, 11);

  const root = new THREE.Group();
  scene.add(root);

  // ---- energy core: noise-displaced sphere with a fresnel rim, plus a white-hot heart for the bloom
  const coreU = { uTime: { value: 0 }, uEnergy: { value: 0.3 }, uLevel: { value: 0 }, uColA: { value: AMBER.clone() }, uColB: { value: CYAN.clone() }, uMix: { value: 0 } };
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(0.78, small ? 24 : 48),
    new THREE.ShaderMaterial({
      uniforms: coreU, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: NOISE + /* glsl */ `
        uniform float uTime, uEnergy, uLevel; varying vec3 vN; varying vec3 vV; varying float vD;
        void main(){
          float n = snoise(normal*1.6 + vec3(uTime*0.35)) * (0.04 + 0.07*uEnergy + 0.2*uLevel);
          n += snoise(normal*4.0 - vec3(uTime*0.8)) * 0.04 * (uEnergy + uLevel);
          vD = n;
          vec3 p = position + normal*n;
          vec4 mv = modelViewMatrix*vec4(p,1.0);
          vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz);
          gl_Position = projectionMatrix*mv;
        }`,
      fragmentShader: /* glsl */ `
        uniform vec3 uColA, uColB; uniform float uMix, uEnergy, uLevel; varying vec3 vN; varying vec3 vV; varying float vD;
        void main(){
          float f = pow(1.0 - max(dot(vN, vV), 0.0), 2.2);
          vec3 c = mix(uColA, uColB, clamp(uMix + vD*1.5, 0.0, 1.0));
          float a = f*(0.3 + 0.4*uEnergy) + 0.03 + vD*0.6 + uLevel*0.1;
          gl_FragColor = vec4(c * (0.25 + f*0.95 + uLevel*0.3), clamp(a, 0.0, 1.0));
        }`,
    }),
  );
  root.add(core);
  const heart = new THREE.Mesh(new THREE.SphereGeometry(0.2, 32, 32), new THREE.MeshBasicMaterial({ color: WHITE.clone(), transparent: true, opacity: 0.9 }));
  root.add(heart);
  const halo = new THREE.Mesh(new THREE.RingGeometry(0.0, 1.05, 64), new THREE.ShaderMaterial({
    uniforms: { uCol: { value: CYAN.clone() }, uI: { value: 0.3 } }, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: "varying vec2 vU; void main(){ vU = position.xy; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }",
    fragmentShader: "uniform vec3 uCol; uniform float uI; varying vec2 vU; void main(){ float d = length(vU)/1.05; float a = pow(1.0-d, 3.0)*uI; gl_FragColor = vec4(uCol*a, a); }",
  }));
  root.add(halo);

  const additive = (color, opacity = 1) => new THREE.MeshBasicMaterial({ color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });

  // ---- the arc-reactor ring: 10 segmented arcs (the classic look), plus an inner and outer hairline
  const segRing = new THREE.Group();
  const SEGS = 10;
  for (let i = 0; i < SEGS; i++) {
    const arc = new THREE.Mesh(new THREE.TorusGeometry(1.55, 0.055, 10, 40, (Math.PI * 2) / SEGS * 0.72), additive(AMBER.clone(), 0.95));
    arc.rotation.z = (i / SEGS) * Math.PI * 2;
    segRing.add(arc);
  }
  root.add(segRing);
  const hair1 = new THREE.Mesh(new THREE.TorusGeometry(1.32, 0.008, 6, 160), additive(CYAN.clone(), 0.45));
  const hair2 = new THREE.Mesh(new THREE.TorusGeometry(1.78, 0.01, 6, 180), additive(CYAN.clone(), 0.35));
  root.add(hair1, hair2);

  // ---- gyroscope rings tilted on different axes
  const gyros = [];
  [[2.15, 0.018, 1.1, 0.3], [2.45, 0.012, -0.6, 1.2], [2.8, 0.01, 0.35, -0.9]].forEach(([r, t, rx, ry], i) => {
    const g = new THREE.Group();
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, t, 6, 220, Math.PI * (1.25 + i * 0.25)), additive(i === 1 ? AMBER.clone() : CYAN.clone(), 0.6));
    const node = new THREE.Mesh(new THREE.SphereGeometry(0.045 + t, 12, 12), additive(WHITE.clone(), 1));
    node.position.set(r, 0, 0);
    g.add(ring, node);
    g.rotation.set(rx, ry, 0);
    g.userData = { axis: new THREE.Vector3(Math.sin(i * 2.1), Math.cos(i * 1.3), 0.5).normalize(), speed: 0.25 + i * 0.12 };
    root.add(g);
    gyros.push(g);
  });

  // ---- HUD dial: 120 tick marks, every 10th long; and a second, counter-rotating dashed dial
  function dial(radius, count, long, len, opacity) {
    const pts = [];
    for (let i = 0; i < count; i++) {
      const a = (i / count) * Math.PI * 2;
      const l = i % long === 0 ? len * 2.4 : len;
      pts.push(Math.cos(a) * radius, Math.sin(a) * radius, 0, Math.cos(a) * (radius + l), Math.sin(a) * (radius + l), 0);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: CYAN.clone(), transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false }));
  }
  const dialA = dial(3.15, 120, 10, 0.07, 0.55);
  const dialB = dial(3.42, 60, 5, 0.04, 0.35);
  root.add(dialA, dialB);

  // ---- waveform ring: radius follows the voice spectrum (mic while listening, Dork's voice while speaking)
  const WAVE = 192;
  const waveGeo = new THREE.BufferGeometry();
  waveGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(WAVE * 3), 3));
  const wave = new THREE.LineLoop(waveGeo, new THREE.LineBasicMaterial({ color: WHITE.clone(), transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending, depthWrite: false }));
  root.add(wave);
  const waveVals = new Float32Array(WAVE);

  // ---- scanning sweep (thinking)
  const sweep = new THREE.Mesh(new THREE.RingGeometry(1.85, 3.1, 64, 1, 0, Math.PI / 5), new THREE.ShaderMaterial({
    uniforms: { uCol: { value: CYAN.clone() }, uI: { value: 0 } }, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
    vertexShader: "varying vec2 vU; void main(){ vU = uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }",
    fragmentShader: "uniform vec3 uCol; uniform float uI; varying vec2 vU; void main(){ float a = pow(vU.x, 4.0)*uI*0.35; gl_FragColor = vec4(uCol*a, a); }",
  }));
  root.add(sweep);

  // ---- particle field: a shell of motes orbiting the core; bursts outward on wake
  const N = small ? 900 : 2600;
  const pGeo = new THREE.BufferGeometry();
  const pos = new Float32Array(N * 3), seed = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const r = 2.2 + Math.pow(Math.random(), 0.6) * 3.8;
    const th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    pos[i * 3] = r * Math.sin(ph) * Math.cos(th);
    pos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) * 0.55;  // flattened into a disc-ish cloud
    pos[i * 3 + 2] = r * Math.cos(ph);
    seed[i] = Math.random();
  }
  pGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  pGeo.setAttribute("seed", new THREE.BufferAttribute(seed, 1));
  const pU = { uTime: { value: 0 }, uBurst: { value: 0 }, uEnergy: { value: 0.3 }, uCol: { value: CYAN.clone() }, uPx: { value: renderer.getPixelRatio() } };
  const particles = new THREE.Points(pGeo, new THREE.ShaderMaterial({
    uniforms: pU, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: /* glsl */ `
      uniform float uTime, uBurst, uEnergy, uPx; attribute float seed; varying float vA;
      void main(){
        vec3 p = position;
        float ang = uTime*(0.05 + seed*0.12)*(0.6+uEnergy);
        float c = cos(ang), s = sin(ang);
        p.xz = mat2(c,-s,s,c)*p.xz;
        p *= 1.0 + uBurst*(0.35 + seed*0.9);
        p.y += sin(uTime*0.6 + seed*40.0)*0.08;
        vec4 mv = modelViewMatrix*vec4(p,1.0);
        gl_PointSize = (1.4 + seed*2.6)*uPx*(6.0/-mv.z)*(0.7+uEnergy*0.6);
        vA = (0.25 + 0.75*fract(seed*7.3 + uTime*0.15))*(0.35+uEnergy*0.65);
        gl_Position = projectionMatrix*mv;
      }`,
    fragmentShader: /* glsl */ `
      uniform vec3 uCol; varying float vA;
      void main(){ float d = length(gl_PointCoord-0.5); if(d>0.5) discard; float a = smoothstep(0.5,0.0,d)*vA; gl_FragColor = vec4(uCol*a, a); }`,
  }));
  root.add(particles);

  // ---- post: bloom
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.7, 0.45, 0.26);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  // ---- state
  let target = LOOK.sleeping, stateName = "sleeping";
  const cur = { ...LOOK.sleeping, hue: 0 };
  let level = 0, levelTarget = 0, spectrum = null, burstKick = 0, flash = 0;
  const mouse = { x: 0, y: 0, tx: 0, ty: 0 };
  const onMove = (e) => {
    const p = e.touches ? e.touches[0] : e;
    mouse.tx = (p.clientX / innerWidth - 0.5) * 2;
    mouse.ty = (p.clientY / innerHeight - 0.5) * 2;
  };
  addEventListener("pointermove", onMove, { passive: true });

  function resize() {
    const w = canvas.clientWidth || innerWidth, h = canvas.clientHeight || innerHeight;
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    camera.aspect = w / h;
    // keep the whole reactor in view on narrow (phone) screens
    camera.position.z = w / h < 0.8 ? 11 / Math.max(0.5, w / h) * 0.75 : 11;
    root.position.y = 0.55;  // sit above the captions
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();

  const clock = new THREE.Clock();
  const tmpCol = new THREE.Color();
  let raf = 0, running = true, t = 0;

  function frame() {
    raf = requestAnimationFrame(frame);
    if (!running) return;
    tick(Math.min(clock.getDelta(), 0.05), true);
  }
  function tick(dt, draw) {
    t += dt;
    const k = 1 - Math.pow(0.0025, dt);   // easing factor (frame-rate independent)
    for (const key of ["energy", "spin", "bloom", "core", "open", "sweep", "ticks"]) cur[key] += (target[key] - cur[key]) * k;
    const hueT = target.hue < 0 ? -1 : target.hue;
    cur.hue += (hueT - cur.hue) * k;
    level += (levelTarget - level) * (1 - Math.pow(0.0005, dt));
    burstKick *= Math.pow(0.18, dt);
    flash *= Math.pow(0.05, dt);
    if (stateName === "wake" && t - wakeAt > 1.6) setState("awake");

    const err = cur.hue < 0 ? Math.min(1, -cur.hue) : 0;
    const mixv = Math.max(0, cur.hue);
    const energy = cur.energy + level * 0.6 + flash;

    // colours: amber (calm) → cyan/white (active); red on error
    tmpCol.copy(AMBER).lerp(CYAN, mixv).lerp(RED, err);
    coreU.uColA.value.copy(AMBER).lerp(RED, err);
    coreU.uColB.value.copy(CYAN).lerp(WHITE, level * 0.6).lerp(RED, err);
    coreU.uMix.value = mixv;
    coreU.uTime.value = t;
    coreU.uEnergy.value = energy;
    coreU.uLevel.value = level;
    const breathe = 1 + Math.sin(t * (stateName === "sleeping" ? 1.1 : 2.2)) * 0.025;
    core.scale.setScalar(cur.core * breathe * (1 + level * 0.12 + flash * 0.25));
    heart.scale.setScalar((0.8 + energy * 0.45) * breathe);
    heart.material.color.copy(WHITE).lerp(tmpCol, 0.25 + err * 0.6);
    heart.material.opacity = 0.25 + Math.min(0.35, energy * 0.25);
    halo.material.uniforms.uCol.value.copy(tmpCol);
    halo.material.uniforms.uI.value = 0.06 + energy * 0.14;

    segRing.rotation.z -= dt * (0.25 + cur.spin * 0.9);
    segRing.children.forEach((s, i) => {
      s.material.color.copy(AMBER).lerp(WHITE, 0.15 + level * 0.5 * (0.5 + 0.5 * Math.sin(t * 6 + i))).lerp(RED, err);
      s.material.opacity = 0.25 + 0.5 * Math.min(1, cur.open + 0.3);
    });
    segRing.scale.setScalar(0.92 + cur.open * 0.08 + level * 0.05);
    hair1.rotation.z += dt * 0.4 * cur.spin;
    hair2.rotation.z -= dt * 0.3 * cur.spin;
    hair1.material.color.copy(tmpCol); hair2.material.color.copy(tmpCol);

    gyros.forEach((g, i) => {
      g.rotateOnAxis(g.userData.axis, dt * g.userData.speed * (0.4 + cur.spin));
      g.children[0].material.opacity = (0.1 + 0.32 * cur.open) * (0.6 + energy * 0.4);
      g.children[0].material.color.copy(i === 1 ? AMBER : tmpCol).lerp(RED, err);
      g.scale.setScalar(0.85 + cur.open * 0.15 + burstKick * 0.25);
    });

    dialA.rotation.z += dt * 0.08 * (0.5 + cur.spin);
    dialB.rotation.z -= dt * 0.12 * (0.5 + cur.spin);
    dialA.material.opacity = 0.12 + 0.45 * cur.ticks;
    dialB.material.opacity = 0.08 + 0.3 * cur.ticks;
    dialA.material.color.copy(tmpCol); dialB.material.color.copy(tmpCol);

    // waveform ring
    const wp = waveGeo.attributes.position.array;
    for (let i = 0; i < WAVE; i++) {
      let v;
      if (spectrum && spectrum.length) {
        const j = Math.floor((i < WAVE / 2 ? i : WAVE - i) / (WAVE / 2) * spectrum.length * 0.6);
        v = spectrum[j] / 255;
      } else {
        v = level * (0.5 + 0.5 * Math.sin(i * 0.45 + t * 9) * Math.sin(i * 0.13 - t * 4));
      }
      waveVals[i] += (v - waveVals[i]) * 0.35;
      const a = (i / WAVE) * Math.PI * 2;
      const r = 1.95 + waveVals[i] * 0.55 * (stateName === "sleeping" ? 0.2 : 1);
      wp[i * 3] = Math.cos(a) * r; wp[i * 3 + 1] = Math.sin(a) * r; wp[i * 3 + 2] = 0;
    }
    waveGeo.attributes.position.needsUpdate = true;
    wave.material.opacity = 0.15 + Math.min(0.85, level * 1.5 + cur.open * 0.25);
    wave.material.color.copy(tmpCol).lerp(WHITE, 0.25);

    sweep.rotation.z -= dt * 4.2;
    sweep.material.uniforms.uI.value = cur.sweep;
    sweep.material.uniforms.uCol.value.copy(tmpCol);

    pU.uTime.value = t;
    pU.uEnergy.value = energy;
    pU.uBurst.value = burstKick;
    pU.uCol.value.copy(tmpCol);

    bloom.strength = cur.bloom * (0.8 + level * 0.2) + flash * 0.5;

    // camera: slow drift + parallax toward the pointer
    mouse.x += (mouse.tx - mouse.x) * 0.04;
    mouse.y += (mouse.ty - mouse.y) * 0.04;
    root.rotation.y = mouse.x * 0.28 + Math.sin(t * 0.13) * 0.08;
    root.rotation.x = mouse.y * 0.22 + Math.cos(t * 0.11) * 0.06;
    camera.lookAt(0, 0, 0);

    if (draw) composer.render();
  }
  let wakeAt = 0;
  frame();

  let paused = false;  // set by the desktop app when the window is hidden (it can't rely on document.hidden)
  const onVis = () => { running = !document.hidden && !paused; clock.getDelta(); };
  document.addEventListener("visibilitychange", onVis);

  function setState(name) {
    if (!LOOK[name]) return;
    if (name === "wake") { wakeAt = t; burstKick = 1; flash = 0.9; }
    stateName = name;
    target = LOOK[name];
  }
  return {
    setState,
    get state() { return stateName; },
    setPaused(p) { paused = !!p; onVis(); },
    setLevel(v) { levelTarget = Math.max(0, Math.min(1, v || 0)); },
    setSpectrum(arr) { spectrum = arr; },
    pulse() { flash = 0.5; burstKick = Math.max(burstKick, 0.35); },
    // for tests/screenshots when the page isn't visible: run `seconds` of animation, then draw once
    advance(seconds) { const n = Math.round(seconds * 60); for (let i = 0; i < n; i++) tick(1 / 60, i === n - 1); },
    dispose() {
      cancelAnimationFrame(raf); ro.disconnect();
      removeEventListener("pointermove", onMove);
      document.removeEventListener("visibilitychange", onVis);
      renderer.dispose();
    },
  };
}

// ---------- no WebGL: a 2D canvas reactor with the same API ----------
function createFallback(canvas) {
  const ctx = canvas.getContext("2d");
  let state = "sleeping", level = 0, lt = 0, t = 0, raf = 0, spectrum = null;
  const look = { sleeping: [0.25, "#e9a23b"], wake: [1, "#7fe3ff"], awake: [0.7, "#7fe3ff"], listening: [0.85, "#7fe3ff"],
    thinking: [0.8, "#a8d8ff"], speaking: [0.9, "#c8f2ff"], error: [0.6, "#ff4d4d"] };
  function frame() {
    raf = requestAnimationFrame(frame);
    t += 0.016; level += (lt - level) * 0.2;
    const w = canvas.width = canvas.clientWidth * devicePixelRatio, h = canvas.height = canvas.clientHeight * devicePixelRatio;
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.18;
    const [e, col] = look[state] || look.awake;
    ctx.clearRect(0, 0, w, h);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.6);
    g.addColorStop(0, "rgba(255,255,255," + (0.6 * e + level * 0.3) + ")"); g.addColorStop(0.3, col); g.addColorStop(1, "transparent");
    ctx.fillStyle = g; ctx.globalAlpha = 0.5 + e * 0.5; ctx.beginPath(); ctx.arc(cx, cy, R * 1.6, 0, 7); ctx.fill();
    ctx.globalAlpha = 1; ctx.strokeStyle = col; ctx.lineWidth = 6 * devicePixelRatio;
    for (let i = 0; i < 10; i++) {
      const a = t * (0.4 + e) + (i / 10) * Math.PI * 2;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.9, a, a + 0.45); ctx.stroke();
    }
    ctx.lineWidth = 1.5 * devicePixelRatio; ctx.beginPath();
    for (let i = 0; i <= 120; i++) {
      const a = (i / 120) * Math.PI * 2;
      const v = spectrum ? spectrum[i % spectrum.length] / 255 : level * Math.abs(Math.sin(i * 0.4 + t * 8));
      const r = R * 2.4 + v * R * 0.6;
      i ? ctx.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : ctx.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r);
    }
    ctx.stroke();
  }
  frame();
  return { setState(s) { state = s === "wake" ? "awake" : s; }, get state() { return state; }, setLevel(v) { lt = v || 0; },
    setSpectrum(a) { spectrum = a; }, pulse() {}, dispose() { cancelAnimationFrame(raf); } };
}
