/* Landing page hero: an animated example of the design flow (brief -> agent steps -> parts appear -> checks tick).
   It is an illustration, not a live model call: the scenes below are fixed specs drawn by a tiny isometric renderer,
   and the volumes in the table are computed from the dimensions shown. */

const C = Math.cos(Math.PI / 6);
const SVGNS = "http://www.w3.org/2000/svg";

/* ---------------------------------------------------------------- example scenes (metres) */
export const SCENES = [
  {
    id: "cement", tab: "Cement line", scale: 9.5, score: 96,
    prompt: "A cement line: preheater cyclone, calciner, rotary kiln, grate cooler and a vent stack",
    parts: [
      { id: "slab", kind: "box", at: [-15, -7, -0.6], size: [30, 14, 0.6], color: "#7d8794", ground: true },
      { id: "frame", kind: "box", at: [-11.2, -1.2, 0], size: [2.4, 2.4, 3], color: "#a9b3c2" },
      { id: "cyclone cone", kind: "cone", at: [-10, 0, 3], r: 0.6, r2: 2.4, h: 4, color: "#5f8ed0" },
      { id: "preheater", kind: "cylinder", at: [-10, 0, 7], r: 2.4, h: 6, color: "#5f8ed0" },
      { id: "calciner", kind: "cylinder", at: [-4.5, 0, 0], r: 1.8, h: 9, color: "#e0883f" },
      { id: "kiln", kind: "pipe", from: [-3.5, 0, 2.2], to: [8, 0, 2.2], r: 1.5, color: "#b5523b" },
      { id: "cooler", kind: "box", at: [8, -2.2, 0], size: [6, 4.4, 2], color: "#3f8f86" },
      { id: "stack", kind: "cylinder", at: [13, 4.5, 0], r: 0.8, h: 14, color: "#cfd5de" },
    ],
    rows: ["preheater", "calciner", "kiln", "cooler", "stack"],
  },
  {
    id: "tanks", tab: "Tank farm", scale: 17, score: 95,
    prompt: "A tank farm: three 20 m³ storage tanks, a transfer pump skid and a manifold",
    parts: [
      { id: "slab", kind: "box", at: [-7, -4.5, -0.4], size: [14, 10, 0.4], color: "#7d8794", ground: true },
      { id: "tank-1", kind: "cylinder", at: [-4, -2, 0], r: 1.3, h: 3.8, color: "#8aa4c8" },
      { id: "roof-1", kind: "cone", at: [-4, -2, 3.8], r: 1.3, r2: 0.15, h: 0.7, color: "#a9bddb" },
      { id: "tank-2", kind: "cylinder", at: [0, -2, 0], r: 1.3, h: 3.8, color: "#8aa4c8" },
      { id: "roof-2", kind: "cone", at: [0, -2, 3.8], r: 1.3, r2: 0.15, h: 0.7, color: "#a9bddb" },
      { id: "tank-3", kind: "cylinder", at: [4, -2, 0], r: 1.3, h: 3.8, color: "#8aa4c8" },
      { id: "roof-3", kind: "cone", at: [4, -2, 3.8], r: 1.3, r2: 0.15, h: 0.7, color: "#a9bddb" },
      { id: "manifold", kind: "pipe", from: [-4, 0.8, 0.7], to: [4, 0.8, 0.7], r: 0.22, color: "#e0a93c" },
      { id: "skid", kind: "box", at: [-5.5, 2, 0], size: [4, 2, 1.1], color: "#3f8f86" },
      { id: "pump", kind: "cylinder", at: [-3.5, 3, 1.1], r: 0.55, h: 1.1, color: "#d9694f" },
    ],
    rows: ["tank-1", "tank-2", "tank-3", "skid", "manifold"],
  },
  {
    id: "water", tab: "Water treatment", scale: 14, score: 97,
    prompt: "A water treatment train: clarifier, two filter basins, a sand filter and a clear-water tank",
    parts: [
      { id: "slab", kind: "box", at: [-10, -6, -0.5], size: [21, 12, 0.5], color: "#7d8794", ground: true },
      { id: "clarifier", kind: "cylinder", at: [-6, -1, 0], r: 3.2, h: 2.2, color: "#4aa3c7" },
      { id: "basin A", kind: "box", at: [0, -5, 0], size: [5, 3, 1.6], color: "#58b5d4" },
      { id: "basin B", kind: "box", at: [0, -0.5, 0], size: [5, 3, 1.6], color: "#58b5d4" },
      { id: "sand filter", kind: "cylinder", at: [8, -3, 0], r: 1.1, h: 4, color: "#d2b26a" },
      { id: "clear tank", kind: "cylinder", at: [8, 2.5, 0], r: 1.6, h: 3, color: "#8aa4c8" },
      { id: "feed", kind: "pipe", from: [-2.8, -1, 1.6], to: [0, -1, 1.6], r: 0.2, color: "#e0a93c" },
      { id: "outlet", kind: "pipe", from: [5, -2, 1.4], to: [8, -2, 1.4], r: 0.2, color: "#e0a93c" },
    ],
    rows: ["clarifier", "basin A", "basin B", "sand filter", "clear tank"],
  },
  {
    id: "boiler", tab: "Boiler house", scale: 12.5, score: 94,
    prompt: "A boiler house: package boiler with steam drum, economiser, ID fan and a 14 m stack",
    parts: [
      { id: "slab", kind: "box", at: [-9, -5, -0.5], size: [19, 10, 0.5], color: "#7d8794", ground: true },
      { id: "boiler", kind: "box", at: [-6, -2, 0], size: [4, 4, 6.5], color: "#b95a45" },
      { id: "steam drum", kind: "pipe", from: [-6.5, 0, 7.4], to: [-1.5, 0, 7.4], r: 0.95, color: "#d8cba8" },
      { id: "economiser", kind: "box", at: [-0.5, -1.5, 0], size: [3, 3, 3], color: "#8d6fb5" },
      { id: "duct", kind: "pipe", from: [2.5, 0, 1.4], to: [5, 0, 1.4], r: 0.45, color: "#a9b3c2" },
      { id: "ID fan", kind: "cylinder", at: [5.8, 0, 0], r: 1.3, h: 2.4, color: "#3f8f86" },
      { id: "stack", kind: "cylinder", at: [8, 2.5, 0], r: 0.85, h: 14, color: "#cfd5de" },
    ],
    rows: ["boiler", "economiser", "ID fan", "stack", "steam drum"],
  },
];

