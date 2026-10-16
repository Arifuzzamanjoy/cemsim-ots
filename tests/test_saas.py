"""Commercial layer: accounts, sessions + CSRF, organisations, credits ledger, billing webhooks, admin, BYOK,
webhooks, privacy.  Everything runs against a temporary database with fake model / FreeCAD backends."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from duosim import api_design, api_saas, server
from duosim.design.llm import LLMError, LLMResult
from duosim.design.service import DesignService
from duosim.platform.store import Store
from duosim.saas import saas_for
from duosim.saas.billing import sign, verify_signature
from duosim.saas.core import InsufficientCredits
from duosim.saas.netguard import URLRejected, check_url
from duosim.saas.security import InputError, hash_password, verify_password
from duosim.saas.vault import Vault, VaultError

SPEC = {"name": "Ball", "parts": [{"id": "ball", "kind": "sphere", "radius": 1, "at": [0, 0, 1]}]}
PW = "correct horse battery staple"
CSRF = {"X-DuoSim-CSRF": "1"}


class FakeProvider:
    name, model, vision = "fake", "fake-1", True

    def __init__(self):
        self.calls, self.fail = [], None

    def generate(self, system, user, *, schema=None, timeout=0, images=None):
        self.calls.append((system[:40], len(images or [])))
        if self.fail:
            raise self.fail
        return LLMResult(json.dumps(SPEC), "fake", "fake-1", 0.01, {"input_tokens": 1, "output_tokens": 1})


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("DUOSIM_DEV_OPEN", "DUOSIM_PUBLIC_DEMO", "DUOSIM_BILLING", "DUOSIM_SIGNUP", "DUOSIM_CREDITS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DUOSIM_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DUOSIM_ADMIN_EMAILS", "root@duosim.test")
    store = Store(tmp_path / "p.sqlite3")
    prov = FakeProvider()
    monkeypatch.setattr(api_design, "_STORE", store)
    monkeypatch.setattr(api_design, "_SERVICE", DesignService({"fake": prov}))
    monkeypatch.setattr(api_design, "_ENV_LOADED", True)
    monkeypatch.setattr(api_design, "get_builder", lambda: None)
    monkeypatch.setattr(api_design, "get_client", lambda: None)
    return {"store": store, "saas": saas_for(store), "prov": prov}


def client():
    return TestClient(server.app)


def signup(email="alice@acme.test", name="Alice", **extra):
    c = client()
    r = c.post("/api/auth/signup", json={"email": email, "password": PW, "name": name, "accept_terms": True, **extra})
    assert r.status_code == 201, r.text
    return c, r.json()


# --------------------------------------------------------------------------- primitives
def test_password_hashing_and_policy():
    h = hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert verify_password(PW, h) and not verify_password("wrong password!", h) and not verify_password(PW, None)
    assert hash_password(PW) != h, "salted"
    from duosim.saas.security import check_password, normalize_email

    for bad in ("short", "aaaaaaaaaaaa", "alice@acme.test"):
        with pytest.raises(InputError):
            check_password(bad, "alice@acme.test")
    assert normalize_email(" Alice@ACME.test ") == "alice@acme.test"
    with pytest.raises(InputError):
        normalize_email("not-an-email")


def test_vault_round_trip_binds_context_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DUOSIM_SECRET_KEY", "k" * 40)
    v = Vault.from_env(tmp_path)
    tok = v.encrypt("sk-secret", "ws1:p1")
    assert "sk-secret" not in tok and v.decrypt(tok, "ws1:p1") == "sk-secret"
    with pytest.raises(VaultError):
        v.decrypt(tok, "ws2:p1")  # a ciphertext cannot be moved to another workspace
    monkeypatch.setenv("DUOSIM_SECRET_KEY", "z" * 40)
    with pytest.raises(VaultError):
        Vault.from_env(tmp_path).decrypt(tok, "ws1:p1")
    monkeypatch.delenv("DUOSIM_SECRET_KEY")
    v2 = Vault.from_env(tmp_path)  # generated key file, 0600
    assert (tmp_path / "secret.key").stat().st_mode & 0o077 == 0
    assert Vault.from_env(tmp_path).decrypt(v2.encrypt("x", "c"), "c") == "x"


def test_url_policy_blocks_ssrf_targets(monkeypatch):
    monkeypatch.delenv("DUOSIM_ALLOW_PRIVATE_URLS", raising=False)

    def fake_resolve(ip):
        return lambda host, port, proto=0: [(2, 1, 6, "", (ip, port))]

    assert check_url("https://hooks.example.com/x", resolve=fake_resolve("93.184.216.34"))
    for url, ip in [
        ("http://hooks.example.com/x", "93.184.216.34"),
        ("https://metadata.internal/", "169.254.169.254"),
        ("https://localhost/", "127.0.0.1"),
        ("https://intranet/", "10.1.2.3"),
        ("https://v6/", "::1"),
        ("https://user:pw@hooks.example.com/", "93.184.216.34"),
    ]:
        with pytest.raises(URLRejected):
            check_url(url, resolve=fake_resolve(ip))


# --------------------------------------------------------------------------- accounts & sessions
def test_signup_login_logout_and_cookie_flags(env):
    c, me = signup()
    token = c.cookies.get("duosim_session")
    assert token and me["user"]["email"] == "alice@acme.test" and me["workspace"]["role"] == "owner"
    assert me["workspace"]["credits"] == 50, "the free plan's monthly allowance is credited at once"
    assert c.get("/api/auth/me").status_code == 200
    raw = c.post("/api/auth/login", json={"email": "alice@acme.test", "password": PW}).headers["set-cookie"].lower()
    assert "httponly" in raw and "samesite=lax" in raw
    assert c.post("/api/auth/logout").status_code == 403, "logout needs the CSRF header"
    assert c.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    bad = client().post("/api/auth/login", json={"email": "alice@acme.test", "password": "wrong password!"})
    assert bad.status_code == 401 and bad.json()["detail"] == "wrong email or password"
    nobody = client().post("/api/auth/login", json={"email": "nobody@acme.test", "password": PW})
    assert nobody.json()["detail"] == bad.json()["detail"], "no account enumeration through login"
    with sqlite3.connect(env["store"].path) as db:
        dump = json.dumps(
            db.execute("SELECT * FROM users").fetchall() + db.execute("SELECT * FROM sessions").fetchall()
        )
    assert PW not in dump and token not in dump, "only hashes of passwords and session tokens are stored"


def test_signup_rules(env, monkeypatch):
    c = client()
    assert c.post("/api/auth/signup", json={"email": "a@b.test", "password": PW}).status_code == 422, "terms"
    assert (
        c.post("/api/auth/signup", json={"email": "a@b.test", "password": "short", "accept_terms": True}).status_code
        == 422
    )
    signup("dup@acme.test")
    r = c.post("/api/auth/signup", json={"email": "DUP@acme.test", "password": PW, "accept_terms": True})
    assert r.status_code == 422 and "already exists" in r.json()["detail"]
    monkeypatch.setenv("DUOSIM_SIGNUP", "invite")
    assert (
        c.post("/api/auth/signup", json={"email": "x@acme.test", "password": PW, "accept_terms": True}).status_code
        == 403
    )
    assert c.get("/api/auth/config").json()["signup"] == "invite"


def test_login_is_rate_limited(env):
    c = client()
    codes = [
        c.post("/api/auth/login", json={"email": "victim@acme.test", "password": f"guess-{i}-xxxx"}).status_code
        for i in range(12)
    ]
    assert codes[:10] == [401] * 10 and codes[-1] == 429


def test_email_verification_and_password_reset(env):
    c, _ = signup()
    s = env["saas"]
    mail = s.mailer.outbox[-1]
    assert mail.to == "alice@acme.test" and "/verify?token=" in mail.text
    token = mail.text.split("/verify?token=")[1].split()[0]
    assert client().post("/api/auth/verify", json={"token": token}).status_code == 200
    assert client().post("/api/auth/verify", json={"token": token}).status_code == 422, "single use"
    assert c.get("/api/auth/me").json()["user"]["email_verified"] is True
    # reset: silent for unknown addresses, signs out every device
    assert client().post("/api/auth/forgot", json={"email": "nobody@acme.test"}).json()["ok"] is True
    n = len(s.mailer.outbox)
    client().post("/api/auth/forgot", json={"email": "alice@acme.test"})
    assert len(s.mailer.outbox) == n + 1
    tok = s.mailer.outbox[-1].text.split("/reset?token=")[1].split()[0]
    assert client().post("/api/auth/reset", json={"token": tok, "password": "a new strong password"}).status_code == 200
    assert c.get("/api/auth/me").status_code == 401, "old sessions are revoked by a reset"
    assert (
        client()
        .post("/api/auth/login", json={"email": "alice@acme.test", "password": "a new strong password"})
        .status_code
        == 200
    )
    assert client().post("/api/auth/reset", json={"token": tok, "password": "another password 1"}).status_code == 422


def test_change_password_keeps_this_session_and_ends_others(env):
    c, _ = signup()
    other = client()
    other.post("/api/auth/login", json={"email": "alice@acme.test", "password": PW})
    r = c.post("/api/auth/password", json={"current": PW, "new": "brand new password 2"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["signed_out_elsewhere"] >= 1
    assert c.get("/api/auth/me").status_code == 200 and other.get("/api/auth/me").status_code == 401
    assert (
        c.post("/api/auth/password", json={"current": "nope nope nope", "new": "x" * 12}, headers=CSRF).status_code
        == 422
    )


# --------------------------------------------------------------------------- organisations
def test_team_invites_roles_and_seat_limits(env):
    owner, me = signup("owner@acme.test")
    s = env["saas"]
    # free plan: 1 seat, already used by the owner
    r = owner.post("/api/account/invites", json={"email": "bob@acme.test"}, headers=CSRF)
    assert r.status_code == 422 and "seats" in r.json()["detail"]
    s.set_plan(me["workspace"]["id"], "pro")
    r = owner.post("/api/account/invites", json={"email": "bob@acme.test", "role": "member"}, headers=CSRF)
    assert r.status_code == 201 and "token" not in r.json(), "the invitation token travels only by email"
    link = s.mailer.outbox[-1].text.split("/signup?invite=")[1].split()[0]
    token = link.split("&")[0]
    bob = client()
    assert (
        bob.post(
            "/api/auth/signup", json={"email": "eve@acme.test", "password": PW, "invite": token, "accept_terms": True}
        ).status_code
        == 422
    )
    r = bob.post(
        "/api/auth/signup", json={"email": "bob@acme.test", "password": PW, "invite": token, "accept_terms": True}
    )
    assert (
        r.status_code == 201
        and r.json()["workspace"]["id"] == me["workspace"]["id"]
        and r.json()["workspace"]["role"] == "member"
    )
    assert len(r.json()["workspaces"]) == 2, "bob also has his own personal workspace"
    # a member cannot manage keys or members; the owner can promote
    assert bob.get("/api/account/keys").status_code == 403
    bob_id = r.json()["user"]["id"]
    assert bob.patch(f"/api/account/members/{bob_id}", json={"role": "owner"}, headers=CSRF).status_code == 403
    assert owner.patch(f"/api/account/members/{bob_id}", json={"role": "admin"}, headers=CSRF).status_code == 200
    assert bob.get("/api/account/keys").status_code == 200
    # the last owner can neither leave nor be demoted
    oid = me["user"]["id"]
    assert owner.delete(f"/api/account/members/{oid}", headers=CSRF).status_code == 422
    assert owner.patch(f"/api/account/members/{oid}", json={"role": "member"}, headers=CSRF).status_code == 422
    # switching workspaces needs a membership
    assert bob.post("/api/auth/switch", json={"workspace_id": "nope"}, headers=CSRF).status_code == 403


def test_csrf_header_is_required_for_cookie_writes_but_not_for_api_keys(env):
    c, _ = signup()
    assert c.post("/api/account/keys", json={"label": "ci"}).status_code == 403
    r = c.post("/api/account/keys", json={"label": "ci"}, headers=CSRF)
    assert r.status_code == 201 and r.json()["key"].startswith("ds_")
    key = r.json()["key"]
    # cookie-authenticated design calls need the header too ...
    assert c.post("/api/design/build", json={"spec": SPEC}).status_code == 403
    # ... an API key does not (no ambient credential to ride on)
    assert client().post("/api/design/build", json={"spec": SPEC}, headers={"X-API-Key": key}).status_code == 200
    keys = c.get("/api/account/keys").json()["keys"]
    assert c.delete(f"/api/account/keys/{keys[0]['id']}", headers=CSRF).status_code == 204
    assert client().get("/api/design/status", headers={"X-API-Key": key}).status_code == 401


# --------------------------------------------------------------------------- credits
def test_credits_are_charged_atomically_and_refunded_on_provider_failure(env):
    c, me = signup()
    ws = me["workspace"]["id"]
    r = c.post("/api/design/generate", json={"prompt": "a ball", "mode": "standard", "build": False}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["credits"] == {"charged": 4, "balance": 46}
    env["prov"].fail = LLMError("upstream exploded")
    r = c.post("/api/design/generate", json={"prompt": "a ball", "mode": "fast", "build": False}, headers=CSRF)
    assert r.status_code == 502
    led = env["saas"].ledger(ws)
    assert [x["kind"] for x in led[:2]] == ["refund", "charge"] and env["saas"].balance(ws) == 46
    usage = c.get("/api/account/credits").json()["usage_30d"]
    assert usage["spent"] == 4 and usage["refunded"] == 2


def test_out_of_credits_is_402_and_nothing_runs(env):
    c, me = signup()
    s, ws = env["saas"], me["workspace"]["id"]
    s.grant(ws, -s.balance(ws) + 1, "adjust", "test")
    n = len(env["prov"].calls)
    r = c.post("/api/design/generate", json={"prompt": "x", "mode": "fast", "build": False}, headers=CSRF)
    assert r.status_code == 402 and "not enough credits" in r.json()["detail"] and len(env["prov"].calls) == n
    with pytest.raises(InsufficientCredits):
        s.charge(ws, 2, "x")


def test_concurrent_charges_never_overspend(env):
    from concurrent.futures import ThreadPoolExecutor

    s = env["saas"]
    ws = s.store.workspace("race")
    s.grant(ws.id, -s.balance(ws.id) + 10, "adjust", "test")

    def go(_):
        try:
            s.charge(ws.id, 1, "race")
            return 1
        except InsufficientCredits:
            return 0

    with ThreadPoolExecutor(8) as ex:
        assert sum(ex.map(go, range(40))) == 10
    assert s.balance(ws.id) == 0


def test_plan_gates_deep_mode_byok_and_webhooks(env):
    c, me = signup()
    r = c.post("/api/design/generate", json={"prompt": "x", "mode": "deep", "build": False}, headers=CSRF)
    assert r.status_code == 402
    assert (
        c.post(
            "/api/account/providers", json={"name": "mine", "type": "openai", "api_key": "sk-test-123456"}, headers=CSRF
        ).status_code
        == 402
    )
    assert c.post("/api/account/webhooks", json={"url": "https://example.com/h"}, headers=CSRF).status_code == 402
    assert c.get("/api/design/status").json()["modes"] == ["fast", "standard"]
    env["saas"].set_plan(me["workspace"]["id"], "pro")
    assert "deep" in c.get("/api/design/status").json()["modes"]


def test_monthly_allowance_tops_up_and_does_not_accumulate(env):
    s = env["saas"]
    ws = s.store.workspace("monthly")
    assert s.balance(ws.id) == 50
    s.charge(ws.id, 30, "x")
    with s._conn() as c:  # pretend the month rolled over
        c.execute("UPDATE workspaces SET credits_period='2000-01' WHERE id=?", (ws.id,))
    assert s.balance(ws.id) == 50, "topped up to the allowance"
    s.grant(ws.id, 500, "purchase", "pack")
    with s._conn() as c:
        c.execute("UPDATE workspaces SET credits_period='2000-01' WHERE id=?", (ws.id,))
    assert s.balance(ws.id) == 550, "purchased credits are kept, no allowance on top of a larger balance"


# --------------------------------------------------------------------------- billing
def test_stripe_signature_verification():
    body, secret = b'{"id":"evt_1"}', "whsec_test"
    h = sign(body, secret)
    assert verify_signature(body, h, secret)
    assert not verify_signature(body + b" ", h, secret)
    assert not verify_signature(body, h, "whsec_other")
    old = sign(body, secret, ts=int(time.time()) - 3600)
    assert not verify_signature(body, old, secret), "replayed events are refused"
    assert not verify_signature(body, None, secret) and not verify_signature(body, "garbage", secret)


def _event(etype, obj, eid=None):
    return json.dumps({"id": eid or f"evt_{etype}_{time.time_ns()}", "type": etype, "data": {"object": obj}}).encode()


def test_stripe_checkout_and_webhooks_drive_plans_and_credits(env, monkeypatch):
    monkeypatch.setenv("DUOSIM_BILLING", "stripe")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("DUOSIM_STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("DUOSIM_STRIPE_PRICE_PACK_500", "price_pack")
    sent = []

    def transport(method, url, headers, body):
        sent.append((url, body.decode()))
        return 200, json.dumps({"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}).encode()

    monkeypatch.setattr(api_saas, "_BILLING_TRANSPORT", transport)
    c, me = signup()
    ws, s = me["workspace"]["id"], env["saas"]
    r = c.post("/api/billing/checkout", json={"item": "pro"}, headers=CSRF)
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test/")
    url, form = sent[-1]
    assert url.endswith("/checkout/sessions") and "mode=subscription" in form and "price_pro" in form and ws in form
    assert c.post("/api/billing/checkout", json={"item": "enterprise"}, headers=CSRF).status_code == 422

    hook = client()

    def post(etype, obj, eid=None):
        body = _event(etype, obj, eid)
        return hook.post("/api/billing/webhook", content=body, headers={"Stripe-Signature": sign(body, "whsec_x")})

    assert (
        hook.post(
            "/api/billing/webhook", content=_event("x", {}), headers={"Stripe-Signature": "t=1,v1=00"}
        ).status_code
        == 400
    )
    assert (
        post(
            "checkout.session.completed",
            {
                "mode": "subscription",
                "customer": "cus_1",
                "client_reference_id": ws,
                "metadata": {"workspace_id": ws, "item": "pro"},
            },
        ).status_code
        == 200
    )
    assert (
        post(
            "customer.subscription.updated",
            {"id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"workspace_id": ws, "plan": "pro"}},
        ).status_code
        == 200
    )
    assert s.workspace_info(ws)["plan"] == "pro" and s.workspace_info(ws)["subscribed"]
    before = s.balance(ws)
    inv = {"id": "in_1", "customer": "cus_1", "subscription_details": {"metadata": {"plan": "pro"}}}
    assert post("invoice.paid", inv, eid="evt_inv_1").json()["result"] == "granted 1000"
    assert post("invoice.paid", inv, eid="evt_inv_1").json()["duplicate"] is True, (
        "at-least-once delivery is deduplicated"
    )
    assert s.balance(ws) == before + 1000
    # a credit pack bought with a one-off payment
    post(
        "checkout.session.completed",
        {
            "id": "cs_2",
            "mode": "payment",
            "payment_status": "paid",
            "customer": "cus_1",
            "metadata": {"workspace_id": ws, "item": "pack_500"},
        },
    )
    assert s.balance(ws) == before + 1500
    # cancellation -> free plan
    post("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {}})
    assert s.workspace_info(ws)["plan"] == "free" and not s.workspace_info(ws)["subscribed"]


def test_mock_billing_applies_purchases_immediately(env, monkeypatch):
    monkeypatch.setenv("DUOSIM_BILLING", "mock")
    c, _ = signup()
    r = c.post("/api/billing/checkout", json={"item": "team"}, headers=CSRF)
    assert r.status_code == 200 and "billing=success" in r.json()["url"]
    ws = c.get("/api/account/workspace").json()
    assert ws["plan"] == "team" and ws["credits"] == 50 + 5000
    plans = client().get("/api/billing/plans").json()
    assert plans["provider"] == "mock" and {p["id"] for p in plans["plans"]} == {"free", "pro", "team", "enterprise"}


def test_billing_disabled_explains_itself(env):
    c, _ = signup()
    r = c.post("/api/billing/checkout", json={"item": "pro"}, headers=CSRF)
    assert r.status_code == 422 and "not enabled" in r.json()["detail"]


# --------------------------------------------------------------------------- BYOK + webhooks
def test_workspace_provider_keys_are_encrypted_masked_and_usable(env, monkeypatch):
    c, me = signup()
    env["saas"].set_plan(me["workspace"]["id"], "pro")
    r = c.post(
        "/api/account/providers",
        json={"name": "my-openai", "type": "openai", "api_key": "sk-live-SECRET-9876", "model": "gpt-x"},
        headers=CSRF,
    )
    assert r.status_code == 201
    lst = c.get("/api/account/providers").json()["providers"]
    assert lst[0]["key_hint"] == "…9876" and "SECRET" not in json.dumps(lst)
    with sqlite3.connect(env["store"].path) as db:
        assert "SECRET" not in json.dumps(db.execute("SELECT * FROM provider_keys").fetchall())
    st = c.get("/api/design/status").json()
    assert any(p["name"] == "my-openai" and p["byok"] for p in st["providers"])
    # a design with the workspace's own provider is billed at the BYOK price and uses that key
    seen = {}

    class Spy:
        name, model, vision = "my-openai", "gpt-x", True

        def generate(self, system, user, *, schema=None, timeout=0, images=None):
            return LLMResult(json.dumps(SPEC), "my-openai", "gpt-x", 0.01, {})

    def fake_from_secret(secret):
        seen.update(secret)
        return Spy()

    monkeypatch.setattr(api_design, "provider_from_secret", fake_from_secret)
    r = c.post(
        "/api/design/generate",
        json={"prompt": "x", "provider": "my-openai", "mode": "deep", "build": False},
        headers=CSRF,
    )
    assert r.status_code == 200 and r.json()["byok"] and r.json()["credits"]["charged"] == 1
    assert seen["key"] == "sk-live-SECRET-9876"
    bad = c.post(
        "/api/account/providers",
        json={
            "name": "lan",
            "type": "openai_compatible",
            "api_key": "k" * 10,
            "model": "m",
            "base_url": "http://10.0.0.5/v1",
        },
        headers=CSRF,
    )
    assert bad.status_code == 422


def test_outgoing_webhooks_are_signed_retried_and_policy_checked(env, monkeypatch):
    from duosim.saas import webhooks

    s = env["saas"]
    ws = s.store.workspace("hooks")
    monkeypatch.setattr(webhooks, "check_url", lambda u: u)
    with s._conn() as c:
        c.execute(
            "INSERT INTO webhooks(id, workspace_id, url, secret, events, created) VALUES ('w1', ?, 'https://h.test/x', 'whsec_1', 'design.completed', 0)",
            (ws.id,),
        )
    hook = s.webhooks(ws.id, "design.completed", with_secret=True)[0]
    calls = []

    def post(url, body, headers):
        calls.append((body, headers))
        return 500 if len(calls) < 2 else 204

    status = webhooks.deliver(s, hook, "design.completed", {"id": "d1"}, post=post, sleep=lambda _: None)
    assert status == "HTTP 204" and len(calls) == 2
    body, headers = calls[-1]
    assert (
        verify_signature(body, headers["DuoSim-Signature"], "whsec_1") and headers["DuoSim-Event"] == "design.completed"
    )
    assert s.webhooks(ws.id)[0]["last_status"] == "HTTP 204" and "secret" not in s.webhooks(ws.id)[0]
    assert s.webhooks(ws.id, "import.completed") == []


# --------------------------------------------------------------------------- admin & privacy
def test_admin_api_is_admin_only_and_works(env):
    user, me = signup("user@acme.test")
    assert user.get("/api/admin/stats").status_code == 403
    root, rm = signup("root@duosim.test")
    assert rm["user"]["is_admin"] is True, "DUOSIM_ADMIN_EMAILS bootstraps platform admins"
    st = root.get("/api/admin/stats").json()
    assert st["users"] == 2 and st["plans"]["free"] >= 2
    ws = me["workspace"]["id"]
    r = root.post(f"/api/admin/workspaces/{ws}/credits", json={"delta": 250, "note": "goodwill"}, headers=CSRF)
    assert r.json()["balance"] == 300
    r = root.post(f"/api/admin/workspaces/{ws}/plan", json={"plan": "team"}, headers=CSRF)
    assert r.json()["plan"] == "team" and r.json()["credits"] == 5000, "a plan change credits the new allowance now"
    assert (
        root.post(f"/api/admin/workspaces/{ws}/status", json={"status": "suspended"}, headers=CSRF).status_code == 200
    )
    assert user.post("/api/design/build", json={"spec": SPEC}, headers=CSRF).status_code == 403
    root.post(f"/api/admin/workspaces/{ws}/status", json={"status": "active"}, headers=CSRF)
    uid = me["user"]["id"]
    assert root.patch(f"/api/admin/users/{uid}", json={"suspended": True}, headers=CSRF).status_code == 200
    assert user.get("/api/auth/me").status_code == 401, "suspension ends sessions"
    assert root.patch(f"/api/admin/users/{rm['user']['id']}", json={"is_admin": False}, headers=CSRF).status_code == 422
    actions = {e["action"] for e in root.get("/api/admin/audit").json()["events"]}
    assert {"admin.credits", "admin.user", "workspace.plan", "user.signup"} <= actions


def test_data_export_and_account_erasure(env):
    c, me = signup()
    c.post("/api/design/build", json={"spec": SPEC}, headers=CSRF)
    exp = c.get("/api/account/export").json()
    assert exp["user"]["email"] == "alice@acme.test" and exp["workspaces"][0]["designs"]
    assert c.post("/api/account/delete", json={"password": PW, "confirm": "nope"}, headers=CSRF).status_code == 422
    assert c.post("/api/account/delete", json={"password": PW, "confirm": "DELETE"}, headers=CSRF).status_code == 200
    s = env["saas"]
    assert s.user_by_email("alice@acme.test") is None and s.workspace_obj(me["workspace"]["id"]) is None
    assert not (env["store"].root / "designs" / me["workspace"]["id"]).exists()


def test_operator_cli_manages_admins_credits_and_plans(env, capsys, monkeypatch):
    from duosim.platform.__main__ import main

    monkeypatch.setattr("duosim.platform.__main__.Store", lambda: env["store"])
    signup("ops@acme.test")
    assert main(["users", "admin", "ops@acme.test"]) == 0
    assert env["saas"].user_by_email("ops@acme.test")["is_admin"]
    env["store"].workspace("cli-ws")
    assert main(["credits", "cli-ws", "70"]) == 0 and "balance 120" in capsys.readouterr().out
    assert main(["plan", "cli-ws", "pro"]) == 0
    assert main(["plan", "cli-ws", "platinum"]) == 2
