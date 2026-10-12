"""Spec -> FreeCAD geometry, built through the FreeCAD-MCP addon and *verified against the spec*.

Only validated numbers and identifiers (see spec.py) are ever formatted into the script that
runs inside FreeCAD, so a prompt cannot smuggle code into `execute_code`.

After building, every part is measured in FreeCAD (volume, centre of mass, shape validity)
and compared with the closed-form value computed from the spec.  That check, not the absence
of an exception, is what `BuildResult.ok` means.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Any

from ..freecad_bridge import FreeCADClient
from .spec import AXES, Part, Spec, analytic_centroid, analytic_volume

VOL_REL_TOL = 1e-6
CENTROID_TOL_M = 1e-3  # 1 mm
MM = 1000.0
log = logging.getLogger("cemsim.design")


# --------------------------------------------------------------------------- script generation
def _f(x: float) -> str:
    return repr(float(x * MM))


def _v(p) -> str:
    return f"V({_f(p[0])}, {_f(p[1])}, {_f(p[2])})"


def _part_line(p: Part) -> str:
    d = AXES.get(p.axis, AXES["z"])
    dirs = f"V({d[0]!r}, {d[1]!r}, {d[2]!r})"
    at = _v(p.at)
    if p.kind == "box":
        assert p.size
        shape = f"Part.makeBox({_f(p.size[0])}, {_f(p.size[1])}, {_f(p.size[2])}, {at})"
    elif p.kind == "cylinder":
        shape = f"Part.makeCylinder({_f(p.radius)}, {_f(p.height)}, {at}, {dirs})"
    elif p.kind == "cone":
        shape = f"Part.makeCone({_f(p.radius)}, {_f(p.radius2 or 0.0)}, {_f(p.height)}, {at}, {dirs})"
    elif p.kind == "sphere":
        shape = f"Part.makeSphere({_f(p.radius)}, {at})"
    elif p.kind == "tube":
        shape = (
            f"Part.makeCylinder({_f(p.radius)}, {_f(p.height)}, {at}, {dirs})"
            f".cut(Part.makeCylinder({_f(p.inner_radius)}, {_f(p.height)}, {at}, {dirs}))"
        )
    else:  # pipe
        pts = "[" + ", ".join(_v(q) for q in p.path) + "]"
        shape = f"_pipe({_f(p.radius)}, {pts})"
    rgb = tuple(round(int(p.color[i : i + 2], 16) / 255.0, 4) for i in (1, 3, 5))
    pivot = _v(p.at) if p.kind != "pipe" else _v(p.path[0])
    return f"_put({p.id!r}, {shape}, {rgb!r}, {float(p.opacity)!r}, {p.group!r}, {float(p.yaw_deg)!r}, {pivot})"


_PRELUDE = """\
import FreeCAD as App, Part, json
V = App.Vector
_doc = App.newDocument({doc!r})
_gui = bool(App.GuiUp)

def _put(pid, shape, rgb, opacity, group, yaw, pivot):
    if yaw:
        shape.rotate(pivot, V(0, 0, 1), yaw)
    o = _doc.addObject("Part::Feature", pid)
    o.Shape = shape
    o.Label2 = group
    if _gui and o.ViewObject is not None:
        o.ViewObject.ShapeColor = rgb
        o.ViewObject.Transparency = int(round((1.0 - opacity) * 100))
    return o

def _pipe(r, pts):
    segs = []
    for a, b in zip(pts, pts[1:]):
        d = b - a
        segs.append(Part.makeCylinder(r, d.Length, a, d))
    for p in pts[1:-1]:
        segs.append(Part.makeSphere(r, p))
    s = segs[0]
    for x in segs[1:]:
        s = s.fuse(x)
    return s.removeSplitter()
"""

_EPILOGUE = """\
_doc.recompute()
_rep = {"doc": _doc.Name, "gui": _gui, "parts": []}
for o in _doc.Objects:
    s = o.Shape
    sol = s.Solids
    vt = sum(x.Volume for x in sol)
    # compounds (boolean results) have no CenterOfMass: volume-weighted over their solids
    c = [sum(x.CenterOfMass[k] * x.Volume for x in sol) / vt for k in range(3)] if vt > 0 else [0.0, 0.0, 0.0]
    bb = s.BoundBox
    _rep["parts"].append({
        "id": o.Name, "volume": s.Volume, "centroid": c, "valid": bool(s.isValid()),
        "solids": len(s.Solids), "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax],
    })
