"""Platform layer: plug-in contract, built-in simulators, scaffold, CLI, session API."""

import copy
import json
import math

import pytest
from fastapi.testclient import TestClient

from duosim import api_design, api_platform, server
from duosim.platform import scaffold
from duosim.platform.__main__ import main as platform_main
from duosim.platform.plugins.water_tank import AREA, H_MAX
from duosim.platform.plugins.water_tank import PLUGIN as TANK
from duosim.platform.registry import (
    CommandRejected,
    CommandSpec,
    Manifest,
    Plugin,
    Registry,
    SimpleModel,
    TagSpec,
    conformance,
    default_registry,
)
from duosim.platform.store import Store


# --------------------------------------------------------------------------- contract
def test_builtin_simulators_conform():
    reg = default_registry()
    assert {"cement_kiln", "water_tank"} <= set(reg.ids())
    for sim_id in ("cement_kiln", "water_tank"):
        assert conformance(reg.get(sim_id)) == [], sim_id


class _Good(SimpleModel):
    manifest = Manifest(
        "good_sim",
        "Good",
        "test",
        "1",
        "d",
        2.0,
        (TagSpec("a", lo=0, hi=10),),
        (CommandSpec("go", args={"v": "number"}),),
    )

    def initial_state(self):
        return {"a": 1.0}

    def advance(self, dt):
        self.state["a"] += 0.1 * dt + self.rng.random() * 0.01

    def read_tags(self):
        return {"a": self.state["a"]}

    def handle(self, name, args):
        self.state["a"] = args["v"]
        return {}


def test_a_minimal_plugin_conforms_and_a_rejected_command_is_harmless():
    p = Plugin(_Good.manifest, _Good)
    assert conformance(p) == []
    s = p.create(7)
    with pytest.raises(CommandRejected):
        s.command("nope")
    with pytest.raises(CommandRejected):
        s.command("go", v="x")
    with pytest.raises(CommandRejected):
        s.command("go", v=1, extra=2)
    assert s.command("go", v=3)["ok"] and s.tags()["a"] == 3.0
    s.step(2)
    assert s.t == 4.0


def _break(**changes):
    class Bad(_Good):
        pass

    for k, v in changes.items():
        setattr(Bad, k, v)
    return Plugin(Bad.manifest, Bad)


@pytest.mark.parametrize(
    ("broken", "needle"),
    [
        (_break(read_tags=lambda self: {"a": 1.0, "extra": 2.0}), "keys differ"),
        (_break(read_tags=lambda self: {"a": float("nan")}), "finite"),
        (_break(read_tags=lambda self: {"a": 99.0}), "outside its declared range"),
        (_break(restore=lambda self, snap: None), "restore"),
        (_break(command=lambda self, name, **a: {"ok": True}), "unknown command was accepted"),
        (_break(step=lambda self, n=1: None), "advanced"),
    ],
)
def test_conformance_catches_contract_violations(broken, needle):
    probs = conformance(broken)
    assert any(needle in p for p in probs), probs


def test_manifest_validation_and_registry_rules():
    bad = Manifest("Bad-Id", "x", "d", "1", "", 0.0, (TagSpec("a"), TagSpec("a")))
    assert len(bad.problems()) >= 3
    reg = Registry()
    reg.register(Plugin(_Good.manifest, _Good))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Plugin(_Good.manifest, _Good))
    with pytest.raises(ValueError, match="invalid manifest"):
        reg.register(Plugin(bad, _Good))
    with pytest.raises(KeyError, match="unknown simulator"):
        reg.get("nope")


# --------------------------------------------------------------------------- water tank physics
def test_tank_mass_balance_closes_in_every_regime():
    s = TANK.create(1)
    for cmds, secs in (
        ([("set_demand", 40.0)], 1800),
        ([("set_level_sp", 5.5), ("set_demand", 0.0)], 3600),
        ([("set_auto", False), ("set_valve", 100.0), ("set_pump", 0.0)], 5000),  # overflows
        ([("set_valve", 0.0), ("set_pump", 100.0), ("set_demand", 150.0)], 6000),  # runs dry
    ):
        for name, v in cmds:
            s.command(name, **({"on": v} if name == "set_auto" else {"value": v}))
        s.step(secs)
        tg = s.tags()
        assert abs(tg["balance_error_m3"]) < 1e-6, (cmds, tg)
        assert 0.0 <= tg["level_m"] <= H_MAX + 1e-9
    assert s.tags()["level_m"] == 0.0  # it really ran dry


