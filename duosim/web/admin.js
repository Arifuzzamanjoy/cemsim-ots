/* Platform administration: KPIs, users (admin / suspend), workspaces (plan, credits, status), audit. */
import { $, $$, api, appNav, el, fmt, me, toast } from "/static/saas.js";

let ME = null;
const PLANS = ["free", "pro", "team", "enterprise"];

const L = {
  async stats() {
    const s = await api("/api/admin/stats");
    const tile = (k, v, sub = "") => el("div", { class: "card stat" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "faint" }, sub));
    $("#stats").replaceChildren(
      tile("Users", fmt.int(s.users), `+${fmt.int(s.users_30d)} in 30 days`),
      tile("Active (30 d)", fmt.int(s.active_30d), "signed in"),
      tile("Designs", fmt.int(s.designs), `+${fmt.int(s.designs_30d)} in 30 days`),
      tile("MRR (list price)", fmt.usd(s.mrr_usd), `${fmt.int(s.credits_spent_30d - s.credits_refunded_30d)} credits used in 30 d`));
    $("#plans").replaceChildren(...Object.entries(s.plans).map(([p, n]) => el("span", { class: "chip brand" }, `${p}: ${n}`)));
  },
  async users(q = "") {
    const r = await api(`/api/admin/users?q=${encodeURIComponent(q)}`);
    $("#users").replaceChildren(...r.users.map((u) => {
      const flag = async (body) => { try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body }); L.users(q); } catch (e) { toast(e.message, "bad"); } };
      return el("tr", {}, el("td", {}, el("div", {}, u.email, u.is_admin ? el("span", { class: "chip brand", style: "margin-left:6px" }, "admin") : ""), el("div", { class: "faint" }, u.name || "")),
        el("td", {}, fmt.date(u.created)), el("td", {}, fmt.dt(u.last_login)),
        el("td", {}, u.suspended ? el("span", { class: "chip bad" }, "suspended") : el("span", { class: "chip ok" }, u.email_verified ? "verified" : "active")),
        el("td", { class: "n" }, u.id === ME.user.id ? el("span", { class: "faint" }, "you") : el("div", { class: "row", style: "justify-content:flex-end" },
          el("button", { class: "btn sm", onclick: () => flag({ is_admin: !u.is_admin }) }, u.is_admin ? "Remove admin" : "Make admin"),
          el("button", { class: `btn sm ${u.suspended ? "" : "danger"}`, onclick: () => flag({ suspended: !u.suspended }) }, u.suspended ? "Reactivate" : "Suspend"))));
    }));
  },
  async workspaces(q = "") {
    const r = await api(`/api/admin/workspaces?q=${encodeURIComponent(q)}`);
    $("#workspaces").replaceChildren(...r.workspaces.map((w) => {
      const plan = el("select", { style: "width:120px" }, ...PLANS.map((p) => el("option", { value: p, selected: p === w.plan }, p)));
      plan.onchange = async () => { try { await api(`/api/admin/workspaces/${w.id}/plan`, { method: "POST", body: { plan: plan.value } }); toast("Plan changed; allowance credited.", "ok"); L.workspaces(q); } catch (e) { toast(e.message, "bad"); } };
      const grant = async () => {
        const v = prompt(`Credits to add to ${w.display_name} (negative to remove):`, "100");
        if (v == null) return;
        const delta = parseInt(v, 10);
        if (!Number.isFinite(delta)) return toast("Not a number.", "bad");
        try { await api(`/api/admin/workspaces/${w.id}/credits`, { method: "POST", body: { delta, note: "admin adjustment" } }); L.workspaces(q); } catch (e) { toast(e.message, "bad"); }
      };
      const st = async () => { try { await api(`/api/admin/workspaces/${w.id}/status`, { method: "POST", body: { status: w.status === "active" ? "suspended" : "active" } }); L.workspaces(q); } catch (e) { toast(e.message, "bad"); } };
      return el("tr", {}, el("td", {}, el("div", {}, w.display_name), el("div", { class: "faint" }, w.name)), el("td", {}, plan),
        el("td", { class: "n" }, fmt.int(w.credits)), el("td", { class: "n" }, w.members), el("td", { class: "n" }, w.designs),
        el("td", {}, w.status === "active" ? el("span", { class: "chip ok" }, "active") : el("span", { class: "chip bad" }, w.status)),
        el("td", { class: "n" }, el("div", { class: "row", style: "justify-content:flex-end" }, el("button", { class: "btn sm", onclick: grant }, "Credits…"),
          el("button", { class: `btn sm ${w.status === "active" ? "danger" : ""}`, onclick: st }, w.status === "active" ? "Suspend" : "Reactivate"))));
    }));
  },
  async audit() {
    const r = await api("/api/admin/audit?limit=300");
    $("#audit").replaceChildren(...r.events.map((a) => el("tr", {}, el("td", {}, fmt.dt(a.ts)), el("td", {}, a.email || "-"), el("td", { class: "faint" }, a.workspace_id || "-"),
      el("td", {}, a.action), el("td", { class: "faint", style: "font-size:12px" }, JSON.stringify(a.detail)), el("td", { class: "faint" }, a.ip || ""))));
  },
};

$("#uq").addEventListener("submit", (e) => { e.preventDefault(); L.users($("#uq-text").value); });
$("#wq").addEventListener("submit", (e) => { e.preventDefault(); L.workspaces($("#wq-text").value); });

function show() {
  const s = (location.hash || "#stats").slice(1), name = s in L ? s : "stats";
  $$(".side a").forEach((a) => a.classList.toggle("on", a.dataset.s === name));
  $$(".main section").forEach((x) => x.classList.toggle("on", x.id === `s-${name}`));
  L[name]().catch((e) => toast(e.message, "bad"));
}
addEventListener("hashchange", show);

(async () => {
  ME = await me();
  if (!ME) { location.href = "/login?next=/admin"; return; }
  appNav(ME);
  if (!ME.user.is_admin) {
    $("#denied").replaceChildren(el("div", { class: "notice bad" }, "Administrators only. Platform admins are set with DUOSIM_ADMIN_EMAILS or `python -m duosim.platform users admin <email>`."));
    $$(".main section").forEach((x) => x.classList.remove("on"));
    return;
  }
  show();
})();
