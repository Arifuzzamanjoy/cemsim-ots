"""Design API: tenant isolation, API keys, quotas, size caps (fake LLM + fake FreeCAD builder)."""

import json

import pytest
from fastapi.testclient import TestClient

from cemsim import api_design, server
from cemsim.design.builder import BuildResult, PartCheck
from cemsim.design.llm import LLMError, LLMResult
from cemsim.design.service import DesignService
from cemsim.platform.store import Store

SPEC = {
    "name": "Ball",
    "parts": [{"id": "ball", "kind": "sphere", "radius": 1, "at": [0, 0, 0], "color": "#ff0000"}],
}


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self):
        self.prompts = []
        self.fail = None

    def generate(self, system, user, *, schema=None, timeout=0):
        self.prompts.append(user)
        if self.fail:
            raise self.fail
        return LLMResult(json.dumps(SPEC), "fake", "fake-1", 0.01, {"input_tokens": 3, "output_tokens": 4})


class FakeBuilder:
    def __init__(self, ok=True):
        self.ok, self.built = ok, []

    def build(self, spec, **_):
        self.built.append(spec)
        checks = [
            PartCheck(p.id, p.kind, self.ok, [] if self.ok else ["volume off"], 4.19, 4.19, 0.0) for p in spec.parts
        ]
        return BuildResult(
            doc="Dfake",
            ok=self.ok,
            checks=checks,
            bbox_m=[-1, -1, -1, 1, 1, 1],
            total_volume_m3=4.19,
            gui=True,
            png=b"\x89PNG-fake",
            artifacts={"step": b"ISO-10303-21;", "mesh": b'{"objects":[]}', "fcstd": b"PK"},
            timings={"build": 0.1},
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("CEMSIM_DEV_OPEN", raising=False)
    prov, builder = FakeProvider(), FakeBuilder()
    store = Store(tmp_path / "p.sqlite3")
    monkeypatch.setattr(api_design, "_STORE", store)
    monkeypatch.setattr(api_design, "_SERVICE", DesignService({"fake": prov}))
    monkeypatch.setattr(api_design, "_ENV_LOADED", True)
    monkeypatch.setattr(api_design, "get_builder", lambda: builder)
    monkeypatch.setattr(api_design, "get_client", lambda: None)
    acme, globex = store.workspace("acme"), store.workspace("globex")
    return {
        "http": TestClient(server.app),
        "store": store,
        "prov": prov,
        "builder": builder,
        "k_acme": store.create_api_key(acme, "t")[1],
        "k_globex": store.create_api_key(globex, "t")[1],
        "acme": acme,
    }


def H(key):
    return {"X-API-Key": key}


def test_every_design_route_requires_an_api_key(env):
    h = env["http"]
    for method, path in [
        ("get", "/api/design/status"),
        ("post", "/api/design/generate"),
        ("post", "/api/design/build"),
        ("get", "/api/design"),
        ("get", "/api/design/abc"),
        ("get", "/api/design/abc/file/step"),
    ]:
        r = getattr(h, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401, (method, path, r.status_code)
    assert h.get("/api/design/status", headers=H("cs_wrong")).status_code == 401
    assert h.get("/api/design/status", headers=H("not-even-prefixed")).status_code == 401


def test_bearer_token_is_accepted_and_revocation_works(env):
    h, st = env["http"], env["store"]
    ok = h.get("/api/design/status", headers={"Authorization": f"Bearer {env['k_acme']}"})
    assert ok.status_code == 200 and ok.json()["workspace"] == "acme"
    kid, key = st.create_api_key(env["acme"], "temp")
    assert h.get("/api/design/status", headers=H(key)).status_code == 200
    assert st.revoke_key(kid)
    assert h.get("/api/design/status", headers=H(key)).status_code == 401


def test_dev_open_mode_is_explicit_opt_in(env, monkeypatch):
    assert env["http"].get("/api/design/status").status_code == 401
    monkeypatch.setenv("CEMSIM_DEV_OPEN", "1")
    r = env["http"].get("/api/design/status")
    assert r.status_code == 200 and r.json()["workspace"] == "dev"


def test_generate_builds_verifies_and_persists(env):
    r = env["http"].post("/api/design/generate", json={"prompt": "a red ball"}, headers=H(env["k_acme"]))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["spec"]["parts"][0]["id"] == "ball" and j["provider"] == "fake" and j["attempts"] == 1
    assert j["build"]["built"] and j["build"]["ok"] and j["build"]["checks"][0]["ok"]
    assert env["builder"].built, "the validated spec (not raw model text) is what gets built"
    d = env["http"].get(f"/api/design/{j['id']}", headers=H(env["k_acme"])).json()
    assert d["verified"] is True and set(d["artifacts"]) >= {"step", "mesh", "png", "spec"}
    f = env["http"].get(f"/api/design/{j['id']}/file/step", headers=H(env["k_acme"]))
    assert f.status_code == 200 and f.content == b"ISO-10303-21;"
    assert env["http"].get(f"/api/design/{j['id']}/file/..%2f..%2fetc", headers=H(env["k_acme"])).status_code == 404


def test_failed_verification_is_reported_not_hidden(env):
    env["builder"].ok = False
    j = env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"])).json()
    assert (
        j["build"]["built"]
        and j["build"]["ok"] is False
        and j["build"]["failed_parts"][0]["problems"] == ["volume off"]
    )
    assert env["store"].get_design(env["acme"], j["id"])["verified"] is False


def test_tenants_cannot_see_each_others_designs_or_files(env):
    j = env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"])).json()
    g = H(env["k_globex"])
    assert env["http"].get(f"/api/design/{j['id']}", headers=g).status_code == 404
    assert env["http"].get(f"/api/design/{j['id']}/file/step", headers=g).status_code == 404
    assert env["http"].get("/api/design", headers=g).json()["designs"] == []
    assert len(env["http"].get("/api/design", headers=H(env["k_acme"])).json()["designs"]) == 1


def test_daily_quota_returns_429(env, monkeypatch):
    monkeypatch.setenv("CEMSIM_DAILY_GENERATIONS", "2")
    for _ in range(2):
        assert (
            env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"])).status_code == 200
        )
    r = env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"]))
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]
    # another tenant is unaffected
    assert env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_globex"])).status_code == 200


def test_model_failures_are_gateway_errors_and_cost_no_build(env):
    env["prov"].fail = LLMError("Gemini API HTTP 429: quota")
    r = env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"]))
    assert r.status_code == 502 and "quota" in r.json()["detail"]
    assert env["builder"].built == []


def test_invalid_prompt_and_spec_are_422_with_precise_errors(env):
    h, k = env["http"], H(env["k_acme"])
    assert h.post("/api/design/generate", json={"prompt": ""}, headers=k).status_code == 422
    assert h.post("/api/design/generate", json={"prompt": "x" * 2001}, headers=k).status_code == 422
    r = h.post("/api/design/build", json={"spec": {"parts": [{"id": "a", "kind": "box", "at": [0, 0, 0]}]}}, headers=k)
    assert r.status_code == 422 and any("size" in e for e in r.json()["errors"])
    assert env["builder"].built == []


def test_build_endpoint_builds_an_edited_spec(env):
    r = env["http"].post("/api/design/build", json={"spec": SPEC}, headers=H(env["k_acme"]))
    assert r.status_code == 200 and r.json()["build"]["ok"] and len(env["builder"].built) == 1


def test_oversized_bodies_are_refused_before_any_work(env):
    big = {"prompt": "x", "pad": "y" * 1_100_000}
    r = env["http"].post("/api/design/generate", json=big, headers=H(env["k_acme"]))
    assert r.status_code == 413
    assert env["prov"].prompts == []


def test_without_a_freecad_worker_the_spec_is_still_returned(env, monkeypatch):
    monkeypatch.setattr(api_design, "get_builder", lambda: None)
    j = env["http"].post("/api/design/generate", json={"prompt": "x"}, headers=H(env["k_acme"])).json()
    assert j["spec"]["name"] == "Ball" and j["build"]["built"] is False and "FreeCAD" in j["build"]["reason"]


def test_api_keys_are_stored_hashed(env):
    import sqlite3

    with sqlite3.connect(env["store"].path) as c:
        blob = json.dumps(c.execute("SELECT * FROM api_keys").fetchall())
    assert env["k_acme"] not in blob and env["k_globex"] not in blob


def test_chunked_uploads_cannot_bypass_the_size_limit(env):
    """No Content-Length: the limit must hold while the body streams."""

    def body():
        yield b'{"prompt": "x", "pad": "'
        for _ in range(40):
            yield b"y" * 50_000
        yield b'"}'

    r = env["http"].post(
        "/api/design/generate", content=body(), headers={**H(env["k_acme"]), "content-type": "application/json"}
    )
    assert r.status_code == 413
    assert env["prov"].prompts == []


def test_design_ids_must_be_server_generated_hex(env):
    h, k = env["http"], H(env["k_acme"])
    for bad in ("../x", "ABCDEF0123456789", "short", "０１２３４５６７８９０１２３４５", "a" * 17):
        assert h.get(f"/api/design/{bad}", headers=k).status_code in (404, 422), bad
        assert h.get(f"/api/design/{bad}/file/step", headers=k).status_code == 404, bad


# ---------------------------------------------------------------- public demo (no key needed, bounded)
@pytest.fixture
def public(env, monkeypatch):
    monkeypatch.setenv("CEMSIM_PUBLIC_DEMO", "1")
    return env


def test_public_demo_works_without_a_key_and_says_so(public):
    r = public["http"].get("/api/design/status")
    assert r.status_code == 200
    j = r.json()
    assert j["public"] is True and j["workspace"] == "public"
    assert j["usage"]["generate"]["limit"] == 20


def test_public_demo_off_by_default_and_a_presented_key_must_still_be_valid(env, monkeypatch):
    assert env["http"].get("/api/design/status").status_code == 401  # not enabled
    monkeypatch.setenv("CEMSIM_PUBLIC_DEMO", "1")
    h = env["http"]
    assert h.get("/api/design/status", headers=H("cs_not_a_real_key_0000000000000")).status_code == 401, (
        "a wrong key must not silently fall back to the anonymous workspace"
    )
    assert h.get("/api/design/status", headers=H("AQ.model-key-typed-by-mistake")).status_code == 401
    ok = h.get("/api/design/status", headers=H(env["k_acme"])).json()
    assert ok["workspace"] == "acme" and ok["public"] is False


def test_public_demo_generates_but_is_capped_per_visitor_and_per_day(public, monkeypatch):
    monkeypatch.setenv("CEMSIM_PUBLIC_HOURLY_GENERATIONS", "2")
    monkeypatch.setenv("CEMSIM_PUBLIC_DAILY_GENERATIONS", "3")
    h = public["http"]
    for _ in range(2):
        assert h.post("/api/design/generate", json={"prompt": "x"}).status_code == 200
    r = h.post("/api/design/generate", json={"prompt": "x"})
    assert r.status_code == 429 and "hourly" in r.json()["detail"] and "own API key" in r.json()["detail"]
    # a different visitor (IP) still has an hourly allowance, until the shared daily budget is gone
    monkeypatch.setenv("CEMSIM_TRUST_FORWARDED", "1")
    other = {"X-Forwarded-For": "203.0.113.9"}
    assert h.post("/api/design/generate", json={"prompt": "x"}, headers=other).status_code == 200
    r = h.post("/api/design/generate", json={"prompt": "x"}, headers={"X-Forwarded-For": "203.0.113.10"})
    assert r.status_code == 429 and "daily" in r.json()["detail"]
    # a keyed workspace is not affected by the public budget
    assert h.post("/api/design/generate", json={"prompt": "x"}, headers=H(public["k_acme"])).status_code == 200


def test_forwarded_ip_is_ignored_unless_the_operator_trusts_the_proxy(public, monkeypatch):
    monkeypatch.setenv("CEMSIM_PUBLIC_HOURLY_GENERATIONS", "1")
    h = public["http"]
    assert (
        h.post("/api/design/generate", json={"prompt": "x"}, headers={"X-Forwarded-For": "198.51.100.1"}).status_code
        == 200
    )
    # spoofing a new address must not buy a new allowance
    r = h.post("/api/design/generate", json={"prompt": "x"}, headers={"X-Forwarded-For": "198.51.100.2"})
    assert r.status_code == 429


def test_public_visitors_cannot_list_each_others_designs_but_keyed_users_can(public):
    h = public["http"]
    did = h.post("/api/design/generate", json={"prompt": "secret brief"}).json()["id"]
    assert h.get("/api/design").json() == {"designs": []}
    assert h.get(f"/api/design/{did}").status_code == 200  # the unguessable id is the capability
    assert h.get("/api/design", headers=H(public["k_acme"])).json()["designs"] == []
