"""Design spec validation, script generation safety and geometric verification (no FreeCAD needed)."""

import math

import pytest

from duosim.design.builder import CENTROID_TOL_M, build_script, export_script, verify
from duosim.design.spec import (
    MAX_PARTS,
    SpecError,
    analytic_centroid,
    analytic_volume,
    parse_spec,
)

GOOD = {
    "name": "t",
    "parts": [
        {"id": "slab", "kind": "box", "size": [6, 4, 0.4], "at": [0, 0, 0], "color": "#7a7f85", "group": "civil"},
        {"id": "tank", "kind": "cylinder", "radius": 1.2, "height": 3, "at": [1.5, 2, 0.4], "axis": "z"},
        {"id": "dome", "kind": "cone", "radius": 1.2, "radius2": 0.3, "height": 0.8, "at": [1.5, 2, 3.4]},
        {"id": "ring", "kind": "tube", "radius": 0.6, "inner_radius": 0.5, "height": 1, "at": [4, 2, 0.4], "axis": "x"},
        {"id": "ball", "kind": "sphere", "radius": 0.3, "at": [5, 3, 1]},
        {"id": "line", "kind": "pipe", "radius": 0.1, "path": [[2.7, 2, 1.5], [4, 2, 1.5], [4, 3, 1.5]]},
    ],
}


def errs(raw):
    with pytest.raises(SpecError) as e:
        parse_spec(raw)
    return e.value.errors


def test_valid_spec_roundtrips():
    spec, warns = parse_spec(GOOD)
    assert warns == [] and [p.id for p in spec.parts] == ["slab", "tank", "dome", "ring", "ball", "line"]
    again, _ = parse_spec(spec.to_dict())  # to_dict output is itself valid input
    assert again.to_dict() == spec.to_dict()


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda s: s["parts"][0].update(size=[1, 2]), "size"),
        (lambda s: s["parts"][0].update(size=[1, 2, -3]), "size"),
        (lambda s: s["parts"][1].update(radius=0), "radius"),
        (lambda s: s["parts"][1].update(radius=float("nan")), "radius"),
        (lambda s: s["parts"][1].update(height=1e9), "height"),
        (lambda s: s["parts"][1].update(radius="1.2"), "radius"),  # strings are not numbers
        (lambda s: s["parts"][1].update(radius=True), "radius"),  # nor are booleans
        (lambda s: s["parts"][1].update(axis="diagonal"), "axis"),
        (lambda s: s["parts"][1].update(at=[0, 0, 5000]), "outside"),
        (lambda s: s["parts"][3].update(inner_radius=0.9), "inner_radius"),
        (lambda s: s["parts"][2].update(radius=0), "radius"),  # base radius must be > 0; only the top may be 0
        (lambda s: s["parts"][5].update(path=[[0, 0, 0]]), "path"),
        (lambda s: s["parts"][5].update(path=[[0, 0, 0], [0, 0, 0]]), "differ"),
        (lambda s: s["parts"][0].update(id="9bad"), "id"),
        (lambda s: s["parts"][0].update(id="has space"), "id"),
        (lambda s: s["parts"][1].update(id="slab"), "duplicate"),
        (lambda s: s["parts"][0].update(kind="torus"), "kind"),
        (lambda s: s["parts"][0].update(yaw_deg=720), "yaw"),
        (lambda s: s["parts"][1].pop("height"), "height"),
    ],
)
def test_invalid_specs_are_rejected_with_a_specific_message(mutate, needle):
    import copy

    raw = copy.deepcopy(GOOD)
    mutate(raw)
    assert any(needle in m for m in errs(raw)), errs(raw)


def test_structure_errors():
    assert errs([]) and errs({"parts": []}) and errs({"parts": "x"}) and errs({"parts": [1]})
    many = [{"id": f"p{i}", "kind": "sphere", "radius": 1, "at": [0, 0, 0]} for i in range(MAX_PARTS + 1)]
    assert "too many parts" in errs({"parts": many})[0]


def test_soft_problems_are_warnings_not_errors():
    raw = {"parts": [{"id": "a", "kind": "sphere", "radius": 1, "at": [0, 0, 0], "color": "red", "opacity": 7, "x": 1}]}
    spec, warns = parse_spec(raw)
    assert spec.parts[0].color.startswith("#") and spec.parts[0].opacity == 1.0
    assert len(warns) == 3


