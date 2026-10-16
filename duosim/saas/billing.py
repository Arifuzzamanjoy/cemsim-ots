"""Billing: Stripe Checkout / Customer Portal over plain HTTPS, signed webhooks, and a mock provider.

DUOSIM_BILLING=stripe   needs STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET and DUOSIM_STRIPE_PRICE_<PLAN|PACK> ids
DUOSIM_BILLING=mock     checkout succeeds immediately (demos and tests only - never in production)
(unset)                 billing off: plans and credits are managed by administrators

Credits are granted only from *verified* webhook events, never from the browser returning to the success
page, and every event id is processed at most once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from . import plans
from .core import SaaS
from .security import InputError

log = logging.getLogger("duosim.billing")
STRIPE_API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_S = 300

Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]


class BillingError(RuntimeError):
    pass


def provider() -> str:
    p = os.environ.get("DUOSIM_BILLING", "").strip().lower()
    return p if p in ("stripe", "mock") else ""


def _urllib(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body or None, headers=headers, method=method)  # noqa: S310 - fixed https host
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BillingError(f"network error talking to Stripe: {type(e).__name__}") from None


def _form(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's nested form encoding: a[b][0][c]=v."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _form(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


class Stripe:
    def __init__(self, secret_key: str, transport: Transport | None = None):
        self._key = secret_key
        self._t = transport or _urllib

    def post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        body = urllib.parse.urlencode(_form(params)).encode()
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": os.environ.get("DUOSIM_STRIPE_API_VERSION", "2024-06-20"),
        }
        status, raw = self._t("POST", f"{STRIPE_API}{path}", headers, body)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if status >= 400:
            msg = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
            raise BillingError(f"Stripe HTTP {status}: {msg[:200]}")
        return data


def verify_signature(payload: bytes, header: str | None, secret: str, now: float | None = None) -> bool:
    """Stripe-Signature: t=<unix>,v1=<hex hmac-sha256 of "<t>.<payload>">[,v1=...]."""
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Build a Stripe-style signature header (tests and the mock provider)."""
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + payload, hashlib.sha256).hexdigest()}"


