// Сфера Джарвиса: полая голубая сфера из светящихся частиц со связями между соседями.
// Чистый WebGL 1 без библиотек и CDN; без WebGL — облегчённый Canvas 2D; без canvas — остаётся SVG-глаз.
// Своя реализация. Визуальный ориентир — демонстрация orb из ethanplusai/jarvis; код оттуда не взят:
// его лицензия разрешает только некоммерческое использование.
//
// Поведение связано с настоящими состояниями HUD (idle/listening/thinking/acting/speaking/error).
// setSignal(0..1) — настоящий уровень (микрофон или звучащая речь); setSignal(null) — сигнала нет,
// тогда играет обозначенная анимация состояния, а не выдуманная громкость.
(function () {
  "use strict";

  const QUALITY = {
    high: { points: 1600, dpr: 2, fps: 60, k: 3 },
    medium: { points: 950, dpr: 1.5, fps: 60, k: 3 },
    low: { points: 480, dpr: 1, fps: 30, k: 2 },
  };
  const CANVAS_Q = { points: 320, dpr: 1, fps: 30, k: 2 };
  const ORDER = ["high", "medium", "low"];
  // Цели состояний; переходы между ними плавные (экспонента ~0.6 с).
  //   breath — амплитуда дыхания, breathHz — его частота; spin — вращение, рад/с; twist — закрутка
  //   поясов (рад), twistHz — скорость её волны; ripple — рябь поверхности; glow/bright/lines — свет.
  const STATES = {
    idle: { breath: 0.032, breathHz: 0.12, spin: 0.05, twist: 0, twistHz: 0.1, ripple: 0.014, rippleHz: 0.07, glow: 0.5, bright: 0.85, lines: 0.2, cam: 1 },
    listening: { breath: 0.012, breathHz: 0.2, spin: 0.06, twist: 0, twistHz: 0.1, ripple: 0.022, rippleHz: 0.2, glow: 0.62, bright: 0.95, lines: 0.24, cam: 0.6 },
    thinking: { breath: 0.012, breathHz: 0.16, spin: 0.2, twist: 0.55, twistHz: 0.32, ripple: 0.045, rippleHz: 0.35, glow: 0.68, bright: 1.0, lines: 0.3, cam: 1.25 },
    acting: { breath: 0.012, breathHz: 0.2, spin: 0.32, twist: 0.8, twistHz: 0.5, ripple: 0.055, rippleHz: 0.5, glow: 0.72, bright: 1.05, lines: 0.32, cam: 1.4 },
    speaking: { breath: 0.012, breathHz: 0.16, spin: 0.07, twist: 0.06, twistHz: 0.2, ripple: 0.022, rippleHz: 0.25, glow: 0.66, bright: 0.95, lines: 0.26, cam: 0.8 },
    error: { breath: 0.008, breathHz: 0.06, spin: 0.012, twist: 0, twistHz: 0.05, ripple: 0.006, rippleHz: 0.04, glow: 0.22, bright: 0.5, lines: 0.12, cam: 0.25 },
  };
  const COLOR = [0.42, 0.8, 1.0];      // голубой
  const HOT = [0.86, 0.97, 1.0];       // искры — почти белые
  const ERROR_COLOR = [1.0, 0.62, 0.35];
  const CAM_DIST = 3.7, FOV = 40 * Math.PI / 180;

  // ---------- геометрия: точки на оболочке и связи с ближайшими соседями ----------
  function rng(seed) {  // mulberry32: одинаковая сфера при каждом запуске (стабильные снимки)
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function buildSphere(n, k) {
    const rand = rng(1729 + n);
    const pos = new Float32Array(n * 3), seed = new Float32Array(n);
    const golden = Math.PI * (3 - Math.sqrt(5));
    const spacing = Math.sqrt(4 * Math.PI / n);
    for (let i = 0; i < n; i++) {
      const y = 1 - 2 * (i + 0.5) / n, r = Math.sqrt(1 - y * y), th = i * golden;
      let x = Math.cos(th) * r, z = Math.sin(th) * r, yy = y;
      // сбить правильную решётку: живой, неровный узор
      x += (rand() - 0.5) * spacing * 0.9; yy += (rand() - 0.5) * spacing * 0.9; z += (rand() - 0.5) * spacing * 0.9;
      const len = Math.hypot(x, yy, z), shell = 1 + (rand() - 0.5) * 0.11;  // полая оболочка с толщиной
      pos[i * 3] = x / len * shell; pos[i * 3 + 1] = yy / len * shell; pos[i * 3 + 2] = z / len * shell;
      seed[i] = rand();
    }
    // соседи через пространственную сетку: O(n)
    const cell = spacing * 1.6, grid = new Map();
    const key = (a, b, c) => a + "," + b + "," + c;
    for (let i = 0; i < n; i++) {
      const g = key(Math.floor(pos[i * 3] / cell), Math.floor(pos[i * 3 + 1] / cell), Math.floor(pos[i * 3 + 2] / cell));
      (grid.get(g) || grid.set(g, []).get(g)).push(i);
    }
    const maxD2 = (spacing * 1.9) ** 2, edges = [], seen = new Set();
    for (let i = 0; i < n; i++) {
      const cx = Math.floor(pos[i * 3] / cell), cy = Math.floor(pos[i * 3 + 1] / cell), cz = Math.floor(pos[i * 3 + 2] / cell);
      const near = [];
      for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) for (let c = -1; c <= 1; c++) {
        const list = grid.get(key(cx + a, cy + b, cz + c));
        if (!list) continue;
        for (const j of list) {
          if (j === i) continue;
          const dx = pos[i * 3] - pos[j * 3], dy = pos[i * 3 + 1] - pos[j * 3 + 1], dz = pos[i * 3 + 2] - pos[j * 3 + 2];
          const d2 = dx * dx + dy * dy + dz * dz;
          if (d2 < maxD2) near.push([d2, j]);
        }
      }
      near.sort((p, q) => p[0] - q[0]);
      // не все точки связаны одинаково: так получаются нити и пустоты, как у паутины
      const take = Math.min(near.length, seed[i] < 0.18 ? 0 : seed[i] > 0.82 ? k + 1 : k);
      for (let m = 0; m < take; m++) {
        const j = near[m][1], id = i < j ? i * n + j : j * n + i;
        if (!seen.has(id)) { seen.add(id); edges.push(i, j); }
      }
    }
    return { pos, seed, edges: new Uint32Array(edges), spacing };
  }

  // ---------- матрицы ----------
  function perspective(aspect) {
    const f = 1 / Math.tan(FOV / 2), near = 0.1, far = 20;
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, 2 * far * near / (near - far), 0]);
  }
  function modelView(rotY, rotX, eye) {
    // M = Rx · Ry, V = lookAt(eye → 0), результат — column-major для WebGL
    const cy = Math.cos(rotY), sy = Math.sin(rotY), cx = Math.cos(rotX), sx = Math.sin(rotX);
    const m = [cy, sx * sy, -cx * sy, 0, 0, cx, sx, 0, sy, -sx * cy, cx * cy, 0, 0, 0, 0, 1];
    const zl = Math.hypot(eye[0], eye[1], eye[2]);
    const z = [eye[0] / zl, eye[1] / zl, eye[2] / zl];
    let x = [z[2], 0, -z[0]]; const xl = Math.hypot(x[0], x[1], x[2]) || 1; x = [x[0] / xl, x[1] / xl, x[2] / xl];
    const y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]];
    const v = [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
      -(x[0] * eye[0] + x[1] * eye[1] + x[2] * eye[2]), -(y[0] * eye[0] + y[1] * eye[1] + y[2] * eye[2]),
      -(z[0] * eye[0] + z[1] * eye[1] + z[2] * eye[2]), 1];
    const out = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let i = 0; i < 4; i++) s += v[i * 4 + r] * m[c * 4 + i];
      out[c * 4 + r] = s;
    }
    return out;
  }

  // ---------- смещение частиц: одна формула в шейдере и в Canvas 2D ----------
  const DISPLACE_GLSL = `
uniform float uScale, uTwist, uTwistPh, uRipple, uRipplePh, uShimmer, uShimmerPh;
vec3 displace(vec3 p, float s) {
  float r = length(p);
  vec3 n = p / r;
  float a = uTwist * sin(uTwistPh + n.y * 4.0 + s * 0.6);      // закрутка поясов волной
  float c = cos(a), si = sin(a);
  n = vec3(c * n.x - si * n.z, n.y, si * n.x + c * n.z);
  float w = sin(n.x * 3.1 + uRipplePh) * sin(n.y * 2.7 - uRipplePh * 0.8 + 1.3) * sin(n.z * 3.4 + uRipplePh * 0.6 + 2.1);
  float sh = sin(uShimmerPh * (0.7 + fract(s * 7.13)) + s * 6.2832);
  return n * r * uScale * (1.0 + uRipple * w * 2.0 + uShimmer * sh);
}`;
  function displaceJS(x, y, z, s, u, out) {
    const r = Math.hypot(x, y, z); let nx = x / r, ny = y / r, nz = z / r;
    const a = u.twist * Math.sin(u.twistPh + ny * 4 + s * 0.6), c = Math.cos(a), si = Math.sin(a);
    const tx = c * nx - si * nz; nz = si * nx + c * nz; nx = tx;
    const w = Math.sin(nx * 3.1 + u.ripplePh) * Math.sin(ny * 2.7 - u.ripplePh * 0.8 + 1.3) * Math.sin(nz * 3.4 + u.ripplePh * 0.6 + 2.1);
    const sh = Math.sin(u.shimmerPh * (0.7 + ((s * 7.13) % 1)) + s * 6.2832);
    const k = r * u.scale * (1 + u.ripple * w * 2 + u.shimmer * sh);
    out[0] = nx * k; out[1] = ny * k; out[2] = nz * k;
  }

  const POINT_VS = `precision highp float;
attribute vec3 aPos; attribute float aSeed;
uniform mat4 uMV, uP; uniform float uSize, uBright;
varying float vA; varying float vHot;
${DISPLACE_GLSL}
void main() {
  vec4 v = uMV * vec4(displace(aPos, aSeed), 1.0);
  gl_Position = uP * v;
  float back = clamp((-v.z - ${(CAM_DIST - 1.25).toFixed(2)}) / 2.5, 0.0, 1.0);   // 0 — ближняя сторона, 1 — дальняя
  vA = mix(1.0, 0.22, back) * uBright * (0.6 + 0.4 * fract(aSeed * 13.7));
  vHot = step(0.9, fract(aSeed * 31.1)) * (1.0 - back);
  gl_PointSize = uSize * (0.65 + 0.7 * fract(aSeed * 5.3)) / -v.z;
}`;
  const POINT_FS = `precision mediump float;
uniform vec3 uColor, uHot;
varying float vA; varying float vHot;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float d = dot(c, c) * 4.0;
  if (d > 1.0) discard;
  float a = exp(-d * 3.2) * vA;
  gl_FragColor = vec4(mix(uColor, uHot, vHot) * a, a);
}`;
  const LINE_VS = `precision highp float;
attribute vec3 aPos; attribute float aSeed; attribute vec3 aOther; attribute float aOtherSeed;
uniform mat4 uMV, uP; uniform float uLine, uTime, uMaxLen;
varying float vA;
${DISPLACE_GLSL}
void main() {
  vec3 q = displace(aPos, aSeed), o = displace(aOther, aOtherSeed);
  vec4 v = uMV * vec4(q, 1.0);
  gl_Position = uP * v;
  float back = clamp((-v.z - ${(CAM_DIST - 1.25).toFixed(2)}) / 2.5, 0.0, 1.0);
  float stretch = smoothstep(uMaxLen * 2.2, uMaxLen * 1.1, distance(q, o) / uScale);  // растянутая связь гаснет
  float life = 0.55 + 0.45 * sin(uTime * 0.45 + (aSeed + aOtherSeed) * 40.0);         // связи то ярче, то тусклее
  vA = uLine * mix(1.0, 0.18, back) * stretch * life;
}`;
  const LINE_FS = `precision mediump float;
uniform vec3 uColor; varying float vA;
void main() { gl_FragColor = vec4(uColor * vA, vA); }`;
  const GLOW_VS = `attribute vec2 aXY; varying vec2 vXY; void main() { vXY = aXY; gl_Position = vec4(aXY, 0.0, 1.0); }`;
  const GLOW_FS = `precision mediump float;
uniform vec3 uColor; uniform float uGlow, uR; uniform vec2 uAspect; varying vec2 vXY;
void main() {
  float d = length(vXY * uAspect) / uR;
  // полая сфера: свет собирается у края (как атмосфера), внутри — едва заметная дымка
  float rim = exp(-pow((d - 0.98) / 0.16, 2.0));
  float a = uGlow * (0.30 * rim + 0.07 * exp(-d * d * 1.4) + 0.05 * exp(-d * d * 0.35)) * (1.0 - smoothstep(1.5, 2.3, d));
  gl_FragColor = vec4(uColor * a, a);
}`;

  // ---------- рендер WebGL ----------
  function makeGL(canvas) {
    const opts = { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, powerPreference: "low-power", preserveDrawingBuffer: false };
    const gl = canvas.getContext("webgl", opts) || canvas.getContext("experimental-webgl", opts);
    if (!gl) return null;
    function shader(type, src) {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
      return s;
    }
    function program(vs, fs) {
      const p = gl.createProgram();
      gl.attachShader(p, shader(gl.VERTEX_SHADER, vs)); gl.attachShader(p, shader(gl.FRAGMENT_SHADER, fs));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || "link");
      const loc = {};
      const nu = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < nu; i++) { const u = gl.getActiveUniform(p, i); loc[u.name] = gl.getUniformLocation(p, u.name); }
      const na = gl.getProgramParameter(p, gl.ACTIVE_ATTRIBUTES);
      for (let i = 0; i < na; i++) { const a = gl.getActiveAttrib(p, i); loc[a.name] = gl.getAttribLocation(p, a.name); }
      return { p, loc };
    }
    let progs;
    try {
      progs = { point: program(POINT_VS, POINT_FS), line: program(LINE_VS, LINE_FS), glow: program(GLOW_VS, GLOW_FS) };
    } catch (e) { console.warn("Сфера: WebGL-шейдеры не собрались —", e.message); return null; }
    const glowBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, glowBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    let geo = null, pointBuf = null, lineBuf = null;

    function load(sphere) {
      geo = sphere;
      const n = sphere.seed.length, pts = new Float32Array(n * 4);
      for (let i = 0; i < n; i++) { pts.set(sphere.pos.subarray(i * 3, i * 3 + 3), i * 4); pts[i * 4 + 3] = sphere.seed[i]; }
      const e = sphere.edges, lines = new Float32Array(e.length * 8);
      for (let m = 0; m < e.length; m += 2) {
        for (let side = 0; side < 2; side++) {
          const a = e[m + side], b = e[m + 1 - side], o = (m + side) * 8;
          lines[o] = sphere.pos[a * 3]; lines[o + 1] = sphere.pos[a * 3 + 1]; lines[o + 2] = sphere.pos[a * 3 + 2]; lines[o + 3] = sphere.seed[a];
          lines[o + 4] = sphere.pos[b * 3]; lines[o + 5] = sphere.pos[b * 3 + 1]; lines[o + 6] = sphere.pos[b * 3 + 2]; lines[o + 7] = sphere.seed[b];
        }
      }
      if (pointBuf) gl.deleteBuffer(pointBuf);
      if (lineBuf) gl.deleteBuffer(lineBuf);
      pointBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, pointBuf); gl.bufferData(gl.ARRAY_BUFFER, pts, gl.STATIC_DRAW);
      lineBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf); gl.bufferData(gl.ARRAY_BUFFER, lines, gl.STATIC_DRAW);
    }
    function common(loc, u) {
      gl.uniformMatrix4fv(loc.uMV, false, u.mv); gl.uniformMatrix4fv(loc.uP, false, u.proj);
      gl.uniform1f(loc.uScale, u.scale); gl.uniform1f(loc.uTwist, u.twist); gl.uniform1f(loc.uTwistPh, u.twistPh);
      gl.uniform1f(loc.uRipple, u.ripple); gl.uniform1f(loc.uRipplePh, u.ripplePh);
      gl.uniform1f(loc.uShimmer, u.shimmer); gl.uniform1f(loc.uShimmerPh, u.shimmerPh);
    }
    function attrib(loc, name, size, stride, offset) {
      if (loc[name] === undefined || loc[name] < 0) return;
      gl.enableVertexAttribArray(loc[name]);
      gl.vertexAttribPointer(loc[name], size, gl.FLOAT, false, stride, offset);
    }
    function draw(u, w, h) {
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);  // свечение складывается
      const aspect = [w >= h ? w / h : 1, h > w ? h / w : 1];
      // ореол
      let g = progs.glow; gl.useProgram(g.p);
      gl.bindBuffer(gl.ARRAY_BUFFER, glowBuf); attrib(g.loc, "aXY", 2, 8, 0);
      gl.uniform3fv(g.loc.uColor, u.color); gl.uniform1f(g.loc.uGlow, u.glow); gl.uniform1f(g.loc.uR, u.glowR); gl.uniform2fv(g.loc.uAspect, aspect);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disableVertexAttribArray(g.loc.aXY);
      // связи
      g = progs.line; gl.useProgram(g.p); common(g.loc, u);
      gl.uniform1f(g.loc.uLine, u.lines); gl.uniform1f(g.loc.uTime, u.time); gl.uniform1f(g.loc.uMaxLen, geo.spacing * 1.9);
      gl.uniform3fv(g.loc.uColor, u.color);
      gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf);
      attrib(g.loc, "aPos", 3, 32, 0); attrib(g.loc, "aSeed", 1, 32, 12); attrib(g.loc, "aOther", 3, 32, 16); attrib(g.loc, "aOtherSeed", 1, 32, 28);
      gl.drawArrays(gl.LINES, 0, geo.edges.length);
      ["aPos", "aSeed", "aOther", "aOtherSeed"].forEach(a => g.loc[a] >= 0 && gl.disableVertexAttribArray(g.loc[a]));
      // частицы
      g = progs.point; gl.useProgram(g.p); common(g.loc, u);
      gl.uniform1f(g.loc.uSize, u.pointSize); gl.uniform1f(g.loc.uBright, u.bright);
      gl.uniform3fv(g.loc.uColor, u.color); gl.uniform3fv(g.loc.uHot, u.hot);
      gl.bindBuffer(gl.ARRAY_BUFFER, pointBuf);
      attrib(g.loc, "aPos", 3, 16, 0); attrib(g.loc, "aSeed", 1, 16, 12);
      gl.drawArrays(gl.POINTS, 0, geo.seed.length);
      ["aPos", "aSeed"].forEach(a => g.loc[a] >= 0 && gl.disableVertexAttribArray(g.loc[a]));
    }
    // Программный рендер (нет драйвера видеокарты, удалённый рабочий стол, ВМ) рисует на процессоре —
    // там сфера сразу начинает с низкого качества.
    let name = "";
    try { const dbg = gl.getExtension("WEBGL_debug_renderer_info"); name = String(gl.getParameter(dbg ? dbg.UNMASKED_RENDERER_WEBGL : gl.RENDERER)); } catch (e) { /* */ }
    return { kind: "webgl", load, draw, lost: () => gl.isContextLost(), gpu: name,
      software: /swiftshader|llvmpipe|softpipe|software|basic render|microsoft basic/i.test(name) };
  }

  // ---------- рендер Canvas 2D (нет WebGL): меньше частиц, та же формула движения ----------
  function make2D(canvas) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    let geo = null, proj = null;
    const tmp = [0, 0, 0];
    function load(sphere) { geo = sphere; proj = new Float32Array(sphere.seed.length * 3); }
    function draw(u, w, h) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = "lighter";
      const cx = w / 2, cy = h / 2, f = Math.min(w, h) / 2 / Math.tan(FOV / 2), m = u.mv;
      const col = u.color.map(c => Math.round(c * 255)).join(",");
      const R = u.glowR * Math.min(w, h) / 2;
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.5);
      glow.addColorStop(0, `rgba(${col},${(0.05 * u.glow).toFixed(3)})`);
      glow.addColorStop(0.55, `rgba(${col},${(0.07 * u.glow).toFixed(3)})`);
      glow.addColorStop(0.66, `rgba(${col},${(0.24 * u.glow).toFixed(3)})`);
      glow.addColorStop(1, `rgba(${col},0)`);
      ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
      const n = geo.seed.length;
      for (let i = 0; i < n; i++) {
        displaceJS(geo.pos[i * 3], geo.pos[i * 3 + 1], geo.pos[i * 3 + 2], geo.seed[i], u, tmp);
        const vx = m[0] * tmp[0] + m[4] * tmp[1] + m[8] * tmp[2] + m[12];
        const vy = m[1] * tmp[0] + m[5] * tmp[1] + m[9] * tmp[2] + m[13];
        const vz = m[2] * tmp[0] + m[6] * tmp[1] + m[10] * tmp[2] + m[14];
        proj[i * 3] = cx + vx * f / -vz; proj[i * 3 + 1] = cy - vy * f / -vz;
        proj[i * 3 + 2] = Math.min(1, Math.max(0, (-vz - (CAM_DIST - 1.25)) / 2.5));
      }
      // связи — тремя слоями глубины (одна обводка на слой — дёшево)
      ctx.lineWidth = 1;
      for (let layer = 0; layer < 3; layer++) {
        ctx.beginPath();
        for (let e = 0; e < geo.edges.length; e += 2) {
          const a = geo.edges[e], b = geo.edges[e + 1];
          if (Math.min(2, Math.floor((proj[a * 3 + 2] + proj[b * 3 + 2]) * 1.5)) !== layer) continue;
          ctx.moveTo(proj[a * 3], proj[a * 3 + 1]); ctx.lineTo(proj[b * 3], proj[b * 3 + 1]);
        }
        ctx.strokeStyle = `rgba(${col},${(u.lines * [1, 0.55, 0.22][layer]).toFixed(3)})`;
        ctx.stroke();
      }
      for (let layer = 0; layer < 3; layer++) {
        ctx.fillStyle = `rgba(${col},${(Math.min(1, u.bright) * [0.95, 0.55, 0.25][layer]).toFixed(3)})`;
        const s = [2.2, 1.7, 1.3][layer] * (w / 400 + 0.5);
        for (let i = 0; i < n; i++) {
          if (Math.min(2, Math.floor(proj[i * 3 + 2] * 3)) !== layer) continue;
          ctx.fillRect(proj[i * 3] - s / 2, proj[i * 3 + 1] - s / 2, s, s);
        }
      }
      ctx.globalCompositeOperation = "source-over";
    }
    return { kind: "canvas", load, draw, lost: () => false };
  }

  // ---------- сфера: состояние, сигнал, цикл кадров, качество ----------
  function create(canvas, opts) {
    opts = opts || {};
    const reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    let reduced = !!(reduceQuery && reduceQuery.matches);
    let preference = opts.quality || "auto";       // auto | high | medium | low | off
    let renderer = null, tier = null, raf = 0, last = 0, lastDraw = 0, visible = true, destroyed = false;
    let state = "idle", signal = null, sigSmooth = 0, kick = 0;
    const cur = Object.assign({}, STATES.idle);
    const ph = { breath: 0, spin: 0.6, twist: 0, ripple: 0, shimmer: 0, cam: 0 };
    const stats = { fps: 0, frameMs: 0, vsync: 1000 / 60, slow: 0, drawn: 0, degraded: [] };
    let colorNow = COLOR.slice();

    function pickTier() {
      if (preference !== "auto") return preference;
      if (renderer && renderer.software) return "low";
      const cores = navigator.hardwareConcurrency || 4, mem = navigator.deviceMemory || 8;
      return cores <= 2 || mem <= 2 ? "low" : cores <= 4 || mem <= 4 || Math.min(screen.width, screen.height) < 700 ? "medium" : "high";
    }
    function start() {
      if (preference === "off") { stop(); renderer = null; return; }
      if (!renderer) {
        try { renderer = opts.noWebGL ? null : makeGL(canvas); } catch (e) { renderer = null; }
        if (!renderer) renderer = make2D(canvas);
      }
      tier = renderer && renderer.kind === "canvas" ? "canvas" : pickTier();
      if (!renderer) return;
      const q = tier === "canvas" ? CANVAS_Q : QUALITY[tier];
      renderer.load(buildSphere(q.points, q.k));
      stats.points = q.points;
      resize();
      loop();
    }
    function quality() { return tier === "canvas" ? CANVAS_Q : QUALITY[tier] || QUALITY.medium; }
    function resize() {
      const q = quality(), dpr = Math.min(window.devicePixelRatio || 1, q.dpr);
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    }
    function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; }
    function loop() { if (!raf && visible && !destroyed && renderer) { last = 0; raf = requestAnimationFrame(frame); } }

    function approach(curV, target, dt, tau) { return curV + (target - curV) * (1 - Math.exp(-dt / tau)); }
    function frame(now) {
      raf = 0;
      if (destroyed || !visible || !renderer) return;
      raf = requestAnimationFrame(frame);
      const q = quality(), cap = reduced ? 12 : q.fps;
      if (last) {
        const gap = now - last;
        stats.vsync = Math.min(stats.vsync * 1.002, Math.max(4, gap));  // самый короткий интервал ≈ частота экрана
        stats.frameMs = stats.frameMs ? stats.frameMs * 0.92 + gap * 0.08 : gap;
      }
      last = now;
      if (now - lastDraw < 1000 / cap - 2) return;
      const dt = Math.min(0.1, lastDraw ? (now - lastDraw) / 1000 : 1 / 60);
      lastDraw = now;
      stats.drawn++;
      stats.fps = stats.fps ? stats.fps * 0.9 + (1 / dt) * 0.1 : 1 / dt;
      watchSpeed();
      step(dt, now / 1000);
      if (renderer.lost()) { fallback("потерян контекст WebGL"); return; }
      renderer.draw(uniforms(now / 1000), canvas.width, canvas.height);
    }
    // Медленно — понижаем качество (только вниз: без качелей туда-обратно).
    function watchSpeed() {
      if (preference !== "auto" || tier === "canvas" || tier === "low" || stats.drawn < 90 || reduced) return;
      stats.slow = stats.frameMs > stats.vsync * 1.45 ? stats.slow + 1 : Math.max(0, stats.slow - 2);
      if (stats.slow > 45) {
        const next = ORDER[ORDER.indexOf(tier) + 1];
        stats.degraded.push(`${tier}→${next} (${stats.frameMs.toFixed(1)} мс/кадр)`);
        tier = next; stats.slow = 0; stats.drawn = 0;
        renderer.load(buildSphere(QUALITY[tier].points, QUALITY[tier].k));
        stats.points = QUALITY[tier].points;
        resize();
      }
    }
    function fallback(why) {
      console.warn("Сфера: " + why + " — переключаюсь на Canvas 2D");
      stats.degraded.push(why);
      // у canvas уже есть контекст webgl — для 2D нужен новый элемент
      const fresh = canvas.cloneNode(false);
      canvas.replaceWith(fresh); canvas = fresh;
      renderer = make2D(canvas); tier = "canvas";
      if (renderer) { renderer.load(buildSphere(CANVAS_Q.points, CANVAS_Q.k)); stats.points = CANVAS_Q.points; resize(); loop(); }
    }

    function step(dt, t) {
      const target = STATES[state] || STATES.idle;
      for (const k in target) cur[k] = approach(cur[k], target[k], dt, 0.6);
      // настоящий сигнал: быстрая атака, плавный спад; толчки слов (озвучка браузера) — короткие
      const raw = signal == null ? 0 : signal;
      sigSmooth = approach(sigSmooth, raw, dt, raw > sigSmooth ? 0.05 : 0.18);
      kick = approach(kick, 0, dt, 0.16);
      const motion = reduced ? 0.15 : 1;
      ph.breath += dt * cur.breathHz * 2 * Math.PI * (reduced ? 0.5 : 1);
      ph.spin += dt * cur.spin * motion;
      ph.twist += dt * cur.twistHz * 2 * Math.PI * motion;
      ph.ripple += dt * cur.rippleHz * 2 * Math.PI * motion * (1 + sigSmooth * 2);
      ph.shimmer += dt * 9 * motion;
      ph.cam += dt * motion;
      // цвет: голубой; ошибка — тёплый
      const want = state === "error" ? ERROR_COLOR : COLOR;
      colorNow = colorNow.map((c, i) => approach(c, want[i], dt, 0.5));
    }
    function level() {  // 0..1: что сейчас «слышит» сфера
      if (signal != null) return sigSmooth;
      return kick;
    }
    function stateAnimation(t) {
      // Сигнала нет: ровная, явно периодическая анимация состояния — не похожа на громкость.
      if (signal != null || kick > 0.02) return 0;
      if (state === "speaking") return 0.5 + 0.5 * Math.sin(t * 2 * Math.PI * 1.1);
      if (state === "listening") return Math.pow(0.5 + 0.5 * Math.sin(t * 2 * Math.PI * 0.55), 3);
      return 0;
    }
    function uniforms(t) {
      const lv = level(), anim = stateAnimation(t), motion = reduced ? 0.25 : 1;
      const active = state === "listening" || state === "speaking";
      const scale = 1 + Math.sin(ph.breath) * cur.breath * (reduced ? 0.4 : 1)
        + (active ? lv * 0.11 * motion + anim * 0.025 * motion : 0);
      const camAmp = cur.cam * (reduced ? 0 : 1);
      const eye = [Math.sin(ph.cam * 0.11) * 0.42 * camAmp, 0.18 + Math.sin(ph.cam * 0.083) * 0.26 * camAmp, CAM_DIST];
      const w = canvas.width, h = canvas.height, aspect = w / h;
      const fitR = 1 / (CAM_DIST * Math.tan(FOV / 2));  // радиус сферы в долях половины кадра
      return {
        time: t % 3600, scale, mv: modelView(ph.spin, 0.32 + Math.sin(ph.cam * 0.07) * 0.06 * camAmp, eye),
        proj: perspective(aspect),
        twist: cur.twist * motion, twistPh: ph.twist,
        ripple: (cur.ripple + (active ? lv * 0.05 : 0)) * motion, ripplePh: ph.ripple,
        shimmer: (active ? lv * 0.045 : 0) * motion, shimmerPh: ph.shimmer,
        glow: cur.glow * (1 + (active ? lv * 0.8 + anim * 0.25 : 0)),
        glowR: fitR * scale * Math.min(1, aspect >= 1 ? 1 : aspect),
        bright: cur.bright * (1 + (active ? lv * 0.55 + anim * 0.15 : 0)),
        lines: cur.lines * (1 + (active ? lv * 0.6 : 0)),
        pointSize: (tier === "low" ? 13 : 11) * Math.min(w, h) / 420,
        color: colorNow, hot: HOT,
      };
    }

    // видимость: вкладка в фоне или сфера за краем экрана — кадры не рисуются
    function onVisibility() { visible = !document.hidden && inView; if (visible) loop(); else stop(); }
    let inView = true;
    document.addEventListener("visibilitychange", onVisibility);
    const io = window.IntersectionObserver ? new IntersectionObserver((es) => { inView = es[es.length - 1].isIntersecting; onVisibility(); }) : null;
    if (io) io.observe(canvas);
    const ro = window.ResizeObserver ? new ResizeObserver(() => resize()) : null;
    if (ro) ro.observe(canvas);
    const onReduce = (e) => { reduced = e.matches; };
    if (reduceQuery) (reduceQuery.addEventListener ? reduceQuery.addEventListener("change", onReduce) : reduceQuery.addListener(onReduce));
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); stop(); setTimeout(() => fallback("потерян контекст WebGL"), 0); });

    start();

    return {
      setState(s) { state = STATES[s] ? s : "idle"; },
      setSignal(v) { signal = v == null || !isFinite(v) ? null : Math.max(0, Math.min(1, v)); },
      pulse(strength) { kick = Math.max(kick, Math.max(0, Math.min(1, strength == null ? 0.7 : strength))); },
      setQuality(q) {
        preference = q;
        stop();
        if (q === "off") { renderer = null; tier = null; return; }
        if (renderer && renderer.kind === "webgl") { tier = pickTier(); renderer.load(buildSphere(quality().points, quality().k)); stats.points = quality().points; resize(); loop(); }
        else if (!renderer) start();
        else loop();
      },
      get active() { return !!renderer && preference !== "off"; },
      get canvas() { return canvas; },
      info() {
        return { renderer: renderer && preference !== "off" ? renderer.kind : "none", quality: tier, preference,
          gpu: renderer && renderer.gpu || "", software: !!(renderer && renderer.software),
          points: stats.points || 0, fps: Math.round(stats.fps), frameMs: +stats.frameMs.toFixed(1),
          reducedMotion: reduced, state, signal: signal == null ? (kick > 0.02 ? "pulse" : null) : +sigSmooth.toFixed(3),
          degraded: stats.degraded.slice(), drawn: stats.drawn };
      },
      destroy() {
        destroyed = true; stop();
        document.removeEventListener("visibilitychange", onVisibility);
        if (io) io.disconnect();
        if (ro) ro.disconnect();
      },
    };
  }

  window.JarvisOrb = { create };
})();