def test_tank_controller_reaches_setpoint_and_rejects_a_demand_step():
    s = TANK.create(1)
    s.command("set_level_sp", value=4.2)
    s.step(4000)
    assert abs(s.tags()["level_m"] - 4.2) < 0.02
    s.command("set_demand", value=25.0)  # feasible: needs ~82 m3/h of the 90 the inlet can deliver
    v0 = s.tags()["valve_pct"]
    s.step(4000)
    tg = s.tags()
    assert abs(tg["level_m"] - 4.2) < 0.03
    assert tg["valve_pct"] > v0 + 10


def test_tank_demand_beyond_inlet_capacity_saturates_the_valve_and_the_level_sags():
    s = TANK.create(1)
    s.command("set_level_sp", value=4.2)
    s.step(4000)
    s.command("set_demand", value=35.0)  # needs ~92 m3/h at 4.2 m: more than the inlet can supply
    s.step(30000)  # ~5 time constants (tau = A*3600 / (dq/dh) ~ 6200 s)
    tg = s.tags()
    assert tg["valve_pct"] == pytest.approx(100.0)
    # new equilibrium where 48*sqrt(h/3) + 35 = 90 m3/h  ->  h = 3 * (55/48)^2 = 3.938 m
    assert tg["level_m"] == pytest.approx(3.0 * (55.0 / 48.0) ** 2, abs=0.01)
    assert abs(tg["balance_error_m3"]) < 1e-6


def test_tank_snapshot_restore_is_exact_and_commands_are_validated():
    a, b = TANK.create(1), TANK.create(1)
    a.step(300)
    snap = a.snapshot()
    a.step(200)
    b.restore(json.loads(json.dumps(snap, default=list)) if False else copy.deepcopy(snap))
    b.step(200)
    assert a.tags() == b.tags()
    for name, args in (
        ("set_level_sp", {"value": 9.0}),
        ("set_pump", {"value": -1.0}),
        ("set_valve", {"value": 50.0}),  # AUTO is on
        ("set_demand", {"value": float("inf")}),
    ):
        with pytest.raises(CommandRejected):
            a.command(name, **args)
    assert math.isclose(AREA * 3.0, TANK.create(1).tags()["volume_m3"])


# --------------------------------------------------------------------------- scaffold / CLI
def test_scaffolded_plugin_compiles_conforms_and_is_discoverable(tmp_path):
    target = scaffold.write(tmp_path / "sims", "boiler_drum", "Boiler drum", "power")
    for f in target.glob("*.py"):
        compile(f.read_text(), str(f), "exec")
    reg = Registry()
    assert reg.load_directory(tmp_path / "sims") == ["boiler_drum"]
    p = reg.get("boiler_drum")
    assert conformance(p) == [] and p.manifest.domain == "power"
    s = p.create(1)
    s.command("set_setpoint", value=5.0)
    s.step(900)
    assert abs(s.tags()["x"] - 5.0) < 1e-6


def test_scaffold_refuses_bad_ids_overwrites_and_injection(tmp_path):
    for bad in ("Bad-Id", "x", "1abc", "a b", "../evil", "a'); import os; ('"):
        with pytest.raises(ValueError):
            scaffold.render(bad, "n", "d")
    scaffold.write(tmp_path, "ok_sim")
    with pytest.raises(FileExistsError):
        scaffold.write(tmp_path, "ok_sim")
    # hostile free text is quoted, not interpreted
    files = scaffold.render("safe_sim", "x'); import os; os.system('id'); ('", 'd"\n__import__("os")')
    src = files["__init__.py"]
    compile(src, "<gen>", "exec")
    ns = {}
    exec(src, ns)
    assert ns["PLUGIN"].manifest.name.startswith("x'); import os")  # still just a string
    assert "\n" not in ns["PLUGIN"].manifest.domain


def test_cli_new_check_and_simulators(tmp_path, capsys):
    d = str(tmp_path / "s")
    assert platform_main(["new", "kettle", "--name", "Kettle", "--dir", d]) == 0
    assert platform_main(["check", "kettle", "--dir", d]) == 0
    assert platform_main(["new", "kettle", "--dir", d]) == 2  # no overwrite
    assert platform_main(["check", "missing", "--dir", d]) == 2
    assert platform_main(["simulators"]) == 0
    out = capsys.readouterr().out
    assert "kettle: conforms" in out and "cement_kiln" in out and "water_tank" in out


# --------------------------------------------------------------------------- session API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSIM_DEV_OPEN", raising=False)
    store = Store(tmp_path / "p.sqlite3")
    monkeypatch.setattr(api_design, "_STORE", store)
    api_platform._reset_for_tests()
    a, b = store.workspace("acme"), store.workspace("globex")
    yield {
        "http": TestClient(server.app),
        "a": {"X-API-Key": store.create_api_key(a)[1]},
        "b": {"X-API-Key": store.create_api_key(b)[1]},
    }
    api_platform._reset_for_tests()


