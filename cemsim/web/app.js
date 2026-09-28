/* CemSim operator HMI - vanilla JS, no build step. */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const NS = "http://www.w3.org/2000/svg";
let S = null;            // last status
let META = null;
let ws = null;
const valueNodes = [];    // {node, tag, dec, unit, scale}
const equipNodes = [];    // {node, drive}
const groupNodes = [];    // {node, group}

// ---------------------------------------------------------------- websocket
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "status") { S = m; render(); renderFreecad(m.freecad); }
    else if (m.type === "meta") { META = m; buildTrainer(); buildTrendPicker(); fcBox(); }
    else if (m.type === "ack") {
      if (m.freecad) renderFreecad(m.freecad);             // instant FreeCAD status feedback
      if (m.ok === false && m.msg) { fpMsg(m.msg); toast(m.msg); }
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}
function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  $("#toasts").appendChild(t); setTimeout(() => t.remove(), 5000);
}
function cmd(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

// ---------------------------------------------------------------- svg helpers
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function txt(parent, x, y, s, cls = "lbl", anchor = "start") {
  const t = el("text", { x, y, class: cls, "text-anchor": anchor }, parent); t.textContent = s; return t;
}
function pipe(p, pts, cls, w = 6) {
  return el("polyline", { points: pts.map(q => q.join(",")).join(" "), class: cls, "stroke-width": w,
    "stroke-linejoin": "round" }, p);
}
function arrow(p, x, y, dir, color) {
  const d = { r: [[0, 0], [-9, -5], [-9, 5]], l: [[0, 0], [9, -5], [9, 5]], u: [[0, 0], [-5, 9], [5, 9]], d: [[0, 0], [-5, -9], [5, -9]] }[dir];
  el("polygon", { points: d.map(([a, b]) => `${x + a},${y + b}`).join(" "), fill: color }, p);
}
function val(p, x, y, tag, unit = "", dec = 0, w = 76) {
  const g = el("g", { class: "val", transform: `translate(${x},${y})` }, p);
  el("rect", { x: 0, y: 0, width: w, height: 18, rx: 1 }, g);
  const t = txt(g, w - 4, 13, "--", "", "end");
  t.setAttribute("style", "font:12px Arial");
  valueNodes.push({ node: t, g, tag, dec, unit });
  g.addEventListener("click", () => openTagFaceplate(tag));
  return g;
}
// ---- connection model: every vessel registers its ports; pipes are routed port->port ----
const GEOM = { ports: {}, vessels: [], pipes: [] };
window.MIMIC_GEOM = GEOM;            // exposed for the layout validation test
function cyclone(p, cx, cy, name, inletSide) {
  // body cylinder cx±32, cy-34..cy+6 | cone to cy+56 | vortex finder (gas outlet) cx±10, cy-50..cy-32
  const g = el("g", {}, p);
  const grad = "url(#steel)";
  el("rect", { x: cx - 32, y: cy - 34, width: 64, height: 40, fill: grad, stroke: "#555" }, g);
  el("path", { d: `M${cx - 32},${cy + 6} L${cx - 6},${cy + 56} L${cx + 6},${cy + 56} L${cx + 32},${cy + 6} Z`, fill: grad, stroke: "#555" }, g);
  el("rect", { x: cx - 10, y: cy - 50, width: 20, height: 18, fill: grad, stroke: "#555" }, g);
  txt(g, cx, cy - 10, name, "lbl", "middle");
  GEOM.ports[name + ".out"] = [cx, cy - 50];                                   // gas leaves via vortex finder
  GEOM.ports[name + ".in"] = [inletSide === "L" ? cx - 32 : cx + 32, cy - 22];  // tangential gas inlet
  GEOM.ports[name + ".meal"] = [cx, cy + 56];                                  // meal discharge (cone tip)
  GEOM.vessels.push({ name, polys: [
    [[cx - 32, cy - 34], [cx + 32, cy - 34], [cx + 32, cy + 6], [cx - 32, cy + 6]],
    [[cx - 32, cy + 6], [cx + 32, cy + 6], [cx + 6, cy + 56], [cx - 6, cy + 56]],
    [[cx - 10, cy - 50], [cx + 10, cy - 50], [cx + 10, cy - 32], [cx - 10, cy - 32]]] });
  return g;
}
function vessel(name, x, y, w, h, ports) {
  GEOM.vessels.push({ name, polys: [[[x, y], [x + w, y], [x + w, y + h], [x, y + h]]] });
  for (const [k, v] of Object.entries(ports)) GEOM.ports[name + "." + k] = v;
}
// route(parent, cls, width, from, via, to): from/to are port names or [x,y] junction points on a pipe
function route(p, cls, w, from, via, to, label) {
  const pt = (q) => (typeof q === "string" ? GEOM.ports[q] : q);
  const pts = [pt(from), ...via, pt(to)];
  if (pts.some((q) => !q)) console.error("route: unknown port", from, to);
  GEOM.pipes.push({ cls, from, to, pts, label });
  return pipe(p, pts, cls, w);
}
function fan(p, x, y, drive, r = 13) {
  const g = el("g", { class: "equip", transform: `translate(${x},${y})` }, p);
  el("circle", { r, fill: "#9aa0a6", stroke: "#333", class: "st-STOPPED", "data-st": "1" }, g);
  el("path", { d: `M0,0 L${r * 0.8},${-r * 0.55} M0,0 L${-r * 0.8},${-r * 0.55} M0,0 L0,${r * 0.9}`, stroke: "#222", "stroke-width": 2 }, g);
  g.dataset.drive = drive;
  equipNodes.push({ node: g, drive });
  g.addEventListener("click", (e) => openDrive(drive, e));
  return g;
}
function motor(p, x, y, drive) {
  const g = el("g", { class: "equip", transform: `translate(${x},${y})` }, p);
  el("circle", { r: 10, fill: "#9aa0a6", stroke: "#333", class: "st-STOPPED", "data-st": "1" }, g);
  txt(g, 0, 4, "M", "lbl", "middle").setAttribute("style", "font:700 11px Arial");
  g.dataset.drive = drive;
  equipNodes.push({ node: g, drive });
  g.addEventListener("click", (e) => openDrive(drive, e));
  return g;
}
function feeder(p, x, y, drive, w = 44) {
  const g = el("g", { class: "equip", transform: `translate(${x},${y})` }, p);
  el("rect", { x: -w / 2, y: -7, width: w, height: 14, fill: "#9aa0a6", stroke: "#333", class: "st-STOPPED", "data-st": "1", rx: 7 }, g);
  for (let i = -w / 2 + 6; i < w / 2 - 3; i += 7) el("line", { x1: i, y1: -7, x2: i, y2: 7, stroke: "#444" }, g);
  g.dataset.drive = drive;
  equipNodes.push({ node: g, drive });
  g.addEventListener("click", (e) => openDrive(drive, e));
  return g;
}
function damper(p, x, y, drive) {
  const g = el("g", { class: "equip", transform: `translate(${x},${y})` }, p);
  el("path", { d: "M-10,-10 L10,10 L10,-10 L-10,10 Z", fill: "#9aa0a6", stroke: "#333", class: "st-STOPPED", "data-st": "1" }, g);
  g.dataset.drive = drive;
  equipNodes.push({ node: g, drive });
  g.addEventListener("click", (e) => openDrive(drive, e));
  return g;
}
function sbtn(p, x, y, label, onClick, pid) {
  const g = el("g", { class: "sbtn equip", transform: `translate(${x},${y})` }, p);
  el("rect", { width: 16, height: 16 }, g);
  txt(g, 8, 12, label, "", "middle");
  g.addEventListener("click", onClick);
  if (pid) g.dataset.pid = pid;
  return g;
}
function gbtn(p, x, y, label, group, w = 104) {
  const g = el("g", { class: "btnbox equip", transform: `translate(${x},${y})` }, p);
  el("rect", { width: w, height: 20, rx: 2 }, g);
  el("rect", { x: 3, y: 4, width: 12, height: 12, class: "st-STOPPED", "data-st": "1" }, g);
  txt(g, 20, 14, label);
  groupNodes.push({ node: g, group });
  g.addEventListener("click", (e) => openGroup(group, e));
  return g;
}
function defs() {
  // gradients live in one always-rendered <svg> (Chrome ignores paint servers inside display:none svgs)
  if (document.getElementById("steel")) return;
  const d = el("defs", {}, $("#gdefs"));
  const lg = el("linearGradient", { id: "steel", x1: 0, x2: 1, y1: 0, y2: 0 }, d);
  [["0", "#6f767c"], ["0.45", "#e2e5e8"], ["1", "#6f767c"]].forEach(([o, c]) => el("stop", { offset: o, "stop-color": c }, lg));
  const lv = el("linearGradient", { id: "steelv", x1: 0, x2: 0, y1: 0, y2: 1 }, d);
  [["0", "#6f767c"], ["0.45", "#e2e5e8"], ["1", "#5d646a"]].forEach(([o, c]) => el("stop", { offset: o, "stop-color": c }, lv));
  const fl = el("radialGradient", { id: "gflame", cx: "0.9", cy: "0.5", r: "0.9" }, d);
  [["0", "#fff6a0"], ["0.4", "#ffb300"], ["1", "rgba(230,60,0,0)"]].forEach(([o, c]) => el("stop", { offset: o, "stop-color": c }, fl));
}

// ---------------------------------------------------------------- main mimic
function buildMimic() {
  const s = $("#mimic");
  defs(s);
  txt(s, 16, 772, "Kiln line - preheater / calciner / rotary kiln / grate cooler", "title");
  // ---- preheater tower: cyclones alternate right/left, inlets face the central riser channel x=270 ----
  // stage centres (y) chosen so each cone tip sits above the next riser; S5 at the calciner exit
  cyclone(s, 340, 110, "S1", "L"); cyclone(s, 200, 205, "S2", "R"); cyclone(s, 340, 300, "S3", "L");
  cyclone(s, 200, 395, "S4", "R"); cyclone(s, 340, 520, "S5", "R");
  // calciner vessel (sits on the kiln inlet chamber) and its gas outlet at the top
  el("rect", { x: 450, y: 390, width: 40, height: 198, fill: "url(#steel)", stroke: "#555" }, s);
  txt(s, 497, 404, "Calciner", "lbl", "start");
  vessel("CAL", 450, 390, 40, 198, { out: [470, 390], meal_in: [450, 462], ta_in: [490, 470], coal_in: [490, 540] });
  vessel("KI", 430, 588, 80, 70, { meal_in: [430, 605] });
  // ---- gas path (yellow): outlet (roof) of the lower stage -> side inlet of the upper stage ----
  const G = "pipe-gas";
  const gas = el("g", {}, s), meal = el("g", {}, s);          // meal drawn on top of gas at crossings
  route(gas, G, 14, "CAL.out", [[470, 375], [410, 375], [410, 498]], "S5.in", "calciner -> S5");
  route(gas, G, 12, "S5.out", [[340, 440], [270, 440], [270, 373]], "S4.in", "S5 -> S4");
  route(gas, G, 12, "S4.out", [[200, 330], [270, 330], [270, 278]], "S3.in", "S4 -> S3");
  route(gas, G, 12, "S3.out", [[340, 232], [270, 232], [270, 183]], "S2.in", "S3 -> S2");
  route(gas, G, 12, "S2.out", [[200, 140], [270, 140], [270, 88]], "S1.in", "S2 -> S1");
  vessel("IDF", 544, 56, 32, 32, { in: [560, 56], out: [576, 72] });
  vessel("STACK", 628, 10, 24, 32, { in: [640, 42] });
  route(gas, G, 10, "S1.out", [[340, 30], [560, 30]], "IDF.in", "S1 -> ID fan");
  route(gas, G, 10, "IDF.out", [[640, 72]], "STACK.in", "ID fan -> stack");
  el("rect", { x: 628, y: 10, width: 24, height: 32, fill: "#aaa", stroke: "#555" }, s); txt(s, 660, 30, "to raw mill / filter / stack");
  // ---- meal path (pink): cone tip -> the riser that feeds the NEXT stage down ----
  const M = "pipe-meal";
  route(meal, M, 4, [60, 120], [], [270, 120], "kiln feed -> riser S2->S1"); txt(s, 60, 112, "kiln feed");
  feeder(s, 80, 120, "KILN_FEED"); val(s, 36, 132, "kiln_feed_tph", "t/h", 0, 70);
  gbtn(s, 36, 156, "Kiln feed", "G_FEED", 90); sbtn(s, 132, 158, "S", () => openDrive("KILN_FEED"));
  route(meal, M, 4, "S1.meal", [[340, 205]], [270, 205], "S1 meal -> riser S3->S2");
  route(meal, M, 4, "S2.meal", [[200, 305]], [270, 305], "S2 meal -> riser S4->S3");
  route(meal, M, 4, "S3.meal", [[340, 410]], [270, 410], "S3 meal -> riser S5->S4");
  route(meal, M, 4, "S4.meal", [[200, 462]], "CAL.meal_in", "S4 meal -> calciner");
  route(meal, M, 5, "S5.meal", [[340, 605]], "KI.meal_in", "S5 hot meal -> kiln inlet");
  arrow(s, 438, 605, "r", "#c47f7f");
  // tertiary air duct
  route(s, "pipe-air", 9, [1080, 560], [[1080, 470]], "CAL.ta_in", "tertiary air -> calciner");
  damper(s, 760, 470, "TAD"); val(s, 780, 478, "tad_pct", "%", 0, 56); txt(s, 700, 462, "tertiary air duct");
  val(s, 900, 478, "T_ter_air", "°C", 0, 66); val(s, 970, 478, "m_ta", "kg/s", 1, 72);
  // calciner burner
  route(s, "pipe-coal", 4, [528, 540], [], "CAL.coal_in", "calciner coal"); feeder(s, 548, 540, "COAL_CAL", 40);
  val(s, 575, 531, "coal_cal_tph", "t/h", 2, 70);
  gbtn(s, 520, 556, "Calc. burner", "G_CALC", 100); sbtn(s, 624, 558, "C", () => openPID("TIC_CAL"), "TIC_CAL");
  // kiln inlet chamber
  el("rect", { x: 430, y: 588, width: 80, height: 70, fill: "url(#steel)", stroke: "#555" }, s);
  // rotary kiln
  const kg = el("g", { id: "kilnbody" }, s);
  for (let i = 0; i < 30; i++) el("rect", { x: 510 + i * 18.5, y: 600, width: 19, height: 52, fill: "#888", class: "kseg" }, kg);
  el("rect", { x: 510, y: 600, width: 555, height: 52, fill: "none", stroke: "#444" }, s);
  el("ellipse", { id: "kflame", cx: 1000, cy: 626, rx: 60, ry: 13, fill: "url(#gflame)" }, s);
  [[600, "I"], [790, "II"], [980, "III"]].forEach(([x]) => { el("rect", { x: x - 8, y: 596, width: 16, height: 60, fill: "#555" }, s);
    el("path", { d: `M${x - 18},${690} L${x},${656} L${x + 18},${690} Z`, fill: "#999", stroke: "#666" }, s); });
  el("rect", { x: 700, y: 594, width: 20, height: 64, fill: "#3b3b3b", class: "equip", id: "girth" }, s);
  motor(s, 710, 700, "KILN_DRIVE"); val(s, 728, 692, "kiln_rpm", "rpm", 2, 66); val(s, 728, 712, "kiln_drive_kW", "kW", 0, 66);
  gbtn(s, 800, 700, "Kiln MainDrive", "G_KILN", 112); sbtn(s, 916, 702, "S", () => openDrive("KILN_DRIVE"));
  // hood + burner
  el("rect", { x: 1065, y: 560, width: 50, height: 110, fill: "url(#steel)", stroke: "#555" }, s); txt(s, 1090, 552, "hood", "lbl", "middle");
  pipe(s, [[1190, 626], [1060, 626]], "pipe-coal", 5); arrow(s, 1052, 626, "l", "#5b3b2a");
  feeder(s, 1225, 610, "COAL_KILN", 44); val(s, 1250, 598, "coal_kiln_tph", "t/h", 2, 70);
  pipe(s, [[1190, 640], [1225, 640], [1225, 660]], "pipe-air", 3); fan(s, 1225, 672, "PA_FAN", 11); val(s, 1242, 662, "pa_fan_pct", "%", 0, 56);
  gbtn(s, 1160, 572, "Main burner", "G_BURNER", 100);
  // kiln values
  val(s, 540, 575, "T_kiln_inlet", "°C", 0); val(s, 540, 555, "p_kiln_inlet", "mbar", 1);
  const an = el("g", { transform: "translate(160,590)" }, s);
  el("rect", { x: 0, y: 0, width: 160, height: 104, fill: "#d8dade", stroke: "#888" }, an);
  txt(an, 6, 14, "kiln inlet gas analyser", "lbl");
  val(an, 6, 20, "O2_kiln_inlet", "Vol% O2", 2, 148); val(an, 6, 40, "CO_kiln_inlet_ppm", "ppm CO", 0, 148);
  val(an, 6, 60, "NO_kiln_inlet_ppm", "ppm NO", 0, 148); val(an, 6, 80, "hot_meal_doc_pct", "% DoC", 1, 148);
  val(s, 930, 570, "T_bz", "°C BZ", 0, 84); txt(s, 930, 566, "pyrometer", "lbl");
  val(s, 820, 570, "flame_len_m", "m flame", 1, 84);
  val(s, 600, 668, "shell_max_C", "°C shell", 0, 84);
  // ---- cooler ----
  el("rect", { x: 1080, y: 660, width: 300, height: 48, fill: "url(#steelv)", stroke: "#555" }, s);
  txt(s, 1230, 690, "grate cooler", "lbl", "middle");
  for (let i = 0; i < 6; i++) { const x = 1100 + i * 48; pipe(s, [[x, 740], [x, 708]], "pipe-air", 4); fan(s, x, 752, `CF${i + 1}`, 10); }
  gbtn(s, 990, 745, "Cooler", "G_COOLER", 70); motor(s, 1370, 730, "GRATE");
  val(s, 1180, 638, "T_sec_air", "°C sec", 0, 80); val(s, 1266, 638, "p_hood", "mbar", 2, 66); sbtn(s, 1334, 639, "C", () => openPID("PIC_HOOD"), "PIC_HOOD");
  val(s, 1290, 716, "dp_undergrate", "mbar", 0, 66); sbtn(s, 1358, 699, "C", () => openPID("PIC_UG"), "PIC_UG");
  // vent
  pipe(s, [[1340, 660], [1340, 520]], "pipe-air", 7); fan(s, 1340, 505, "VENT_FAN", 14);
  val(s, 1250, 540, "T_vent", "°C", 0, 60); val(s, 1250, 520, "vent_fan_pct", "%", 0, 60);
  // clinker out
  val(s, 1300, 760, "T_clinker_out", "°C clk", 0, 80);
  // ---- ID fan & exit gas ----
  fan(s, 560, 72, "ID_FAN", 16); val(s, 580, 88, "id_fan_pct", "%", 1, 60); gbtn(s, 580, 108, "ID fan", "G_EXH", 70);
  sbtn(s, 654, 110, "C", () => openPID("AIC_O2"), "AIC_O2");
  val(s, 420, 40, "T_ph_exit", "°C", 0, 64); val(s, 420, 60, "p_ph_exit", "mbar", 1, 64);
  const ex = el("g", { transform: "translate(700,60)" }, s);
  el("rect", { width: 150, height: 84, fill: "#d8dade", stroke: "#888" }, ex); txt(ex, 6, 14, "preheater exit analyser");
  val(ex, 6, 20, "O2_ph_exit", "Vol% O2", 2, 138); val(ex, 6, 40, "CO_ph_exit_ppm", "ppm CO", 0, 138); val(ex, 6, 60, "NO_ph_exit_ppm", "ppm NO", 0, 138);
  val(s, 700, 150, "id_fan_kW", "kW ID", 0, 90);
  // stage values
  [["S1", 390, 84], ["S2", 90, 183], ["S3", 390, 280], ["S4", 90, 373]].forEach(([n, x, y]) => {
    val(s, x, y, "T_" + n, "°C", 0, 64); val(s, x, y + 20, "p_" + n, "mbar", 1, 64); });
  val(s, 500, 414, "T_calciner", "°C", 0, 66); sbtn(s, 570, 415, "C", () => openPID("TIC_CAL"), "TIC_CAL"); val(s, 500, 434, "p_CAL", "mbar", 1, 66);
  val(s, 90, 520, "cone_buildup_t", "t cone S4", 2, 90);
  // ---- production box ----
  const pb = el("g", { transform: "translate(880,190)" }, s);
  el("rect", { width: 250, height: 190, fill: "#d8dade", stroke: "#888" }, pb);
  txt(pb, 8, 16, "production & quality", "title");
  val(pb, 8, 26, "clinker_tph_avg", "t/h clinker", 1, 234); val(pb, 8, 46, "spec_heat_kJkg", "kJ/kg cli", 0, 234);
  val(pb, 8, 66, "free_lime_pct", "% free CaO (kiln outlet)", 2, 234); val(pb, 8, 86, "lab_free_lime", "% free CaO (lab)", 2, 234);
  val(pb, 8, 106, "C3S_pct", "% C3S", 1, 234); val(pb, 8, 126, "heat_MW", "MW fuel", 1, 234);
  val(pb, 8, 146, "main_burner_share", "% main burner fuel", 0, 234); val(pb, 8, 166, "cooler_air_Nm3kg", "Nm3/kg cooling air", 2, 234);
}

// ---------------------------------------------------------------- burner screen
function buildBurner() {
  const s = $("#burner"); defs(s);
  txt(s, 16, 26, "Main burner - burner management system (BMS)", "title");
  gbtn(s, 16, 40, "311.9 Main burner group", "G_BURNER", 200);
  txt(s, 230, 55, "", "lbl").id = "bms-msg";
  // silo + dosing
  el("path", { d: "M120,110 L200,110 L200,200 L175,240 L145,240 L120,200 Z", fill: "url(#steel)", stroke: "#555" }, s);
  txt(s, 160, 150, "coal bin", "lbl", "middle");
  pipe(s, [[160, 240], [160, 300]], "pipe-coal", 5); feeder(s, 160, 310, "COAL_KILN", 60);
  val(s, 200, 300, "coal_kiln_tph", "t/h", 2, 80); sbtn(s, 284, 301, "S", () => openDrive("COAL_KILN"));
  pipe(s, [[160, 320], [160, 400], [700, 400]], "pipe-coal", 6);
  // primary air
  fan(s, 300, 520, "PA_FAN", 20); pipe(s, [[320, 520], [650, 520], [650, 420]], "pipe-air", 6);
  val(s, 330, 530, "pa_fan_pct", "%", 0, 60); val(s, 400, 530, "m_pa", "kg/s PA", 2, 90);
  // burner pipe
  el("rect", { x: 650, y: 385, width: 360, height: 34, fill: "url(#steelv)", stroke: "#555" }, s);
  el("rect", { x: 1010, y: 340, width: 60, height: 130, fill: "url(#steel)", stroke: "#555" }, s);
  txt(s, 1040, 330, "kiln hood", "lbl", "middle");
  el("ellipse", { id: "bflame", cx: 1180, cy: 402, rx: 120, ry: 24, fill: "url(#gflame)" }, s);
  el("rect", { x: 1070, y: 360, width: 320, height: 84, fill: "none", stroke: "#444", "stroke-dasharray": "4 3" }, s);
  txt(s, 1230, 356, "rotary kiln", "lbl", "middle");
  val(s, 1120, 460, "flame_len_m", "m flame (63 % burnout)", 1, 190);
  val(s, 1120, 480, "T_gas_max", "°C gas peak", 0, 190);
  val(s, 1120, 500, "T_bz", "°C burning zone", 0, 190);
  val(s, 1120, 520, "T_sec_air", "°C secondary air", 0, 190);
  val(s, 1120, 540, "m_sec", "kg/s secondary air", 1, 190);
  val(s, 1120, 560, "O2_kiln_inlet", "Vol% O2 kiln inlet", 2, 190);
  val(s, 1120, 580, "CO_kiln_inlet_ppm", "ppm CO kiln inlet", 0, 190);
  val(s, 1120, 600, "NO_kiln_inlet_ppm", "ppm NO kiln inlet", 0, 190);
  const bx = el("g", { transform: "translate(60,600)" }, s);
  el("rect", { width: 420, height: 130, fill: "#d8dade", stroke: "#888" }, bx);
  txt(bx, 8, 18, "start-up sequence: 1 primary air fan  ->  2 purge 60 s (ID fan running)  ->  3 coal dosing");
  txt(bx, 8, 40, "permissive: ID fan running.  Trips: ID fan off, primary air fan off, CO > 0.5 %");
  txt(bx, 8, 58, "or gas > 550 C at preheater exit.  Calciner: needs calciner > 750 C, trips > 1050 C.");
  txt(bx, 8, 86, "Flame length shortens with primary-air momentum and hotter secondary");
  txt(bx, 8, 104, "air; long flames move the burning zone upstream and raise kiln inlet T.");
}

// ---------------------------------------------------------------- cooler screen
function buildCooler() {
  const s = $("#coolersvg"); defs(s);
  txt(s, 16, 26, "Clinker cooler - reciprocating grate, 6 fan compartments, 18 m", "title");
  el("rect", { x: 100, y: 260, width: 1080, height: 150, fill: "url(#steelv)", stroke: "#555" }, s);
  const cg = el("g", { id: "coolcells" }, s);
  for (let i = 0; i < 18; i++) el("rect", { x: 104 + i * 59.6, y: 330, width: 58, height: 40, fill: "#888", class: "ccell" }, cg);
  txt(s, 104, 320, "clinker bed (colour = temperature)");
  const bounds = [[0, 2], [2, 5], [5, 8], [8, 11], [11, 14], [14, 18]];
  bounds.forEach(([a, b], i) => {
    const x = 104 + (a + b) / 2 * 59.6;
    el("line", { x1: 104 + a * 59.6, y1: 410, x2: 104 + a * 59.6, y2: 470, stroke: "#777" }, s);
    pipe(s, [[x, 560], [x, 412]], "pipe-air", 6); fan(s, x, 575, `CF${i + 1}`, 16);
    txt(s, x, 610, `fan ${i + 1}`, "lbl", "middle");
  });
  motor(s, 1210, 380, "GRATE"); val(s, 1230, 372, "grate_spm", "spm", 1, 70); gbtn(s, 1210, 410, "Cooler group", "G_COOLER", 120);
  val(s, 100, 430, "dp_undergrate", "mbar undergrate 1", 0, 170); sbtn(s, 274, 431, "C", () => openPID("PIC_UG"), "PIC_UG");
  val(s, 100, 450, "cooler_bed_m", "m bed depth 1", 2, 170);
  // hood / sec / ter / vent
  pipe(s, [[140, 250], [140, 120]], "pipe-air", 10); txt(s, 150, 130, "secondary air -> kiln"); val(s, 150, 140, "T_sec_air", "°C", 0, 70); val(s, 150, 160, "m_sec", "kg/s", 1, 70);
  pipe(s, [[420, 250], [420, 120]], "pipe-air", 8); txt(s, 430, 130, "tertiary air -> calciner"); val(s, 430, 140, "T_ter_air", "°C", 0, 70); val(s, 430, 160, "m_ta", "kg/s", 1, 70);
  pipe(s, [[1000, 250], [1000, 120]], "pipe-air", 8); fan(s, 1000, 110, "VENT_FAN", 18); txt(s, 1025, 100, "vent air -> cooler filter");
  val(s, 1025, 120, "T_vent", "°C", 0, 70); val(s, 1025, 140, "m_vent", "kg/s", 1, 70); val(s, 1025, 160, "vent_fan_pct", "%", 0, 70);
  val(s, 600, 200, "p_hood", "mbar hood", 2, 110); sbtn(s, 714, 201, "C", () => openPID("PIC_HOOD"), "PIC_HOOD");
  val(s, 600, 220, "m_leak_hood", "kg/s hood in-leak", 2, 150);
  val(s, 1190, 460, "T_clinker_out", "°C clinker out", 0, 130);
  val(s, 1190, 480, "clinker_cooler_tph", "t/h", 1, 130);
  val(s, 600, 650, "cooler_air_Nm3kg", "Nm3/kg cli", 2, 130);
}

// ---------------------------------------------------------------- rendering
function fmt(v, dec) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return Number(v).toFixed(dec);
}
function hms(t) { t = Math.floor(t); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60; return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`; }
function tempColor(T, lo = 150, hi = 450) {   // shell-scanner palette
  const x = Math.max(0, Math.min(1, (T - lo) / (hi - lo)));
  const stops = [[0, [30, 60, 190]], [0.35, [40, 190, 90]], [0.6, [240, 220, 40]], [0.8, [245, 130, 20]], [1, [220, 30, 30]]];
  for (let i = 1; i < stops.length; i++) if (x <= stops[i][0]) {
    const [a, ca] = stops[i - 1], [b, cb] = stops[i], f = (x - a) / (b - a);
    return `rgb(${ca.map((c, k) => Math.round(c + f * (cb[k] - c))).join(",")})`;
  }
  return "rgb(220,30,30)";
}
function glowColor(T) {   // incandescence for clinker (degC)
  const x = Math.max(0, Math.min(1, (T - 150) / 1250));
  const r = Math.round(90 + 165 * Math.min(1, x * 1.6)), g = Math.round(70 + 185 * Math.max(0, x - 0.35) / 0.65), b = Math.round(70 + 120 * Math.max(0, x - 0.8) / 0.2);
  return `rgb(${r},${g},${b})`;
}

function render() {
  if (!S) return;
  const k = S.kpi;
  const alarmTags = new Set(S.alarms.map(a => a.tag));
  for (const v of valueNodes) {
    const x = k[v.tag];
    v.node.textContent = `${fmt(x, v.dec)} ${v.unit}`;
    v.g.classList.toggle("alarm", alarmTags.has(v.tag));
  }
  for (const e of equipNodes) {
    const d = S.drives[e.drive]; if (!d) continue;
    const sh = e.node.querySelector("[data-st]"); sh.setAttribute("class", "st-" + d.state);
  }
  for (const g of groupNodes) {
    const d = S.groups[g.group]; if (!d) continue;
    g.node.querySelector("[data-st]").setAttribute("class", "st-" + d.state);
  }
  $$(".sbtn").forEach(b => { if (b.dataset.pid) b.classList.toggle("auto", S.pids[b.dataset.pid].mode === "AUTO"); });
  // kiln shell colours + flame
  const P = S.profiles;
  $$(".kseg").forEach((r, i) => r.setAttribute("fill", tempColor(P.T_shell[i])));
  const fl = $("#kflame");
  const fL = k.flame_len_m || 0, on = (S.drives.COAL_KILN.pv > 0.1);
  fl.setAttribute("rx", on ? Math.min(240, fL * 18.5 * 0.9) : 0);
  fl.setAttribute("cx", 1062 - (on ? Math.min(240, fL * 18.5 * 0.9) : 0));
  const bf = $("#bflame"); bf.setAttribute("rx", on ? Math.min(210, fL * 8) : 0); bf.setAttribute("cx", 1072 + (on ? Math.min(210, fL * 8) : 0));
  $$(".ccell").forEach((r, i) => r.setAttribute("fill", glowColor(P.cooler_T[i])));
  const bm = $("#bms-msg"); if (bm) bm.textContent = S.groups.G_BURNER.msg || "";
  // footer
  $("#simclock").textContent = hms(S.t);
  $("#btn-run").classList.toggle("on", S.running); $("#btn-run").textContent = S.running ? "stop" : "start";
  $("#speed").value = String(S.speed); $("#speed2").value = String(S.speed);
  const lights = [["raw feed", "KILN_FEED"], ["kiln", "KILN_DRIVE"], ["burner", "COAL_KILN"], ["calciner", "COAL_CAL"], ["cooler", "GRATE"], ["ID fan", "ID_FAN"]];
  $("#unit-lights").innerHTML = lights.map(([n, t]) => `<span class="${S.drives[t].state === "RUNNING" ? "on" : S.drives[t].state === "FAULT" ? "fault" : ""}">${n}</span>`).join("");
  $("#footer-kpis").innerHTML = [["BZ", "T_bz", 0, "°C"], ["O2 in", "O2_kiln_inlet", 1, "%"], ["cli", "clinker_tph_avg", 0, "t/h"], ["q", "spec_heat_kJkg", 0, "kJ/kg"], ["fCaO", "free_lime_pct", 1, "%"]]
    .map(([n, t, d, u]) => `<div>${n} <b>${fmt(k[t], d)}</b> ${u}</div>`).join("");
  // alarms
  const unack = S.alarms.filter(a => a.state !== "ACK_ACTIVE");
  $("#nav-alarm-count").textContent = S.alarms.length ? S.alarms.length : "";
  $("#alarm-banner").innerHTML = S.alarms.slice(0, 4).map(a => `<div class="${a.state === "ACK_ACTIVE" ? "ack" : ""}" data-id="${a.id}">${hms(a.t_on)} ${a.desc} ${fmt(a.value, 1)}</div>`).join("");
  $$("#alarm-banner div").forEach(d => d.onclick = () => cmd({ type: "ack", id: d.dataset.id }));
  const scr = currentScreen();
  if (scr === "alarms") renderAlarms();
  if (scr === "groups") renderGroups();
  if (scr === "profiles") renderProfiles();
  if (scr === "trainer") renderTrainer();
  if (fp.kind) refreshFaceplate();
}

// ---------------------------------------------------------------- faceplates
const fp = { kind: null, tag: null };
function placeFP(e) {
  const f = $("#faceplate");
  f.classList.remove("hidden");
  if (e && e.clientX) { f.style.left = Math.min(e.clientX + 10, innerWidth - 270) + "px"; f.style.top = Math.min(e.clientY + 10, innerHeight - 320) + "px"; }
  else if (!f.style.left) { f.style.left = "200px"; f.style.top = "120px"; }
}
function fpShell(title, body) {
  const f = $("#faceplate");
  f.innerHTML = `<div class="fp-head"><span>${title}</span><span style="cursor:pointer" id="fp-x">&#10005;</span></div><div class="fp-body">${body}<div class="msg" id="fp-msg"></div></div>`;
  $("#fp-x").onclick = closeFP;
  dragFP();
}
function fpMsg(m) { const e = $("#fp-msg"); if (e) e.textContent = m; }
function closeFP() { $("#faceplate").classList.add("hidden"); fp.kind = null; }
function dragFP() {
  const f = $("#faceplate"), h = f.querySelector(".fp-head"); let dx, dy;
  h.onmousedown = (e) => { dx = e.clientX - f.offsetLeft; dy = e.clientY - f.offsetTop;
    document.onmousemove = (m) => { f.style.left = m.clientX - dx + "px"; f.style.top = m.clientY - dy + "px"; };
    document.onmouseup = () => { document.onmousemove = null; }; };
}
function openGroup(g, e) {
  fp.kind = "group"; fp.tag = g;
  const d = S.groups[g];
  fpShell(`${g} - ${d.desc}`, `<div class="kv"><span>state</span><span id="fp-state"></span></div>
    <button id="g-ack">acknowledge</button><button id="g-start">start</button><button id="g-stop" class="danger">STOP</button>
    <button id="g-fast" class="danger">fast stop</button><button id="g-ss">stop start up</button><button id="g-close">Close</button>
    <div id="fp-steps" style="font-size:11px"></div>`);
  $("#g-ack").onclick = () => cmd({ type: "group", tag: g, cmd: "ack" });
  $("#g-start").onclick = () => cmd({ type: "group", tag: g, cmd: "start" });
  $("#g-stop").onclick = () => cmd({ type: "group", tag: g, cmd: "stop" });
  $("#g-fast").onclick = () => cmd({ type: "group", tag: g, cmd: "fast_stop" });
  $("#g-ss").onclick = () => cmd({ type: "group", tag: g, cmd: "stop_start" });
  $("#g-close").onclick = closeFP;
  placeFP(e); refreshFaceplate();
}
function openDrive(t, e) {
  fp.kind = "drive"; fp.tag = t;
  const d = S.drives[t];
  fpShell(`${t} - ${d.desc}`, `<div class="kv"><span>state</span><span id="fp-state"></span></div>
    <div class="kv"><span>actual</span><b id="fp-pv"></b></div><div class="kv"><span>setpoint</span><b id="fp-sp"></b></div>
    <div class="row"><input id="fp-in" type="number" step="any" style="width:100px"><button id="fp-set">set SP</button></div>
    <div class="row"><button id="fp-dn">&minus;</button><button id="fp-up">+</button></div>
    <button id="d-start">start</button><button id="d-stop" class="danger">stop</button><button id="d-reset">reset fault</button>`);
  const step = () => (d.hi - d.lo) / 100;
  $("#fp-set").onclick = () => cmd({ type: "drive", tag: t, cmd: "sp", value: parseFloat($("#fp-in").value) });
  const clampSp = (v) => Math.min(Math.max(v, d.lo), d.hi);
  $("#fp-up").onclick = () => cmd({ type: "drive", tag: t, cmd: "sp", value: clampSp(S.drives[t].sp + step()) });
  $("#fp-dn").onclick = () => cmd({ type: "drive", tag: t, cmd: "sp", value: clampSp(S.drives[t].sp - step()) });
  $("#d-start").onclick = () => cmd({ type: "drive", tag: t, cmd: "start" });
  $("#d-stop").onclick = () => cmd({ type: "drive", tag: t, cmd: "stop" });
  $("#d-reset").onclick = () => cmd({ type: "drive", tag: t, cmd: "reset" });
  $("#fp-in").value = d.sp.toFixed(2);
  placeFP(e); refreshFaceplate();
}
function openPID(t, e) {
  fp.kind = "pid"; fp.tag = t;
  const p = S.pids[t];
  fpShell(`${t} - ${p.desc}`, `<div class="kv"><span>mode</span><b id="fp-mode"></b></div>
    <div class="kv"><span>PV</span><b id="fp-pv"></b></div><div class="kv"><span>SP</span><b id="fp-sp"></b></div><div class="kv"><span>OUT (${p.target})</span><b id="fp-out"></b></div>
    <div class="row"><button id="p-auto">AUTO</button><button id="p-man">MAN</button></div>
    <div class="row"><input id="p-sp" type="number" step="any" style="width:90px"><button id="p-setsp">set SP</button></div>
    <div class="row"><input id="p-out" type="number" step="any" style="width:90px"><button id="p-setout">set OUT</button></div>`);
  $("#p-auto").onclick = () => cmd({ type: "pid", tag: t, field: "mode", value: "AUTO" });
  $("#p-man").onclick = () => cmd({ type: "pid", tag: t, field: "mode", value: "MAN" });
  $("#p-setsp").onclick = () => cmd({ type: "pid", tag: t, field: "sp", value: parseFloat($("#p-sp").value) });
  $("#p-setout").onclick = () => cmd({ type: "pid", tag: t, field: "out", value: parseFloat($("#p-out").value) });
  $("#p-sp").value = p.sp; $("#p-out").value = p.out.toFixed(2);
  placeFP(e); refreshFaceplate();
}
const TAG_FP = { T_calciner: ["pid", "TIC_CAL"], p_hood: ["pid", "PIC_HOOD"], dp_undergrate: ["pid", "PIC_UG"], O2_ph_exit: ["pid", "AIC_O2"],
  kiln_feed_tph: ["drive", "KILN_FEED"], coal_kiln_tph: ["drive", "COAL_KILN"], coal_cal_tph: ["drive", "COAL_CAL"], kiln_rpm: ["drive", "KILN_DRIVE"],
  id_fan_pct: ["drive", "ID_FAN"], vent_fan_pct: ["drive", "VENT_FAN"], pa_fan_pct: ["drive", "PA_FAN"], tad_pct: ["drive", "TAD"], grate_spm: ["drive", "GRATE"] };
