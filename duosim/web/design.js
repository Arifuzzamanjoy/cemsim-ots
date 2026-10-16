/* DuoSim Studio: the platform's main page.
 *   Design tab     prompt -> LLM -> validated spec -> FreeCAD build (verified) -> three.js view
 *   Simulators tab gallery of simulator plug-ins (cement kiln = flagship demo) + a generic live console
 * Everything the model or a simulator returns is untrusted text: it is only rendered with textContent. */
import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";

const $ = (s) => document.querySelector(s);
const STATE = { id: null, busy: false, noProvider: false, parts: 0, meshObjects: 0, lastError: "", publicMode: false,
  view: "design", console: null, sims: [] };
window.DESIGN = STATE; // read by the end-to-end tests

// ------------------------------------------------------------------ optional platform key
// Only platform keys (cs_...) are ever sent.  A model key (Gemini "AQ.", Google "AIza", Anthropic "sk-ant")
// is configured on the server; typing one into a browser over plain HTTP would only leak it.
const KEY_RE = /^cs_[A-Za-z0-9_-]{20,200}$/;
const store = {
  get() { try { return sessionStorage.getItem("duosim_key") || ""; } catch { return ""; } },
  set(v) { try { v ? sessionStorage.setItem("duosim_key", v) : sessionStorage.removeItem("duosim_key"); } catch { /* private mode */ } },
};
let KEY = "";
function acceptKey(v, { quiet = false } = {}) {
  v = (v || "").trim();
  if (!v) { KEY = ""; store.set(""); return true; }
  if (!KEY_RE.test(v)) {
    KEY = ""; store.set(""); $("#key").value = "";
    if (!quiet) say("That is not a platform key (those start with cs_). Model keys such as Gemini's are set on the server " +
      "- they must never be entered in a browser. Nothing was stored or sent.", "err");
    return false;
  }
  KEY = v; store.set(v); return true;
}
acceptKey(store.get(), { quiet: true });
$("#key").value = KEY;
$("#key").addEventListener("change", (e) => { if (acceptKey(e.target.value)) refreshStatus(); });

async function api(path, opts = {}, retried = false) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(KEY ? { "X-API-Key": KEY } : {}), ...(opts.headers || {}) },
  });
  if (r.status === 401 && KEY && !retried) { // the stored key is no longer valid: carry on anonymously
    acceptKey("", { quiet: true }); $("#key").value = "";
    say("The saved key was not accepted and was removed; continuing without a key.", "");
    return api(path, opts, true);
  }
  if (!r.ok) {
    let detail = r.statusText, errors = [];
    try { const j = await r.json(); detail = j.detail || detail; errors = j.errors || []; } catch { /* not JSON */ }
    const e = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    e.errors = errors; e.status = r.status;
    throw e;
  }
  return r;
}

// ------------------------------------------------------------------ 3-D view
const host = $("#view");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1b222b);
const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 5000);
camera.position.set(14, 10, 14);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xdfeaf5, 0x40403a, 1.2));
const sun = new THREE.DirectionalLight(0xffffff, 1.8);
sun.position.set(30, 50, 20);
scene.add(sun);
const grid = new THREE.GridHelper(100, 100, 0x4a5563, 0x2c3440);
scene.add(grid);
let model = new THREE.Group();
scene.add(model);

function resize() {
  const w = host.clientWidth || 600, h = host.clientHeight || 400;
  renderer.setSize(w, h, false);
  renderer.domElement.style.width = w + "px";
  renderer.domElement.style.height = h + "px";
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener("resize", resize);
new ResizeObserver(resize).observe(host);
resize();
(function loop() { controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); })();

function showMesh(data) {
  scene.remove(model);
  model.traverse((o) => { o.geometry?.dispose(); o.material?.dispose?.(); });
  model = new THREE.Group();
  const S = 0.001; // mm -> m
  for (const o of data.objects) {
    if (!o.i.length) continue;
    const g = new THREE.BufferGeometry();
    const pos = new Float32Array(o.v.length);
    for (let k = 0; k < pos.length; k++) pos[k] = o.v[k] * S;
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setIndex(new THREE.BufferAttribute(new Uint32Array(o.i), 1));
    g.computeVertexNormals();
    const m = new THREE.MeshStandardMaterial({
      color: new THREE.Color().setRGB(o.color[0], o.color[1], o.color[2], THREE.SRGBColorSpace),
      transparent: o.opacity < 1, opacity: o.opacity, side: THREE.DoubleSide, roughness: 0.65, metalness: 0.1,
    });
    const mesh = new THREE.Mesh(g, m);
    mesh.name = o.name;
    model.add(mesh);
  }
  scene.add(model);
  STATE.meshObjects = model.children.length;
  const box = new THREE.Box3().setFromObject(model);
  if (!box.isEmpty()) {
    const c = box.getCenter(new THREE.Vector3()), s = box.getSize(new THREE.Vector3());
    const r = Math.max(s.x, s.y, s.z, 1);
    controls.target.copy(c);
    camera.position.set(c.x + r * 1.1, c.y + r * 0.8, c.z + r * 1.1);
    camera.near = r / 500; camera.far = r * 50; camera.updateProjectionMatrix();
    grid.position.y = box.min.y;
    grid.scale.setScalar(Math.max(1, r / 50));
  }
  $("#empty").style.display = "none";
}