/* ---------------------------------------------------------------- geometry helpers */
function volume(p) {
  const PI = Math.PI;
  if (p.kind === "box") return p.size[0] * p.size[1] * p.size[2];
  if (p.kind === "cylinder") return PI * p.r * p.r * p.h;
  if (p.kind === "cone") return (PI * p.h / 3) * (p.r * p.r + p.r * p.r2 + p.r2 * p.r2);
  const [a, b] = [p.from, p.to];
  return PI * p.r * p.r * Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
}

function tone(hex, f) {
  const rgb = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const t = f >= 1 ? 255 : 0, k = f >= 1 ? f - 1 : 1 - f;
  return `rgb(${rgb.map((v) => Math.round(v + (t - v) * k)).join(",")})`;
}

let uid = 0;
const n = (v) => Math.round(v * 10) / 10;

/** Draw a scene to an SVG fragment. Returns { viewBox, inner } where every part is a <g class="part" data-id>. */
export function renderScene(scene) {
  const S = scene.scale, key = `g${++uid}`;
  const P = (x, y, z) => [(x - y) * C * S, (x + y) * 0.5 * S - z * S];
  const bb = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  const ext = (x, y) => { bb.x0 = Math.min(bb.x0, x); bb.x1 = Math.max(bb.x1, x); bb.y0 = Math.min(bb.y0, y); bb.y1 = Math.max(bb.y1, y); };
  const defs = [];
  const poly = (pts, fill) => {
    pts.forEach(([x, y]) => ext(x, y));
    return `<polygon points="${pts.map(([x, y]) => `${n(x)},${n(y)}`).join(" ")}" fill="${fill}"/>`;
  };
  const grad = (id, hex) => defs.push(
    `<linearGradient id="${id}" x1="0" x2="1"><stop offset="0" stop-color="${tone(hex, 0.72)}"/><stop offset=".42" stop-color="${tone(hex, 1.16)}"/><stop offset="1" stop-color="${tone(hex, 0.62)}"/></linearGradient>`);

  const draw = (p, i) => {
    let g = "";
    if (p.kind === "box") {
      const [x0, y0, z0] = p.at, [dx, dy, dz] = p.size, x1 = x0 + dx, y1 = y0 + dy, z1 = z0 + dz;
      g += poly([P(x0, y1, z0), P(x1, y1, z0), P(x1, y1, z1), P(x0, y1, z1)], tone(p.color, 0.7));
      g += poly([P(x1, y0, z0), P(x1, y1, z0), P(x1, y1, z1), P(x1, y0, z1)], tone(p.color, 0.88));
      g += poly([P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)], tone(p.color, 1.14));
    } else if (p.kind === "cylinder" || p.kind === "cone") {
      const [cx, cy, z0] = p.at, r1 = p.r, r2 = p.kind === "cone" ? p.r2 : p.r;
      const [bx, by] = P(cx, cy, z0), [, ty] = P(cx, cy, z0 + p.h);
      const k = S * 1.2247, q = S * 0.7071, rx1 = r1 * k, ry1 = r1 * q, rx2 = r2 * k, ry2 = r2 * q;
      const id = `${key}-${i}`;
      grad(id, p.color);
      ext(bx - Math.max(rx1, rx2), ty - ry2); ext(bx + Math.max(rx1, rx2), by + ry1);
      g += `<path d="M${n(bx - rx1)},${n(by)} A${n(rx1)},${n(ry1)} 0 0 0 ${n(bx + rx1)},${n(by)} L${n(bx + rx2)},${n(ty)} L${n(bx - rx2)},${n(ty)} Z" fill="url(#${id})"/>`;
      if (rx2 > 0.5) g += `<ellipse cx="${n(bx)}" cy="${n(ty)}" rx="${n(rx2)}" ry="${n(ry2)}" fill="${tone(p.color, 1.22)}"/>`;
    } else {
      const [ax, ay] = P(...p.from), [bx, by] = P(...p.to), w = 2 * p.r * S;
      ext(ax - w / 2, ay - w / 2); ext(bx + w / 2, by + w / 2); ext(ax - w / 2, ay + w / 2); ext(bx + w / 2, by - w / 2);
      g += `<line x1="${n(ax)}" y1="${n(ay)}" x2="${n(bx)}" y2="${n(by)}" stroke="${tone(p.color, 0.8)}" stroke-width="${n(w)}" stroke-linecap="round"/>`;
      g += `<line x1="${n(ax)}" y1="${n(ay - w * 0.16)}" x2="${n(bx)}" y2="${n(by - w * 0.16)}" stroke="${tone(p.color, 1.25)}" stroke-width="${n(w * 0.42)}" stroke-linecap="round" opacity=".75"/>`;
    }
    return g;
  };

  // painter's order: ground first, then back-to-front by footprint centre, then by height
  const centre = (p) => p.kind === "box" ? [p.at[0] + p.size[0] / 2, p.at[1] + p.size[1] / 2, p.at[2]]
    : p.kind === "pipe" ? p.from.map((v, j) => (v + p.to[j]) / 2) : p.at;
  const order = scene.parts.map((p, i) => ({ p, i, c: centre(p) }))
    .sort((a, b) => (b.p.ground - a.p.ground) || ((a.c[0] + a.c[1]) - (b.c[0] + b.c[1])) || (a.c[2] - b.c[2]));

  const slab = scene.parts.find((p) => p.ground);
  let grid = "";
  if (slab) {
    const [sx, sy] = slab.at, [dx, dy] = slab.size, m = 1.5, gs = 1.5;
    const x0 = sx - m, x1 = sx + dx + m, y0 = sy - m, y1 = sy + dy + m;
    for (let x = x0; x <= x1; x += gs) { const a = P(x, y0, -slab.size[2]), b = P(x, y1, -slab.size[2]); grid += `M${n(a[0])},${n(a[1])}L${n(b[0])},${n(b[1])}`; ext(...a); ext(...b); }
    for (let y = y0; y <= y1; y += gs) { const a = P(x0, y, -slab.size[2]), b = P(x1, y, -slab.size[2]); grid += `M${n(a[0])},${n(a[1])}L${n(b[0])},${n(b[1])}`; ext(...a); ext(...b); }
  }
  const parts = order.map(({ p, i }, z) => `<g class="part" data-id="${p.id}" style="--z:${z}">${draw(p, i)}</g>`).join("");
  const pad = 14;
  return {
    viewBox: `${n(bb.x0 - pad)} ${n(bb.y0 - pad)} ${n(bb.x1 - bb.x0 + 2 * pad)} ${n(bb.y1 - bb.y0 + 2 * pad)}`,
    inner: `<defs>${defs.join("")}</defs><path class="iso-grid" d="${grid}"/>${parts}`,
    order: order.map(({ p }) => p.id),
  };
}