function openTagFaceplate(tag) {
  const m = TAG_FP[tag];
  if (m) return m[0] === "pid" ? openPID(m[1]) : openDrive(m[1]);
  // otherwise jump to trends with this tag
  if (META && META.trend_tags.includes(tag)) { trendSel = new Set([tag, ...[...trendSel].slice(0, 3)]); show("trends"); buildTrendPicker(); }
}
function refreshFaceplate() {
  if (fp.kind === "group") {
    const g = S.groups[fp.tag];
    $("#fp-state").innerHTML = `<span class="badge b-${g.state}">${g.state}</span> ${g.msg || ""}`;
    $("#fp-steps").innerHTML = g.steps.map(t => t === "PURGE" ? `<div>purge</div>` :
      `<div class="kv"><span>${t}</span><span class="badge b-${S.drives[t].state}">${S.drives[t].state}</span></div>`).join("");
  } else if (fp.kind === "drive") {
    const d = S.drives[fp.tag];
    $("#fp-state").innerHTML = `<span class="badge b-${d.state}">${d.state}</span> ${d.fault || ""}`;
    $("#fp-pv").textContent = `${d.pv.toFixed(2)} ${d.unit}`; $("#fp-sp").textContent = `${d.sp.toFixed(2)} ${d.unit}`;
  } else if (fp.kind === "pid") {
    const p = S.pids[fp.tag];
    $("#fp-mode").textContent = p.mode; $("#fp-pv").textContent = fmt(p.pv, 2); $("#fp-sp").textContent = fmt(p.sp, 2); $("#fp-out").textContent = fmt(p.out, 2);
  }
}

