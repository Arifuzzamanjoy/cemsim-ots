/* Shared helpers of the DuoSim web pages (ES module).  Everything from the server is rendered as text. */
export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];

export function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k in e && k !== "style") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

export class ApiError extends Error {
  constructor(msg, status, body) { super(msg); this.status = status; this.body = body; }
}

/* Cookie-authenticated JSON API.  Writes carry the CSRF header the server requires. */
export async function api(path, { method = "GET", body, headers = {}, raw = false } = {}) {
  const h = { ...headers };
  if (method !== "GET" && method !== "HEAD") h["X-DuoSim-CSRF"] = "1";
  if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) h["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method, headers: h, credentials: "same-origin",
    body: body === undefined ? undefined : (body instanceof Blob || body instanceof ArrayBuffer ? body : JSON.stringify(body)),
  });
  if (raw) return r;
  let data = null;
  const text = await r.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const d = data && typeof data === "object" ? data.detail : null;
    throw new ApiError(typeof d === "string" ? d : (d ? JSON.stringify(d) : r.statusText || `HTTP ${r.status}`), r.status, data);
  }
  return data;
}

export async function me() {
  try { return await api("/api/auth/me"); } catch (e) { if (e.status === 401) return null; throw e; }
}

export function toast(msg, kind = "") {
  let box = $("#toast");
  if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
  const m = el("div", { class: `msg ${kind}` }, msg);
  box.append(m);
  setTimeout(() => m.remove(), kind === "bad" ? 7000 : 4000);
}

export const fmt = {
  int: (n) => (n == null ? "-" : Number(n).toLocaleString()),
  usd: (n) => (n == null ? "Custom" : `$${Number(n).toLocaleString()}`),
  date: (t) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "-"),
  dt: (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "-"),
};

export async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already signed out */ }
  location.href = "/";
}

/* The top navigation of the logged-in pages. */
export function appNav(m, active = "") {
  const nav = $("#nav");
  if (!nav) return;
  const ws = m?.workspace;
  const links = el("div", { class: "links" },
    el("a", { href: "/app", class: active === "app" ? "on" : "" }, "Studio"),
    el("a", { href: "/hmi" }, "Kiln simulator"),
    el("a", { href: "/3d" }, "3-D plant"),
    el("a", { href: "/account" }, "Account"),
    m?.user?.is_admin ? el("a", { href: "/admin" }, "Admin") : null,
    el("a", { href: "/docs", target: "_blank", rel: "noopener" }, "API"));
  const right = el("div", { class: "actions" });
  if (m) {
    if (ws && ws.credits != null) right.append(el("a", { class: "chip brand", href: "/account#billing", title: "credits left" }, `${fmt.int(ws.credits)} credits`));
    right.append(el("span", { class: "chip hide-sm", title: m.user.email }, ws?.display_name || m.user.email));
    right.append(el("button", { class: "btn sm ghost", onclick: logout }, "Sign out"));
  } else {
    right.append(el("a", { class: "btn sm ghost", href: "/login" }, "Sign in"), el("a", { class: "btn sm primary", href: "/signup" }, "Start free"));
  }
  const menu = el("button", { class: "btn sm ghost menu-btn", "aria-label": "menu", onclick: () => links.classList.toggle("open") }, "☰");
  nav.replaceChildren(el("div", { class: "wrap" }, el("a", { class: "brand", href: "/" }, el("span", { class: "logo" }), "Duo", el("b", {}, "Sim")), links, right, menu));
}

export function confirmText(question) { return window.confirm(question); }
