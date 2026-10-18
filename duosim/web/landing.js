/* Landing page: live pricing from the billing catalogue, signed-in state, mobile menu, scroll effects, hero demo. */
import { $, $$, api, el, fmt, me } from "/static/saas.js";
import { SCENES, initDemo, sceneSvg } from "/static/landing-demo.js";

$("#year").textContent = new Date().getFullYear();
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------------------------------------------------------- nav: mobile menu, scrolled state, scroll-spy */
const nav = $("#nav"), links = $("#links"), menu = $("#menu");
const setMenu = (open) => { links.classList.toggle("open", open); menu.setAttribute("aria-expanded", String(open)); };
menu.onclick = () => setMenu(!links.classList.contains("open"));
for (const a of $$("#links a")) a.addEventListener("click", () => setMenu(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && links.classList.contains("open")) { setMenu(false); menu.focus(); } });
document.addEventListener("click", (e) => { if (!nav.contains(e.target)) setMenu(false); });

const bar = $("#progress"), steps = $$("#timeline li"), timeline = $("#timeline");
let ticking = false;
function onScroll() {
  ticking = false;
  const y = window.scrollY, max = document.documentElement.scrollHeight - innerHeight;
  nav.classList.toggle("scrolled", y > 8);
  bar.style.setProperty("--p", max > 0 ? Math.min(y / max, 1).toFixed(4) : 0);
  // timeline fills as it crosses the middle of the viewport
  const r = timeline.getBoundingClientRect(), p = Math.min(Math.max((innerHeight * 0.62 - r.top) / r.height, 0), 1);
  timeline.style.setProperty("--tp", p.toFixed(3));
  steps.forEach((li, i) => li.classList.toggle("lit", p > i / steps.length + 0.02));
}
addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
addEventListener("resize", onScroll);
onScroll();

const spy = new Map($$("#links a[href^='#']").map((a) => [$(a.getAttribute("href")), a]));
const spyIO = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) for (const [sec, a] of spy) a.classList.toggle("on", sec === e.target);
}, { rootMargin: "-45% 0px -50% 0px" });
for (const sec of spy.keys()) if (sec) spyIO.observe(sec);

/* ---------------------------------------------------------------- reveal on scroll, count-up, trace */
const revealIO = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); revealIO.unobserve(e.target); }
}, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
const reveal = (root = document) => $$(".reveal, .term", root).forEach((n) => revealIO.observe(n));
$$("#trace li").forEach((li, i) => li.style.setProperty("--k", i));
reveal();

const countIO = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    countIO.unobserve(e.target);
    const to = +e.target.dataset.count;
    if (reduced || !to) { e.target.textContent = to; continue; }
    const t0 = performance.now();
    const tick = (t) => { const k = Math.min((t - t0) / 1100, 1); e.target.textContent = Math.round(to * (1 - (1 - k) ** 3)); if (k < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }
}, { threshold: 0.6 });
$$("[data-count]").forEach((n) => { if (!reduced) n.textContent = "0"; countIO.observe(n); });

/* ---------------------------------------------------------------- cursor spotlight on cards (pointer devices only) */
if (matchMedia("(hover: hover)").matches) {
  document.addEventListener("pointermove", (e) => {
    const c = e.target.closest?.(".tilt, .uc, .price");
    if (!c) return;
    const r = c.getBoundingClientRect();
    c.style.setProperty("--mx", `${e.clientX - r.left}px`);
    c.style.setProperty("--my", `${e.clientY - r.top}px`);
  }, { passive: true });
}

/* ---------------------------------------------------------------- hero demo + use-case thumbnails */
initDemo($("#demo"));
for (const card of $$(".uc[data-scene]")) {
  const scene = SCENES.find((s) => s.id === card.dataset.scene);
  if (scene) $(".uc-art", card).append(sceneSvg(scene));
}

/* ---------------------------------------------------------------- pricing from the billing catalogue */
const COST_LABELS = {
  "generate.fast": "Design, fast agent",
  "generate.standard": "Design, standard agent (plan, critique, repair)",
  "generate.deep": "Design, deep agent (candidates, visual judge)",
  "generate.byok": "Design with your own model key (any mode)",
  image: "Reference images (per request)",
  build: "FreeCAD build, verification and exports",
  import: "CAD / mesh import and primitive fit",
  session: "Simulator session",
};

function planCard(p, signedIn, i) {
  const featured = p.id === "pro";
  const price = p.price_usd == null ? el("div", { class: "amount" }, "Custom") : el("div", { class: "amount" }, fmt.usd(p.price_usd), el("small", {}, " / month"));
  const href = p.id === "enterprise" ? "mailto:sales@duosim.io?subject=DuoSim%20Enterprise" : signedIn ? (p.id === "free" ? "/app" : "/account#billing") : `/signup?plan=${p.id}`;
  const label = p.id === "enterprise" ? "Talk to sales" : p.id === "free" ? (signedIn ? "Open the Studio" : "Start free") : `Choose ${p.name}`;
  const card = el("div", { class: `card price reveal${featured ? " featured" : ""}` },
    el("h3", {}, p.name), el("div", { class: "muted" }, p.blurb), price,
    el("ul", {}, p.highlights.map((h) => el("li", {}, h))),
    el("a", { class: `btn block ${featured ? "primary" : ""}`, href }, label));
  card.style.setProperty("--d", `${i * 0.08}s`);
  return card;
}

async function main() {
  const [catalog, m] = await Promise.all([api("/api/billing/plans").catch(() => null), me().catch(() => null)]);
  if (m) {
    $("#nav-actions").replaceChildren(
      el("a", { class: "btn sm ghost hide-sm", href: "/account" }, "Account"),
      el("a", { class: "btn sm primary", href: "/app" }, "Open Studio"));
  }
  if (!catalog) return;
  $("#plans").replaceChildren(...catalog.plans.map((p, i) => planCard(p, !!m, i)));
  reveal($("#plans"));
  const c = catalog.costs;
  $("#costs").replaceChildren(...Object.entries(COST_LABELS).filter(([k]) => k in c).map(([k, label]) =>
    el("tr", {}, el("td", {}, label), el("td", { class: "n" }, `${c[k]} credit${c[k] === 1 ? "" : "s"}`))));
  $("#packs").textContent = "Credit packs: " + catalog.packs.map((p) => `${fmt.int(p.credits)} for ${fmt.usd(p.price_usd)}`).join(" · ");
  const rows = $("#mode-costs").rows;
  ["fast", "standard", "deep"].forEach((m2, i) => { if (rows[i] && c[`generate.${m2}`] != null) rows[i].cells[2].textContent = c[`generate.${m2}`]; });
}
main();
