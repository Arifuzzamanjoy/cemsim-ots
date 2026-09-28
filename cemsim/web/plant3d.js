/* CemSim live 3-D plant (three.js).
 * Geometry: FreeCAD-designed kiln line (tools/freecad/build_plant3d.py) exported to
 * /static/assets/plant3d.json - no FreeCAD needed at runtime.
 * Live data: the simulator WebSocket status stream (same as the HMI).
 */
import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "/static/vendor/CSS2DRenderer.js";

const $ = (s) => document.querySelector(s);
const API = { ready: false, objects: 0, frames: 0, statusCount: 0, selected: null };
window.PLANT3D = API;

// ------------------------------------------------------------------ renderer / scene
const host = $("#view");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
host.appendChild(renderer.domElement);
const labels = new CSS2DRenderer();
labels.setSize(innerWidth, innerHeight);
Object.assign(labels.domElement.style, { position: "fixed", inset: "0", pointerEvents: "none" });
document.body.appendChild(labels.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x9fb6c9);
scene.fog = new THREE.Fog(0x9fb6c9, 180, 520);
const camera = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 0.5, 2000);
camera.position.set(95, 105, 195);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(15, 36, 0);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.495;

scene.add(new THREE.HemisphereLight(0xdfeaf5, 0x5a5a48, 1.1));
const sun = new THREE.DirectionalLight(0xfff2dd, 2.2);
sun.position.set(80, 140, 90);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, { left: -130, right: 130, top: 130, bottom: -130, near: 10, far: 400 });
sun.shadow.bias = -0.0004;
scene.add(sun);

// ------------------------------------------------------------------ colour helpers
// all palette / design colours are sRGB; three.js works in linear light internally
const srgb = (r, g, b) => new THREE.Color().setRGB(r, g, b, THREE.SRGBColorSpace);
const scanner = (T, lo = 150, hi = 450) => {
  const x = Math.max(0, Math.min(1, (T - lo) / (hi - lo)));
  const st = [[0, [30, 60, 190]], [0.35, [40, 190, 90]], [0.6, [240, 220, 40]], [0.8, [245, 130, 20]], [1, [220, 30, 30]]];
  for (let i = 1; i < st.length; i++) if (x <= st[i][0]) {
    const [a, ca] = st[i - 1], [b, cb] = st[i], f = (x - a) / (b - a);
    return srgb(...ca.map((c, k) => (c + f * (cb[k] - c)) / 255));
  }
  return srgb(220 / 255, 30 / 255, 30 / 255);
};
const glow = (T) => {         // incandescence of clinker / meal (degC)
  const x = Math.max(0, Math.min(1, (T - 150) / 1300));
  return srgb(0.30 + 0.70 * Math.min(1, x * 1.6), 0.22 + 0.78 * Math.max(0, x - 0.35) / 0.65,
    0.20 + 0.60 * Math.max(0, x - 0.8) / 0.2);
};
const heatTint = (base, T, lo = 250, hi = 1100) => {
  const x = Math.max(0, Math.min(1, (T - lo) / (hi - lo)));
  return base.clone().lerp(srgb(0.95, 0.35, 0.12), 0.75 * x);
};

// ------------------------------------------------------------------ state
let S = null, META = null;
const byName = {};
const segs = [], bedSlabs = [], coolBed = [];
let kilnPivot, kilnDir, kilnIn, kilnLen, flame, flameLight, smoke;
const statusLights = {};
const tags = {};
const opt = { thermal: true, xray: false, labels: true, struct: true, orbit: false };
let kilnAngle = 0;