class Billing:
    def __init__(self, saas: SaaS, transport: Transport | None = None):
        self.saas = saas
        self.kind = provider()
        self._transport = transport

    def stripe(self) -> Stripe:
        key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not key:
            raise BillingError("STRIPE_SECRET_KEY is not configured")
        return Stripe(key, self._transport)

    def catalog(self) -> dict[str, Any]:
        prices = plans.stripe_price_ids()
        return {
            "provider": self.kind or None,
            "plans": [
                {**p.to_dict(), "purchasable": bool(self.kind == "mock" or (self.kind == "stripe" and pid in prices))}
                for pid, p in plans.PLANS.items()
            ],
            "packs": [
                {**p.to_dict(), "purchasable": bool(self.kind == "mock" or (self.kind == "stripe" and pid in prices))}
                for pid, p in plans.PACKS.items()
            ],
            "costs": plans.credit_costs(),
        }

    # ------------------------------------------------------------ checkout
    def checkout(self, ws_id: str, item: str, email: str, base_url: str) -> str:
        """Returns the URL to send the browser to."""
        is_plan, is_pack = item in plans.PLANS, item in plans.PACKS
        if not (is_plan or is_pack) or item in ("free", "enterprise"):
            raise InputError("choose a paid plan or a credit pack")
        if not self.kind:
            raise InputError("online billing is not enabled on this server - contact the administrator")
        if self.kind == "mock":
            self.apply_mock(ws_id, item)
            return f"{base_url}/account?billing=success&item={item}#billing"
        price = plans.stripe_price_ids().get(item)
        if not price:
            raise InputError(f"{item} is not for sale online yet")
        params: dict[str, Any] = {
            "mode": "subscription" if is_plan else "payment",
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{base_url}/account?billing=success#billing",
            "cancel_url": f"{base_url}/account?billing=cancel#billing",
            "client_reference_id": ws_id,
            "metadata": {"workspace_id": ws_id, "item": item},
            "allow_promotion_codes": True,
        }
        cus = self.saas.customer_id(ws_id)
        if cus:
            params["customer"] = cus
        else:
            params["customer_email"] = email
            if not is_plan:
                params["customer_creation"] = "always"
        if is_plan:
            params["subscription_data"] = {"metadata": {"workspace_id": ws_id, "plan": item}}
        else:
            params["payment_intent_data"] = {"metadata": {"workspace_id": ws_id, "pack": item}}
        url = self.stripe().post("/checkout/sessions", params).get("url")
        if not url:
            raise BillingError("Stripe did not return a checkout URL")
        return url

    def portal(self, ws_id: str, base_url: str) -> str:
        if self.kind == "mock":
            return f"{base_url}/account#billing"
        if self.kind != "stripe":
            raise InputError("online billing is not enabled on this server")
        cus = self.saas.customer_id(ws_id)
        if not cus:
            raise InputError("this workspace has no billing account yet - buy a plan or credits first")
        return self.stripe().post("/billing_portal/sessions", {"customer": cus, "return_url": f"{base_url}/account"})[
            "url"
        ]

    def apply_mock(self, ws_id: str, item: str) -> None:
        if item in plans.PLANS:
            self.saas.set_plan(ws_id, item, subscription_id=None)
            p = plans.plan(item)
            self.saas.grant(ws_id, p.monthly_credits, "grant", "plan", note=f"{p.name} plan (mock billing)")
        else:
            pk = plans.PACKS[item]
            self.saas.grant(ws_id, pk.credits, "purchase", "pack", note=f"{pk.credits} credit pack (mock billing)")
        self.saas.audit("billing.mock_purchase", workspace_id=ws_id, item=item)

    # ------------------------------------------------------------ webhooks
    def handle_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        if self.kind != "stripe" or not verify_signature(payload, signature, secret):
            raise PermissionError("invalid webhook signature")
        ev = json.loads(payload)
        eid, etype = str(ev.get("id", "")), str(ev.get("type", ""))
        if not eid or self.saas.event_seen(eid, etype):
            return {"received": True, "duplicate": True}
        obj = (ev.get("data") or {}).get("object") or {}
        handler = {
            "checkout.session.completed": self._checkout_completed,
            "customer.subscription.created": self._subscription,
            "customer.subscription.updated": self._subscription,
            "customer.subscription.deleted": self._subscription_deleted,
            "invoice.paid": self._invoice_paid,
        }.get(etype)
        result = handler(obj) if handler else "ignored"
        log.info("stripe event %s %s: %s", etype, eid, result)
        return {"received": True, "result": result}

    def _ws_of(self, obj: dict[str, Any]) -> str | None:
        md = obj.get("metadata") or {}
        ws = md.get("workspace_id") or obj.get("client_reference_id")
        if ws and self.saas.workspace_obj(ws):
            return ws
        return self.saas.workspace_by_customer(obj.get("customer"))

    def _checkout_completed(self, obj: dict[str, Any]) -> str:
        ws = self._ws_of(obj)
        if ws is None:
            return "unknown workspace"
        if obj.get("customer"):
            self.saas.link_customer(ws, obj["customer"])
        item = (obj.get("metadata") or {}).get("item", "")
        if obj.get("mode") == "payment" and obj.get("payment_status") == "paid" and item in plans.PACKS:
            pk = plans.PACKS[item]
            self.saas.grant(ws, pk.credits, "purchase", "pack", note=f"{pk.credits} credit pack", ref=obj.get("id"))
            self.saas.audit("billing.pack_purchased", workspace_id=ws, pack=item)
            return f"granted {pk.credits}"
        return "linked"

    def _plan_from(self, obj: dict[str, Any]) -> str | None:
        md_plan = (obj.get("metadata") or {}).get("plan")
        if md_plan in plans.PLANS:
            return md_plan
        by_price = {v: k for k, v in plans.stripe_price_ids().items()}
        for line in ((obj.get("items") or obj.get("lines") or {}).get("data")) or []:
            price = line.get("price") or {}
            pid = price.get("id") if isinstance(price, dict) else price
            pid = pid or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
            if pid in by_price and by_price[pid] in plans.PLANS:
                return by_price[pid]
        return None

    def _subscription(self, obj: dict[str, Any]) -> str:
        ws = self._ws_of(obj)
        if ws is None:
            return "unknown workspace"
        if obj.get("customer"):
            self.saas.link_customer(ws, obj["customer"])
        status = obj.get("status")
        if status in ("active", "trialing", "past_due"):
            p = self._plan_from(obj) or "pro"
            self.saas.set_plan(ws, p, subscription_id=obj.get("id"))
            return f"plan {p} ({status})"
        if status in ("canceled", "unpaid", "incomplete_expired"):
            return self._subscription_deleted(obj)
        return f"status {status} ignored"

    def _subscription_deleted(self, obj: dict[str, Any]) -> str:
        ws = self._ws_of(obj)
        if ws is None:
            return "unknown workspace"
        self.saas.set_plan(ws, plans.DEFAULT_PLAN, subscription_id=None)
        return "downgraded to free"

    def _invoice_paid(self, obj: dict[str, Any]) -> str:
        ws = self._ws_of(obj) or self.saas.workspace_by_customer(obj.get("customer"))
        if ws is None:
            return "unknown workspace"
        sub_md = ((obj.get("subscription_details") or {}).get("metadata")) or {}
        p = sub_md.get("plan") if sub_md.get("plan") in plans.PLANS else self._plan_from(obj)
        if p is None:
            info = self.saas.workspace_info(ws)
            p = info["plan"] if info else plans.DEFAULT_PLAN
        allowance = plans.plan(p).monthly_credits
        self.saas.grant(
            ws, allowance, "grant", "plan", note=f"{plans.plan(p).name} plan, invoice paid", ref=obj.get("id")
        )
        return f"granted {allowance}"