print("DESIGN_REPORT " + json.dumps(_rep))
"""


def build_script(spec: Spec, doc_name: str) -> str:
    """FreeCAD code that creates `doc_name` from the (already validated) spec and prints a report."""
    return _PRELUDE.format(doc=doc_name) + "\n".join(_part_line(p) for p in spec.parts) + "\n" + _EPILOGUE


_EXPORT = """\
import FreeCAD as App, Part, json, base64, zlib, os, tempfile, shutil
_doc = App.getDocument({doc!r})
_objs = [o for o in _doc.Objects if hasattr(o, "Shape")]
_tmp = tempfile.mkdtemp(prefix="cemsim-export-")
_out = {{}}
def _b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()
try:
    if "step" in {want!r}:
        p = os.path.join(_tmp, "design.step"); Part.export(_objs, p); _out["step"] = _b64(p)
    if "fcstd" in {want!r}:
        p = os.path.join(_tmp, "design.FCStd"); _doc.saveAs(p); _out["fcstd"] = _b64(p)
    if "mesh" in {want!r}:
        objs = []
        for o in _objs:
            sh = o.Shape
            diag = sh.BoundBox.DiagonalLength
            tol = max(1.0, diag * 0.002)
            v, idx = [], []
            for f in sh.Faces:
                pts, tris = f.tessellate(tol)
                base = len(v) // 3
                for q in pts:
                    v.extend((round(q[0]), round(q[2]), round(-q[1])))   # mm, three.js axes (Y up)
                for t in tris:
                    idx.extend((base + t[0], base + t[1], base + t[2]))
            col, tr = (0.6, 0.62, 0.65), 0
            if App.GuiUp and o.ViewObject is not None:
                col = tuple(round(float(c), 3) for c in o.ViewObject.ShapeColor[:3])
                tr = int(o.ViewObject.Transparency)
            objs.append({{"name": o.Name, "group": o.Label2, "color": col,
                         "opacity": round(1 - tr / 100.0, 2), "v": v, "i": idx}})
        raw = json.dumps({{"meta": {{"units": "mm, three.js axes (Y up)"}}, "objects": objs}}, separators=(",", ":"))
        _out["mesh"] = base64.b64encode(zlib.compress(raw.encode(), 6)).decode()
finally:
    shutil.rmtree(_tmp, ignore_errors=True)
