"""Accounts, organisations, sessions, credits, audit, provider keys and webhooks on the tenant database.

A *workspace* (organisation) owns API keys, designs, credits and a plan; *users* belong to workspaces through
*memberships* with a role (owner > admin > member).  Browser sign-in creates a server-side *session*
(random token in an HttpOnly cookie, only its SHA-256 stored).  Every billable operation is a row in the
append-only *credit ledger*; the balance on the workspace row is updated in the same transaction with a
conditional UPDATE, so concurrent requests can never spend the same credit twice.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from typing import Any

from ..platform.store import Store, Workspace
from . import plans
from .mailer import Mailer
from .schema import migrate
from .security import (
    InputError,
    check_password,
    clean_name,
    hash_password,
    needs_rehash,
    new_token,
    normalize_email,
    token_hash,
    verify_password,
)
from .vault import Vault

log = logging.getLogger("duosim.saas")

SESSION_TTL_S = 30 * 86400
VERIFY_TTL_S = 3 * 86400
RESET_TTL_S = 3600
INVITE_TTL_S = 14 * 86400
ROLES = ("owner", "admin", "member")
ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}
PROVIDER_TYPES = ("openai", "openai_compatible", "gemini", "claude")
WEBHOOK_EVENTS = ("design.completed", "design.failed", "import.completed", "credits.low", "ping")
_KEEP = object()  # "leave unchanged" marker for optional updates
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


class InsufficientCredits(RuntimeError):
    def __init__(self, balance: int, needed: int):
        super().__init__(f"not enough credits: this needs {needed}, the workspace has {balance}")
        self.balance, self.needed = balance, needed


class Forbidden(PermissionError):
    pass


def credits_enabled() -> bool:
    return os.environ.get("DUOSIM_CREDITS", "1") != "0"


def _period(t: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(t))


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("DUOSIM_ADMIN_EMAILS", "").split(",") if e.strip()}


class SaaS:
    def __init__(self, store: Store, mailer: Mailer | None = None):
        self.store = store
        self.mailer = mailer or Mailer()
        self._vault: Vault | None = None
        with store._conn() as c:
            migrate(c)

    def _conn(self):
        return self.store._conn()

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = Vault.from_env(self.store.root)
        return self._vault

    # ================================================================== users
    def create_user(self, email: str, password: str, name: str = "", invite: str | None = None) -> dict[str, Any]:
        email = normalize_email(email)
        check_password(password, email)
        name = clean_name(name)
        uid = uuid.uuid4().hex[:16]
        pw = hash_password(password)
        inv = self._token_row("invite", invite) if invite else None
        if invite and (inv is None or inv["email"] != email):
            raise InputError("that invitation is not valid for this email address (it may have expired)")
        with self._conn() as c:
            if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise InputError("an account with this email already exists - sign in instead")
            c.execute(
                "INSERT INTO users(id, email, name, password_hash, is_admin, created) VALUES (?,?,?,?,?,?)",
                (uid, email, name, pw, int(email in _admin_emails()), time.time()),
            )
        ws = self.create_workspace(uid, f"{name or email.split('@')[0]}'s workspace")
        current = ws.id
        if inv is not None:
            self._consume(inv["token_hash"])
            self.add_member(inv["workspace_id"], uid, inv["role"] or "member")
            with self._conn() as c:  # the address received the invitation: it is verified
                c.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
            current = inv["workspace_id"]
        self.audit("user.signup", workspace_id=ws.id, user_id=uid)
        return {**self.user(uid), "workspace_id": current}

    def user(self, uid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT id, email, name, email_verified, is_admin, suspended, created, last_login FROM users WHERE id=?",
                (uid,),
            ).fetchone()
        if r is None:
            return None
        d = dict(r)
        for k in ("email_verified", "is_admin", "suspended"):
            d[k] = bool(d[k])
        return d

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            email = normalize_email(email)
        except InputError:
            return None
        with self._conn() as c:
            r = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        return self.user(r["id"]) if r else None

    def login(self, email: str, password: str) -> dict[str, Any] | None:
        try:
            email = normalize_email(email)
        except InputError:
            verify_password(password or "", None)
            return None
        with self._conn() as c:
            r = c.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
        if not verify_password(password or "", r["password_hash"] if r else None):
            return None
        assert r is not None
        with self._conn() as c:
            c.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), r["id"]))
            if needs_rehash(r["password_hash"]):
                c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), r["id"]))
            if email in _admin_emails():
                c.execute("UPDATE users SET is_admin=1 WHERE id=?", (r["id"],))
        return self.user(r["id"])

    def set_password(self, uid: str, current: str, new: str) -> None:
        with self._conn() as c:
            r = c.execute("SELECT email, password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if r is None or not verify_password(current or "", r["password_hash"]):
            raise InputError("the current password is not correct")
        check_password(new, r["email"])
        with self._conn() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), uid))

    def update_profile(self, uid: str, name: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET name=? WHERE id=?", (clean_name(name), uid))

    # ================================================================== sessions
    def create_session(self, uid: str, workspace_id: str | None, ip: str = "", ua: str = "") -> str:
        tok = new_token()
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            c.execute(
                "INSERT INTO sessions(token_hash, user_id, workspace_id, created, expires, ip, user_agent) "
                "VALUES (?,?,?,?,?,?,?)",
                (token_hash(tok), uid, workspace_id, now, now + SESSION_TTL_S, ip[:45], ua[:200]),
            )
        return tok

    def session(self, tok: str | None) -> dict[str, Any] | None:
        """The live session for a cookie value: user + current workspace (falls back to any membership)."""
        if not tok or len(tok) > 100:
            return None
        with self._conn() as c:
            r = c.execute(
                "SELECT s.user_id, s.workspace_id, s.expires, u.suspended FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash=?",
                (token_hash(tok),),
            ).fetchone()
        if r is None or r["expires"] < time.time() or r["suspended"]:
            return None
        ws_id = r["workspace_id"]
        if ws_id is None or self.role(r["user_id"], ws_id) is None:
            ms = self.memberships(r["user_id"])
            ws_id = ms[0]["workspace_id"] if ms else None
        return {"user_id": r["user_id"], "workspace_id": ws_id, "token_hash": token_hash(tok)}

    def end_session(self, tok: str | None) -> None:
        if tok:
            with self._conn() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))

    def end_other_sessions(self, uid: str, keep_hash: str | None = None) -> int:
        with self._conn() as c:
            return c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash IS NOT ?", (uid, keep_hash)).rowcount

    def switch_workspace(self, keep_hash: str, uid: str, workspace_id: str) -> None:
        if self.role(uid, workspace_id) is None:
            raise Forbidden("you are not a member of that workspace")
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET workspace_id=? WHERE token_hash=? AND user_id=?", (workspace_id, keep_hash, uid)
            )

    # ================================================================== workspaces & members
    def create_workspace(self, owner_uid: str, display_name: str) -> Workspace:
        slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")[:24] or "workspace"
        ws = self.store.workspace(f"{slug}-{secrets.token_hex(3)}")
        assert ws is not None
        with self._conn() as c:
            c.execute("UPDATE workspaces SET display_name=? WHERE id=?", (clean_name(display_name, 60), ws.id))
        self.add_member(ws.id, owner_uid, "owner")
        return ws

    def workspace_info(self, ws_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT id, name, display_name, plan, credits, credits_period, status, stripe_customer_id, "
                "stripe_subscription_id, created FROM workspaces WHERE id=?",
                (ws_id,),
            ).fetchone()
            if r is None:
                return None
            seats = c.execute("SELECT COUNT(*) FROM memberships WHERE workspace_id=?", (ws_id,)).fetchone()[0]
        d = dict(r)
        p = plans.plan(d["plan"])
        d["display_name"] = d["display_name"] or d["name"]
        d["plan_info"] = p.to_dict()
        d["seats_used"] = seats
        d["billing_linked"] = bool(d.pop("stripe_customer_id"))
        d["subscribed"] = bool(d.pop("stripe_subscription_id"))
        return d

    def workspace_obj(self, ws_id: str) -> Workspace | None:
        with self._conn() as c:
            r = c.execute("SELECT id, name FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return Workspace(r["id"], r["name"]) if r else None

    def rename_workspace(self, ws_id: str, display_name: str) -> None:
        dn = clean_name(display_name, 60)
        if not dn:
            raise InputError("workspace name must not be empty")
        with self._conn() as c:
            c.execute("UPDATE workspaces SET display_name=? WHERE id=?", (dn, ws_id))

    def memberships(self, uid: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT m.workspace_id, m.role, w.name, COALESCE(w.display_name, w.name) AS display_name, w.plan "
                "FROM memberships m JOIN workspaces w ON w.id = m.workspace_id WHERE m.user_id=? ORDER BY m.created",
                (uid,),
            ).fetchall()
        return [dict(r) for r in rows]

    def role(self, uid: str, ws_id: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT role FROM memberships WHERE user_id=? AND workspace_id=?", (uid, ws_id)).fetchone()
        return r["role"] if r else None

    def require_role(self, uid: str, ws_id: str, at_least: str) -> str:
        r = self.role(uid, ws_id)
        if r is None or ROLE_RANK[r] < ROLE_RANK[at_least]:
            raise Forbidden(f"this needs the {at_least} role in the workspace")
        return r

    def members(self, ws_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT u.id, u.email, u.name, m.role, m.created FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.workspace_id=? ORDER BY m.created",
                (ws_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_member(self, ws_id: str, uid: str, role: str) -> None:
        if role not in ROLES:
            raise InputError("role must be owner, admin or member")
        with self._conn() as c:
            c.execute(
                "INSERT INTO memberships(user_id, workspace_id, role, created) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id, workspace_id) DO UPDATE SET role=excluded.role",
                (uid, ws_id, role, time.time()),
            )

    def _owners(self, ws_id: str) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM memberships WHERE workspace_id=? AND role='owner'", (ws_id,)
            ).fetchone()[0]

    def set_role(self, ws_id: str, uid: str, role: str) -> None:
        cur = self.role(uid, ws_id)
        if cur is None:
            raise InputError("no such member")
        if cur == "owner" and role != "owner" and self._owners(ws_id) <= 1:
            raise InputError("a workspace needs at least one owner")
        self.add_member(ws_id, uid, role)

    def remove_member(self, ws_id: str, uid: str) -> None:
        cur = self.role(uid, ws_id)
        if cur is None:
            raise InputError("no such member")
        if cur == "owner" and self._owners(ws_id) <= 1:
            raise InputError("the last owner cannot be removed")
        with self._conn() as c:
            c.execute("DELETE FROM memberships WHERE workspace_id=? AND user_id=?", (ws_id, uid))
            c.execute("UPDATE sessions SET workspace_id=NULL WHERE user_id=? AND workspace_id=?", (uid, ws_id))

    # ================================================================== one-time tokens
    def _issue(self, kind: str, ttl: float, **cols: Any) -> str:
        tok = new_token()
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM tokens WHERE expires < ?", (now - 86400,))
            c.execute(
                "INSERT INTO tokens(token_hash, kind, user_id, email, workspace_id, role, created, expires) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    token_hash(tok),
                    kind,
                    cols.get("user_id"),
                    cols.get("email"),
                    cols.get("workspace_id"),
                    cols.get("role"),
                    now,
                    now + ttl,
                ),
            )
        return tok

    def _token_row(self, kind: str, tok: str | None) -> dict[str, Any] | None:
        if not tok or len(tok) > 100:
            return None
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM tokens WHERE token_hash=? AND kind=? AND used=0 AND expires>?",
                (token_hash(tok), kind, time.time()),
            ).fetchone()
        return dict(r) if r else None

    def _consume(self, th: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE tokens SET used=1 WHERE token_hash=? AND used=0", (th,)).rowcount == 1

    def send_verification(self, uid: str, base_url: str) -> None:
        u = self.user(uid)
        if u is None or u["email_verified"]:
            return
        tok = self._issue("verify", VERIFY_TTL_S, user_id=uid, email=u["email"])
        self.mailer.send(
            u["email"],
            "Confirm your DuoSim email address",
            f"Welcome to DuoSim.\n\nConfirm your email address:\n{base_url}/verify?token={tok}\n\n"
            "The link is valid for 3 days. If you did not sign up, ignore this message.",
        )

    def verify_email(self, tok: str) -> bool:
        r = self._token_row("verify", tok)
        if r is None or not self._consume(r["token_hash"]):
            return False
        with self._conn() as c:
            c.execute("UPDATE users SET email_verified=1 WHERE id=? AND email=?", (r["user_id"], r["email"]))
        self.audit("user.email_verified", user_id=r["user_id"])
        return True

    def start_password_reset(self, email: str, base_url: str) -> None:
        """Always silent about whether the address has an account."""
        u = self.user_by_email(email)
        if u is None or u["suspended"]:
            return
        tok = self._issue("reset", RESET_TTL_S, user_id=u["id"], email=u["email"])
        self.mailer.send(
            u["email"],
            "Reset your DuoSim password",
            f"Someone asked to reset the password of this DuoSim account.\n\nChoose a new password:\n"
            f"{base_url}/reset?token={tok}\n\nThe link is valid for one hour. If it was not you, ignore this message.",
        )

    def reset_password(self, tok: str, new: str) -> str:
        r = self._token_row("reset", tok)
        if r is None:
            raise InputError("this reset link is invalid or has expired")
        check_password(new, r["email"] or "")
        if not self._consume(r["token_hash"]):
            raise InputError("this reset link has already been used")
        with self._conn() as c:
            c.execute(
                "UPDATE users SET password_hash=?, email_verified=1 WHERE id=?", (hash_password(new), r["user_id"])
            )
        self.end_other_sessions(r["user_id"])  # a reset signs out every device
        self.audit("user.password_reset", user_id=r["user_id"])
        return r["user_id"]

    def invite(self, ws_id: str, email: str, role: str, inviter_uid: str, base_url: str) -> dict[str, Any]:
        email = normalize_email(email)
        if role not in ("admin", "member"):
            raise InputError("invite as admin or member")
        info = self.workspace_info(ws_id)
        assert info is not None
        if info["seats_used"] >= info["plan_info"]["seats"]:
            raise InputError(f"all {info['plan_info']['seats']} seats of the {info['plan_info']['name']} plan are used")
        existing = self.user_by_email(email)
        if existing and self.role(existing["id"], ws_id):
            raise InputError("that person is already a member")
        tok = self._issue("invite", INVITE_TTL_S, email=email, workspace_id=ws_id, role=role, user_id=inviter_uid)
        inviter = self.user(inviter_uid) or {"email": "a colleague"}
        self.mailer.send(
            email,
            f"You are invited to {info['display_name']} on DuoSim",
            f'{inviter["email"]} invited you to the workspace "{info["display_name"]}" on DuoSim.\n\n'
            f"Accept the invitation:\n{base_url}/signup?invite={tok}&email={email}\n\nThe link is valid for 14 days.",
        )
        self.audit("member.invited", workspace_id=ws_id, user_id=inviter_uid, email=email, role=role)
        return {"email": email, "role": role, "token": tok}

    def accept_invite(self, uid: str, tok: str) -> str:
        r = self._token_row("invite", tok)
        u = self.user(uid)
        if r is None or u is None or r["email"] != u["email"]:
            raise InputError("this invitation is invalid, expired, or for another email address")
        if not self._consume(r["token_hash"]):
            raise InputError("this invitation has already been used")
        self.add_member(r["workspace_id"], uid, r["role"] or "member")
        self.audit("member.joined", workspace_id=r["workspace_id"], user_id=uid)
        return r["workspace_id"]

    def pending_invites(self, ws_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT email, role, created, expires FROM tokens WHERE kind='invite' AND workspace_id=? AND used=0 "
                "AND expires>? ORDER BY created DESC",
                (ws_id, time.time()),
            ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== plans & credits
    def _refresh(self, c: sqlite3.Connection, ws_id: str) -> None:
        """Lazy monthly allowance for workspaces without a paid Stripe subscription (those are credited on
        `invoice.paid`).  Allowance credits top the balance up to the plan amount; they do not accumulate."""
        r = c.execute(
            "SELECT plan, credits, credits_period, stripe_subscription_id FROM workspaces WHERE id=?", (ws_id,)
        ).fetchone()
        if r is None or r["credits_period"] == _period() or r["stripe_subscription_id"]:
            return
        allowance = plans.plan(r["plan"]).monthly_credits
        new = max(r["credits"], allowance)
        c.execute("UPDATE workspaces SET credits=?, credits_period=? WHERE id=?", (new, _period(), ws_id))
        if new > r["credits"]:
            self._ledger(c, ws_id, new - r["credits"], new, "grant", "allowance", note=f"{_period()} monthly allowance")

    @staticmethod
    def _ledger(c, ws_id, delta, balance, kind, op, ref=None, user_id=None, note="") -> int:
        cur = c.execute(
            "INSERT INTO credit_ledger(workspace_id, delta, balance, kind, op, ref, user_id, note, created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ws_id, delta, balance, kind, op, ref, user_id, note[:200], time.time()),
        )
        return int(cur.lastrowid or 0)

    def balance(self, ws_id: str) -> int:
        with self._conn() as c:
            self._refresh(c, ws_id)
            r = c.execute("SELECT credits FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return int(r["credits"]) if r else 0

    def charge(self, ws_id: str, amount: int, op: str, user_id: str | None = None, ref: str | None = None) -> int:
        """Debit `amount` credits atomically.  Returns the ledger id (pass it to `refund`)."""
        if amount <= 0:
            return 0
        with self._conn() as c:
            self._refresh(c, ws_id)
            st = c.execute("SELECT status FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if st is not None and st["status"] != "active":
                raise Forbidden("this workspace is suspended - contact support")
            ok = c.execute(
                "UPDATE workspaces SET credits = credits - ? WHERE id=? AND credits >= ?", (amount, ws_id, amount)
            ).rowcount
            bal = c.execute("SELECT credits FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            balance = int(bal["credits"]) if bal else 0
            if not ok:
                raise InsufficientCredits(balance, amount)
            return self._ledger(c, ws_id, -amount, balance, "charge", op, ref, user_id)

    def refund(self, ws_id: str, ledger_id: int, note: str = "provider failure") -> None:
        if not ledger_id:
            return
        with self._conn() as c:
            r = c.execute(
                "SELECT delta, op FROM credit_ledger WHERE id=? AND workspace_id=? AND kind='charge'",
                (ledger_id, ws_id),
            ).fetchone()
            if (
                r is None
                or c.execute(
                    "SELECT 1 FROM credit_ledger WHERE ref=? AND kind='refund' AND workspace_id=?",
                    (str(ledger_id), ws_id),
                ).fetchone()
            ):
                return  # unknown or already refunded
            amount = -int(r["delta"])
            c.execute("UPDATE workspaces SET credits = credits + ? WHERE id=?", (amount, ws_id))
            bal = c.execute("SELECT credits FROM workspaces WHERE id=?", (ws_id,)).fetchone()["credits"]
            self._ledger(c, ws_id, amount, bal, "refund", r["op"], str(ledger_id), note=note)

    def grant(self, ws_id: str, amount: int, kind: str, op: str, note: str = "", ref: str | None = None) -> int:
        with self._conn() as c:
            self._refresh(c, ws_id)
            c.execute("UPDATE workspaces SET credits = MAX(credits + ?, 0) WHERE id=?", (amount, ws_id))
            bal = c.execute("SELECT credits FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if bal is None:
                raise InputError("no such workspace")
            return self._ledger(c, ws_id, amount, bal["credits"], kind, op, ref, note=note)

    def set_plan(self, ws_id: str, plan_id: str, subscription_id: object = _KEEP, grant: bool = False) -> None:
        if plan_id not in plans.PLANS:
            raise InputError(f"unknown plan {plan_id!r}")
        with self._conn() as c:
            c.execute("UPDATE workspaces SET plan=? WHERE id=?", (plan_id, ws_id))
            if subscription_id is not _KEEP:
                c.execute("UPDATE workspaces SET stripe_subscription_id=? WHERE id=?", (subscription_id, ws_id))
            if grant:  # an admin moved the workspace: credit the new allowance now, not next month
                c.execute("UPDATE workspaces SET credits_period=NULL WHERE id=?", (ws_id,))
                self._refresh(c, ws_id)
        self.audit("workspace.plan", workspace_id=ws_id, plan=plan_id)

    def ledger(self, ws_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, delta, balance, kind, op, note, created FROM credit_ledger WHERE workspace_id=? "
                "ORDER BY id DESC LIMIT ?",
                (ws_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_summary(self, ws_id: str, days: int = 30) -> dict[str, Any]:
        since = time.time() - days * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT op, SUM(-delta) AS credits, COUNT(*) AS n FROM credit_ledger WHERE workspace_id=? "
                "AND kind='charge' AND created>=? GROUP BY op ORDER BY credits DESC",
                (ws_id, since),
            ).fetchall()
            refunds = c.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM credit_ledger WHERE workspace_id=? AND kind='refund' AND created>=?",
                (ws_id, since),
            ).fetchone()[0]
        by_op = [dict(r) for r in rows]
        return {"days": days, "by_op": by_op, "spent": sum(r["credits"] for r in by_op) - refunds, "refunded": refunds}

    # ================================================================== billing links
    def link_customer(self, ws_id: str, customer_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE workspaces SET stripe_customer_id=? WHERE id=?", (customer_id, ws_id))

    def customer_id(self, ws_id: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT stripe_customer_id FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return r["stripe_customer_id"] if r else None

    def workspace_by_customer(self, customer_id: str | None) -> str | None:
        if not customer_id:
            return None
        with self._conn() as c:
            r = c.execute("SELECT id FROM workspaces WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        return r["id"] if r else None

    def event_seen(self, event_id: str, event_type: str) -> bool:
        """Record a billing event id; True if it was processed before (webhooks are delivered at least once)."""
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO billing_events(id, type, created) VALUES (?,?,?)", (event_id, event_type, time.time())
                )
            except sqlite3.IntegrityError:
                return True
        return False

    # ================================================================== audit
    def audit(
        self,
        action: str,
        workspace_id: str | None = None,
        user_id: str | None = None,
        ip: str | None = None,
        **detail: Any,
    ) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO audit_log(ts, workspace_id, user_id, action, detail, ip) VALUES (?,?,?,?,?,?)",
                    (time.time(), workspace_id, user_id, action, json.dumps(detail, default=str)[:2000], ip),
                )
        except sqlite3.Error as e:  # auditing must never break the request it records
            log.error("audit write failed: %s", e)

    def audit_rows(self, workspace_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = "SELECT a.id, a.ts, a.workspace_id, a.user_id, u.email, a.action, a.detail, a.ip FROM audit_log a "
        q += "LEFT JOIN users u ON u.id = a.user_id "
        args: tuple = ()
        if workspace_id:
            q += "WHERE a.workspace_id=? "
            args = (workspace_id,)
        q += "ORDER BY a.id DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, (*args, max(1, min(limit, 500)))).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]

    # ================================================================== API keys (workspace-managed)
    def api_keys(self, ws_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, label, created, revoked FROM api_keys WHERE workspace_id=? ORDER BY created DESC", (ws_id,)
            ).fetchall()
        return [{**dict(r), "revoked": bool(r["revoked"])} for r in rows]

    def revoke_api_key(self, ws_id: str, key_id: str) -> bool:
        with self._conn() as c:
            return (
                c.execute("UPDATE api_keys SET revoked=1 WHERE id=? AND workspace_id=?", (key_id, ws_id)).rowcount > 0
            )

    # ================================================================== provider keys (BYOK)
    def add_provider_key(
        self, ws_id: str, name: str, ptype: str, key: str, model: str = "", base_url: str = "", vision: bool = True
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise InputError("name: 1-40 letters, digits, spaces, dots, dashes or underscores")
        if ptype not in PROVIDER_TYPES:
            raise InputError(f"type must be one of {', '.join(PROVIDER_TYPES)}")
        if not isinstance(key, str) or not 8 <= len(key.strip()) <= 400:
            raise InputError("the API key looks wrong (8-400 characters)")
        model = clean_name(model, 120)
        if ptype == "openai_compatible":
            if not base_url:
                raise InputError("an OpenAI-compatible endpoint needs its base URL")
            from .netguard import URLRejected, check_url

            try:
                base_url = check_url(base_url)
            except URLRejected as e:
                raise InputError(str(e)) from None
            if not model:
                raise InputError("an OpenAI-compatible endpoint needs a model name")
        else:
            base_url = ""
        pid = uuid.uuid4().hex[:12]
        enc = self.vault.encrypt(key.strip(), f"{ws_id}:{pid}")
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO provider_keys(id, workspace_id, name, type, model, base_url, key_enc, key_hint, vision, "
                    "created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, ws_id, name, ptype, model, base_url, enc, "…" + key.strip()[-4:], int(vision), time.time()),
                )
            except sqlite3.IntegrityError:
                raise InputError(f"a provider named {name!r} already exists") from None
        return {"id": pid, "name": name, "type": ptype, "model": model, "base_url": base_url}

    def provider_keys(self, ws_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, type, model, base_url, key_hint, vision, created FROM provider_keys "
                "WHERE workspace_id=? ORDER BY created",
                (ws_id,),
            ).fetchall()
        return [{**dict(r), "vision": bool(r["vision"])} for r in rows]

    def provider_secret(self, ws_id: str, name: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM provider_keys WHERE workspace_id=? AND name=?", (ws_id, name)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["key"] = self.vault.decrypt(d.pop("key_enc"), f"{ws_id}:{d['id']}")
        d["vision"] = bool(d["vision"])
        return d

    def delete_provider_key(self, ws_id: str, pid: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM provider_keys WHERE id=? AND workspace_id=?", (pid, ws_id)).rowcount > 0

    # ================================================================== webhooks (registry; delivery in webhooks.py)
    def add_webhook(self, ws_id: str, url: str, events: list[str]) -> dict[str, Any]:
        from .netguard import URLRejected, check_url

        try:
            url = check_url(url)
        except URLRejected as e:
            raise InputError(str(e)) from None
        evs = sorted({e for e in events if e in WEBHOOK_EVENTS})
        if not evs:
            raise InputError(f"choose at least one event: {', '.join(WEBHOOK_EVENTS)}")
        with self._conn() as c:
            if c.execute("SELECT COUNT(*) FROM webhooks WHERE workspace_id=?", (ws_id,)).fetchone()[0] >= 10:
                raise InputError("at most 10 webhooks per workspace")
            wid, secret = uuid.uuid4().hex[:12], "whsec_" + new_token(24)
            c.execute(
                "INSERT INTO webhooks(id, workspace_id, url, secret, events, created) VALUES (?,?,?,?,?,?)",
                (wid, ws_id, url, secret, ",".join(evs), time.time()),
            )
        return {"id": wid, "url": url, "events": evs, "secret": secret}

    def webhooks(self, ws_id: str, event: str | None = None, with_secret: bool = False) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM webhooks WHERE workspace_id=? AND active=1 ORDER BY created", (ws_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["events"] = d["events"].split(",")
            if event and event not in d["events"] and event != "ping":
                continue
            if not with_secret:
                d.pop("secret")
            out.append(d)
        return out

    def webhook_result(self, wid: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE webhooks SET last_status=?, last_delivery=? WHERE id=?", (status[:80], time.time(), wid))

    def delete_webhook(self, ws_id: str, wid: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM webhooks WHERE id=? AND workspace_id=?", (wid, ws_id)).rowcount > 0

    # ================================================================== administration
    def admin_stats(self) -> dict[str, Any]:
        since = time.time() - 30 * 86400
        with self._conn() as c:

            def one(q, *a):
                return c.execute(q, a).fetchone()[0]

            plans_ = {r["plan"]: r["n"] for r in c.execute("SELECT plan, COUNT(*) AS n FROM workspaces GROUP BY plan")}
            return {
                "users": one("SELECT COUNT(*) FROM users"),
                "users_30d": one("SELECT COUNT(*) FROM users WHERE created>=?", since),
                "active_30d": one("SELECT COUNT(*) FROM users WHERE last_login>=?", since),
                "workspaces": one("SELECT COUNT(*) FROM workspaces"),
                "designs": one("SELECT COUNT(*) FROM designs"),
                "designs_30d": one("SELECT COUNT(*) FROM designs WHERE created>=?", since),
                "credits_spent_30d": one(
                    "SELECT COALESCE(SUM(-delta),0) FROM credit_ledger WHERE kind='charge' AND created>=?", since
                ),
                "credits_refunded_30d": one(
                    "SELECT COALESCE(SUM(delta),0) FROM credit_ledger WHERE kind='refund' AND created>=?", since
                ),
                "plans": plans_,
                "mrr_usd": sum((plans.plan(p).price_usd or 0) * n for p, n in plans_.items()),
            }

    def list_users(self, q: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT u.id, u.email, u.name, u.email_verified, u.is_admin, u.suspended, u.created, u.last_login, "
                "(SELECT COUNT(*) FROM memberships m WHERE m.user_id=u.id) AS workspaces FROM users u "
                "WHERE u.email LIKE ? OR u.name LIKE ? ORDER BY u.created DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", max(1, min(limit, 500))),
            ).fetchall()
        return [{**dict(r), **{k: bool(r[k]) for k in ("email_verified", "is_admin", "suspended")}} for r in rows]

    def list_workspaces(self, q: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.id, w.name, COALESCE(w.display_name, w.name) AS display_name, w.plan, w.credits, w.status, "
                "w.created, (SELECT COUNT(*) FROM memberships m WHERE m.workspace_id=w.id) AS members, "
                "(SELECT COUNT(*) FROM designs d WHERE d.workspace_id=w.id) AS designs FROM workspaces w "
                "WHERE w.name LIKE ? OR COALESCE(w.display_name,'') LIKE ? ORDER BY w.created DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", max(1, min(limit, 500))),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_user_flags(self, uid: str, *, is_admin: bool | None = None, suspended: bool | None = None) -> None:
        with self._conn() as c:
            if is_admin is not None:
                c.execute("UPDATE users SET is_admin=? WHERE id=?", (int(is_admin), uid))
            if suspended is not None:
                c.execute("UPDATE users SET suspended=? WHERE id=?", (int(suspended), uid))
                if suspended:
                    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def set_workspace_status(self, ws_id: str, status: str) -> None:
        if status not in ("active", "suspended"):
            raise InputError("status must be active or suspended")
        with self._conn() as c:
            c.execute("UPDATE workspaces SET status=? WHERE id=?", (status, ws_id))

    def workspace_status(self, ws_id: str) -> str:
        with self._conn() as c:
            r = c.execute("SELECT status FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return r["status"] if r and r["status"] else "active"

    # ================================================================== privacy (export / erasure)
    def export_user(self, uid: str) -> dict[str, Any]:
        u = self.user(uid)
        ms = self.memberships(uid)
        out: dict[str, Any] = {"user": u, "workspaces": []}
        for m in ms:
            ws = self.workspace_obj(m["workspace_id"])
            entry: dict[str, Any] = {"membership": m}
            if ws and ROLE_RANK[m["role"]] >= ROLE_RANK["admin"]:
                entry["designs"] = self.store.list_designs(ws, limit=1000)
                entry["ledger"] = self.ledger(ws.id, 500)
            out["workspaces"].append(entry)
        out["audit"] = [r for r in self.audit_rows(limit=500) if r["user_id"] == uid]
        return out

    def delete_user(self, uid: str) -> None:
        """Erase a user.  Workspaces they solely own are deleted with their designs; shared ones are kept."""
        for m in self.memberships(uid):
            ws_id = m["workspace_id"]
            others = [x for x in self.members(ws_id) if x["id"] != uid]
            if not others:
                self.delete_workspace(ws_id)
            elif m["role"] == "owner" and self._owners(ws_id) <= 1:
                self.add_member(ws_id, others[0]["id"], "owner")  # hand the workspace over, never orphan it
        with self._conn() as c:
            c.execute("DELETE FROM users WHERE id=?", (uid,))
            c.execute("UPDATE audit_log SET user_id=NULL, ip=NULL WHERE user_id=?", (uid,))
        self.audit("user.deleted")

    def delete_workspace(self, ws_id: str) -> None:
        with self._conn() as c:
            for t in ("designs", "api_keys"):
                c.execute(f"DELETE FROM {t} WHERE workspace_id=?", (ws_id,))  # noqa: S608 - fixed names
            c.execute("DELETE FROM usage WHERE workspace_id=?", (ws_id,))
            c.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        shutil.rmtree(self.store.root / "designs" / ws_id, ignore_errors=True)


_CACHE: dict[int, SaaS] = {}


def saas_for(store: Store) -> SaaS:
    """One SaaS facade per tenant store (tests swap stores; each gets its own migrated facade)."""
    s = _CACHE.get(id(store))
    if s is None or s.store is not store:
        s = _CACHE[id(store)] = SaaS(store)
    return s
