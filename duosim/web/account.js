/* Account dashboard: one page, hash-routed sections; every section loads lazily and re-renders after writes. */
import { $, $$, api, appNav, el, fmt, me, toast } from "/static/saas.js";

let ME = null;
const isAdmin = () => ["owner", "admin"].includes(ME?.workspace?.role);
const isOwner = () => ME?.workspace?.role === "owner";

async function refreshMe() {
  ME = await me();
  if (!ME) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash); return null; }
  appNav(ME);
  return ME;
}

function banner() {
  const q = new URLSearchParams(location.search), b = $("#banner");
  const items = [];
  if (q.get("billing") === "success") items.push(el("div", { class: "notice ok" }, "Thank you! Your purchase is being applied: credits appear as soon as the payment is confirmed."));
  if (q.get("billing") === "cancel") items.push(el("div", { class: "notice warn" }, "Checkout was cancelled. Nothing was charged."));
  if (q.get("welcome")) items.push(el("div", { class: "notice ok" }, "Welcome to DuoSim! Pick a plan below or start with the free credits."));
  if (ME && !ME.user.email_verified) {
    items.push(el("div", { class: "notice warn" }, "Please confirm your email address. ",
      el("button", { class: "btn sm", onclick: async () => { await api("/api/auth/verify/resend", { method: "POST" }); toast("Confirmation email sent.", "ok"); } }, "Resend email")));
  }
  b.replaceChildren(...items);
}