/** A small static drawing of a scene (use-case cards). */
export function sceneSvg(scene, label) {
  const { viewBox, inner } = renderScene(scene);
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label || scene.prompt);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.classList.add("iso", "static");
  svg.innerHTML = inner;
  svg.querySelectorAll(".part").forEach((g) => g.classList.add("on"));
  return svg;
}

/* ---------------------------------------------------------------- the animated demo */
const STEPS = ["plan", "retrieve", "generate", "critique", "build", "verify"];
const CANCEL = Symbol("cancel");

export function initDemo(root) {
  const $ = (s) => root.querySelector(s);
  const svg = $("#demo-svg"), promptEl = $("#demo-prompt"), tabs = $("#demo-tabs"), chips = $("#demo-chips");
  const rowsEl = $("#demo-rows"), scoreEl = $("#demo-score"), pauseBtn = $("#demo-pause"), count = $("#demo-count");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const state = { i: 0, run: 0, paused: false, visible: true };

  const tabBtns = SCENES.map((s, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.role = "tab"; b.textContent = s.tab; b.id = `tab-${s.id}`;
    b.setAttribute("aria-selected", "false"); b.tabIndex = -1;
    b.addEventListener("click", () => show(i));
    b.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const j = (i + d + SCENES.length) % SCENES.length;
      tabBtns[j].focus(); show(j);
    });
    tabs.append(b);
    return b;
  });
  chips.replaceChildren(...STEPS.map((s) => Object.assign(document.createElement("span"), { textContent: s, className: "chip-step" })));

  const rowEls = () => [...rowsEl.children];
  const parts = () => [...svg.querySelectorAll(".part")];

  function mount(i) {
    const s = SCENES[i], { viewBox, inner } = renderScene(s);
    svg.setAttribute("viewBox", viewBox);
    svg.setAttribute("aria-label", `Isometric drawing: ${s.prompt}`);
    svg.innerHTML = inner;
    tabBtns.forEach((b, j) => { b.setAttribute("aria-selected", j === i ? "true" : "false"); b.tabIndex = j === i ? 0 : -1; });
    const byId = Object.fromEntries(s.parts.map((p) => [p.id, p]));
    rowsEl.replaceChildren(...s.rows.map((id) => {
      const p = byId[id], r = document.createElement("div");
      r.className = "vrow";
      r.innerHTML = `<span class="n"></span><span class="k"></span><span class="v"></span><span class="ok" aria-hidden="true"></span>`;
      r.querySelector(".n").textContent = p.id;
      r.querySelector(".k").textContent = p.kind;
      r.querySelector(".v").textContent = `${volume(p).toFixed(volume(p) < 10 ? 2 : 1)} m³`;
      return r;
    }));
    count.textContent = `${s.parts.filter((p) => !p.ground).length + 1} parts`;
    chips.querySelectorAll(".chip-step").forEach((c) => c.classList.remove("on"));
    scoreEl.textContent = "–"; scoreEl.classList.remove("done");
    promptEl.textContent = "";
    root.classList.remove("typing");
  }

  function finish(i) {
    const s = SCENES[i];
    promptEl.textContent = s.prompt;
    chips.querySelectorAll(".chip-step").forEach((c) => c.classList.add("on"));
    parts().forEach((g) => g.classList.add("on"));
    rowEls().forEach((r) => r.classList.add("ok"));
    scoreEl.textContent = `${s.score} / 100`; scoreEl.classList.add("done");
  }

  async function wait(ms, id) {
    let left = ms;
    while (left > 0) {
      if (id !== state.run) throw CANCEL;
      await new Promise((r) => setTimeout(r, 40));
      if (!state.paused && state.visible) left -= 40;
    }
    if (id !== state.run) throw CANCEL;
  }

  async function play(i) {
    const id = ++state.run, s = SCENES[i];
    state.i = i;
    mount(i);
    try {
      root.classList.add("typing");
      for (let c = 1; c <= s.prompt.length; c++) { promptEl.textContent = s.prompt.slice(0, c); await wait(22, id); }
      root.classList.remove("typing");
      const chipEls = [...chips.children];
      const lit = async (k) => { chipEls[k].classList.add("on"); await wait(320, id); };
      await lit(0); await lit(1); await lit(2);
      for (const g of parts()) { g.classList.add("on"); await wait(g.dataset.id === "slab" ? 120 : 300, id); }
      await lit(3); await lit(4); await lit(5);
      for (const r of rowEls()) { r.classList.add("ok"); await wait(230, id); }
      const from = performance.now();
      scoreEl.classList.add("done");
      for (let t = 0; t <= 1; t = (performance.now() - from) / 700) {
        scoreEl.textContent = `${Math.round(s.score * Math.min(t, 1))} / 100`;
        await wait(40, id);
      }
      scoreEl.textContent = `${s.score} / 100`;
      await wait(3400, id);
      play((i + 1) % SCENES.length);
    } catch (e) { if (e !== CANCEL) throw e; }
  }

  function show(i) {
    if (reduced.matches) { state.run++; state.i = i; mount(i); finish(i); return; }
    play(i);
  }

  pauseBtn.addEventListener("click", () => {
    state.paused = !state.paused;
    pauseBtn.setAttribute("aria-pressed", String(state.paused));
    pauseBtn.setAttribute("aria-label", state.paused ? "Play animation" : "Pause animation");
    root.classList.toggle("paused", state.paused);
  });
  new IntersectionObserver(([e]) => { state.visible = e.isIntersecting; }, { threshold: 0.15 }).observe(root);
  const sync = () => {
    pauseBtn.hidden = reduced.matches;
    show(state.i);
  };
  reduced.addEventListener?.("change", sync);
  sync();
}