// ------------------------------------------------------------------ build scene from FreeCAD export
async function load() {
  const data = await (await fetch("/static/assets/plant3d.json")).json();
  META = data.meta;
  const K = META.kiln;
  kilnIn = new THREE.Vector3(...K.inlet);
  const kilnOut = new THREE.Vector3(...K.outlet);
  kilnLen = kilnIn.distanceTo(kilnOut);
  kilnDir = kilnOut.clone().sub(kilnIn).normalize();
  // rotating group: pivot at the feed-end axis point, children expressed relative to it
  kilnPivot = new THREE.Group(); kilnPivot.position.copy(kilnIn);
  const kilnInner = new THREE.Group(); kilnInner.position.copy(kilnIn).negate();
  kilnPivot.add(kilnInner); scene.add(kilnPivot);

  for (const o of data.objects) {
    const pos = new Float32Array(o.v.length);
    for (let i = 0; i < o.v.length; i++) pos[i] = o.v[i] / 100;          // cm -> m
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setIndex(o.i);
    g.computeVertexNormals();
    const base = srgb(...o.color);
    const m = new THREE.MeshStandardMaterial({ color: base, metalness: o.name.startsWith("Ground") ? 0 : 0.35,
      roughness: 0.62, transparent: o.opacity < 1, opacity: o.opacity, side: THREE.DoubleSide,
      depthWrite: o.opacity >= 1 });
    const mesh = new THREE.Mesh(g, m);
    mesh.name = o.name;
    mesh.userData.base = base;
    mesh.castShadow = o.opacity >= 1 && o.name !== "Ground";
    mesh.receiveShadow = true;
    (o.group === "kiln_rot" ? kilnInner : scene).add(mesh);
    byName[o.name] = mesh;
    if (o.name.startsWith("KilnSeg")) segs[parseInt(o.name.slice(7), 10)] = mesh;
  }
  API.objects = data.objects.length;
  buildKilnInternals(K);
  buildCoolerBed();
  buildIndicators();
  buildSmoke();
  buildTags();
  $("#loading").remove();
  API.ready = true;
}

function axisPoint(s) { return kilnIn.clone().addScaledVector(kilnDir, s); }

function buildKilnInternals(K) {
  // material bed (does not rotate): one slab per cell, lying in the lower part of the tube
  const up = new THREE.Vector3(0, 1, 0);
  const u = up.clone().addScaledVector(kilnDir, -kilnDir.dot(up)).normalize();   // "up" normal to the axis
  const w = new THREE.Vector3().crossVectors(kilnDir, u);
  const basis = new THREE.Matrix4().makeBasis(kilnDir, u, w);
  const n = K.n_seg, cell = kilnLen / n;
  for (let i = 0; i < n; i++) {
    const slab = new THREE.Mesh(new THREE.BoxGeometry(cell * 0.98, 0.55, 2.8),
      new THREE.MeshBasicMaterial({ color: 0x884422 }));
    slab.position.copy(axisPoint((i + 0.5) * cell)).addScaledVector(u, -K.radius * 0.78);
    slab.quaternion.setFromRotationMatrix(basis);
    slab.visible = false;
    scene.add(slab); bedSlabs.push(slab);
  }
  // flame: cone from the burner tip into the kiln, additive glow + light
  const B = META.burner;
  const cg = new THREE.ConeGeometry(1.05, 1, 28, 1, true); cg.translate(0, 0.5, 0);   // wide base at the burner (y=0), apex at y=1
  flame = new THREE.Mesh(cg, new THREE.MeshBasicMaterial({ color: 0xffb030, transparent: true, opacity: 0.85,
    blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
  flame.position.set(...B.tip);
  flame.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(...B.dir).normalize());
  flame.visible = false;
  scene.add(flame);
  flameLight = new THREE.PointLight(0xffa040, 0, 45, 1.6);
  flameLight.position.copy(axisPoint(kilnLen - 10));
  scene.add(flameLight);
}

function buildCoolerBed() {
  const C = META.cooler, n = C.cells, dx = (C.x1 - C.x0) / n, width = C.z[1] - C.z[0];
  for (let i = 0; i < n; i++) {
    const b = new THREE.Mesh(new THREE.BoxGeometry(dx * 0.96, 1, width),
      new THREE.MeshStandardMaterial({ color: 0x663322, emissive: 0x000000, roughness: 0.9 }));
    b.position.set(C.x0 + (i + 0.5) * dx, C.y_grate + 0.3, 0);
    b.castShadow = true; scene.add(b); coolBed.push(b);
  }
}

function buildIndicators() {
  // run/fault lamp above each drive
  const map = { IDFan: "ID_FAN", VentFan: "VENT_FAN", PAFan: "PA_FAN", KilnDriveMotor: "KILN_DRIVE" };
  for (let i = 1; i <= 6; i++) map[`CoolFan${i}`] = `CF${i}`;
  for (const [obj, drive] of Object.entries(map)) {
    const m = byName[obj]; if (!m) continue;
    m.geometry.computeBoundingBox();
    const bb = m.geometry.boundingBox, c = bb.getCenter(new THREE.Vector3());
    const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.45, 16, 12), new THREE.MeshBasicMaterial({ color: 0x888888 }));
    lamp.position.set(c.x, bb.max.y + 0.8, c.z);
    scene.add(lamp); statusLights[drive] = lamp;
  }
}

