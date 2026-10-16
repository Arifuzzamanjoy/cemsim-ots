"""CAD / mesh import: every supported format round-trips to a valid spec with the right primitives."""

import io
import json
import math
import struct
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from duosim import api_design, server
from duosim.design.cad_import import ImportError_, bodies_from_report, import_cad, measure_script
from duosim.design.spec import parse_spec
from duosim.platform.store import Store


# --------------------------------------------------------------------------- mesh builders (millimetres)
def box(o, s):
    x0, y0, z0 = o
    dx, dy, dz = s
    v = np.array([[x0 + i * dx, y0 + j * dy, z0 + k * dz] for i in (0, 1) for j in (0, 1) for k in (0, 1)], float)
    f = [
        (0, 2, 3),
        (0, 3, 1),
        (4, 5, 7),
        (4, 7, 6),
        (0, 1, 5),
        (0, 5, 4),
        (2, 6, 7),
        (2, 7, 3),
        (0, 4, 6),
        (0, 6, 2),
        (1, 3, 7),
        (1, 7, 5),
    ]
    return v, np.array(f)


def revolve(c, r1, r2, h, n=48, axis="z"):
    v, f = [], []
    for k in range(n):
        a = 2 * math.pi * k / n
        v += [(r1 * math.cos(a), r1 * math.sin(a), 0), (r2 * math.cos(a), r2 * math.sin(a), h)]
    v += [(0, 0, 0), (0, 0, h)]
    b, t = 2 * n, 2 * n + 1
    for k in range(n):
        i0, i1 = 2 * k, 2 * ((k + 1) % n)
        f += [(i0, i1, i1 + 1), (i0, i1 + 1, i0 + 1), (b, i1, i0), (t, i0 + 1, i1 + 1)]
    v = np.array(v, float)
    if axis == "x":
        v = v[:, [2, 1, 0]]
    return v + np.array(c, float), np.array(f)