// ------------------------------------------------------------------ sections
const LOADERS = {
  async overview() {
    const ws = ME.workspace, c = await api("/api/account/credits");
    $("#hello").textContent = `Hello${ME.user.name ? ", " + ME.user.name.split(" ")[0] : ""}`;
    $("#ov-credits").textContent = c.enabled ? fmt.int(c.balance) : "∞";
    const allowance = ws.plan_info.monthly_credits || 1;
    $("#ov-bar").style.width = `${Math.min(100, (100 * c.balance) / allowance)}%`;
    $("#ov-plan").textContent = ws.plan_info.name;
    $("#ov-plan-sub").textContent = `${fmt.int(ws.plan_info.monthly_credits)} credits / month`;
    $("#ov-spent").textContent = fmt.int(c.usage_30d.spent);
    $("#ov-seats").textContent = `${ws.seats_used} / ${ws.plan_info.seats >= 1000 ? "∞" : ws.plan_info.seats}`;
    $("#ov-role").textContent = `you are ${ws.role}`;
    const ops = c.usage_30d.by_op, max = Math.max(1, ...ops.map((o) => o.credits));
    $("#ov-bars").replaceChildren(...(ops.length ? ops.map((o) => el("div", { style: `height:${(100 * o.credits) / max}%`, title: `${o.op}: ${o.credits} credits, ${o.n}×` }, el("span", {}, o.op.replace("generate.", "")))) : [el("p", { class: "muted" }, "No usage yet.")]));
    $("#ws-list").replaceChildren(...ME.workspaces.map((w) => el("tr", {},
      el("td", {}, w.display_name), el("td", {}, w.plan), el("td", {}, w.role),
      el("td", { class: "n" }, w.workspace_id === ws.id ? el("span", { class: "chip ok" }, "current")
        : el("button", { class: "btn sm", onclick: async () => { await api("/api/auth/switch", { method: "POST", body: { workspace_id: w.workspace_id } }); await refreshMe(); show(); } }, "Switch")))));
  },

  async billing() {
    const [cat, c] = await Promise.all([api("/api/billing/plans"), api("/api/account/credits")]);
    const cur = ME.workspace.plan;
    $("#bill-sub").textContent = cat.provider ? `Current plan: ${ME.workspace.plan_info.name}. Balance: ${fmt.int(c.balance)} credits.`
      : `Current plan: ${ME.workspace.plan_info.name}. Balance: ${fmt.int(c.balance)} credits. Online payment is not enabled on this server: ask your administrator to change plans or add credits.`;
    const buy = async (item) => {
      try { const r = await api("/api/billing/checkout", { method: "POST", body: { item } }); location.href = r.url; } catch (e) { toast(e.message, "bad"); }
    };
    $("#bill-plans").replaceChildren(...cat.plans.map((p) => el("div", { class: `card price${p.id === cur ? " featured" : ""}` },
      el("h3", {}, p.name), el("div", { class: "amount" }, p.price_usd == null ? "Custom" : fmt.usd(p.price_usd), p.price_usd ? el("small", {}, " / month") : ""),
      el("ul", {}, p.highlights.map((h) => el("li", {}, h))),
      p.id === cur ? el("span", { class: "chip ok" }, "Current plan")
        : p.id === "enterprise" ? el("a", { class: "btn block", href: "mailto:sales@duosim.io" }, "Contact sales")
          : p.id === "free" ? el("span", { class: "faint" }, "Downgrade in the billing portal")
            : el("button", { class: "btn block primary", disabled: !p.purchasable || !isAdmin(), onclick: () => buy(p.id) }, `Upgrade to ${p.name}`))));
    $("#bill-packs").replaceChildren(...cat.packs.map((p) => el("button", { class: "btn", disabled: !p.purchasable || !isAdmin(), onclick: () => buy(p.id) },
      `${fmt.int(p.credits)} credits · ${fmt.usd(p.price_usd)}`)));
    $("#portal").disabled = !cat.provider || !isAdmin();
    $("#portal").onclick = async () => { try { location.href = (await api("/api/billing/portal", { method: "POST" })).url; } catch (e) { toast(e.message, "bad"); } };
    $("#ledger").replaceChildren(...c.ledger.map((r) => el("tr", {},
      el("td", {}, fmt.dt(r.created)), el("td", {}, `${r.kind} · ${r.op}`), el("td", { class: "muted" }, r.note || ""),
      el("td", { class: "n", style: `color:var(${r.delta < 0 ? "--muted" : "--ok"})` }, (r.delta > 0 ? "+" : "") + fmt.int(r.delta)),
      el("td", { class: "n" }, fmt.int(r.balance)))));
  },

  async team() {
    $("#ws-name").value = ME.workspace.display_name;
    $("#ws-name").disabled = !isAdmin();
    const t = await api("/api/account/members");
    $("#members").replaceChildren(...t.members.map((m) => {
      const role = el("select", { style: "width:120px", disabled: !isOwner() || m.id === ME.user.id },
        ...["owner", "admin", "member"].map((r) => el("option", { value: r, selected: r === m.role }, r)));
      role.onchange = async () => { try { await api(`/api/account/members/${m.id}`, { method: "PATCH", body: { role: role.value } }); toast("Role updated.", "ok"); } catch (e) { toast(e.message, "bad"); LOADERS.team(); } };
      const rm = (isAdmin() || m.id === ME.user.id) ? el("button", { class: "btn sm danger", onclick: async () => {
        if (!confirm(m.id === ME.user.id ? "Leave this workspace?" : `Remove ${m.email}?`)) return;
        try { await api(`/api/account/members/${m.id}`, { method: "DELETE" }); if (m.id === ME.user.id) { await refreshMe(); } LOADERS.team(); } catch (e) { toast(e.message, "bad"); }
      } }, m.id === ME.user.id ? "Leave" : "Remove") : "";
      return el("tr", {}, el("td", {}, el("div", {}, m.name || m.email), el("div", { class: "faint" }, m.email)), el("td", {}, role), el("td", {}, fmt.date(m.created)), el("td", { class: "n" }, rm));
    }));
    $("#invite").style.display = isAdmin() ? "" : "none";
    $("#invites").replaceChildren(...(t.invites.length ? [el("div", { class: "faint" }, "Pending invitations: " + t.invites.map((i) => `${i.email} (${i.role})`).join(", "))] : []));
  },

  async keys() {
    if (!isAdmin()) { $("#keys").replaceChildren(el("tr", {}, el("td", { class: "muted" }, "Only workspace admins manage API keys."))); return; }
    const r = await api("/api/account/keys");
    $("#keys").replaceChildren(...r.keys.map((k) => el("tr", {}, el("td", {}, k.label || el("span", { class: "faint" }, "(no label)")), el("td", {}, fmt.date(k.created)),
      el("td", {}, k.revoked ? el("span", { class: "chip bad" }, "revoked") : el("span", { class: "chip ok" }, "active")),
      el("td", { class: "n" }, k.revoked ? "" : el("button", { class: "btn sm danger", onclick: async () => { if (confirm("Revoke this key? Integrations using it stop working.")) { await api(`/api/account/keys/${k.id}`, { method: "DELETE" }); LOADERS.keys(); } } }, "Revoke")))));
    $("#curl").textContent = `curl -X POST ${location.origin}/api/design/generate \\\n  -H "X-API-Key: ds_…" -H "Content-Type: application/json" \\\n  -d '{"prompt": "a tank farm with three tanks and a pump skid", "mode": "standard"}'`;
  },

  async providers() {
    const r = await api("/api/account/providers");
    $("#providers").replaceChildren(...(r.providers.length ? r.providers.map((p) => el("tr", {}, el("td", {}, el("b", {}, p.name)), el("td", {}, p.type), el("td", {}, p.model || "default"),
      el("td", { class: "faint" }, p.key_hint), el("td", {}, p.vision ? "yes" : "no"),
      el("td", { class: "n" }, isAdmin() ? el("button", { class: "btn sm danger", onclick: async () => { if (confirm(`Delete ${p.name}?`)) { await api(`/api/account/providers/${p.id}`, { method: "DELETE" }); LOADERS.providers(); } } }, "Delete") : "")))
      : [el("tr", {}, el("td", { class: "muted", colSpan: 6 }, "No providers yet: the platform's models are used."))]));
    const t = $("#pv-type"), base = $("#pv-base-f");
    const sync = () => { base.style.display = t.value === "openai_compatible" ? "" : "none"; };
    t.onchange = sync; sync();
  },

  async webhooks() {
    if (!isAdmin()) return;
    const r = await api("/api/account/webhooks");
    const sel = $("#hook-events");
    if (!sel.options.length) sel.replaceChildren(...r.events.filter((e) => e !== "ping").map((e) => el("option", { value: e, selected: e === "design.completed" }, e)));
    $("#hooks").replaceChildren(...r.webhooks.map((h) => el("tr", {}, el("td", { style: "word-break:break-all" }, h.url), el("td", {}, h.events.join(", ")),
      el("td", { class: "faint" }, h.last_status ? `${h.last_status} · ${fmt.dt(h.last_delivery)}` : "never"),
      el("td", { class: "n" }, el("div", { class: "row", style: "justify-content:flex-end" },
        el("button", { class: "btn sm", onclick: async () => { await api(`/api/account/webhooks/${h.id}/test`, { method: "POST" }); toast("Test event queued.", "ok"); setTimeout(LOADERS.webhooks, 2500); } }, "Send test"),
        el("button", { class: "btn sm danger", onclick: async () => { if (confirm("Delete this endpoint?")) { await api(`/api/account/webhooks/${h.id}`, { method: "DELETE" }); LOADERS.webhooks(); } } }, "Delete"))))));
  },

  async profile() {
    $("#pf-name").value = ME.user.name || "";
    $("#pf-email").textContent = ME.user.email;
    $("#pf-verified").replaceChildren(ME.user.email_verified ? el("span", { class: "chip ok" }, "verified") : el("span", { class: "chip warn" }, "not verified"));
  },

  async audit() {
    if (!isAdmin()) { $("#audit").replaceChildren(el("tr", {}, el("td", { class: "muted" }, "Only workspace admins see the audit log."))); return; }
    const r = await api("/api/account/audit?limit=200");
    $("#audit").replaceChildren(...r.events.map((a) => el("tr", {}, el("td", {}, fmt.dt(a.ts)), el("td", {}, a.email || "-"), el("td", {}, a.action),
      el("td", { class: "faint", style: "font-size:12px" }, Object.entries(a.detail).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ")))));
  },
};

// ------------------------------------------------------------------ forms
function on(id, fn) {
  $(id).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button");
    if (btn) btn.disabled = true;
    try { await fn(ev); } catch (e) { toast(e.message, "bad"); } finally { if (btn) btn.disabled = false; }
  });
}
on("#new-ws", async () => {
  const name = $("#new-ws-name").value.trim();
  if (!name) return;
  await api("/api/account/workspaces", { method: "POST", body: { display_name: name } });
  $("#new-ws-name").value = ""; await refreshMe(); toast("Workspace created and selected.", "ok"); show();
});
on("#rename", async () => { await api("/api/account/workspace", { method: "PATCH", body: { display_name: $("#ws-name").value } }); await refreshMe(); toast("Saved.", "ok"); });
on("#invite", async () => {
  await api("/api/account/invites", { method: "POST", body: { email: $("#inv-email").value, role: $("#inv-role").value } });
  $("#inv-email").value = ""; toast("Invitation sent.", "ok"); LOADERS.team();
});
on("#new-key", async () => {
  const r = await api("/api/account/keys", { method: "POST", body: { label: $("#key-label").value } });
  $("#key-label").value = "";
  $("#key-new").replaceChildren(el("div", { class: "notice ok" }, el("div", {}, "Your new key (copy it now, it will not be shown again):"), el("div", { class: "secret", style: "margin-top:8px" }, r.key)));
  LOADERS.keys();
});
on("#new-prov", async () => {
  await api("/api/account/providers", { method: "POST", body: { name: $("#pv-name").value.trim(), type: $("#pv-type").value, api_key: $("#pv-key").value,
    model: $("#pv-model").value.trim(), base_url: $("#pv-base").value.trim(), vision: $("#pv-vision").checked } });
  for (const f of ["#pv-name", "#pv-key", "#pv-model", "#pv-base"]) $(f).value = "";
  toast("Provider added. It is now selectable in the Studio.", "ok"); LOADERS.providers();
});
on("#new-hook", async () => {
  const events = [...$("#hook-events").selectedOptions].map((o) => o.value);
  const r = await api("/api/account/webhooks", { method: "POST", body: { url: $("#hook-url").value.trim(), events } });
  $("#hook-url").value = "";
  $("#hook-new").replaceChildren(el("div", { class: "notice ok" }, el("div", {}, "Signing secret (shown once):"), el("div", { class: "secret", style: "margin-top:8px" }, r.secret)));
  LOADERS.webhooks();
});
on("#profile", async () => { await api("/api/auth/me", { method: "PATCH", body: { name: $("#pf-name").value } }); await refreshMe(); toast("Saved.", "ok"); });
on("#pw", async () => {
  const r = await api("/api/auth/password", { method: "POST", body: { current: $("#pw-cur").value, new: $("#pw-new").value } });
  $("#pw-cur").value = $("#pw-new").value = "";
  toast(`Password changed${r.signed_out_elsewhere ? `, ${r.signed_out_elsewhere} other session(s) signed out` : ""}.`, "ok");
});
on("#del", async () => {
  if (!confirm("This permanently erases your account. Continue?")) return;
  await api("/api/account/delete", { method: "POST", body: { password: $("#del-pw").value, confirm: $("#del-confirm").value } });
  location.href = "/";
});

// ------------------------------------------------------------------ routing
function show() {
  const s = (location.hash || "#overview").slice(1);
  const name = s in LOADERS ? s : "overview";
  $$(".side a").forEach((a) => a.classList.toggle("on", a.dataset.s === name));
  $$(".main section").forEach((x) => x.classList.toggle("on", x.id === `s-${name}`));
  LOADERS[name]().catch((e) => toast(e.message, "bad"));
}
addEventListener("hashchange", show);

(async () => {
  if (!(await refreshMe())) return;
  banner();
  show();
})();
