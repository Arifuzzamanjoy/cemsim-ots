/* Sign in / sign up / forgot / reset / verify: one page, the mode comes from the path. */
import { $, api, el } from "/static/saas.js";

const mode = location.pathname.replace(/^\//, "") || "login";
const q = new URLSearchParams(location.search);
const next = (() => { const n = q.get("next") || "/app"; return n.startsWith("/") && !n.startsWith("//") ? n : "/app"; })();
const show = (id, on) => { $(id).style.display = on ? "" : "none"; };
const notice = (msg, kind = "") => $("#notice").replaceChildren(msg ? el("div", { class: `notice ${kind}` }, msg) : "");

const MODES = {
  login: { title: "Sign in", sub: "Welcome back.", btn: "Sign in", fields: ["email", "password"],
    alt: () => [el("a", { href: "/forgot" }, "Forgot your password?"), " · New here? ", el("a", { href: "/signup" + location.search }, "Create an account")] },
  signup: { title: "Create your workspace", sub: "50 free credits every month. No card required.", btn: "Create account", fields: ["name", "email", "password", "terms"],
    alt: () => ["Already have an account? ", el("a", { href: "/login" + location.search }, "Sign in")] },
  forgot: { title: "Reset your password", sub: "We will email you a link.", btn: "Send reset link", fields: ["email"],
    alt: () => [el("a", { href: "/login" }, "Back to sign in")] },
  reset: { title: "Choose a new password", sub: "All your other sessions will be signed out.", btn: "Set password", fields: ["password"],
    alt: () => [el("a", { href: "/login" }, "Back to sign in")] },
  verify: { title: "Confirming your email", sub: "One moment…", btn: "Continue", fields: [], alt: () => [el("a", { href: "/app" }, "Go to the Studio")] },
};
const M = MODES[mode] || MODES.login;
document.title = `DuoSim — ${M.title}`;
$("#title").textContent = M.title;
$("#subtitle").textContent = M.sub;
$("#submit").textContent = M.btn;
for (const f of ["name", "email", "password", "terms"]) show(`#f-${f}`, M.fields.includes(f));
show("#pw-hint", mode !== "login");
$("#password").autocomplete = mode === "login" ? "current-password" : "new-password";
$("#alt").replaceChildren(...M.alt());
if (q.get("email")) $("#email").value = q.get("email");
if (q.get("invite")) notice("You were invited to a workspace. Create your account (or sign in) with the invited address to join.", "ok");

async function submit(ev) {
  ev?.preventDefault();
  const btn = $("#submit");
  btn.disabled = true;
  notice("");
  const email = $("#email").value.trim(), password = $("#password").value;
  try {
    if (mode === "login") {
      await api("/api/auth/login", { method: "POST", body: { email, password } });
      if (q.get("invite")) await api("/api/auth/invites/accept", { method: "POST", body: { token: q.get("invite") } }).catch(() => null);
      location.href = next;
    } else if (mode === "signup") {
      if (!$("#terms").checked) throw new Error("Please accept the terms and the privacy policy.");
      await api("/api/auth/signup", { method: "POST", body: { email, password, name: $("#name").value.trim(), invite: q.get("invite") || null, accept_terms: true } });
      const plan = q.get("plan");
      location.href = plan && plan !== "free" ? "/account?welcome=1#billing" : "/app?welcome=1";
    } else if (mode === "forgot") {
      const r = await api("/api/auth/forgot", { method: "POST", body: { email } });
      notice(r.msg, "ok");
    } else if (mode === "reset") {
      await api("/api/auth/reset", { method: "POST", body: { token: q.get("token") || "", password } });
      notice("Password changed. You can sign in now.", "ok");
      setTimeout(() => { location.href = "/login"; }, 1200);
    } else if (mode === "verify") {
      location.href = "/app";
    }
  } catch (e) {
    notice(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}
$("#form").addEventListener("submit", submit);

if (mode === "verify") {
  api("/api/auth/verify", { method: "POST", body: { token: q.get("token") || "" } })
    .then(() => { $("#title").textContent = "Email confirmed"; $("#subtitle").textContent = "Thank you. Your account is fully active."; })
    .catch((e) => { $("#title").textContent = "Link not valid"; notice(e.message, "bad"); $("#subtitle").textContent = "Request a new link from your account page."; });
}
if (mode === "reset" && !q.get("token")) notice("This page needs the link from the reset email.", "bad");