// ---------------------------------------------------------------- screens
function currentScreen() { const a = $(".screen.active"); return a ? a.id.slice(4) : ""; }
function show(name) {
  $$(".screen").forEach(s => s.classList.toggle("active", s.id === "scr-" + name));
  $$("#nav button").forEach(b => b.classList.toggle("active", b.dataset.screen === name));
  if (name === "events") loadEvents();
  if (name === "scoring") loadScore();
  if (name === "balance") loadHB();
  if (name === "trends") drawTrend();
  if (name === "alarms") loadAlarmHist();
}
function renderAlarms() {
  $("#alarm-table tbody").innerHTML = S.alarms.map(a => `<tr class="p${a.priority} ${a.state.startsWith("UNACK") ? "unack" : ""}"><td>${hms(a.t_on)}</td><td>${a.priority}</td><td>${a.tag}</td><td>${a.desc}</td><td>${fmt(a.value, 2)}</td><td>${fmt(a.limit, 2)}</td><td>${a.state}</td></tr>`).join("");
}
async function loadAlarmHist() {
  const r = await (await fetch("/api/alarm_history?n=200")).json();
  $("#alarm-hist tbody").innerHTML = r.reverse().map(a => `<tr><td>${hms(a.t)}</td><td>${a.event}</td><td>${a.tag}</td><td>${a.desc}</td><td>${fmt(a.value, 2)}</td></tr>`).join("");
}
async function loadEvents() {
  const r = await (await fetch("/api/events?n=400")).json();
  $("#event-table tbody").innerHTML = r.reverse().map(e => `<tr><td>${hms(e.t)}</td><td>${e.source}</td><td>${e.tag}</td><td>${e.value}</td><td>${e.old ?? ""}</td><td>${e.comment}</td></tr>`).join("");
}
function renderGroups() {
  $("#group-grid").innerHTML = Object.entries(S.groups).map(([t, g]) => `<div class="gcard"><h4><span>${t} ${g.desc}</span><span class="state b-${g.state}">${g.state}</span></h4>
    ${g.steps.map(s => s === "PURGE" ? `<div class="drv"><span>purge timer</span><span>${g.msg}</span></div>` : `<div class="drv"><span>${s} ${S.drives[s].desc}</span><span class="badge b-${S.drives[s].state}">${S.drives[s].pv.toFixed(1)} ${S.drives[s].unit}</span></div>`).join("")}
    <div class="row"><button onclick="cmd({type:'group',tag:'${t}',cmd:'start'})">start</button><button onclick="cmd({type:'group',tag:'${t}',cmd:'stop'})">stop</button><button onclick="cmd({type:'group',tag:'${t}',cmd:'ack'})">ack</button></div>
    <div style="color:#b00;font-size:11px">${g.msg && g.msg.includes("blocked") ? g.msg : ""}</div></div>`).join("");
}

