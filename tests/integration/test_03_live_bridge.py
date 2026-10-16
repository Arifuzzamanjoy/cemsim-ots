"""The live kiln model: simulator state -> DuoSim bridge -> FreeCAD, *measured* inside FreeCAD.

A green push is not evidence.  After each push these tests read the model back from the
running FreeCAD (colours, cone length, clinker-bed heights, rotation angle) and compare it to
what the simulator reported, so a wrong mapping or a silently-ignored update fails here.
"""

import copy
import time

import pytest
from fastapi.testclient import TestClient

from duosim import server
from duosim.freecad_bridge import FC_MODULE_SRC, GEOMETRY_ID, FreeCADBridge
from duosim.sim import Simulator

from .pngstat import decode, non_background_fraction

pytestmark = pytest.mark.freecad

# the pure colour maps live inside the FreeCAD-side module; run just those functions locally
_ns: dict = {}
exec(FC_MODULE_SRC[FC_MODULE_SRC.index("def _scan(") : FC_MODULE_SRC.index("def _col(")], _ns)
scan, glow, heat = _ns["_scan"], _ns["_glow"], _ns["_heat"]

# the documented operator colours (README: green running, grey stopped, red fault)
GREEN, GREY, RED = (0.18, 0.7, 0.29), (0.6, 0.62, 0.65), (0.85, 0.15, 0.15)
COL_TOL = 0.006  # FreeCAD stores colours as float32 / quantised


def close(a, b, tol=COL_TOL):
    return a is not None and all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


@pytest.fixture(scope="module")
def bridge(worker):
    b = FreeCADBridge(worker.host, worker.port, worker.token)
    b.build()
    return b


@pytest.fixture
def sim():
    s = Simulator()
    s.load_file("nominal")
    s.run(30)
    return s


def push(bridge, sim):
    st = bridge.state_from_sim(sim)
    keep = copy.deepcopy(st)  # push() consumes 't'
    bridge.push(st)
    return keep


