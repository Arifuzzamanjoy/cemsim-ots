"""HTTP API of the commercial layer: sign-up / sign-in, account & organisation, credits, billing, admin.

    /api/auth/*      config, signup, login, logout, me, verify, forgot, reset, password, switch, invites/accept
    /api/account/*   workspace, members, invites, api-keys, credits, providers (BYOK), webhooks, export, delete
    /api/billing/*   plans (public), checkout, portal, webhook (Stripe, signature-verified)
    /api/admin/*     stats, users, workspaces, credits, plan, status, audit   (platform administrators)

Browser sessions are an HttpOnly, SameSite=Lax cookie holding a random token (only its hash is stored).
Every state-changing request authenticated by that cookie must carry the header `X-DuoSim-CSRF: 1`; a
cross-site form or script cannot add a custom header without a CORS pre-flight, which this server never
grants, so cookie-riding (CSRF) requests are refused.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import api_design
from .platform.store import QuotaExceeded
from .saas import plans
from .saas.billing import Billing, BillingError
from .saas.core import ROLE_RANK, WEBHOOK_EVENTS, Forbidden, SaaS, credits_enabled, saas_for
from .saas.security import InputError
from .saas.webhooks import emit

log = logging.getLogger("duosim.api_saas")
SESSION_COOKIE = "duosim_session"
CSRF_HEADER = "x-duosim-csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

auth = APIRouter(prefix="/api/auth", tags=["auth"])
account = APIRouter(prefix="/api/account", tags=["account"])
billing = APIRouter(prefix="/api/billing", tags=["billing"])
admin = APIRouter(prefix="/api/admin", tags=["admin"])
routers = (auth, account, billing, admin)

_BILLING_TRANSPORT = None  # tests inject a fake Stripe transport


def get_saas() -> SaaS:
    return saas_for(api_design.get_store())


def get_billing() -> Billing:
    return Billing(get_saas(), _BILLING_TRANSPORT)


def base_url(request: Request) -> str:
    return (os.environ.get("DUOSIM_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def signup_mode() -> str:
    m = os.environ.get("DUOSIM_SIGNUP", "open").strip().lower()
    return m if m in ("open", "invite", "closed") else "open"


def require_verified() -> bool:
    return os.environ.get("DUOSIM_REQUIRE_VERIFIED_EMAIL") == "1"


def _secure_cookie(request: Request) -> bool:
    if os.environ.get("DUOSIM_COOKIE_SECURE") in ("0", "1"):
        return os.environ["DUOSIM_COOKIE_SECURE"] == "1"
    proto = request.headers.get("x-forwarded-proto") if os.environ.get("DUOSIM_TRUST_FORWARDED") == "1" else None
    return (proto or request.url.scheme) == "https"


def set_session_cookie(resp: Response, request: Request, token: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )


def csrf_ok(request: Request) -> bool:
    return request.method in SAFE_METHODS or request.headers.get(CSRF_HEADER) == "1"


def _rate(request: Request, kind: str, per_hour: int, extra: str = "") -> None:
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime())
    try:
        api_design.get_store().take_scoped(f"{kind}:{api_design._client_ip(request)}{extra}", hour, kind, per_hour)
    except QuotaExceeded:
        raise HTTPException(429, "too many attempts - wait a while and try again") from None


# ------------------------------------------------------------------ the signed-in user
class Ctx:
    def __init__(self, user: dict[str, Any], session: dict[str, Any], saas: SaaS):
        self.user, self.session, self.saas = user, session, saas

    @property
    def uid(self) -> str:
        return self.user["id"]

    @property
    def ws_id(self) -> str:
        ws = self.session.get("workspace_id")
        if not ws:
            raise HTTPException(403, "you are not a member of any workspace")
        return ws

    def need(self, role: str) -> str:
        try:
            return self.saas.require_role(self.uid, self.ws_id, role)
        except Forbidden as e:
            raise HTTPException(403, str(e)) from None


def current(request: Request) -> Ctx:
    s = get_saas()
    sess = s.session(request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise HTTPException(401, "sign in first")
    if not csrf_ok(request):
        raise HTTPException(403, "missing X-DuoSim-CSRF header")
    user = s.user(sess["user_id"])
    if user is None or user["suspended"]:
        raise HTTPException(403, "this account is suspended")
    return Ctx(user, sess, s)


def current_admin(ctx: Annotated[Ctx, Depends(current)]) -> Ctx:
    if not ctx.user["is_admin"]:
        raise HTTPException(403, "administrators only")
    return ctx


CTX = Annotated[Ctx, Depends(current)]
ADMIN = Annotated[Ctx, Depends(current_admin)]


def _input(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except InputError as e:
        raise HTTPException(422, str(e)) from None
    except Forbidden as e:
        raise HTTPException(403, str(e)) from None


def me_payload(s: SaaS, uid: str, ws_id: str | None) -> dict[str, Any]:
    user = s.user(uid)
    info = s.workspace_info(ws_id) if ws_id else None
    if info is not None:
        info["credits"] = s.balance(ws_id) if credits_enabled() else None
        info["role"] = s.role(uid, ws_id)
    return {
        "user": user,
        "workspace": info,
        "workspaces": s.memberships(uid),
        "credits_enabled": credits_enabled(),
        "require_verified_email": require_verified(),
    }


# ================================================================== /api/auth
class SignupIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    name: str = Field(default="", max_length=80)
    invite: str | None = Field(default=None, max_length=100)
    accept_terms: bool = False


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class TokenIn(BaseModel):
    token: str = Field(max_length=100)


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(max_length=100)
    password: str = Field(max_length=256)


class PasswordIn(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class ProfileIn(BaseModel):
    name: str = Field(max_length=80)


class SwitchIn(BaseModel):
    workspace_id: str = Field(max_length=40)


@auth.get("/config")
async def auth_config():
    return {
        "signup": signup_mode(),
        "billing": get_billing().kind or None,
        "require_verified_email": require_verified(),
        "public_demo": api_design.public_demo(),
        "terms_version": "2026-10",
    }


@auth.post("/signup", status_code=201)
async def signup(body: SignupIn, request: Request):
    mode = signup_mode()
    if mode == "closed" or (mode == "invite" and not body.invite):
        raise HTTPException(403, "sign-up is by invitation only on this server")
    if not body.accept_terms:
        raise HTTPException(422, "please accept the terms of service and the privacy policy")
    _rate(request, "signup", 10)
    s = get_saas()
    u = _input(s.create_user, body.email, body.password, body.name, body.invite)
    s.send_verification(u["id"], base_url(request))
    tok = s.create_session(
        u["id"], u["workspace_id"], api_design._client_ip(request), request.headers.get("user-agent", "")
    )
    resp = JSONResponse(me_payload(s, u["id"], u["workspace_id"]), status_code=201)
    set_session_cookie(resp, request, tok)
    return resp


@auth.post("/login")
async def login(body: LoginIn, request: Request):
    _rate(request, "login", 30)
    _rate(request, "login_email", 10, ":" + body.email.strip().lower()[:254])
    s = get_saas()
    u = s.login(body.email, body.password)
    if u is None:
        raise HTTPException(401, "wrong email or password")
    if u["suspended"]:
        raise HTTPException(403, "this account is suspended - contact support")
    ms = s.memberships(u["id"])
    ws = ms[0]["workspace_id"] if ms else None
    tok = s.create_session(u["id"], ws, api_design._client_ip(request), request.headers.get("user-agent", ""))
    s.audit("user.login", workspace_id=ws, user_id=u["id"], ip=api_design._client_ip(request))
    resp = JSONResponse(me_payload(s, u["id"], ws))
    set_session_cookie(resp, request, tok)
    return resp


@auth.post("/logout")
async def logout(request: Request):
    if not csrf_ok(request):
        raise HTTPException(403, "missing X-DuoSim-CSRF header")
    get_saas().end_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@auth.get("/me")
async def me(ctx: CTX):
    return me_payload(ctx.saas, ctx.uid, ctx.session.get("workspace_id"))


@auth.patch("/me")
async def update_me(body: ProfileIn, ctx: CTX):
    _input(ctx.saas.update_profile, ctx.uid, body.name)
    return me_payload(ctx.saas, ctx.uid, ctx.session.get("workspace_id"))


@auth.post("/verify")
async def verify(body: TokenIn):
    if not get_saas().verify_email(body.token):
        raise HTTPException(422, "this confirmation link is invalid or has expired")
    return {"ok": True}


@auth.post("/verify/resend")
async def resend_verification(ctx: CTX, request: Request):
    _rate(request, "verify_mail", 5)
    ctx.saas.send_verification(ctx.uid, base_url(request))
    return {"ok": True}


@auth.post("/forgot")
async def forgot(body: EmailIn, request: Request):
    _rate(request, "forgot", 10)
    get_saas().start_password_reset(body.email, base_url(request))
    return {"ok": True, "msg": "if an account exists for that address, a reset link is on its way"}


@auth.post("/reset")
async def reset(body: ResetIn, request: Request):
    _rate(request, "reset", 20)
    _input(get_saas().reset_password, body.token, body.password)
    return {"ok": True}


@auth.post("/password")
async def change_password(body: PasswordIn, ctx: CTX):
    _input(ctx.saas.set_password, ctx.uid, body.current, body.new)
    n = ctx.saas.end_other_sessions(ctx.uid, ctx.session["token_hash"])
    ctx.saas.audit("user.password_changed", user_id=ctx.uid, other_sessions_ended=n)
    return {"ok": True, "signed_out_elsewhere": n}


@auth.post("/switch")
async def switch(body: SwitchIn, ctx: CTX):
    _input(ctx.saas.switch_workspace, ctx.session["token_hash"], ctx.uid, body.workspace_id)
    return me_payload(ctx.saas, ctx.uid, body.workspace_id)


@auth.post("/invites/accept")
async def accept_invite(body: TokenIn, ctx: CTX):
    ws = _input(ctx.saas.accept_invite, ctx.uid, body.token)
    ctx.saas.switch_workspace(ctx.session["token_hash"], ctx.uid, ws)
    return me_payload(ctx.saas, ctx.uid, ws)


# ================================================================== /api/account
class WorkspaceIn(BaseModel):
    display_name: str = Field(max_length=60)


class NewWorkspaceIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)


class InviteIn(BaseModel):
    email: str = Field(max_length=254)
    role: str = Field(default="member", max_length=10)


class RoleIn(BaseModel):
    role: str = Field(max_length=10)


class KeyIn(BaseModel):
    label: str = Field(default="", max_length=80)


class ProviderIn(BaseModel):
    name: str = Field(max_length=40)
    type: str = Field(max_length=20)
    api_key: str = Field(max_length=400)
    model: str = Field(default="", max_length=120)
    base_url: str = Field(default="", max_length=500)
    vision: bool = True


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000)
    events: list[str] = Field(default_factory=lambda: ["design.completed"], max_length=10)


class DeleteAccountIn(BaseModel):
    password: str = Field(max_length=256)
    confirm: str = Field(max_length=40)


def _feature(ctx: Ctx, feature: str) -> None:
    info = ctx.saas.workspace_info(ctx.ws_id)
    if info is None or feature not in info["plan_info"]["features"]:
        raise HTTPException(402, f"this needs a plan with '{feature}' - upgrade at /account#billing")


@account.get("/workspace")
async def get_workspace(ctx: CTX):
    info = ctx.saas.workspace_info(ctx.ws_id)
    assert info is not None
    info["role"] = ctx.saas.role(ctx.uid, ctx.ws_id)
    info["credits"] = ctx.saas.balance(ctx.ws_id) if credits_enabled() else None
    return info


@account.patch("/workspace")
async def rename_workspace(body: WorkspaceIn, ctx: CTX):
    ctx.need("admin")
    _input(ctx.saas.rename_workspace, ctx.ws_id, body.display_name)
    ctx.saas.audit("workspace.renamed", workspace_id=ctx.ws_id, user_id=ctx.uid, name=body.display_name)
    return {"ok": True}


@account.post("/workspaces", status_code=201)
async def new_workspace(body: NewWorkspaceIn, ctx: CTX):
    owned = [m for m in ctx.saas.memberships(ctx.uid) if m["role"] == "owner"]
    if len(owned) >= 10:
        raise HTTPException(422, "at most 10 workspaces per user")
    ws = ctx.saas.create_workspace(ctx.uid, body.display_name)
    ctx.saas.switch_workspace(ctx.session["token_hash"], ctx.uid, ws.id)
    ctx.saas.audit("workspace.created", workspace_id=ws.id, user_id=ctx.uid)
    return me_payload(ctx.saas, ctx.uid, ws.id)


@account.get("/members")
async def members(ctx: CTX):
    ctx.need("member")
    pending = ctx.saas.pending_invites(ctx.ws_id) if ROLE_RANK[ctx.need("member")] >= ROLE_RANK["admin"] else []
    return {"members": ctx.saas.members(ctx.ws_id), "invites": pending}


@account.post("/invites", status_code=201)
async def invite(body: InviteIn, ctx: CTX, request: Request):
    ctx.need("admin")
    r = _input(ctx.saas.invite, ctx.ws_id, body.email, body.role, ctx.uid, base_url(request))
    r.pop("token")  # mailed, never returned
    return r


@account.patch("/members/{uid}")
async def set_member_role(uid: str, body: RoleIn, ctx: CTX):
    ctx.need("owner")
    _input(ctx.saas.set_role, ctx.ws_id, uid, body.role)
    ctx.saas.audit("member.role", workspace_id=ctx.ws_id, user_id=ctx.uid, member=uid, role=body.role)
    return {"ok": True}


@account.delete("/members/{uid}", status_code=204)
async def remove_member(uid: str, ctx: CTX):
    if uid != ctx.uid:
        ctx.need("admin")
        if ctx.saas.role(uid, ctx.ws_id) == "owner":
            ctx.need("owner")
    _input(ctx.saas.remove_member, ctx.ws_id, uid)
    ctx.saas.audit("member.removed", workspace_id=ctx.ws_id, user_id=ctx.uid, member=uid)


@account.get("/keys")
async def list_keys(ctx: CTX):
    ctx.need("admin")
    return {"keys": ctx.saas.api_keys(ctx.ws_id)}


@account.post("/keys", status_code=201)
async def create_key(body: KeyIn, ctx: CTX):
    ctx.need("admin")
    if require_verified() and not ctx.user["email_verified"]:
        raise HTTPException(403, "confirm your email address first")
    if sum(not k["revoked"] for k in ctx.saas.api_keys(ctx.ws_id)) >= 20:
        raise HTTPException(422, "at most 20 active API keys per workspace")
    ws = ctx.saas.workspace_obj(ctx.ws_id)
    assert ws is not None
    kid, key = api_design.get_store().create_api_key(ws, body.label)
    ctx.saas.audit("apikey.created", workspace_id=ctx.ws_id, user_id=ctx.uid, key_id=kid)
    return {"id": kid, "key": key, "label": body.label, "note": "shown once - store it now"}


@account.delete("/keys/{kid}", status_code=204)
async def revoke_key(kid: str, ctx: CTX):
    ctx.need("admin")
    if not ctx.saas.revoke_api_key(ctx.ws_id, kid):
        raise HTTPException(404, "no such key")
    ctx.saas.audit("apikey.revoked", workspace_id=ctx.ws_id, user_id=ctx.uid, key_id=kid)


@account.get("/credits")
async def credits(ctx: CTX, limit: int = 50):
    return {
        "enabled": credits_enabled(),
        "balance": ctx.saas.balance(ctx.ws_id),
        "costs": plans.credit_costs(),
        "ledger": ctx.saas.ledger(ctx.ws_id, limit),
        "usage_30d": ctx.saas.usage_summary(ctx.ws_id, 30),
    }


@account.get("/providers")
async def list_providers(ctx: CTX):
    return {
        "providers": ctx.saas.provider_keys(ctx.ws_id),
        "types": ["openai", "openai_compatible", "gemini", "claude"],
    }


@account.post("/providers", status_code=201)
async def add_provider(body: ProviderIn, ctx: CTX):
    ctx.need("admin")
    _feature(ctx, "byok")
    r = _input(
        ctx.saas.add_provider_key, ctx.ws_id, body.name, body.type, body.api_key, body.model, body.base_url, body.vision
    )
    ctx.saas.audit("provider.added", workspace_id=ctx.ws_id, user_id=ctx.uid, name=body.name, type=body.type)
    return r


@account.delete("/providers/{pid}", status_code=204)
async def delete_provider(pid: str, ctx: CTX):
    ctx.need("admin")
    if not ctx.saas.delete_provider_key(ctx.ws_id, pid):
        raise HTTPException(404, "no such provider")
    ctx.saas.audit("provider.deleted", workspace_id=ctx.ws_id, user_id=ctx.uid, provider=pid)


@account.get("/webhooks")
async def list_webhooks(ctx: CTX):
    ctx.need("admin")
    return {"webhooks": ctx.saas.webhooks(ctx.ws_id), "events": list(WEBHOOK_EVENTS)}


@account.post("/webhooks", status_code=201)
async def add_webhook(body: WebhookIn, ctx: CTX):
    ctx.need("admin")
    _feature(ctx, "webhooks")
    r = _input(ctx.saas.add_webhook, ctx.ws_id, body.url, body.events)
    ctx.saas.audit("webhook.added", workspace_id=ctx.ws_id, user_id=ctx.uid, url=r["url"])
    return {**r, "note": "the signing secret is shown once - store it now"}


@account.post("/webhooks/{wid}/test")
async def test_webhook(wid: str, ctx: CTX):
    ctx.need("admin")
    if not any(h["id"] == wid for h in ctx.saas.webhooks(ctx.ws_id)):
        raise HTTPException(404, "no such webhook")
    n = emit(ctx.saas, ctx.ws_id, "ping", {"workspace_id": ctx.ws_id, "hello": "from DuoSim"})
    return {"queued": n}


@account.delete("/webhooks/{wid}", status_code=204)
async def delete_webhook(wid: str, ctx: CTX):
    ctx.need("admin")
    if not ctx.saas.delete_webhook(ctx.ws_id, wid):
        raise HTTPException(404, "no such webhook")


@account.get("/audit")
async def workspace_audit(ctx: CTX, limit: int = 100):
    ctx.need("admin")
    return {"events": ctx.saas.audit_rows(ctx.ws_id, limit)}


@account.get("/export")
async def export(ctx: CTX):
    ctx.saas.audit("user.exported", user_id=ctx.uid)
    return JSONResponse(
        ctx.saas.export_user(ctx.uid),
        headers={"Content-Disposition": 'attachment; filename="duosim-export.json"'},
    )


@account.post("/delete")
async def delete_account(body: DeleteAccountIn, ctx: CTX):
    if body.confirm != "DELETE" or ctx.saas.login(ctx.user["email"], body.password) is None:
        raise HTTPException(422, "type DELETE and your current password to erase the account")
    ctx.saas.delete_user(ctx.uid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ================================================================== /api/billing
class CheckoutIn(BaseModel):
    item: str = Field(max_length=20)


@billing.get("/plans")
async def billing_plans():
    return get_billing().catalog()


@billing.post("/checkout")
async def checkout(body: CheckoutIn, ctx: CTX, request: Request):
    ctx.need("admin")
    try:
        url = await _thread(get_billing().checkout, ctx.ws_id, body.item, ctx.user["email"], base_url(request))
    except InputError as e:
        raise HTTPException(422, str(e)) from None
    except BillingError as e:
        raise HTTPException(502, str(e)) from None
    ctx.saas.audit("billing.checkout", workspace_id=ctx.ws_id, user_id=ctx.uid, item=body.item)
    return {"url": url}


@billing.post("/portal")
async def portal(ctx: CTX, request: Request):
    ctx.need("admin")
    try:
        return {"url": await _thread(get_billing().portal, ctx.ws_id, base_url(request))}
    except InputError as e:
        raise HTTPException(422, str(e)) from None
    except BillingError as e:
        raise HTTPException(502, str(e)) from None


@billing.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        return get_billing().handle_webhook(payload, request.headers.get("stripe-signature"))
    except PermissionError:
        raise HTTPException(400, "invalid signature") from None
    except ValueError:
        raise HTTPException(400, "invalid payload") from None


async def _thread(fn, *a):
    import asyncio

    return await asyncio.to_thread(fn, *a)


# ================================================================== /api/admin
class GrantIn(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    note: str = Field(default="", max_length=200)


class PlanIn(BaseModel):
    plan: str = Field(max_length=20)


class StatusIn(BaseModel):
    status: str = Field(max_length=20)


class UserFlagsIn(BaseModel):
    is_admin: bool | None = None
    suspended: bool | None = None


@admin.get("/stats")
async def admin_stats(ctx: ADMIN):
    return ctx.saas.admin_stats()


@admin.get("/users")
async def admin_users(ctx: ADMIN, q: str = "", limit: int = 100):
    return {"users": ctx.saas.list_users(q[:100], limit)}


@admin.patch("/users/{uid}")
async def admin_user(uid: str, body: UserFlagsIn, ctx: ADMIN):
    if uid == ctx.uid and (body.is_admin is False or body.suspended):
        raise HTTPException(422, "you cannot demote or suspend yourself")
    if ctx.saas.user(uid) is None:
        raise HTTPException(404, "no such user")
    ctx.saas.set_user_flags(uid, is_admin=body.is_admin, suspended=body.suspended)
    ctx.saas.audit("admin.user", user_id=ctx.uid, target=uid, **body.model_dump(exclude_none=True))
    return ctx.saas.user(uid)


@admin.get("/workspaces")
async def admin_workspaces(ctx: ADMIN, q: str = "", limit: int = 100):
    return {"workspaces": ctx.saas.list_workspaces(q[:100], limit)}


def _admin_ws(ctx: Ctx, ws_id: str) -> None:
    if ctx.saas.workspace_obj(ws_id) is None:
        raise HTTPException(404, "no such workspace")


@admin.post("/workspaces/{ws_id}/credits")
async def admin_credits(ws_id: str, body: GrantIn, ctx: ADMIN):
    _admin_ws(ctx, ws_id)
    ctx.saas.grant(ws_id, body.delta, "adjust", "admin", note=body.note or f"adjusted by {ctx.user['email']}")
    ctx.saas.audit("admin.credits", workspace_id=ws_id, user_id=ctx.uid, delta=body.delta, note=body.note)
    return {"balance": ctx.saas.balance(ws_id)}


@admin.post("/workspaces/{ws_id}/plan")
async def admin_plan(ws_id: str, body: PlanIn, ctx: ADMIN):
    _admin_ws(ctx, ws_id)
    _input(ctx.saas.set_plan, ws_id, body.plan, grant=True)
    return ctx.saas.workspace_info(ws_id)


@admin.post("/workspaces/{ws_id}/status")
async def admin_status(ws_id: str, body: StatusIn, ctx: ADMIN):
    _admin_ws(ctx, ws_id)
    _input(ctx.saas.set_workspace_status, ws_id, body.status)
    ctx.saas.audit("admin.workspace_status", workspace_id=ws_id, user_id=ctx.uid, status=body.status)
    return ctx.saas.workspace_info(ws_id)


@admin.get("/audit")
async def admin_audit(ctx: ADMIN, limit: int = 200):
    return {"events": ctx.saas.audit_rows(None, limit)}