// ---------------------------------------------------------------- charts
function setupCanvas(c) {
  // The logical (CSS) height is read from the markup ONCE: writing c.height below also
  // rewrites the height attribute, so re-reading it would compound devicePixelRatio on
  // every redraw (canvas grows each frame on scaled displays until it breaks).
  if (!c.dataset.h) c.dataset.h = c.getAttribute("height");
  const r = c.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  const h = parseInt(c.dataset.h, 10);
  const pw = Math.round(Math.max(300, r.width) * dpr), ph = Math.round(h * dpr);
  if (c.width !== pw || c.height !== ph) { c.width = pw; c.height = ph; }
  c.style.height = h + "px";
  const x = c.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0); return [x, r.width, h];
}
function lineChart(c, series, opt = {}) {
  const [g, W, H] = setupCanvas(c);
  const L = 52, R = 12, T = 12, B = 26;
  g.clearRect(0, 0, W, H);
  const xs = series.flatMap(s => s.x), ys = series.flatMap(s => s.y).filter(v => v !== null && isFinite(v));
  if (!xs.length || !ys.length) return;
  let x0 = opt.x0 ?? Math.min(...xs), x1 = opt.x1 ?? Math.max(...xs);
  let y0 = opt.y0 ?? Math.min(...ys), y1 = opt.y1 ?? Math.max(...ys);
  if (y1 - y0 < 1e-9) { y1 += 1; y0 -= 1; }
  const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
  if (x1 <= x0) x1 = x0 + 1;
  const X = v => L + (v - x0) / (x1 - x0) * (W - L - R), Y = v => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  g.strokeStyle = "#e3e3e3"; g.fillStyle = "#555"; g.font = "11px Arial"; g.lineWidth = 1;
  for (let i = 0; i <= 5; i++) { const v = y0 + (y1 - y0) * i / 5, y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.fillText(v.toFixed(Math.abs(y1 - y0) < 10 ? 1 : 0), 4, y + 4); }
  for (let i = 0; i <= 6; i++) { const v = x0 + (x1 - x0) * i / 6, x = X(v); g.beginPath(); g.moveTo(x, T); g.lineTo(x, H - B); g.stroke(); g.fillText(opt.xfmt ? opt.xfmt(v) : v.toFixed(0), x - 14, H - 8); }
  for (const s of series) {
    g.strokeStyle = s.color; g.lineWidth = s.w || 2; g.beginPath(); let first = true;
    s.x.forEach((xv, i) => { const yv = s.y[i]; if (yv === null || !isFinite(yv)) return; const px = X(xv), py = Y(yv); first ? g.moveTo(px, py) : g.lineTo(px, py); first = false; });
    g.stroke();
  }
  if (opt.legend) { let lx = L + 8; for (const s of series) { g.fillStyle = s.color; g.fillRect(lx, T + 4, 14, 3); g.fillStyle = "#222"; g.fillText(s.name, lx + 18, T + 9); lx += g.measureText(s.name).width + 36; } }
}
function renderProfiles() {
  const P = S.profiles;
  lineChart($("#prof"), [
    { name: "gas", x: P.x, y: P.T_gas, color: "#e5a100" }, { name: "bed (material)", x: P.x, y: P.T_bed, color: "#c0392b" },
    { name: "wall (brick face)", x: P.x, y: P.T_wall, color: "#7f8c8d" }, { name: "shell", x: P.x, y: P.T_shell, color: "#2c6fbb" }], { legend: true, y0: 0, xfmt: v => v.toFixed(0) + " m" });
  const [g, W, H] = setupCanvas($("#scanner"));
  const n = P.T_shell.length, w = (W - 60) / n;
  P.T_shell.forEach((T, i) => { g.fillStyle = tempColor(T); g.fillRect(50 + i * w, 10, w + 1, 50); });
  g.fillStyle = "#222"; g.font = "11px Arial"; g.fillText("inlet", 4, 40); g.fillText("outlet", W - 40, 75);
  for (let T = 150; T <= 450; T += 50) { const x = 50 + (T - 150) / 300 * (W - 60); g.fillStyle = tempColor(T); g.fillRect(x, 66, 30, 10); g.fillStyle = "#222"; g.fillText(T + "°C", x + 32, 75); }
  lineChart($("#coolprof"), [{ name: "clinker", x: P.cooler_T.map((_, i) => i + 0.5), y: P.cooler_T, color: "#c0392b" }], { xfmt: v => v.toFixed(0) + " m", y0: 0 });
  lineChart($("#phprof"), [{ name: "stage T", x: [1, 2, 3, 4, 5], y: P.ph_T, color: "#8e44ad" }], { xfmt: v => ["", "S1", "S2", "S3", "S4", "CAL"][Math.round(v)] || "" });
}
const PRESETS = {
  "Burning zone": ["T_bz", "free_lime_pct", "kiln_drive_kW", "NO_kiln_inlet_ppm"],
  "Kiln inlet": ["T_kiln_inlet", "O2_kiln_inlet", "CO_kiln_inlet_ppm", "p_kiln_inlet"],
  "Calciner / PH": ["T_calciner", "coal_cal_tph", "hot_meal_doc_pct", "T_ph_exit"],
  "Draught": ["id_fan_pct", "p_ph_exit", "O2_ph_exit", "p_hood"],
  "Cooler": ["T_sec_air", "T_ter_air", "dp_undergrate", "T_clinker_out"],
  "Production": ["kiln_feed_tph", "clinker_tph_avg", "spec_heat_kJkg", "coal_kiln_tph"],
};
let trendSel = new Set(PRESETS["Burning zone"]);
const PEN = ["#c0392b", "#2c6fbb", "#27ae60", "#8e44ad", "#e67e22", "#16a085", "#7f8c8d", "#d35400"];
function buildTrendPicker() {
  if (!META) return;
  const ps = $("#trend-preset");
  ps.innerHTML = `<option value="">-</option>` + Object.keys(PRESETS).map(p => `<option>${p}</option>`).join("");
  ps.onchange = () => { if (PRESETS[ps.value]) { trendSel = new Set(PRESETS[ps.value]); buildTrendPicker(); drawTrend(); } };
  $("#trend-tags").innerHTML = META.trend_tags.map(t => `<label><input type="checkbox" value="${t}" ${trendSel.has(t) ? "checked" : ""}>${t}</label>`).join("");
  $$("#trend-tags input").forEach(i => i.onchange = () => { i.checked ? trendSel.add(i.value) : trendSel.delete(i.value); drawTrend(); });
}
async function drawTrend() {
  if (currentScreen() !== "trends" || !trendSel.size) return;
  const tags = [...trendSel].slice(0, 8);
  const r = await (await fetch(`/api/trend?tags=${tags.join(",")}&minutes=${$("#trend-win").value}`)).json();
  if (!r.t.length) return;
  // normalise each pen to its own range so all fit, legend shows ranges
  const series = [], leg = [];
  tags.forEach((t, i) => {
    const y = r[t] || []; const f = y.filter(v => v !== null);
    let lo = Math.min(...f), hi = Math.max(...f); if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
    series.push({ name: t, x: r.t, y: y.map(v => v === null ? null : (v - lo) / (hi - lo) * 100), color: PEN[i % PEN.length] });
    leg.push(`<span><i style="background:${PEN[i % PEN.length]}"></i>${t}: <b>${fmt(y[y.length - 1], 2)}</b> <small>[${fmt(lo, 1)} .. ${fmt(hi, 1)}]</small></span>`);
  });
  lineChart($("#trend"), series, { y0: 0, y1: 100, xfmt: hms });
  $("#trend-legend").innerHTML = leg.join("");
}