def test_session_lifecycle_on_a_non_cement_simulator(api):
    h, k = api["http"], api["a"]
    assert {s["id"] for s in h.get("/api/platform/simulators", headers=k).json()["simulators"]} >= {
        "water_tank",
        "cement_kiln",
    }
    r = h.post("/api/platform/sessions", json={"simulator": "water_tank", "seed": 1}, headers=k)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["tags"]["level_m"] == pytest.approx(3.0)
    r = h.post(
        f"/api/platform/sessions/{sid}/command", json={"name": "set_level_sp", "args": {"value": 4.5}}, headers=k
    )
    assert r.status_code == 200 and r.json()["tags"]["level_sp_m"] == 4.5
    r = h.post(f"/api/platform/sessions/{sid}/step", json={"steps": 3000}, headers=k).json()
    assert r["t"] == 3000.0 and abs(r["tags"]["level_m"] - 4.5) < 0.05
    assert abs(r["tags"]["balance_error_m3"]) < 1e-6
    assert h.delete(f"/api/platform/sessions/{sid}", headers=k).status_code == 204
    assert h.get(f"/api/platform/sessions/{sid}", headers=k).status_code == 404


def test_session_api_validates_and_isolates_tenants(api):
    h, a, b = api["http"], api["a"], api["b"]
    assert h.get("/api/platform/simulators").status_code == 401
    sid = h.post("/api/platform/sessions", json={"simulator": "water_tank"}, headers=a).json()["id"]
    assert h.get(f"/api/platform/sessions/{sid}", headers=b).status_code == 404
    assert h.post("/api/platform/sessions", json={"simulator": "nope"}, headers=a).status_code == 404
    bad = h.post(f"/api/platform/sessions/{sid}/command", json={"name": "set_pump", "args": {"value": 500}}, headers=a)
    assert bad.status_code == 422 and "set_pump" in bad.json()["detail"]
    assert (
        h.post(f"/api/platform/sessions/{sid}/command", json={"name": "zap", "args": {}}, headers=a).status_code == 422
    )
    assert h.post(f"/api/platform/sessions/{sid}/step", json={"steps": 10**6}, headers=a).status_code == 422
    assert h.post(f"/api/platform/sessions/{sid}/step", json={"steps": 0}, headers=a).status_code == 422


def test_session_cap_and_cement_kiln_through_the_generic_api(api, monkeypatch):
    h, a = api["http"], api["a"]
    monkeypatch.setenv("DUOSIM_MAX_SESSIONS", "2")
    h.post("/api/platform/sessions", json={"simulator": "water_tank"}, headers=a)
    r = h.post("/api/platform/sessions", json={"simulator": "cement_kiln"}, headers=a)
    assert r.status_code == 201
    assert r.json()["tags"]["T_bz"] > 1300, "kiln session starts from the calibrated nominal state"
    sid = r.json()["id"]
    s = h.post(f"/api/platform/sessions/{sid}/step", json={"steps": 20}, headers=a).json()
    assert s["t"] == 20.0 and all(math.isfinite(v) for v in s["tags"].values())
    over = h.post("/api/platform/sessions", json={"simulator": "water_tank"}, headers=a)
    assert over.status_code == 429
    c = h.post(
        f"/api/platform/sessions/{sid}/command",
        json={"name": "disturbance", "args": {"id": "coal_lhv_kiln", "value": 19000}},
        headers=a,
    )
    assert c.status_code == 200
    c = h.post(
        f"/api/platform/sessions/{sid}/command",
        json={"name": "disturbance", "args": {"id": "coal_lhv_kiln", "value": 1}},
        headers=a,
    )
    assert c.status_code == 422, "the simulator's own range check must surface as a 422"


def test_public_sessions_are_private_to_a_visitor_and_expire_quickly(api, monkeypatch):
    monkeypatch.setenv("DUOSIM_PUBLIC_DEMO", "1")
    monkeypatch.setenv("DUOSIM_TRUST_FORWARDED", "1")
    h = api["http"]
    a, b = {"X-Forwarded-For": "203.0.113.1"}, {"X-Forwarded-For": "203.0.113.2"}
    sid = h.post("/api/platform/sessions", json={"simulator": "water_tank"}, headers=a).json()["id"]
    assert h.get(f"/api/platform/sessions/{sid}", headers=a).status_code == 200
    assert h.get(f"/api/platform/sessions/{sid}", headers=b).status_code == 404  # another visitor
    assert h.get(f"/api/platform/sessions/{sid}", headers=api["a"]).status_code == 404  # a keyed tenant
    # an abandoned anonymous console is reaped after the short public idle time
    monkeypatch.setenv("DUOSIM_PUBLIC_SESSION_IDLE_S", "0")
    import time as _t

    _t.sleep(0.05)
    assert h.get(f"/api/platform/sessions/{sid}", headers=a).status_code == 404