def stl_binary(meshes):
    tris = [v[t] for v, f in meshes for t in f]
    out = bytearray(b"\0" * 80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<12fH", 0, 0, 0, *t.ravel(), 0)
    return bytes(out)


def stl_ascii(meshes):
    lines = ["solid test"]
    for v, f in meshes:
        for t in f:
            lines += [
                "facet normal 0 0 0",
                "outer loop",
                *[f"vertex {p[0]} {p[1]} {p[2]}" for p in v[t]],
                "endloop",
                "endfacet",
            ]
    return ("\n".join([*lines, "endsolid test"]) + "\n").encode()


def by_kind(spec):
    out = {}
    for p in spec["parts"]:
        out.setdefault(p["kind"], []).append(p)
    return out


PLANT = [box((0, 0, 0), (4000, 2000, 1000)), revolve((8000, 0, 0), 1000, 1000, 5000), revolve((0, 6000, 0), 1500, 300, 2000),
         revolve((0, -5000, 500), 400, 400, 6000, axis="x")]  # fmt: skip


def test_binary_stl_bodies_are_fitted_to_the_right_primitives():
    r = import_cad(stl_binary(PLANT), "plant.stl")
    k = by_kind(r.spec)
    assert r.report["bodies"] == 4 and r.report["units"].startswith("mm")
    assert len(k["box"]) == 1 and k["box"][0]["size"] == pytest.approx([4, 2, 1], abs=1e-6)
    cyl = sorted(k["cylinder"], key=lambda p: p["radius"])
    assert (
        cyl[0]["axis"] == "x"
        and cyl[0]["radius"] == pytest.approx(0.4, rel=1e-3)
        and cyl[0]["height"] == pytest.approx(6)
    )
    assert (
        cyl[1]["axis"] == "z"
        and cyl[1]["radius"] == pytest.approx(1.0, rel=1e-3)
        and cyl[1]["height"] == pytest.approx(5)
    )
    cone = k["cone"][0]
    assert cone["radius"] == pytest.approx(1.5, rel=1e-3) and cone["radius2"] == pytest.approx(0.3, rel=1e-3)
    assert r.report["mean_volume_error"] < 0.01, "polygonal cylinders are within 1 % of the fitted volume"
    assert min(p["at"][2] for p in r.spec["parts"]) == pytest.approx(0.0), "placed on the ground"
    parse_spec(r.spec)


def test_ascii_stl_and_explicit_units():
    r = import_cad(stl_ascii([box((0, 0, 0), (2, 3, 4))]), "block.stl", units="m")
    assert r.spec["parts"][0]["size"] == pytest.approx([2, 3, 4]) and r.report["units"] == "m"


def test_rotated_box_gets_a_yaw():
    v, f = box((0, 0, 0), (6000, 1000, 1000))
    a = math.radians(30)
    rot = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    r = import_cad(stl_binary([(v @ rot.T, f)]), "beam.stl")
    p = r.spec["parts"][0]
    assert (
        p["kind"] == "box"
        and abs(abs(p["yaw_deg"]) % 180 - 30) < 0.5
        and sorted(p["size"])[-1] == pytest.approx(6, rel=1e-3)
    )


def test_open_meshes_become_honest_envelope_boxes():
    v, f = box((0, 0, 0), (1000, 1000, 1000))
    r = import_cad(stl_binary([(v, f[:-2])]), "open.stl")  # two triangles missing: not watertight
    assert r.spec["parts"][0]["kind"] == "box" and r.report["methods"] == {"envelope": 1}
    assert any("not closed" in w for w in r.warnings) and r.spec["parts"][0]["opacity"] == 0.6


def test_obj_with_groups_and_y_up():
    obj = "o tower\n" + "".join(f"v {x} {y} {z}\n" for x, y, z in box((0, 0, 0), (2, 10, 2))[0])
    obj += "".join(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in box((0, 0, 0), (2, 10, 2))[1])
    r = import_cad(obj.encode(), "tower.obj")
    p = r.spec["parts"][0]
    assert p["id"] == "tower" and r.report["up"] == "y" and p["size"][2] == pytest.approx(10), "Y-up OBJ stands upright"


STEP = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('test'),'2;1');
ENDSEC;
DATA;
#1=CARTESIAN_POINT('',(0.,0.,0.));
#2=DIRECTION('',(0.,0.,1.));
#3=DIRECTION('',(1.,0.,0.));
#4=AXIS2_PLACEMENT_3D('',#1,#2,#3);
#5=CYLINDRICAL_SURFACE('',#4,500.);
#6=CIRCLE('',#4,500.);
#7=CARTESIAN_POINT('',(0.,0.,2000.));
#8=AXIS2_PLACEMENT_3D('',#7,#2,#3);
#9=CIRCLE('',#8,500.);
#10=CARTESIAN_POINT('',(500.,0.,0.));
#11=VERTEX_POINT('',#10);
#12=CARTESIAN_POINT('',(500.,0.,2000.));
#13=VERTEX_POINT('',#12);
#14=EDGE_CURVE('',#11,#11,#6,.T.);
#15=EDGE_CURVE('',#13,#13,#9,.T.);
#16=ADVANCED_FACE('',(#14,#15),#5,.T.);
#17=CLOSED_SHELL('',(#16));
#18=MANIFOLD_SOLID_BREP('Column',#17);
#20=CARTESIAN_POINT('',(2000.,0.,0.));
#21=VERTEX_POINT('',#20);
#22=CARTESIAN_POINT('',(3000.,1000.,500.));
#23=VERTEX_POINT('',#22);
#24=PLANE('',#4);
#25=ADVANCED_FACE('',(#21,#23),#24,.T.);
#26=CLOSED_SHELL('',(#25));
#27=MANIFOLD_SOLID_BREP('Plinth',#26);
#30=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );
ENDSEC;
END-ISO-10303-21;
"""


def test_step_solids_units_and_cylinder_detection():
    r = import_cad(STEP.encode(), "parts.step")
    k = by_kind(r.spec)
    assert r.report["units"].startswith("mm") and r.report["bodies"] == 2
    col = k["cylinder"][0]
    assert (
        col["id"] == "Column"
        and col["radius"] == pytest.approx(0.5)
        and col["height"] == pytest.approx(2)
        and col["axis"] == "z"
    )
    assert k["box"][0]["id"] == "Plinth" and k["box"][0]["size"] == pytest.approx([1, 1, 0.5])


def fcstd(objects_xml, data_xml, gui_xml=None, extra=None):
    doc = f'<?xml version="1.0"?><Document SchemaVersion="4"><Objects Count="9">{objects_xml}</Objects><ObjectData Count="9">{data_xml}</ObjectData></Document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Document.xml", doc)
        if gui_xml:
            z.writestr("GuiDocument.xml", gui_xml)
        for n, b in (extra or {}).items():
            z.writestr(n, b)
    return buf.getvalue()


def prop(name, tag, value):
    return f'<Property name="{name}" type="x"><{tag} value="{value}"/></Property>'


def placement(px=0, py=0, pz=0, q=(0, 0, 0, 1)):
    return (f'<Property name="Placement" type="App::PropertyPlacement"><PropertyPlacement Px="{px}" Py="{py}" Pz="{pz}" '
            f'Q0="{q[0]}" Q1="{q[1]}" Q2="{q[2]}" Q3="{q[3]}" A="0" Ox="0" Oy="0" Oz="1"/></Property>')  # fmt: skip


def test_fcstd_primitives_are_exact_with_placement_rotation_and_colour():
    s45 = math.sin(math.radians(22.5))
    objs = ('<Object type="Part::Box" name="Box"/><Object type="Part::Cylinder" name="Cyl"/><Object type="Part::Cone" name="Cone"/>'
            '<Object type="Part::Box" name="Hidden"/><Object type="Part::Cut" name="Cut"/><Object type="Part::Box" name="Operand"/>')  # fmt: skip
    data = (
        f'<Object name="Box"><Properties>{prop("Label", "String", "Base slab")}{prop("Length", "Float", 4000)}{prop("Width", "Float", 2000)}'
        f"{prop('Height', 'Float', 300)}{placement(1000, 0, 0, (0, 0, s45, math.cos(math.radians(22.5))))}</Properties></Object>"
        f'<Object name="Cyl"><Properties>{prop("Radius", "Float", 800)}{prop("Height", "Float", 3000)}{prop("Angle", "Float", 360)}'
        f"{placement(0, 0, 300)}</Properties></Object>"
        f'<Object name="Cone"><Properties>{prop("Radius1", "Float", 200)}{prop("Radius2", "Float", 900)}{prop("Height", "Float", 1000)}'
        f"{placement(0, 0, 3300)}</Properties></Object>"
        f'<Object name="Hidden"><Properties>{prop("Length", "Float", 1)}{prop("Width", "Float", 1)}{prop("Height", "Float", 1)}'
        f"{prop('Visibility', 'Bool', 'false')}</Properties></Object>"
        f'<Object name="Cut"><Properties><Property name="Base" type="App::PropertyLink"><Link value="Operand"/></Property></Properties></Object>'
        f'<Object name="Operand"><Properties>{prop("Length", "Float", 9)}{prop("Width", "Float", 9)}{prop("Height", "Float", 9)}</Properties></Object>'
    )
    gui = '<Document><ViewProviderData><ViewProvider name="Cyl"><Properties><Property name="ShapeColor" type="App::PropertyColor"><PropertyColor value="4278190335"/></Property></Properties></ViewProvider></ViewProviderData></Document>'
    r = import_cad(fcstd(objs, data, gui), "tower.FCStd")
    k = by_kind(r.spec)
    assert r.report["methods"] == {"exact": 3}, "hidden objects and boolean operands are skipped"
    b = k["box"][0]
    assert b["id"] == "Base_slab" and b["size"] == pytest.approx([4, 2, 0.3]) and b["yaw_deg"] == pytest.approx(45)
    c = k["cylinder"][0]
    assert c["radius"] == pytest.approx(0.8) and c["color"] == "#ff0000"
    cone = k["cone"][0]
    assert cone["radius"] == pytest.approx(0.9) and cone["radius2"] == pytest.approx(0.2) and cone["axis"] == "-z", (
        "wide end is the base"
    )
    assert cone["at"][2] == pytest.approx(4.3), "base moved to the top of the FreeCAD cone"


BREP = """DBRep_DrawableShape