// ------------------------------------------------------------------ small helpers
function say(msg, cls = "") { const s = $("#status"); s.textContent = msg; s.className = cls; }
function busy(b) {
  STATE.busy = b;
  $("#go").disabled = b || STATE.noProvider;
  $("#rebuild").disabled = b || !$("#spec").value.trim();
}
function fillList(ul, items) {
  ul.replaceChildren(...items.map((t) => { const li = document.createElement("li"); li.textContent = t; return li; }));
}
function cell(tr, text, cls) { const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); }
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props);
  for (const k of kids) e.append(k);
  return e;
}
async function blobUrl(path) {
  const r = await api(path, { headers: { "Content-Type": "application/octet-stream" } });
  return URL.createObjectURL(await r.blob());
}

// ------------------------------------------------------------------ Design tab
async function show(r) {
  STATE.id = r.id;
  $("#spec").value = JSON.stringify(r.spec, null, 1);
  STATE.parts = r.spec.parts.length;
  $("#rebuild").disabled = false;
  const warns = [...(r.warnings || [])];
  const b = r.build || {};
  const tbody = $("#checks tbody");
  tbody.replaceChildren();
  const verdict = $("#verdict");
  if (!b.built) {
    verdict.textContent = "not built: " + (b.reason || "unknown");
    verdict.className = "chip bad";
    $("#shot").style.display = "none";
    $("#downloads").replaceChildren();
    fillList($("#warns"), warns);
    return;
  }
  verdict.textContent = b.ok
    ? `verified: ${b.parts} parts match the spec (volume, position, solid validity)`
    : `${b.failed_parts.length} of ${b.parts} parts FAILED verification`;
  verdict.className = "chip " + (b.ok ? "ok" : "bad");
  for (const c of b.checks) {
    const tr = document.createElement("tr");
    cell(tr, c.id); cell(tr, c.kind);
    cell(tr, c.volume_m3 == null ? "-" : c.volume_m3.toPrecision(4), "n");
    cell(tr, c.ok ? "pass" : c.problems.join("; "), c.ok ? "pass" : "fail");
    tbody.appendChild(tr);
  }
  const [mesh, png] = await Promise.all([
    api(`/api/design/${r.id}/file/mesh`).then((x) => x.json()).catch(() => null),
    blobUrl(`/api/design/${r.id}/file/png`).catch(() => null),
  ]);
  if (mesh) showMesh(mesh);
  const shot = $("#shot");
  if (png) { shot.src = png; shot.style.display = "block"; } else { shot.style.display = "none"; }
  const dl = $("#downloads");
  dl.replaceChildren();
  for (const [kind, label] of [["step", "STEP"], ["fcstd", "FreeCAD (.FCStd)"], ["mesh", "mesh JSON"], ["spec", "spec JSON"]]) {
    const btn = el("button", { textContent: label });
    btn.onclick = async () => {
      const a = document.createElement("a");
      a.href = await blobUrl(`/api/design/${r.id}/file/${kind}`);
      a.download = { step: "design.step", fcstd: "design.FCStd", mesh: "mesh.json", spec: "spec.json" }[kind];
      a.click();
    };
    dl.appendChild(btn);
  }
  fillList($("#warns"), warns);
}

function fail(e) {
  STATE.lastError = e.message;
  say(e.status === 401 ? "A platform key is required on this server - enter it above." : e.message, "err");
  if (e.errors?.length) fillList($("#warns"), e.errors);
}

async function run(path, body) {
  busy(true);
  fillList($("#warns"), []);
  say(path.endsWith("generate") ? "Asking the model and building in FreeCAD (up to ~2 minutes when the model API is busy) ..." : "Building in FreeCAD ...");
  try {
    const r = await (await api(path, { method: "POST", body: JSON.stringify(body) })).json();
    await show(r);
    const m = r.provider ? `${r.provider} / ${r.model}, ${r.attempts} attempt(s), ${r.latency_s}s - ` : "";
    say(m + (r.build?.built ? `built in ${(r.build.timings_s?.build ?? 0).toFixed(1)}s` : "spec ready (not built)"), "ok");
    refreshStatus();
  } catch (e) { fail(e); } finally { busy(false); }
}

