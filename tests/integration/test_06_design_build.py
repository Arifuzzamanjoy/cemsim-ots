"""Design pipeline on a real FreeCAD: spec -> geometry -> measured against closed-form values."""

import copy
import json
import math
import os

import pytest
from fastapi.testclient import TestClient

from duosim import api_design, server
from duosim.design.builder import DesignBuilder, verify
from duosim.design.llm import LLMResult
from duosim.design.service import DesignService
from duosim.design.spec import AXES, analytic_volume, parse_spec
from duosim.platform.store import Store

from .pngstat import non_background_fraction

pytestmark = pytest.mark.freecad

PLANT = {
    "name": "Mixed primitives",
    "parts": [
        {"id": "slab", "kind": "box", "size": [6, 4, 0.4], "at": [0, 0, 0], "yaw_deg": 17, "color": "#7a7f85"},
        {"id": "tank", "kind": "cylinder", "radius": 1.2, "height": 3, "at": [1.5, 2, 0.4], "color": "#4f86c6"},
        {"id": "dome", "kind": "cone", "radius": 1.2, "radius2": 0.3, "height": 0.8, "at": [1.5, 2, 3.4]},
        {"id": "point", "kind": "cone", "radius": 0.5, "radius2": 0, "height": 2, "at": [4, 1, 0.4], "yaw_deg": -40},
        {
            "id": "ring",
            "kind": "tube",
            "radius": 0.6,
            "inner_radius": 0.5,
            "height": 1,
            "at": [4.5, 2, 0.9],
            "axis": "x",
        },
        {"id": "ball", "kind": "sphere", "radius": 0.3, "at": [5, 3, 1], "color": "#c0392b", "opacity": 0.5},
        {"id": "duct", "kind": "pipe", "radius": 0.1, "path": [[2.7, 2, 1.5], [4, 2, 1.5], [4, 3, 1.5], [5, 3, 1.3]]},
    ],
}


@pytest.fixture(scope="module")
def builder(client):
    return DesignBuilder(client)


@pytest.fixture(scope="module")
def built(builder):
    spec, _ = parse_spec(PLANT)
    return spec, builder.build(spec)


def test_every_primitive_matches_its_closed_form(built):
    spec, res = built
    assert res.ok, [(c.id, c.problems) for c in res.checks if not c.ok]
    by = {c.id: c for c in res.checks}
    for part in spec.parts:
        exp = analytic_volume(part)
        if exp is not None:
            assert by[part.id].volume_m3 == pytest.approx(exp, rel=1e-6), part.id
            assert by[part.id].centroid_error_m < 1e-6, part.id
    assert by["duct"].volume_m3 > 0
    assert res.gui and res.png is not None


@pytest.mark.parametrize("axis", list(AXES))
@pytest.mark.parametrize("kind", ["cylinder", "cone", "tube"])
def test_every_axis_direction_places_the_part_as_specified(builder, kind, axis):
    p = {"id": "p", "kind": kind, "radius": 0.8, "height": 2.5, "axis": axis, "at": [3, -2, 1.5], "yaw_deg": 25}
    if kind == "cone":
        p["radius2"] = 0.2
    if kind == "tube":
        p["inner_radius"] = 0.5
    spec, _ = parse_spec({"parts": [p]})
    res = builder.build(spec, screenshot=False, export=())
    assert res.ok, res.checks[0].problems
    assert res.checks[0].centroid_error_m < 1e-6


def test_verification_is_not_vacuous(built, builder):
    """Negative control on real measurements: FreeCAD builds a *tampered* design, and the
    faithful spec must then fail verification against what FreeCAD actually produced."""
    faithful, _ = built
    raw = copy.deepcopy(PLANT)
    raw["parts"][1]["radius"] = 1.3  # tank 10 cm fatter
    raw["parts"][4]["at"] = [4.5, 2, 0.95]  # ring moved 5 cm
    tampered, _ = parse_spec(raw)
    res = builder.build(tampered, screenshot=False, export=())
    assert res.ok, "the tampered design is internally consistent"
    by = {c.id: c for c in verify(faithful, res.report)}
    assert not by["tank"].ok and "volume" in by["tank"].problems[0]
    assert not by["ring"].ok and "centre of mass" in by["ring"].problems[0]
    assert by["slab"].ok and by["ball"].ok, "untouched parts must still verify"


def test_step_export_round_trips_with_the_same_volume(built, client, tmp_path):
    spec, res = built
    step = tmp_path / "d.step"
    step.write_bytes(res.artifacts["step"])
    assert res.artifacts["step"].startswith(b"ISO-10303-21;")
    out = client.execute(
        f"import Part\ns = Part.read({str(step)!r})\nprint(sum(x.Volume for x in s.Solids), len(s.Solids))\n"
    )
    vol, n = out.split()
    assert float(vol) / 1e9 == pytest.approx(res.total_volume_m3, rel=1e-6)
    assert int(n) >= len(spec.parts) - 1  # fused pipe may merge segments; nothing may disappear