function buildSmoke() {
  const N = 220, g = new THREE.BufferGeometry(), p = new Float32Array(N * 3), life = new Float32Array(N);
  const top = new THREE.Vector3(...META.stack_top);
  for (let i = 0; i < N; i++) { life[i] = Math.random(); p.set([top.x, top.y + life[i] * 25, top.z], i * 3); }
  g.setAttribute("position", new THREE.BufferAttribute(p, 3));
  smoke = new THREE.Points(g, new THREE.PointsMaterial({ color: 0xd8dde2, size: 2.6, transparent: true, opacity: 0.35, depthWrite: false }));
  smoke.userData = { life, top, N };
  scene.add(smoke);
}

function tag(key, pos) {
  const el = document.createElement("div"); el.className = "tag";
  const o = new CSS2DObject(el); o.position.copy(pos); scene.add(o);
  tags[key] = { el, o };
  return tags[key];
}
function buildTags() {
  const cy = META.cyclones;
  for (const n of ["S1", "S2", "S3", "S4", "S5"]) tag(n, new THREE.Vector3(...cy[n].center).add(new THREE.Vector3(0, 0, cy[n].radius + 1.2)));
  tag("CAL", new THREE.Vector3(...META.calciner.mid).add(new THREE.Vector3(0, 0, 3.8)));
  tag("KI", kilnIn.clone().add(new THREE.Vector3(-3, 5.5, 3)));
  tag("BZ", axisPoint(kilnLen * 0.8).add(new THREE.Vector3(0, 3.4, 0)));
  tag("SHELL", axisPoint(kilnLen * 0.8).add(new THREE.Vector3(0, -3.2, 2.6)));
  tag("HOOD", new THREE.Vector3(...META.hood).add(new THREE.Vector3(1, 8, 0)));
  tag("BURNER", new THREE.Vector3(...META.burner.tip).add(new THREE.Vector3(10, 3, 0)));
  tag("COOLER", new THREE.Vector3((META.cooler.x0 + META.cooler.x1) / 2, 8.5, 0));
  tag("IDFAN", new THREE.Vector3(...META.id_fan).add(new THREE.Vector3(0, 5, 0)));
  tag("STACK", new THREE.Vector3(...META.stack_top).add(new THREE.Vector3(0, 3, 0)));
  tag("TA", new THREE.Vector3(...META.ta_duct).add(new THREE.Vector3(0, 2.6, 0)));
  tag("FEED", new THREE.Vector3(...META.feed).add(new THREE.Vector3(-4, 1.5, -8)));
}