print("ARTIFACTS " + json.dumps(_out))
"""


def export_script(doc_name: str, want=("step", "fcstd", "mesh")) -> str:
    return _EXPORT.format(doc=doc_name, want=tuple(want))


def _marker_json(stdout: str, marker: str) -> Any:
    for line in reversed(stdout.splitlines()):
        if line.startswith(marker + " "):
            return json.loads(line[len(marker) + 1 :])
    raise RuntimeError(f"FreeCAD did not report {marker}: {stdout[-300:]!r}")


# --------------------------------------------------------------------------- verification
@dataclass
class PartCheck:
    id: str
    kind: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    volume_m3: float | None = None
    expected_volume_m3: float | None = None
    centroid_error_m: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "ok": self.ok,
            "problems": self.problems,
            "volume_m3": self.volume_m3,
            "expected_volume_m3": self.expected_volume_m3,
            "centroid_error_m": self.centroid_error_m,
        }


def verify(spec: Spec, report: dict[str, Any]) -> list[PartCheck]:
    """Compare what FreeCAD built with what the spec says (closed form)."""
    by_id = {p["id"]: p for p in report["parts"]}
    out: list[PartCheck] = []
    for part in spec.parts:
        c = PartCheck(part.id, part.kind, ok=True)
        r = by_id.get(part.id)
        if r is None:
            c.ok, c.problems = False, ["object missing in FreeCAD"]
            out.append(c)
            continue
        if not r["valid"]:
            c.problems.append("shape failed OCCT validity check")
        if r["solids"] < 1:
            c.problems.append("no solid produced")
        c.volume_m3 = r["volume"] / MM**3
        c.expected_volume_m3 = analytic_volume(part)
        if c.expected_volume_m3 is not None:
            if abs(c.volume_m3 - c.expected_volume_m3) > VOL_REL_TOL * c.expected_volume_m3:
                c.problems.append(
                    f"volume {c.volume_m3:.6g} m3 != expected {c.expected_volume_m3:.6g} m3 "
                    f"(rel. error {abs(c.volume_m3 / c.expected_volume_m3 - 1):.2e})"
                )
        elif c.volume_m3 <= 0:
            c.problems.append("pipe has no volume")
        exp_c = analytic_centroid(part)
        if exp_c is not None and r["solids"]:
            got = [x / MM for x in r["centroid"]]
            c.centroid_error_m = max(abs(a - b) for a, b in zip(got, exp_c, strict=True))
            if c.centroid_error_m > CENTROID_TOL_M:
                c.problems.append(
                    f"centre of mass off by {c.centroid_error_m:.4f} m: got {[round(x, 4) for x in got]}, "
                    f"expected {[round(x, 4) for x in exp_c]}"
                )
        c.ok = not c.problems
        out.append(c)
    extra = set(by_id) - {p.id for p in spec.parts}
    if extra:  # should be impossible; flag rather than ignore
        out.append(PartCheck("*", "doc", False, [f"unexpected objects in document: {sorted(extra)}"]))
    return out


# --------------------------------------------------------------------------- orchestration
@dataclass
class BuildResult:
    doc: str
    ok: bool
    checks: list[PartCheck]
    bbox_m: list[float]
    total_volume_m3: float
    gui: bool
    png: bytes | None = None
    artifacts: dict[str, bytes] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)  # raw measurements from FreeCAD

    def summary(self) -> dict[str, Any]:
        bad = [c for c in self.checks if not c.ok]
        return {
            "doc": self.doc,
            "ok": self.ok,
            "parts": len(self.checks),
            "failed_parts": [c.to_dict() for c in bad],
            "bbox_m": self.bbox_m,
            "total_volume_m3": self.total_volume_m3,
            "timings_s": self.timings,
            "artifacts": {k: len(v) for k, v in self.artifacts.items()},
            "screenshot": self.png is not None,
        }


class DesignBuilder:
    """Builds specs in one FreeCAD worker.  Calls are serialised: the addon's screenshot targets
    the *active* document, so a build + capture + export must not interleave with another."""

    def __init__(self, client: FreeCADClient, keep_docs: int = 8):
        self.client = client
        self.keep_docs = keep_docs
        self._lock = threading.Lock()
        self._docs: list[str] = []

    def build(
        self,
        spec: Spec,
        *,
        screenshot: bool = True,
        export: tuple[str, ...] = ("step", "fcstd", "mesh"),
        size: tuple[int, int] = (1280, 720),
    ) -> BuildResult:
        doc = "D" + uuid.uuid4().hex[:10]
        with self._lock:
            t0 = time.perf_counter()
            out = self.client.execute(build_script(spec, doc), slow=True)
            report = _marker_json(out, "DESIGN_REPORT")
            t1 = time.perf_counter()
            self._remember(doc)
            checks = verify(spec, report)

            xs = [p["bbox"] for p in report["parts"] if p["solids"]]
            bbox = (
                [
                    min(b[0] for b in xs) / MM,
                    min(b[1] for b in xs) / MM,
                    min(b[2] for b in xs) / MM,
                    max(b[3] for b in xs) / MM,
                    max(b[4] for b in xs) / MM,
                    max(b[5] for b in xs) / MM,
                ]
                if xs
                else [0.0] * 6
            )
            res = BuildResult(
                doc=doc,
                ok=all(c.ok for c in checks),
                checks=checks,
                bbox_m=[round(x, 4) for x in bbox],
                total_volume_m3=sum(p["volume"] for p in report["parts"]) / MM**3,
                gui=bool(report.get("gui")),
                report=report,
            )
            res.timings["build"] = round(t1 - t0, 3)
            if screenshot and res.gui:
                t = time.perf_counter()
                res.png = self.client.screenshot("Isometric", size[0], size[1], doc=doc)
                res.timings["screenshot"] = round(time.perf_counter() - t, 3)
            if export:
                t = time.perf_counter()
                out = self.client.execute(export_script(doc, export), slow=True)
                for k, b64 in _marker_json(out, "ARTIFACTS").items():
                    raw = base64.b64decode(b64)
                    res.artifacts[k] = zlib.decompress(raw) if k == "mesh" else raw
                res.timings["export"] = round(time.perf_counter() - t, 3)
            return res

    def _remember(self, doc: str) -> None:
        self._docs.append(doc)
        while len(self._docs) > self.keep_docs:
            old = self._docs.pop(0)
            try:
                self.client.execute(
                    f"import FreeCAD\nif {old!r} in FreeCAD.listDocuments(): FreeCAD.closeDocument({old!r})"
                )
            except Exception as e:  # best effort: a worker restart already dropped it
                log.info("could not close design document %s: %s", old, e)