// ---------------------------------------------------------------- trainer
function buildTrainer() {
  const box = $("#dist-box"); let html = "", grp = "";
  html += `<div class="dist">`;
  for (const d of META.disturbances) {
    if (d.group !== grp) { grp = d.group; html += `<h4>${grp}</h4>`; }
    if (d.kind === "value") html += `<span>${d.label} <small>(${d.unit})</small></span><input type="number" step="any" id="dv-${d.id}" value="${d.default}" min="${d.min}" max="${d.max}"><button data-d="${d.id}">apply</button><span class="cur" id="dc-${d.id}"></span>`;
    else html += `<span>${d.label}</span><span></span><button data-t="${d.id}">trigger</button><span></span>`;
  }
  box.innerHTML = html + `</div>`;
  $$("#dist-box button[data-d]").forEach(b => b.onclick = () => cmd({ type: "dist", id: b.dataset.d, value: parseFloat($("#dv-" + b.dataset.d).value) }));
  $$("#dist-box button[data-t]").forEach(b => b.onclick = () => cmd({ type: "dist", id: b.dataset.t }));
  renderSnaps(META.snapshots);
}
function renderSnaps(list) {
  $("#snap-table tbody").innerHTML = list.map(s => `<tr><td>${s.name}</td><td>${s.comment}</td><td><button data-s="${s.name}">load</button></td></tr>`).join("");
  $$("#snap-table button").forEach(b => b.onclick = () => { cmd({ type: "snapshot_load", name: b.dataset.s }); });
}
function renderTrainer() {
  for (const [id, v] of Object.entries(S.dist)) { const e = $("#dc-" + id); if (e) e.textContent = v === null ? "" : Number(v).toFixed(id.includes("lhv") ? 0 : 2); }
  const L = S.lab;
  $("#lab-box").innerHTML = L && L.t_sample !== undefined ? `<table class="grid"><tr><th>sampled</th><th>free CaO %</th><th>C3S %</th><th>C2S %</th><th>C3A %</th><th>C4AF %</th><th>LSF</th><th>hot meal DoC %</th></tr>
    <tr><td>${hms(L.t_sample)}</td><td>${fmt(L.free_lime_pct, 2)}</td><td>${fmt(L.C3S_pct, 1)}</td><td>${fmt(L.C2S_pct, 1)}</td><td>${fmt(L.C3A_pct, 1)}</td><td>${fmt(L.C4AF_pct, 1)}</td><td>${fmt(L.LSF_clinker, 3)}</td><td>${fmt(L.hot_meal_doc_pct, 1)}</td></tr></table>` : "no sample analysed yet";
}
async function loadScore() {
  const r = await (await fetch("/api/session")).json();
  $("#sess-total").innerHTML = `session ${r.running ? "<b>running</b>" : "stopped"} &nbsp; total score: <b>${fmt(r.total, 1)}</b>`;
  $("#score-table tbody").innerHTML = r.criteria.map(c => `<tr><td>${c.subject}</td><td>${c.desc}</td><td>${c.method}</td><td>${c.ref}</td><td>${c.tol}</td><td>${c.weight}</td><td>${fmt(c.avg, 1)}</td><td>${fmt(c.min, 1)}</td><td>${fmt(c.max, 1)}</td><td>${fmt(c.in_band_pct, 0)}</td><td><b>${fmt(c.score, 1)}</b></td></tr>`).join("");
}
async function loadHB() {
  const r = await (await fetch("/api/heat_balance")).json();
  const rows = [["fuel (LHV) input", "fuel"], ["sensible heat of feed, coal, air", "sensible_inputs"], ["theoretical heat (chemical, incl. ash & moisture)", "theoretical"], ["preheater exit gas", "exit_gas"], ["cooler vent air", "vent_air"],
    ["clinker sensible", "clinker"], ["dust", "dust"], ["kiln shell", "shell_kiln"], ["preheater/calciner shell", "shell_preheater"], ["cooler casing", "shell_cooler"],
    ["unburnt fuel / CO", "unburnt_CO"], ["sum of outputs", "sum_out"], ["accumulation (in - out)", "closure"]];
  const mx = r.fuel || 1;
  $("#hb-box").innerHTML = `<table class="grid">${rows.map(([n, k]) => `<tr><td>${n}</td><td class="num">${fmt(r[k], 0)}</td><td style="width:220px"><span class="bar" style="width:${Math.max(0, (r[k] || 0) / mx * 200)}px"></span></td></tr>`).join("")}</table>
    <p style="color:#555">A heat balance is only meaningful at (quasi) steady state; transient accumulation in brick and holdup shows as closure.</p>`;
}
function fcBox() {
  if (!META || !META.freecad) { $("#fc-box").innerHTML = ""; return; }
  $("#fc-box").innerHTML = `<div class="fc-row"><span class="fc-dot" id="fc-dot"></span><b>FreeCAD 3D</b>
      <button id="fc-build">build</button><button id="fc-on">live on</button><button id="fc-off">off</button></div>
    <div class="fc-msg" id="fc-msg">-</div>`;
  $("#fc-build").onclick = () => cmd({ type: "freecad", cmd: "build", on: true });
  $("#fc-on").onclick = () => cmd({ type: "freecad", on: true });
  $("#fc-off").onclick = () => cmd({ type: "freecad", on: false });
}
let fcLast = "";
function renderFreecad(f) {
  if (!f || !$("#fc-dot")) return;
  $("#fc-dot").className = "fc-dot fc-" + f.state;
  const busy = f.state === "building" || f.state === "checking";
  $("#fc-build").disabled = busy;
  $("#fc-build").textContent = busy ? "building..." : "build";
  const txt = `${f.state}: ${f.msg || ""}`;
  $("#fc-msg").textContent = txt; $("#fc-msg").title = `${txt}  (${f.url}, ${f.pushes || 0} updates)`;
  if (f.state === "error" && txt !== fcLast) toast("FreeCAD: " + f.msg);
  fcLast = txt;
}

