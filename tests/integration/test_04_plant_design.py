"""The FreeCAD design script behind the 3-D plant, run in a real FreeCAD.

README claims: 149 parts, zero clashes, exported to cemsim/web/assets/plant3d.json.  These
tests regenerate the model and check the committed browser asset is what the script produces
today (so the asset cannot silently drift from the design source).
"""

import json
import math
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.freecad

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/freecad/build_plant3d.py"
COMMITTED = ROOT / "cemsim/web/assets/plant3d.json"


@pytest.fixture(scope="module")
def generated(client, tmp_path_factory):
    out = tmp_path_factory.mktemp("plant") / "plant3d.json"
    printed = client.execute(
        f"exec(open({str(SCRIPT)!r}).read())\n"
        "n = build()\n"
        "print('PARTS', n)\n"
        "print('CLASHES', json.dumps(clashes()))\n"
        f"print('EXPORT', json.dumps(export_json({str(out)!r})))\n",
        slow=True,
    )
    lines = dict(ln.split(" ", 1) for ln in printed.strip().splitlines() if " " in ln)
    return {"parts": int(lines["PARTS"]), "clashes": json.loads(lines["CLASHES"]), "path": out}


def test_design_has_the_documented_part_count(generated):
    assert generated["parts"] == 149


def test_design_has_no_unintended_clashes(generated):
    assert generated["clashes"] == []


def _points(o):
    v = o["v"]
    return Counter(tuple(v[i : i + 3]) for i in range(0, len(v), 3))


def _area(o):
    """Surface area (cm^2) of the triangle soup."""
    v, idx = o["v"], o["i"]
    total = 0.0
    for k in range(0, len(idx), 3):
        a, b, c = (v[3 * idx[k + j] : 3 * idx[k + j] + 3] for j in range(3))
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        w = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        cx = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        total += 0.5 * math.sqrt(cx[0] ** 2 + cx[1] ** 2 + cx[2] ** 2)
    return total


def test_committed_browser_asset_matches_the_design_source(generated):
    """Geometric comparison: the mesher may pick different triangle diagonals between runs or
    FreeCAD builds, so compare vertex sets and surface area, not index-by-index."""
    new = json.loads(generated["path"].read_text())
    old = json.loads(COMMITTED.read_text())
    assert new["meta"] == old["meta"]
    n_new = {o["name"]: o for o in new["objects"]}
    n_old = {o["name"]: o for o in old["objects"]}
    assert set(n_new) == set(n_old)
    for name, a in n_new.items():
        b = n_old[name]
        assert a["group"] == b["group"] and a["color"] == b["color"], name
        assert _points(a) == _points(b), f"{name}: vertex set changed"
        assert _area(a) == pytest.approx(_area(b), rel=1e-3), f"{name}: surface area changed"


def test_equipment_names_used_by_the_live_bindings_exist(generated):
    """plant3d.js binds live data to these names; renaming a part would silently unbind it."""
    names = {o["name"] for o in json.loads(generated["path"].read_text())["objects"]}
    required = {f"KilnSeg{i:02d}" for i in range(30)}
    required |= {f"Cyc{s}" for s in ("S1", "S2", "S3", "S4", "S5")}
    required |= {"Calciner", "IDFan", "VentFan", "PAFan", "GirthGear", "BurnerPipe"}
    required |= {f"CoolFan{i}" for i in range(1, 7)}
    assert required <= names, sorted(required - names)