def assert_matches(rb: dict, st: dict):
    """Everything FreeCAD holds must equal what the simulator sent."""
    assert rb["gui"] is True and rb["geometry"] == GEOMETRY_ID
    for i, T in enumerate(st["shell"]):
        assert close(rb["shell_rgb"][i], scan(T)), f"kiln segment {i}: {rb['shell_rgb'][i]} vs {scan(T)} @ {T} C"
    for name, T in st["stages"].items():
        assert close(rb["stage_rgb"][name], heat(T)), f"cyclone {name}"
    assert close(rb["calciner_rgb"], heat(st["T_cal"]))
    for i, T in enumerate(st["cooler_T"]):
        want_h = max(50.0, st["bed_m"][min(i * 6 // 18, 5)] * 1000.0)
        assert rb["bed_h_mm"][i] == pytest.approx(want_h, abs=0.01), f"clinker bed {i} height"
        assert close(rb["bed_rgb"][i], glow(T)), f"clinker bed {i} colour"
    if st["burner_on"]:
        assert rb["flame_len_mm"] == pytest.approx(max(1.0, st["flame_m"]) * 1000.0, abs=0.5)
    for name, state in st["drives"].items():
        want = {"RUNNING": GREEN, "FAULT": RED}.get(state, GREY)
        assert close(rb["drive_rgb"][name], want), f"{name} is {state}: {rb['drive_rgb'][name]}"


# ---------------------------------------------------------------------------
def test_build_creates_the_model(bridge):
    rb = bridge.readback()
    assert rb["geometry"] == GEOMETRY_ID and rb["gui"] is True
    assert rb["n_objects"] >= 80
    assert len(rb["shell_rgb"]) == 30 and all(c is not None for c in rb["shell_rgb"])
    assert len(rb["bed_h_mm"]) == 18 and all(h is not None for h in rb["bed_h_mm"])
    assert bridge.diagnose(deep=True)["model_present"] is True


def test_push_puts_the_simulator_state_into_freecad(bridge, sim):
    st = push(bridge, sim)
    assert_matches(bridge.readback(), st)


def test_shell_scanner_colours_follow_temperature_not_just_the_code_path(bridge, sim):
    """Independent of the colour function: hotter segments must be redder (less green/blue)."""
    st = push(bridge, sim)
    rb = bridge.readback()
    pairs = sorted(zip(st["shell"], rb["shell_rgb"], strict=True))
    (t_lo, c_lo), (t_hi, c_hi) = pairs[0], pairs[-1]
    assert t_hi - t_lo > 50, "nominal kiln should have a clear temperature spread"
    assert c_hi[0] > c_lo[0] or c_hi[1] < c_lo[1], f"hot {t_hi} C {c_hi} vs cold {t_lo} C {c_lo}"


def test_disturbances_propagate(bridge, sim):
    before = push(bridge, sim)
    rb0 = bridge.readback()
    assert_matches(rb0, before)

    sim.cmd_disturbance("coal_lhv_kiln", 19000.0)  # main-burner coal heating value drops (allowed 18-32 MJ/t)
    sim.cmd_disturbance("id_fan_trip")
    sim.run(900)
    after = push(bridge, sim)
    rb1 = bridge.readback()
    assert_matches(rb1, after)

    assert after["drives"]["IDFan"] == "FAULT" and close(rb1["drive_rgb"]["IDFan"], RED)
    assert after["shell"] != before["shell"]
    assert rb1["shell_rgb"] != rb0["shell_rgb"], "FreeCAD model did not react to the process change"


def test_kiln_rotation_integrates_rpm_over_simulated_time(worker, sim):
    b = FreeCADBridge(worker.host, worker.port, worker.token)
    b.build()  # fresh module state: phase starts at 0
    push(b, sim)  # first push after a build has dt = 0
    phi = 0.0
    for secs in (40, 25):
        sim.run(secs)
        st = push(b, sim)
        phi += st["rpm"] * 6.0 * secs
    want = ((phi + 180.0) % 360.0) - 180.0
    got = b.readback()["stripe0_deg"]
    delta = ((got - want + 180.0) % 360.0) - 180.0
    assert phi > 10, f"kiln should be turning (rpm {st['rpm']})"
    assert abs(delta) < 0.05, f"stripe angle {got} vs expected {want}"


def test_lost_model_is_rebuilt_automatically(bridge, sim):
    bridge.execute(f"import FreeCAD\nFreeCAD.closeDocument({bridge.DOC!r})")
    assert bridge.DOC not in bridge.list_documents()
    st = push(bridge, sim)  # must notice, rebuild, and apply
    assert bridge.DOC in bridge.list_documents()
    assert_matches(bridge.readback(), st)


def test_rendered_view_changes_with_the_process(bridge, sim, client):
    push(bridge, sim)
    a = client.screenshot("Isometric", 800, 500, doc=bridge.DOC)
    assert a is not None and non_background_fraction(a) > 0.01
    sim.cmd_disturbance("coal_lhv_kiln", 18000.0)
    sim.run(1500)
    push(bridge, sim)
    b = client.screenshot("Isometric", 800, 500, doc=bridge.DOC)
    wa, ha, ch, ra = decode(a)
    _, _, _, rb_ = decode(b)
    changed = sum(
        1 for x, y in zip(ra, rb_, strict=True) for i in range(0, wa * ch, ch) if x[i : i + 3] != y[i : i + 3]
    )
    assert changed / (wa * ha) > 0.0005, "the 3-D view did not visibly change"


# ---------------------------------------------------------------------------
def test_hmi_command_to_freecad_end_to_end(worker, client):
    """Operator presses 'build' in the HMI: POST /api/cmd -> server -> bridge -> FreeCAD -> live pushes."""
    server.BRIDGE = FreeCADBridge(worker.host, worker.port, worker.token)
    try:
        with TestClient(server.app) as http:
            h = http.get("/api/freecad/health", params={"deep": True}).json()
            assert h["configured"] and h["ok"], h

            r = http.post("/api/cmd", json={"type": "freecad", "cmd": "build", "on": True}).json()
            assert r["ok"], r
            t0 = time.time()
            while time.time() - t0 < 60:
                st = server.BRIDGE.status
                if st["state"] == "live" and st.get("pushes", 0) >= 2:
                    break
                time.sleep(0.5)
            assert server.BRIDGE.status["state"] == "live", server.BRIDGE.status
            assert server.BRIDGE.status["pushes"] >= 2

            h = http.get("/api/freecad/health", params={"deep": True}).json()
            assert h["ok"] and h["model_present"] and h["state"] == "live", h

            # what FreeCAD holds equals the simulator's current state
            rb = server.BRIDGE.readback()
            st = server.BRIDGE.state_from_sim(server.get_sim())
            assert_matches(rb, st)

            assert http.post("/api/cmd", json={"type": "freecad", "on": False}).json()["ok"]
    finally:
        server.BRIDGE = None