// ------------------------------------------------------------------ live data -> scene
const f = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "--" : Number(v).toFixed(d));
function hms(t) { t = Math.floor(t); return `${Math.floor(t / 3600)}:${String(Math.floor(t / 60) % 60).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; }

function applyStatus() {
  if (!S || !API.ready) return;
  const k = S.kpi, P = S.profiles, D = S.drives;
  // kiln shell: scanner colours (thermal) or dark steel; x-ray makes it transparent
  segs.forEach((m, i) => {
    m.material.color.copy(opt.thermal ? scanner(P.T_shell[i]) : m.userData.base);
    m.material.emissive.copy(opt.thermal ? scanner(P.T_shell[i]).multiplyScalar(0.15) : new THREE.Color(0));
    m.material.transparent = opt.xray; m.material.opacity = opt.xray ? 0.18 : 1; m.material.depthWrite = !opt.xray;
  });
  bedSlabs.forEach((b, i) => { b.visible = opt.xray; b.material.color.copy(glow(P.T_bed[i])); });
  // cyclones & calciner tinted by stage temperature
  const stageT = { S1: k.T_S1, S2: k.T_S2, S3: k.T_S3, S4: k.T_S4, S5: k.T_calciner };
  for (const [n, T] of Object.entries(stageT)) {
    const m = byName["Cyc" + n]; if (m) m.material.color.copy(opt.thermal ? heatTint(m.userData.base, T) : m.userData.base);
  }
  const cal = byName.Calciner; if (cal) cal.material.color.copy(opt.thermal ? heatTint(cal.userData.base, k.T_calciner) : cal.userData.base);
  // flame
  const burning = D.COAL_KILN && D.COAL_KILN.pv > 0.1;
  flame.visible = burning && opt.xray;
  flame.userData.len = Math.max(1, k.flame_len_m || 1);
  const hot = Math.max(0, Math.min(1, ((k.T_gas_max || 1500) - 1400) / 700));
  flame.material.color.setRGB(1, 0.45 + 0.45 * hot, 0.1 + 0.5 * hot * hot);
  // the flame is inside a closed steel tube: its light is only meaningful in x-ray view
  flameLight.intensity = burning && opt.xray ? 900 * (D.COAL_KILN.pv / 3) : 0;
  flameLight.position.copy(axisPoint(Math.max(0, kilnLen - (k.flame_len_m || 10) * 0.7)));
  // cooler bed: depth per compartment, incandescence per 1 m cell
  const bounds = [[0, 2], [2, 5], [5, 8], [8, 11], [11, 14], [14, 18]];
  const bedm = S.cooler_bed_m || null;
  coolBed.forEach((b, i) => {
    const comp = bounds.findIndex(([a, c]) => i >= a && i < c);
    const h = Math.max(0.05, (bedm ? bedm[comp] : k.cooler_bed_m) || 0.6);
    b.scale.y = h; b.position.y = META.cooler.y_grate + h / 2;
    const c = glow(P.cooler_T[i]);
    b.material.color.copy(c); b.material.emissive.copy(c).multiplyScalar(Math.max(0, (P.cooler_T[i] - 450) / 1000));
  });
  // drive lamps
  for (const [drive, lamp] of Object.entries(statusLights)) {
    const st = D[drive] ? D[drive].state : "STOPPED";
    lamp.material.color.set(st === "RUNNING" ? 0x2fe060 : st === "FAULT" ? 0xff3030 : st.endsWith("ING") ? 0xffc020 : 0x777777);
  }
  // HUD + tags
  $("#h-time").textContent = hms(S.t); $("#h-bz").textContent = f(k.T_bz); $("#h-fl").textContent = f(k.free_lime_pct, 2);
  $("#h-cli").textContent = f(k.clinker_tph_avg, 1); $("#h-q").textContent = f(k.spec_heat_kJkg); $("#h-rpm").textContent = f(D.KILN_DRIVE.pv, 2);
  const alarmTags = new Set(S.alarms.map((a) => a.tag));
  const T = (key, html, tagsUsed = []) => {
    const t = tags[key]; if (!t) return;
    t.el.innerHTML = html; t.el.classList.toggle("alarm", tagsUsed.some((x) => alarmTags.has(x)));
    t.o.visible = opt.labels;
  };
  for (const n of ["S1", "S2", "S3", "S4"]) T(n, `<span class="n">${n}</span><b>${f(k["T_" + n])} °C</b> ${f(k["p_" + n], 1)} mbar`, ["T_" + n]);
  T("S5", `<span class="n">S5</span><b>${f(k.T_calciner)} °C</b> · hot meal ${f(k.hot_meal_doc_pct, 1)} %`);
  T("CAL", `<span class="n">calciner</span><b>${f(k.T_calciner)} °C</b> · coal ${f(k.coal_cal_tph, 2)} t/h`, ["T_calciner"]);
  T("KI", `<span class="n">kiln inlet</span><b>${f(k.T_kiln_inlet)} °C</b> O2 ${f(k.O2_kiln_inlet, 1)} % CO ${f(k.CO_kiln_inlet_ppm)} ppm`,
    ["T_kiln_inlet", "O2_kiln_inlet", "CO_kiln_inlet_ppm"]);
  const bzPos = Math.min(kilnLen - 2, Math.max(2, k.T_bz_pos_m || kilnLen * 0.8));
  tags.BZ.o.position.copy(axisPoint(bzPos).add(new THREE.Vector3(0, 3.4, 0)));
  T("BZ", `<span class="n">burning zone</span><b>${f(k.T_bz)} °C</b> free CaO ${f(k.free_lime_pct, 2)} %`, ["T_bz"]);
  const iMax = P.T_shell.indexOf(Math.max(...P.T_shell));
  tags.SHELL.o.position.copy(axisPoint((iMax + 0.5) * kilnLen / P.T_shell.length).add(new THREE.Vector3(0, -3.3, 2.6)));
  T("SHELL", `<span class="n">shell max</span><b>${f(k.shell_max_C)} °C</b>`, ["shell_max_C"]);
  T("HOOD", `<span class="n">hood</span><b>${f(k.p_hood, 2)} mbar</b> · sec. air ${f(k.T_sec_air)} °C`, ["p_hood", "T_sec_air"]);
  T("BURNER", `<span class="n">main burner</span><b>${f(k.coal_kiln_tph, 2)} t/h</b> · flame ${f(k.flame_len_m, 1)} m`);
  T("COOLER", `<span class="n">cooler</span>clinker out <b>${f(k.T_clinker_out)} °C</b> · ${f(k.dp_undergrate)} mbar`, ["T_clinker_out", "dp_undergrate"]);
  T("IDFAN", `<span class="n">ID fan</span><b>${f(D.ID_FAN.pv, 1)} %</b> · ${f(k.id_fan_kW)} kW`);
  T("STACK", `<span class="n">PH exit</span><b>${f(k.T_ph_exit)} °C</b> O2 ${f(k.O2_ph_exit, 1)} % · ${f(k.p_ph_exit, 1)} mbar`, ["T_ph_exit", "O2_ph_exit"]);
  T("TA", `<span class="n">tertiary air</span><b>${f(k.T_ter_air)} °C</b> · ${f(k.m_ta, 1)} kg/s · damper ${f(k.tad_pct)} %`);
  T("FEED", `<span class="n">kiln feed</span><b>${f(k.kiln_feed_tph)} t/h</b>`);
  // alarms
  const al = $("#alarms");
  al.style.display = S.alarms.length ? "block" : "none";
  al.innerHTML = S.alarms.slice(0, 4).map((a) => `<div>${a.desc} ${f(a.value, 1)}</div>`).join("");
  if (API.selected) showInfo(API.selected);
  API.statusCount++;
}

// ------------------------------------------------------------------ picking / info panel
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
let selMesh = null;
renderer.domElement.addEventListener("click", (e) => {
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  // pick what the user sees: skip hidden meshes, the ground and see-through parts (floors, casing)
  const hits = ray.intersectObjects(Object.values(byName).filter((m) => m.visible && m.name !== "Ground"
    && !(m.material.transparent && m.material.opacity < 0.5)), false);
  if (selMesh) selMesh.material.emissive.setHex(0x000000);
  if (!hits.length) { API.selected = null; $("#info").style.display = "none"; return; }
  selMesh = hits[0].object; selMesh.material.emissive.setHex(0x223344);
  API.selected = selMesh.name; showInfo(selMesh.name);
});
function showInfo(name) {
  if (!S) return;
  const k = S.kpi, P = S.profiles, D = S.drives, rows = [];
  const drv = (t) => D[t] && rows.push([D[t].desc, `${D[t].state} ${f(D[t].pv, 2)} ${D[t].unit}`]);
  let title = name;
  if (name.startsWith("KilnSeg")) {
    const i = parseInt(name.slice(7), 10); title = `Kiln shell ${f(P.x[i], 1)} m from inlet`;
    rows.push(["shell", `${f(P.T_shell[i])} °C`], ["brick face", `${f(P.T_wall[i])} °C`], ["material", `${f(P.T_bed[i])} °C`], ["gas", `${f(P.T_gas[i])} °C`]);
  } else if (name.startsWith("Cyc")) {
    const n = name.slice(3); title = `Cyclone ${n}`;
    rows.push(["temperature", `${f(n === "S5" ? k.T_calciner : k["T_" + n])} °C`], ["pressure", `${f(n === "S5" ? k.p_CAL : k["p_" + n], 1)} mbar`]);
  } else if (name === "Calciner" || name === "DuctCalS5") {
    title = "Calciner"; rows.push(["outlet", `${f(k.T_calciner)} °C`], ["coal", `${f(k.coal_cal_tph, 2)} t/h`], ["hot meal calcination", `${f(k.hot_meal_doc_pct, 1)} %`]);
    drv("COAL_CAL");
  } else if (/^(Tyre|GirthGear|KilnPinion|KilnGearbox|KilnDriveMotor|Roller|KilnStripe)/.test(name)) {
    title = "Kiln drive"; drv("KILN_DRIVE"); rows.push(["power", `${f(k.kiln_drive_kW)} kW`], ["fill", `${f(k.kiln_fill_pct, 1)} %`], ["residence", `${f(k.kiln_residence_min, 1)} min`]);
  } else if (/^(IDFan|DuctS1Fan|DuctFanStack|Stack)/.test(name)) {
    title = "ID fan / exit gas"; drv("ID_FAN"); rows.push(["power", `${f(k.id_fan_kW)} kW`], ["exit gas", `${f(k.T_ph_exit)} °C`], ["O2 / CO", `${f(k.O2_ph_exit, 2)} % / ${f(k.CO_ph_exit_ppm)} ppm`], ["draught", `${f(k.p_ph_exit, 1)} mbar`]);
  } else if (/^CoolFan/.test(name)) {
    const i = name.slice(7); title = `Cooler fan ${i}`; drv(`CF${i}`);
  } else if (/^(Cooler|ClinkerCrusher|PanConveyor|VentDuct|VentFan|CoolAirDuct)/.test(name)) {
    title = "Clinker cooler"; drv("GRATE"); drv("VENT_FAN");
    rows.push(["secondary air", `${f(k.T_sec_air)} °C`], ["tertiary air", `${f(k.T_ter_air)} °C`], ["clinker out", `${f(k.T_clinker_out)} °C`], ["undergrate", `${f(k.dp_undergrate)} mbar`]);
  } else if (/^(KilnHood|BurnerPipe|BurnerPlatform|PAFan|CoalBinKiln)/.test(name)) {
    title = "Main burner / hood"; drv("COAL_KILN"); drv("PA_FAN");
    rows.push(["flame length", `${f(k.flame_len_m, 1)} m`], ["hood", `${f(k.p_hood, 2)} mbar`], ["secondary air", `${f(k.T_sec_air)} °C`]);
  } else if (/^(TADuct|TADamper)/.test(name)) {
    title = "Tertiary air duct"; drv("TAD"); rows.push(["temperature", `${f(k.T_ter_air)} °C`], ["flow", `${f(k.m_ta, 1)} kg/s`]);
  } else if (/^(InletChamber)/.test(name)) {
    title = "Kiln inlet"; rows.push(["gas", `${f(k.T_kiln_inlet)} °C`], ["O2", `${f(k.O2_kiln_inlet, 2)} %`], ["CO", `${f(k.CO_kiln_inlet_ppm)} ppm`], ["NO", `${f(k.NO_kiln_inlet_ppm)} ppm`]);
  } else if (/^(FeedElevator|KilnFeedLine|Meal)/.test(name)) {
    title = "Raw meal"; drv("KILN_FEED"); rows.push(["hot meal calcination", `${f(k.hot_meal_doc_pct, 1)} %`]);
  }
  const box = $("#info");
  box.style.display = "block";
  box.innerHTML = `<h3>${title}</h3><table>${rows.map(([a, b]) => `<tr><td>${a}</td><td class="v">${b}</td></tr>`).join("") ||
    "<tr><td>structure</td><td></td></tr>"}</table>`;
}

// ------------------------------------------------------------------ cameras & toggles
const CAMS = {
  overview: [[95, 105, 195], [15, 36, 0]], tower: [[12, 68, 75], [-17, 55, 0]], kiln: [[32, 22, 48], [31, 8, 0]],
  burner: [[78, 15, 30], [62, 8, 0]], cooler: [[82, 16, 26], [74, 3, 0]],
};
let camAnim = null;
document.querySelectorAll("[data-cam]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-cam]").forEach((x) => x.classList.toggle("on", x === b));
  const [p, t] = CAMS[b.dataset.cam];
  camAnim = { p0: camera.position.clone(), t0: controls.target.clone(), p1: new THREE.Vector3(...p), t1: new THREE.Vector3(...t), s: 0 };
}));
const toggle = (id, key, after) => $(id).addEventListener("click", (e) => {
  opt[key] = !opt[key]; e.currentTarget.classList.toggle("on", opt[key]); if (after) after(); applyStatus();
});
toggle("#t-thermal", "thermal"); toggle("#t-xray", "xray"); toggle("#t-labels", "labels");
toggle("#t-struct", "struct", () => {
  for (const [n, m] of Object.entries(byName)) if (/^(Column|Beam|Floor|FeedElevator)/.test(n)) m.visible = opt.struct;
});
toggle("#t-rotate", "orbit", () => { controls.autoRotate = opt.orbit; });

// ------------------------------------------------------------------ websocket
function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => $("#conn").className = "ok";
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "status") { S = m; applyStatus(); } };
  ws.onclose = () => { $("#conn").className = "bad"; setTimeout(connect, 1500); };
}

// ------------------------------------------------------------------ animation
const clock = new THREE.Clock();
function frame() {
  requestAnimationFrame(frame);
  const rawDt = clock.getDelta();                 // real elapsed time (kinematics)
  const dt = Math.min(rawDt, 0.1);                 // capped step for visual effects
  if (API.ready && S) {
    // kiln rotation: real rpm x simulation speed (only while the simulation runs)
    const rpm = S.drives.KILN_DRIVE ? S.drives.KILN_DRIVE.pv : 0;
    if (S.running) kilnAngle += rpm / 60 * 2 * Math.PI * Math.min(rawDt, 2) * Math.min(S.speed, 10);
    kilnPivot.quaternion.setFromAxisAngle(kilnDir, kilnAngle);
    // flame flicker
    if (flame.visible) {
      const fl = 1 + 0.06 * Math.sin(performance.now() / 70) + 0.04 * Math.random();
      flame.scale.set(fl, flame.userData.len, fl);
    }
    // stack plume
    const idf = S.drives.ID_FAN ? S.drives.ID_FAN.pv / 100 : 0, sm = smoke.userData, pa = smoke.geometry.attributes.position;
    for (let i = 0; i < sm.N; i++) {
      sm.life[i] += dt * (0.08 + 0.25 * idf);
      if (sm.life[i] > 1) sm.life[i] = 0;
      const L = sm.life[i];
      pa.setXYZ(i, sm.top.x + L * 18 + Math.sin(i * 1.7) * L * 4, sm.top.y + L * 22, sm.top.z + Math.cos(i * 2.3) * L * 4);
    }
    pa.needsUpdate = true;
    smoke.material.opacity = 0.08 + 0.35 * idf;
  }
  if (camAnim) {
    camAnim.s = Math.min(1, camAnim.s + Math.min(rawDt, 0.5) / 1.2);   // time-based: same duration at any fps
    const e = camAnim.s < 0.5 ? 2 * camAnim.s * camAnim.s : 1 - Math.pow(-2 * camAnim.s + 2, 2) / 2;
    camera.position.lerpVectors(camAnim.p0, camAnim.p1, e); controls.target.lerpVectors(camAnim.t0, camAnim.t1, e);
    if (camAnim.s >= 1) camAnim = null;
  }
  controls.update();
  renderer.render(scene, camera);
  labels.render(scene, camera);
  API.frames++;
}
addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight); labels.setSize(innerWidth, innerHeight);
});

load().then(() => { applyStatus(); }).catch((e) => { $("#loading").textContent = "Failed to load 3D model: " + e; console.error(e); });
connect();
frame();
API.segColor = (i) => segs[i] ? "#" + segs[i].material.color.getHexString() : null;
API.flameLen = () => flame ? flame.scale.y : 0;
API.kilnAngle = () => kilnAngle;
API.camBusy = () => camAnim !== null;
API.flameLightOn = () => flameLight.intensity > 0;
API.screenOf = (name) => {          // screen position of an equipment item (tests / tooling)
  const m = byName[name]; if (!m) return null;
  m.geometry.computeBoundingBox();
  const c = m.geometry.boundingBox.getCenter(new THREE.Vector3());
  m.localToWorld(c); c.project(camera);
  return [(c.x + 1) / 2 * innerWidth, (1 - c.y) / 2 * innerHeight];
};