$("#go").onclick = () => {
  const prompt = $("#prompt").value.trim();
  if (!prompt) return say("Describe what to design first.", "err");
  run("/api/design/generate", { prompt, provider: $("#provider").value || null, build: true });
};
$("#rebuild").onclick = () => {
  let spec;
  try { spec = JSON.parse($("#spec").value); } catch (e) { return say("Spec is not valid JSON: " + e.message, "err"); }
  run("/api/design/build", { spec });
};
$("#spec").addEventListener("input", () => { $("#rebuild").disabled = STATE.busy || !$("#spec").value.trim(); });
const EXAMPLES = {
  "cyclone preheater": "A 4-stage cyclone preheater tower for a 3000 t/d kiln: four cyclones stacked on a steel frame with floors, riser ducts between them, an ID fan and a stack.",
  "rotary kiln on piers": "A rotary kiln, 4.4 m diameter and 62 m long, on three concrete piers with tyres, a girth gear with drive motor, a feed-end inlet chamber and a burner hood at the outlet.",
  "tank farm": "A tank farm with three vertical storage tanks of different sizes on a bund slab, interconnected by a pipe header and a pump skid.",
  "raw mill + filter": "A vertical raw mill with its motor and gearbox, a dynamic separator on top, a bag filter beside it with a stack, and ducts connecting them.",
};
document.querySelectorAll("button.ex").forEach((b) => { b.onclick = () => { $("#prompt").value = EXAMPLES[b.textContent]; }; });

async function refreshStatus() {
  const fc = $("#chip-freecad"), us = $("#chip-usage"), sel = $("#provider");
  try {
    const s = await (await api("/api/design/status")).json();
    STATE.publicMode = !!s.public;
    fc.textContent = "FreeCAD: " + (!s.freecad.configured ? "not configured" : s.freecad.ok ? `ok (addon ${s.freecad.addon_version})` : "problem");
    fc.className = "chip " + (s.freecad.ok ? "ok" : "bad");
    fc.title = (s.freecad.problems || []).join("\n");
    us.textContent = s.public
      ? `public demo: ${s.usage.generate.used}/${s.usage.generate.limit} designs today`
      : `${s.workspace}: ${s.usage.generate.used}/${s.usage.generate.limit} designs today`;
    us.className = "chip";
    sel.replaceChildren(...s.providers.map((p) => {
      const o = document.createElement("option");
      o.value = p.name; o.textContent = `${p.name} (${p.model})`; o.selected = p.default; return o;
    }));
    STATE.noProvider = !s.providers.length;
    $("#go").disabled = STATE.busy || STATE.noProvider;
    if (STATE.noProvider) say("No LLM provider configured: set GEMINI_API_KEY or ANTHROPIC_API_KEY on the server.", "err");
  } catch (e) {
    fc.textContent = e.status === 401 ? "enter your key" : "server error";
    fc.className = "chip bad";
    if (e.status === 401) say("This server requires a platform key (cs_...). Enter it at the top right.", "err");
  }
}

// ------------------------------------------------------------------ tabs
function setView(v) {
  STATE.view = v;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  $("#view-design").hidden = v !== "design";
  $("#view-sims").hidden = v !== "sims";
  if (v === "sims" && !STATE.sims.length) loadSims();
}
document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => setView(t.dataset.view); });

// ------------------------------------------------------------------ Simulators tab
async function loadSims() {
  const box = $("#cards");
  try {
    const j = await (await api("/api/platform/simulators")).json();
    STATE.sims = j.simulators;
    const featured = (m) => (m.id === "cement_kiln" ? 0 : 1);
    box.replaceChildren(...[...j.simulators].sort((a, b) => featured(a) - featured(b) || a.name.localeCompare(b.name)).map(card));
  } catch (e) {
    box.replaceChildren(el("p", { className: "muted", textContent: "Could not load simulators: " + e.message }));
  }
}