// ---------------------------------------------------------------- init
function init() {
  buildMimic(); buildBurner(); buildCooler();
  $$("#nav button, .trainerbtn").forEach(b => { if (b.dataset.screen) b.onclick = () => show(b.dataset.screen); });
  const open3d = () => window.open("/3d", "cemsim3d");
  $("#open3d").onclick = open3d; $("#nav3d").onclick = open3d;
  const toggle = () => cmd({ type: "run", on: !(S && S.running) });
  $("#btn-run").onclick = toggle; $("#btn-run2").onclick = toggle;
  $("#speed").onchange = (e) => cmd({ type: "speed", value: parseFloat(e.target.value) });
  $("#speed2").onchange = (e) => cmd({ type: "speed", value: parseFloat(e.target.value) });
  $("#ack-all").onclick = () => cmd({ type: "ack" });
  $("#snap-save").onclick = async () => { cmd({ type: "snapshot_save", name: $("#snap-name").value || "snap", comment: $("#snap-comment").value });
    setTimeout(async () => renderSnaps(await (await fetch("/api/snapshots")).json()), 600); };
  $("#sess-start").onclick = () => { cmd({ type: "session", cmd: "start" }); setTimeout(loadScore, 400); };
  $("#sess-stop").onclick = () => { cmd({ type: "session", cmd: "stop" }); setTimeout(loadScore, 400); };
  $("#trend-win").onchange = drawTrend;
  setInterval(() => { const s = currentScreen(); if (s === "trends") drawTrend(); if (s === "scoring") loadScore(); if (s === "events") loadEvents(); if (s === "balance") loadHB(); }, 3000);
  connect();
}
init();
