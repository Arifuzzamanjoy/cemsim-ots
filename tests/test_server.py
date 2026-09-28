"""Server/API tests with FastAPI's in-process TestClient (runs on every OS in CI)."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from cemsim import fileset, server
from cemsim.sim import DATA, Simulator


@pytest.fixture(scope="module")
def client():
    with TestClient(server.app) as c:
        yield c


def cmd(client, **c):
    r = client.post("/api/cmd", json=c)
    assert r.status_code == 200
    return r.json()


def test_pages_and_static_assets(client):
    assert "CemSim" in client.get("/").text
    assert "importmap" in client.get("/3d").text
    r = client.get("/static/assets/plant3d.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert len(r.json()["objects"]) > 100


def test_snapshots_listed(client):
    names = {s["name"] for s in client.get("/api/snapshots").json()}
    assert {"nominal", "hot_standby"} <= names


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "drive", "tag": "NOPE", "cmd": "sp", "value": 1},
        {"type": "drive", "tag": "ID_FAN", "cmd": "sp", "value": None},
        {"type": "pid", "tag": "TIC_CAL", "field": "sp", "value": "NaN"},
        {"type": "dist", "id": "coal_lhv_kiln", "value": -5},
        {"type": "snapshot_load", "name": "../../README"},
        {"type": "group", "tag": "G_FEED", "cmd": "dance"},
        {"type": "session", "cmd": "bogus"},
        {"type": "freecad", "cmd": "nope"},
        {},
    ],
)
def test_bad_commands_rejected_not_500(client, bad):
    res = cmd(client, **bad)
    assert res["ok"] is False and res["msg"]


def test_freecad_absent_in_production(client):
    res = cmd(client, type="freecad", cmd="build")
    assert res["ok"] is False and "not configured" in res["msg"]


def test_valid_commands_and_state_endpoints(client):
    assert cmd(client, type="snapshot_load", name="nominal")["ok"]
    assert cmd(client, type="speed", value=5)["ok"]
    assert cmd(client, type="drive", tag="ID_FAN", cmd="sp", value=80)["ok"]
    ev = client.get("/api/events?n=5").json()
    assert any(e["tag"] == "ID_FAN.SP" and e["value"] == 80 for e in ev)
    hb = client.get("/api/heat_balance").json()
    assert hb == {} or abs(hb["closure"]) < 0.05 * hb["fuel"]
    assert "criteria" in client.get("/api/session").json()


def test_websocket_meta_status_and_commands(client):
    with client.websocket_connect("/ws") as ws:
        meta = ws.receive_json()
        assert meta["type"] == "meta" and len(meta["disturbances"]) >= 19 and meta["freecad"] is False
        ws.send_text("not json")
        while (m := ws.receive_json())["type"] != "ack":
            pass
        assert m["ok"] is False
        ws.send_json({"type": "speed", "value": 2})
        while (m := ws.receive_json())["type"] != "ack":
            pass
        assert m["ok"] is True
        while (m := ws.receive_json())["type"] != "status":
            pass
        assert {"kpi", "drives", "alarms", "profiles"} <= m.keys()
        assert len(m["profiles"]["T_shell"]) == 30


def test_fileset_round_trip_is_bit_exact(tmp_path):
    sim = Simulator()
    sim.load_file("nominal")
    sim.run(20)
    snap = sim.snapshot("unit test")
    path = tmp_path / "rt.json"
    fileset.dump(snap, path)
    back = fileset.load(path)
    assert fileset.comment(path) == "unit test"

    def same(a, b):
        if isinstance(a, np.ndarray):
            return a.dtype == b.dtype and a.shape == b.shape and np.array_equal(a, b, equal_nan=True)
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
        if isinstance(a, (list, tuple)):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b or (a != a and b != b)

    assert same(snap, back)
    a, b = Simulator(), Simulator()
    a.restore(snap)
    b.restore(back)
    a.run(60)
    b.run(60)
    assert a.kpi["T_bz"] == b.kpi["T_bz"]


def test_fileset_rejects_foreign_files(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"format": "something-else"}')
    with pytest.raises(ValueError):
        fileset.load(p)


def test_save_file_uses_validated_names():
    sim = Simulator()
    sim.load_file("nominal")
    path = sim.save_file("pytest_tmp_state", "tmp")
    try:
        assert path.parent == DATA / "snapshots" and path.suffix == ".json"
        sim.load_file("pytest_tmp_state")
    finally:
        path.unlink(missing_ok=True)