function card(m) {
  const flagship = m.id === "cement_kiln";
  const c = el("div", { className: "card" + (flagship ? " featured" : "") });
  const h = el("h4", { textContent: m.name });
  h.append(el("span", { className: "badge" + (flagship ? " demo" : ""), textContent: flagship ? "flagship demo" : m.domain }));
  c.append(h, el("div", { className: "muted", textContent: m.description }),
    el("div", { className: "muted", textContent: `${m.tags.length} tags · ${m.commands.length} commands · ${m.domain}` }));
  const row = el("div", { className: "row" });
  if (flagship) {
    row.append(el("a", { href: "/hmi", textContent: "Open operator HMI demo ↗", className: "chip ok" }));
  }
  const con = el("button", { textContent: "Live console" });
  con.onclick = () => openConsole(m);
  const des = el("button", { textContent: "Design its 3-D model" });
  des.onclick = () => { $("#prompt").value = m.design_prompt || ""; setView("design"); $("#prompt").focus(); };
  row.append(con, des);
  c.append(row);
  return c;
}

let conTimer = null;
async function closeConsole() {
  clearInterval(conTimer); conTimer = null;
  const c = STATE.console; STATE.console = null;
  if (c) { try { await api(`/api/platform/sessions/${c.sid}`, { method: "DELETE" }); } catch { /* expired */ } }
  $("#console").hidden = true;
}
$("#con-stop").onclick = closeConsole;

function renderTags(m, tags) {
  const tb = $("#con-tags tbody");
  if (tb.rows.length !== m.tags.length) {
    tb.replaceChildren();
    for (const t of m.tags) {
      const tr = document.createElement("tr"); tr.dataset.tag = t.name;
      cell(tr, t.name); cell(tr, "", "n"); cell(tr, t.unit || ""); tb.appendChild(tr);
    }
  }
  for (const tr of tb.rows) {
    const v = tags[tr.dataset.tag];
    tr.cells[1].textContent = v == null ? "-" : Number(v).toPrecision(6);
  }
}

async function openConsole(m) {
  await closeConsole();
  $("#con-title").textContent = `${m.name} - live console`;
  $("#con-msg").textContent = "starting a session ...";
  $("#console").hidden = false;
  $("#con-tags tbody").replaceChildren();
  try {
    const s = await (await api("/api/platform/sessions", { method: "POST", body: JSON.stringify({ simulator: m.id }) })).json();
    STATE.console = { sid: s.id, sim: m.id, t: s.t, ticks: 0, tags: s.tags };
    renderTags(m, s.tags);
    $("#con-t").textContent = `t = ${s.t.toFixed(0)} s`;
    $("#con-msg").textContent = "running at 5 simulated seconds per second";
    buildCommands(m);
    conTimer = setInterval(async () => {
      const c = STATE.console;
      if (!c || c.busy) return;
      c.busy = true;
      try {
        const r = await (await api(`/api/platform/sessions/${c.sid}/step`, { method: "POST", body: JSON.stringify({ steps: 5 }) })).json();
        c.t = r.t; c.tags = r.tags; c.ticks++;
        renderTags(m, r.tags);
        $("#con-t").textContent = `t = ${r.t.toFixed(0)} s`;
      } catch (e) { $("#con-msg").textContent = e.message; clearInterval(conTimer); } finally { c.busy = false; }
    }, 1000);
  } catch (e) { $("#con-msg").textContent = e.message; }
}

function buildCommands(m) {
  const box = $("#con-cmds");
  box.replaceChildren();
  for (const c of m.commands) {
    const f = document.createElement("form");
    f.append(el("b", { textContent: c.name }));
    const inputs = {};
    for (const [arg, kind] of Object.entries(c.args)) {
      const i = el("input", kind === "number" ? { type: "number", step: "any", placeholder: arg, required: true }
        : kind === "bool" ? { type: "checkbox", title: arg } : { type: "text", placeholder: arg, required: true });
      inputs[arg] = [i, kind];
      f.append(i);
    }
    f.append(el("button", { type: "submit", textContent: "send" }));
    f.title = c.desc || "";
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      const args = {};
      for (const [a, [i, kind]] of Object.entries(inputs)) args[a] = kind === "number" ? Number(i.value) : kind === "bool" ? i.checked : i.value;
      try {
        const r = await (await api(`/api/platform/sessions/${STATE.console.sid}/command`, { method: "POST",
          body: JSON.stringify({ name: c.name, args }) })).json();
        $("#con-msg").textContent = `${c.name}: ${r.result?.ok === false ? r.result.msg : "ok"}`;
      } catch (e) {  // simulators already prefix their own messages with the command name
        $("#con-msg").textContent = e.message.startsWith(c.name) ? e.message : `${c.name}: ${e.message}`;
      }
    };
    box.appendChild(f);
  }
}
// a closed tab cannot send DELETE (and beacons cannot carry the key): idle sessions simply expire server-side
refreshStatus();
