/* Landing page: live pricing from the billing catalogue, signed-in state, mobile menu. */
import { $, api, el, fmt, me } from "/static/saas.js";

$("#year").textContent = new Date().getFullYear();
$("#menu").onclick = () => $("#links").classList.toggle("open");
for (const a of document.querySelectorAll("#links a")) a.addEventListener("click", () => $("#links").classList.remove("open"));

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

function planCard(p, signedIn) {
  const featured = p.id === "pro";
  const price = p.price_usd == null ? el("div", { class: "amount" }, "Custom") : el("div", { class: "amount" }, fmt.usd(p.price_usd), el("small", {}, " / month"));
  const href = p.id === "enterprise" ? "mailto:sales@duosim.io?subject=DuoSim%20Enterprise" : signedIn ? (p.id === "free" ? "/app" : "/account#billing") : `/signup?plan=${p.id}`;
  const label = p.id === "enterprise" ? "Talk to sales" : p.id === "free" ? (signedIn ? "Open the Studio" : "Start free") : `Choose ${p.name}`;
  return el("div", { class: `card price${featured ? " featured" : ""}` },
    el("h3", {}, p.name), el("div", { class: "muted" }, p.blurb), price,
    el("ul", {}, p.highlights.map((h) => el("li", {}, h))),
    el("a", { class: `btn block ${featured ? "primary" : ""}`, href }, label));
}

async function main() {
  const [catalog, m] = await Promise.all([api("/api/billing/plans").catch(() => null), me().catch(() => null)]);
  if (m) {
    $("#nav-actions").replaceChildren(
      el("a", { class: "btn sm ghost hide-sm", href: "/account" }, "Account"),
      el("a", { class: "btn sm primary", href: "/app" }, "Open Studio"));
  }
  if (!catalog) return;
  $("#plans").replaceChildren(...catalog.plans.map((p) => planCard(p, !!m)));
  const c = catalog.costs;
  $("#costs").replaceChildren(...Object.entries(COST_LABELS).filter(([k]) => k in c).map(([k, label]) =>
    el("tr", {}, el("td", {}, label), el("td", { class: "n" }, `${c[k]} credit${c[k] === 1 ? "" : "s"}`))));
  $("#packs").textContent = "Credit packs: " + catalog.packs.map((p) => `${fmt.int(p.credits)} for ${fmt.usd(p.price_usd)}`).join(" · ");
  const rows = $("#mode-costs").rows;
  ["fast", "standard", "deep"].forEach((m2, i) => { if (rows[i] && c[`generate.${m2}`] != null) rows[i].cells[2].textContent = c[`generate.${m2}`]; });
}
main();