def test_analytic_reference_values():
    s, _ = parse_spec(GOOD)
    by = {p.id: p for p in s.parts}
    assert analytic_volume(by["slab"]) == pytest.approx(9.6)
    assert analytic_volume(by["tank"]) == pytest.approx(math.pi * 1.44 * 3)
    assert analytic_volume(by["dome"]) == pytest.approx(math.pi * 0.8 / 3 * (1.44 + 0.36 + 0.09))
    assert analytic_volume(by["ring"]) == pytest.approx(math.pi * (0.36 - 0.25) * 1)
    assert analytic_volume(by["ball"]) == pytest.approx(4 / 3 * math.pi * 0.027)
    assert analytic_volume(by["line"]) is None
    assert analytic_centroid(by["slab"]) == pytest.approx((3, 2, 0.2))
    assert analytic_centroid(by["ring"]) == pytest.approx((4.5, 2, 0.4))  # axis x
    # cone centroid lies at h/4 for a pointed cone
    cone, _ = parse_spec(
        {"parts": [{"id": "c", "kind": "cone", "radius": 2, "radius2": 0, "height": 4, "at": [0, 0, 0]}]}
    )
    assert analytic_centroid(cone.parts[0])[2] == pytest.approx(1.0)


def test_yaw_rotates_the_centroid_about_the_anchor():
    s, _ = parse_spec({"parts": [{"id": "b", "kind": "box", "size": [2, 2, 2], "at": [1, 1, 0], "yaw_deg": 90}]})
    # box centre (2,2,1) rotated 90 deg about (1,1): (1,1)+(−1,1) = (0,2)
    assert analytic_centroid(s.parts[0]) == pytest.approx((0, 2, 1))


# ---------------------------------------------------------------- script safety
def test_generated_script_is_valid_python_and_carries_only_validated_literals():
    spec, _ = parse_spec(GOOD)
    code = build_script(spec, "Dabc123")
    compile(code, "<design>", "exec")
    compile(export_script("Dabc123"), "<export>", "exec")
    for banned in ("os.system", "subprocess", "__import__", "open(", "eval(", "exec("):
        assert banned not in code


@pytest.mark.parametrize(
    "evil",
    [
        "x'); import os; os.system('id'); ('",
        "a\nimport os",
        "a;b",
        "../../etc/passwd",
        "__import__('os')",
        "é",
    ],
)
def test_hostile_identifiers_never_reach_the_script(evil):
    for field in ("id", "group"):
        raw = {"parts": [{"id": "a", "kind": "sphere", "radius": 1, "at": [0, 0, 0]}]}
        raw["parts"][0][field] = evil
        try:
            spec, _ = parse_spec(raw)
        except SpecError:
            continue  # rejected outright (ids)
        code = build_script(spec, "D1")
        assert "import os" not in code and "system" not in code
        assert spec.parts[0].group == "main"  # sanitised (groups)


# ---------------------------------------------------------------- verification logic
def _report(spec, **over):
    parts = []
    for p in spec.parts:
        v = analytic_volume(p)
        c = analytic_centroid(p)
        parts.append(
            {
                "id": p.id,
                "volume": (v if v is not None else 1.0) * 1e9,
                "centroid": [x * 1000 for x in (c or (0, 0, 0))],
                "valid": True,
                "solids": 1,
                "bbox": [0] * 6,
            }
        )
    for k, patch in over.items():
        next(x for x in parts if x["id"] == k).update(patch)
    return {"parts": parts}


def test_verify_accepts_a_faithful_build():
    spec, _ = parse_spec(GOOD)
    assert all(c.ok for c in verify(spec, _report(spec)))


def test_verify_catches_wrong_volume_position_validity_and_missing_objects():
    spec, _ = parse_spec(GOOD)
    rep = _report(
        spec,
        tank={"volume": analytic_volume(spec.parts[1]) * 1e9 * 1.01},
        ring={"centroid": [4500 + 5 + CENTROID_TOL_M * 1000, 2000, 400]},
        ball={"valid": False},
    )
    rep["parts"] = [p for p in rep["parts"] if p["id"] != "dome"]
    by = {c.id: c for c in verify(spec, rep)}
    assert not by["tank"].ok and "volume" in by["tank"].problems[0]
    assert not by["ring"].ok and "centre of mass" in by["ring"].problems[0]
    assert not by["ball"].ok and "validity" in by["ball"].problems[0]
    assert not by["dome"].ok and "missing" in by["dome"].problems[0]
    assert by["slab"].ok and by["line"].ok


def test_total_pipe_waypoints_are_capped_to_protect_the_shared_worker():
    """Pipes are fused boolean-by-boolean (~0.19 s per waypoint): an uncapped valid design could
    wedge the shared FreeCAD GUI thread for minutes (measured: 792 points > 90 s)."""
    from duosim.design.spec import MAX_PATH_POINTS, MAX_TOTAL_PATH_POINTS

    def pipe(i, n):
        return {"id": f"p{i}", "kind": "pipe", "radius": 0.1, "path": [[k, i, 0] for k in range(n)]}

    ok = [pipe(i, MAX_PATH_POINTS) for i in range(MAX_TOTAL_PATH_POINTS // MAX_PATH_POINTS)]
    parse_spec({"parts": ok})
    over = [*ok, pipe(99, 2)]
    assert any("waypoints" in m for m in errs({"parts": over}))
    assert MAX_TOTAL_PATH_POINTS <= 150