def test_mesh_artifact_is_consistent_with_the_built_model(built):
    spec, res = built
    mesh = json.loads(res.artifacts["mesh"])
    names = {o["name"] for o in mesh["objects"]}
    assert names == {p.id for p in spec.parts}
    assert all(len(o["i"]) % 3 == 0 and len(o["i"]) > 0 for o in mesh["objects"])
    xs = [v for o in mesh["objects"] for v in o["v"][0::3]]
    ys = [v for o in mesh["objects"] for v in o["v"][1::3]]  # three.js Y = FreeCAD Z
    zs = [v for o in mesh["objects"] for v in o["v"][2::3]]  # three.js Z = -FreeCAD Y
    x0, y0, z0, x1, y1, z1 = (b * 1000 for b in res.bbox_m)
    tol = 0.02 * max(x1 - x0, y1 - y0, z1 - z0) + 5
    for got, want in ((min(xs), x0), (max(xs), x1), (min(ys), z0), (max(ys), z1), (min(zs), -y1), (max(zs), -y0)):
        assert math.isclose(got, want, abs_tol=tol), (got, want)


def test_screenshot_of_the_design_is_not_blank(built):
    _, res = built
    assert non_background_fraction(res.png) > 0.02


def test_design_api_end_to_end_on_a_real_worker(worker, tmp_path, monkeypatch):
    """HTTP -> auth -> (scripted) model -> validated spec -> real FreeCAD -> verified -> files."""

    class Scripted:
        name, model = "scripted", "s-1"

        def generate(self, system, user, *, schema=None, timeout=0):
            return LLMResult(json.dumps(PLANT), "scripted", "s-1", 0.0, {})

    store = Store(tmp_path / "p.sqlite3")
    monkeypatch.setattr(api_design, "_STORE", store)
    monkeypatch.setattr(api_design, "_SERVICE", DesignService({"scripted": Scripted()}))
    monkeypatch.setattr(api_design, "_ENV_LOADED", True)
    monkeypatch.setattr(api_design, "_BUILDERS", {})
    monkeypatch.setenv("DUOSIM_FREECAD_URL", f"{worker.host}:{worker.port}")
    monkeypatch.setenv("FREECAD_MCP_TOKEN", worker.token)
    monkeypatch.delenv("DUOSIM_DEV_OPEN", raising=False)
    key = store.create_api_key(store.workspace("acme"), "t")[1]
    h = {"X-API-Key": key}
    http = TestClient(server.app)

    s = http.get("/api/design/status", headers=h).json()
    assert s["freecad"]["configured"] and s["freecad"]["ok"], s

    j = http.post("/api/design/generate", json={"prompt": "mixed primitives"}, headers=h).json()
    assert j["build"]["built"] and j["build"]["ok"], j["build"]
    assert j["build"]["parts"] == len(PLANT["parts"])
    step = http.get(f"/api/design/{j['id']}/file/step", headers=h)
    assert step.status_code == 200 and step.content.startswith(b"ISO-10303-21;")
    png = http.get(f"/api/design/{j['id']}/file/png", headers=h).content
    assert non_background_fraction(png) > 0.02


@pytest.mark.llm
def test_live_model_designs_something_that_freecad_verifies(builder):
    """Real model API, real FreeCAD.  Opt-in (spends credits): pytest -m "freecad and llm"."""
    from duosim.design.llm import load_dotenv, providers_from_env

    load_dotenv(os.environ.get("DUOSIM_ENV_FILE", ".env"))
    provs = providers_from_env()
    if not provs:
        pytest.skip("no GEMINI_API_KEY / ANTHROPIC_API_KEY configured")
    svc = DesignService(provs)
    g = svc.generate("A vertical storage silo on four legs with a bucket elevator beside it and a feed spout.")
    assert g.attempts <= 2 and len(g.spec.parts) >= 5
    res = builder.build(g.spec, screenshot=False, export=())
    bad = [(c.id, c.problems) for c in res.checks if not c.ok]
    assert res.ok, bad


def test_worst_case_valid_design_builds_well_inside_the_gui_budget(builder):
    """The validator's limits are what protect the shared GUI thread: a maximal valid design
    (300 parts incl. the full pipe-waypoint budget) must finish far under the addon's 90 s task
    budget, because a started GUI task cannot be cancelled."""
    import time

    from duosim.design.spec import MAX_PARTS, MAX_PATH_POINTS, MAX_TOTAL_PATH_POINTS

    pipes = [
        {
            "id": f"p{i}",
            "kind": "pipe",
            "radius": 0.2,
            "path": [[10 * math.cos(k / 2) + i * 3, 10 * math.sin(k / 2), k * 0.5] for k in range(MAX_PATH_POINTS)],
        }
        for i in range(MAX_TOTAL_PATH_POINTS // MAX_PATH_POINTS)
    ]
    n = MAX_PARTS - len(pipes)
    others = [
        {
            "id": f"t{i}",
            "kind": "tube",
            "radius": 1.0,
            "inner_radius": 0.8,
            "height": 2,
            "at": [i * 0.7, 30, 0],
            "axis": "x",
        }
        for i in range(n // 2)
    ] + [{"id": f"s{i}", "kind": "sphere", "radius": 0.5, "at": [i, 40, 0]} for i in range(n - n // 2)]
    spec, _ = parse_spec({"parts": pipes + others})
    assert len(spec.parts) == MAX_PARTS
    t0 = time.time()
    res = builder.build(spec, screenshot=False, export=("step",))
    dt = time.time() - t0
    assert res.ok
    assert dt < 60, f"worst valid design took {dt:.0f}s; the addon aborts tasks at 90 s and cannot cancel them"