CASCADE Topology V1, (c) Matra-Datavision
Locations 1
1
              1               0               0           10000
              0               1               0               0
              0               0               1               0
Curve2ds 0
Curves 1
2 0 0 0 0 0 1 1 0 0 0 1 0 1000
Polygon3D 0
PolygonOnTriangulations 0
Surfaces 1
2 0 0 0 0 0 1 1 0 0 0 1 0 1000
Triangulations 0

TShapes 2
Ve
1e-07
1000 0 0
0 0

0101101
*
Ve
1e-07
1000 0 4000
0 0

0101101
*

+2 1
"""


def test_fcstd_non_parametric_solids_come_from_their_brep():
    objs = '<Object type="Part::Feature" name="Shell"/>'
    data = '<Object name="Shell"><Properties><Property name="Shape" type="Part::PropertyPartShape"><Part file="PartShape.brp"/></Property></Properties></Object>'
    r = import_cad(fcstd(objs, data, extra={"PartShape.brp": BREP}), "imported.FCStd")
    p = r.spec["parts"][0]
    assert p["kind"] == "cylinder" and p["radius"] == pytest.approx(1.0) and p["height"] == pytest.approx(4.0)


DXF = """0
SECTION
2
HEADER
9
$INSUNITS
70
6
0
ENDSEC
0
SECTION
2
ENTITIES
0
CIRCLE
8
TANKS
10
5.0
20
0.0
30
0.0
40
2.0
0
LWPOLYLINE
8
BUILDING
90
4
70
1
10
0.0
20
0.0
10
10.0
20
0.0
10
10.0
20
6.0
10
0.0
20
6.0
39
4.0
0
LINE
8
0
10
0
20
0
11
1
21
1
0
ENDSEC
0
EOF
"""


def test_dxf_footprints_are_extruded():
    r = import_cad(DXF.encode(), "site.dxf", height=8)
    k = by_kind(r.spec)
    assert r.report["units"].startswith("m")
    assert (
        k["cylinder"][0]["radius"] == pytest.approx(2)
        and k["cylinder"][0]["height"] == pytest.approx(8)
        and k["cylinder"][0]["group"] == "TANKS"
    )
    assert k["box"][0]["size"] == pytest.approx([10, 6, 4]), "the polyline's own thickness wins over the default height"
    assert any("LINE" in w for w in r.warnings)


@pytest.mark.parametrize(
    ("data", "name", "needle"),
    [
        (b"hello", "x.xyz", "unsupported"),
        (b"hello", "x.stl", "not an STL"),
        (b"ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;", "x.step", "no solid"),
        (b"not a zip", "x.FCStd", "not a zip"),
        (b"v 0 0 0\nf 1 2 3\n", "x.obj", "missing vertex"),
        (b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n", "x.dxf", "no circles"),
        (b"anything", "x.igs", "FreeCAD worker"),
    ],
)
def test_bad_files_are_rejected_with_a_reason(data, name, needle):
    with pytest.raises(ImportError_) as e:
        import_cad(data, name)
    assert needle in str(e.value)


def test_zip_bombs_and_huge_extents_are_refused(monkeypatch):
    from duosim.design import cad_import

    monkeypatch.setattr(cad_import, "MAX_UNZIPPED", 4096)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Document.xml", "<Document/>" + " " * 100_000)  # compresses to almost nothing
    with pytest.raises(ImportError_, match="unpacks to more than"):
        import_cad(buf.getvalue(), "bomb.FCStd")
    with pytest.raises(ImportError_, match="check the units"):
        import_cad(stl_binary([box((0, 0, 0), (5000, 10, 10))]), "far.stl", units="m")


def test_worker_measurement_script_and_report_mapping():
    script = measure_script(b"ISO-10303-21; '); import os; os.system('x') #", ".step")
    assert "os.system('x')" not in script, "file content only travels base64-encoded"
    with pytest.raises(ImportError_):
        measure_script(b"x", ".py")
    report = [{"name": "solid1", "volume": math.pi * 500**2 * 2000, "points": [[500, 0, 0], [-500, 0, 0], [0, 500, 0], [0, -500, 2000]],
               "hints": {"cylinder": [[500, [0, 0, 1]]]}, "bbox": [-500, -500, 0, 500, 500, 2000]}]  # fmt: skip
    r = import_cad(b"", "part.step", bodies=bodies_from_report(report))
    p = r.spec["parts"][0]
    assert p["kind"] == "cylinder" and p["radius"] == pytest.approx(0.5) and r.report["methods"] == {"fitted": 1}


# --------------------------------------------------------------------------- HTTP route
@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSIM_DEV_OPEN", raising=False)
    store = Store(tmp_path / "p.sqlite3")
    monkeypatch.setattr(api_design, "_STORE", store)
    monkeypatch.setattr(api_design, "get_builder", lambda: None)
    monkeypatch.setattr(api_design, "get_client", lambda: None)
    key = store.create_api_key(store.workspace("acme"), "t")[1]
    return TestClient(server.app), {"X-API-Key": key}


def test_import_route_charges_persists_and_refunds_bad_files(http):
    c, k = http
    r = c.post("/api/design/import?filename=plant.stl", content=stl_binary(PLANT), headers=k)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["import"]["parts"] == 4 and j["credits"]["balance"] == 48 and j["build"]["built"] is False
    assert c.get(f"/api/design/{j['id']}", headers=k).json()["summary"]["import"]["format"] == "stl"
    bad = c.post("/api/design/import?filename=x.stl", content=b"garbage", headers=k)
    assert bad.status_code == 422 and c.get("/api/design/status", headers=k).json()["credits"] == 48, "refunded"
    assert c.post("/api/design/import?filename=x.stl", content=b"", headers=k).status_code == 422
    assert c.post("/api/design/import?filename=x.stl&height=0", content=b"x", headers=k).status_code == 422


def test_import_route_size_limit(http, monkeypatch):
    c, k = http
    monkeypatch.setenv("DUOSIM_MAX_IMPORT_MB", "1")
    r = c.post("/api/design/import?filename=big.stl", content=b"x" * (1024 * 1024 + 10), headers=k)
    assert r.status_code == 413


def test_import_then_delete(http):
    c, k = http
    did = c.post(
        "/api/design/import?filename=a.stl", content=stl_binary([box((0, 0, 0), (1, 1, 1))]), headers={**k}
    ).json()
    assert "id" in did, json.dumps(did)[:200]
    assert c.delete(f"/api/design/{did['id']}", headers=k).status_code == 204
    assert c.get(f"/api/design/{did['id']}", headers=k).status_code == 404
