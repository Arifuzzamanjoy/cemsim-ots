"""CAD / mesh files -> a validated design spec (the same JSON the language model writes).

Supported without FreeCAD (pure Python + numpy/scipy):

  .stl (ASCII / binary), .obj   meshes: welded, split into connected bodies, each body fitted to a primitive
  .step / .stp                  B-rep text: one body per MANIFOLD_SOLID_BREP, envelope from its vertices and
                                circles, primitive type from its analytic surfaces (cylinder / cone / sphere)
  .fcstd                        FreeCAD documents: parametric primitives (Part::Box / Cylinder / Cone / Sphere /
                                Tube) are read *exactly* with their placement and colour; other solids from their
                                BREP (vertex + circle envelope)
  .dxf                          2-D layouts: circles -> cylinders, closed polylines -> boxes, extruded by their
                                thickness or `height`

With a FreeCAD worker (`measure_with_worker`), STEP / IGES / BREP bodies are measured exactly by OpenCASCADE
(volume, bounding box, surface types) before fitting.  An untrusted .FCStd is never *opened* in FreeCAD
(documents can reference Python modules); only its geometry streams are.

Primitive fitting.  Each body is described by an oriented envelope (yaw from the principal axes of its
footprint) and, when it is closed, its volume.  The fill ratio V / V_envelope separates the primitives:
box ~1, cylinder pi/4 = 0.785, sphere pi/6 = 0.524, pointed cone pi/12 = 0.262, and a cross-section that is
square with a radius profile that changes along the axis is a cone (frustum).  The report lists, per part,
how it was obtained (exact / fitted / envelope) and the volume error of the fit, so nothing approximate is
presented as exact.
"""

from __future__ import annotations

import contextlib
import io
import math
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np

from .spec import COORD_LIMIT, DIM_MIN, MAX_PARTS, parse_spec

FORMATS = {".stl": "stl", ".obj": "obj", ".step": "step", ".stp": "step", ".fcstd": "fcstd", ".dxf": "dxf"}
WORKER_FORMATS = {".iges": "iges", ".igs": "iges", ".brep": "brep", ".brp": "brep"}
MAX_TRIANGLES = 3_000_000
MAX_UNZIPPED = 200 * 1024 * 1024
UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048}
KIND_COLORS = {"box": "#8d9399", "cylinder": "#6f8fb5", "cone": "#7f9cc0", "sphere": "#c0763a", "tube": "#d4ac0d"}
_AXES = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1), "-x": (-1, 0, 0), "-y": (0, -1, 0), "-z": (0, 0, -1)}


class ImportError_(ValueError):
    """The file cannot be imported; the message is safe to show."""


@dataclass
class Body:
    """One solid, in source units and source axes until `normalise` runs."""

    name: str
    points: np.ndarray | None = None  # (n, 3) sample of surface points (vertices), for envelopes and profiles
    volume: float | None = None  # signed-volume of a closed mesh / exact volume from a kernel
    closed: bool = False
    hints: dict[str, Any] = field(default_factory=dict)  # surfaces: {"cylinder": [(r, axis)], "cone": n, ...}
    exact: dict[str, Any] | None = None  # an exactly known primitive part (spec fields, source units)
    color: str | None = None
    group: str = "imported"


@dataclass
class ImportResult:
    spec: dict[str, Any]
    warnings: list[str]
    report: dict[str, Any]


# =========================================================================== entry point
def import_cad(
    data: bytes,
    filename: str,
    *,
    units: str = "auto",
    up: str = "auto",
    height: float = 3.0,
    ground: bool = True,
    bodies: list[Body] | None = None,
) -> ImportResult:
    """Parse `data` (the file's bytes) and fit a spec.  `bodies` (from a FreeCAD worker) skips parsing."""
    ext = PurePath(filename or "").suffix.lower()
    fmt = FORMATS.get(ext) or WORKER_FORMATS.get(ext)
    if fmt is None:
        raise ImportError_(
            f"unsupported file type {ext or '(none)'}; use STL, OBJ, STEP, FCStd or DXF (IGES / BREP need a FreeCAD worker)"
        )
    warns: list[str] = []
    file_units: float | None = None
    if bodies is None:
        if fmt in ("iges", "brep"):
            raise ImportError_(f"{ext} files are read by OpenCASCADE: this server needs a FreeCAD worker for them")
        parser = {"stl": _parse_stl, "obj": _parse_obj, "step": _parse_step, "fcstd": _parse_fcstd, "dxf": _parse_dxf}
        bodies, file_units = parser[fmt](data, warns, height)
    else:
        file_units = 0.001  # OpenCASCADE reports millimetres whatever the file declared
    if not bodies:
        raise ImportError_("no solid bodies found in the file")
    default_up = "y" if fmt == "obj" else "z"
    scale, unit_note = _pick_units(units, file_units, bodies)
    up_axis = default_up if up == "auto" else up
    if up_axis not in ("z", "y"):
        raise ImportError_("up must be auto, z or y")
    parts, stats = _fit_all(bodies, scale, up_axis, ground, warns)
    if not parts:
        raise ImportError_("every body was smaller than 5 mm after unit conversion - check the units")
    name = re.sub(r"[^A-Za-z0-9 _\-.]", "", PurePath(filename).stem)[:60] or "Imported model"
    spec = {"name": name, "description": f"Imported from {PurePath(filename).name} ({fmt.upper()})", "parts": parts}
    parsed, spec_warns = parse_spec(spec)  # the fitter must only ever produce valid specs
    report = {
        "format": fmt,
        "units": unit_note,
        "up": up_axis,
        "bodies": len(bodies),
        **stats,
    }
    return ImportResult(parsed.to_dict(), warns + spec_warns, report)


def _pick_units(units: str, file_units: float | None, bodies: list[Body]) -> tuple[float, str]:
    if units != "auto":
        if units not in UNITS:
            raise ImportError_(f"units must be auto or one of {', '.join(UNITS)}")
        return UNITS[units], units
    if file_units is not None:
        inv = {v: k for k, v in UNITS.items()}
        return file_units, inv.get(file_units, f"{file_units:g} m") + " (from file)"
    ext = _extent(bodies)
    return (0.001, "mm (assumed: model is larger than 200 units)") if ext > 200 else (1.0, "m (assumed)")


def _extent(bodies: list[Body]) -> float:
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for b in bodies:
        if b.points is not None and len(b.points):
            lo, hi = np.minimum(lo, b.points.min(0)), np.maximum(hi, b.points.max(0))
        elif b.exact is not None:
            a = np.asarray(b.exact["at"], float)
            lo, hi = np.minimum(lo, a), np.maximum(hi, a)
    return float(np.max(hi - lo)) if np.all(np.isfinite(lo)) else 0.0


# =========================================================================== meshes
def _weld(v: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    span = float(np.ptp(v, axis=0).max()) or 1.0
    q = np.round(v / (span * 1e-7)).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    return v[first], inv.reshape(-1)[f]


def _mesh_bodies(v: np.ndarray, f: np.ndarray, names: list[str] | None, warns: list[str]) -> list[Body]:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if len(f) == 0:
        return []
    if len(f) > MAX_TRIANGLES:
        raise ImportError_(f"mesh has {len(f):,} triangles (limit {MAX_TRIANGLES:,}); decimate it first")
    v, f = _weld(v.astype(float), f)
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]  # drop degenerate triangles
    n = len(v)
    a = np.concatenate([f[:, 0], f[:, 1]])
    b = np.concatenate([f[:, 1], f[:, 2]])
    ncomp, label = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)), directed=False)
    face_label = label[f[:, 0]]
    order = np.argsort(face_label, kind="stable")
    bounds = np.searchsorted(face_label[order], np.arange(ncomp + 1))
    bodies = []
    for ci in range(ncomp):
        ff = f[order[bounds[ci] : bounds[ci + 1]]]
        if len(ff) == 0:
            continue
        idx = np.unique(ff)
        p0, p1, p2 = v[ff[:, 0]], v[ff[:, 1]], v[ff[:, 2]]
        vol = float(np.einsum("ij,ij->i", p0, np.cross(p1, p2)).sum() / 6.0)
        edges = np.sort(np.concatenate([ff[:, [0, 1]], ff[:, [1, 2]], ff[:, [2, 0]]]), axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        closed = bool(np.all(counts == 2))
        name = f"body{ci + 1}"
        if names is not None:
            nm = Counter(names[i] for i in order[bounds[ci] : bounds[ci + 1]][:200]).most_common(1)
            if nm and nm[0][0]:
                name = nm[0][0]
        bodies.append(Body(name, v[idx], abs(vol) if closed else None, closed))
    open_n = sum(not b.closed for b in bodies)
    if open_n:
        warns.append(f"{open_n} body(ies) are not closed (watertight): they are imported as envelope boxes")
    return bodies


def _parse_stl(data: bytes, warns: list[str], _h: float) -> tuple[list[Body], float | None]:
    if len(data) >= 84:
        (n,) = struct.unpack("<I", data[80:84])
        if 84 + 50 * n == len(data):
            if n > MAX_TRIANGLES:
                raise ImportError_(f"mesh has {n:,} triangles (limit {MAX_TRIANGLES:,}); decimate it first")
            rec = np.frombuffer(
                data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84
            )
            v = rec["v"].reshape(-1, 3).astype(float)
            return _mesh_bodies(v, np.arange(len(v)).reshape(-1, 3), None, warns), None
    text = data.decode("latin-1")
    if not text.lstrip().lower().startswith("solid"):
        raise ImportError_("not an STL file (neither binary nor ASCII)")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not nums or len(nums) % 3:
        raise ImportError_("ASCII STL has no complete triangles")
    try:
        v = np.array(nums, dtype=float)
    except ValueError:
        raise ImportError_("ASCII STL has non-numeric vertices") from None
    return _mesh_bodies(v, np.arange(len(v)).reshape(-1, 3), None, warns), None


def _parse_obj(data: bytes, warns: list[str], _h: float) -> tuple[list[Body], float | None]:
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    names: list[str] = []
    group = ""
    for raw in data.decode("utf-8", "replace").splitlines():
        s = raw.split()
        if not s:
            continue
        if s[0] == "v" and len(s) >= 4:
            try:
                verts.append((float(s[1]), float(s[2]), float(s[3])))
            except ValueError:
                raise ImportError_("OBJ vertex is not numeric") from None
        elif s[0] in ("o", "g") and len(s) > 1:
            group = s[1][:40]
        elif s[0] == "f" and len(s) >= 4:
            try:
                idx = [int(t.split("/")[0]) for t in s[1:]]
            except ValueError:
                raise ImportError_("OBJ face index is not an integer") from None
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            if any(not 0 <= i < len(verts) for i in idx):
                raise ImportError_("OBJ face refers to a missing vertex")
            for k in range(1, len(idx) - 1):  # fan triangulation of polygons
                faces.append((idx[0], idx[k], idx[k + 1]))
                names.append(group)
            if len(faces) > MAX_TRIANGLES:
                raise ImportError_(f"mesh has more than {MAX_TRIANGLES:,} triangles; decimate it first")
    if not faces:
        raise ImportError_("OBJ file has no faces")
    return _mesh_bodies(np.array(verts, float), np.array(faces, np.int64), names, warns), None


# =========================================================================== STEP (ISO 10303-21)
_ENTITY = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]*)\s*\((.*?)\)\s*;(?=\s*(?:#\d+\s*=|ENDSEC))", re.S)
_REF = re.compile(r"#(\d+)")
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?")


def _step_units(text: str) -> float | None:
    if re.search(r"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)", text):
        return 0.001
    if re.search(r"SI_UNIT\s*\(\s*\.CENTI\.\s*,\s*\.METRE\.\s*\)", text):
        return 0.01
    if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", text, re.I):
        return 0.0254
    if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'FOOT'", text, re.I):
        return 0.3048
    if re.search(r"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.\s*\)", text):
        return 1.0
    return None


def _parse_step(data: bytes, warns: list[str], _h: float) -> tuple[list[Body], float | None]:
    text = data.decode("latin-1")
    if "ISO-10303-21" not in text[:200]:
        raise ImportError_("not a STEP file (missing ISO-10303-21 header)")
    a, b = text.find("DATA;"), text.rfind("ENDSEC;")
    if a < 0 or b < a:
        raise ImportError_("STEP file has no DATA section")
    ents: dict[int, tuple[str, str]] = {}
    for m in _ENTITY.finditer(text[a + 5 : b + 7]):
        ents[int(m.group(1))] = (m.group(2), m.group(3))
    if not ents:
        raise ImportError_("STEP DATA section has no entities")

    def nums(eid: int) -> list[float]:
        return [float(x) for x in _NUM.findall(ents[eid][1].split(",(", 1)[-1])] if eid in ents else []

    def point(eid: int) -> np.ndarray | None:
        t = ents.get(eid)
        if t is None or t[0] != "CARTESIAN_POINT":
            return None
        v = [float(x) for x in _NUM.findall(t[1][t[1].find("(") :])]
        return np.array([*v, 0.0, 0.0, 0.0][:3])

    def direction(eid: int) -> np.ndarray | None:
        t = ents.get(eid)
        if t is None or t[0] != "DIRECTION":
            return None
        v = [float(x) for x in _NUM.findall(t[1][t[1].find("(") :])]
        d = np.array([*v, 0.0, 0.0, 1.0][:3])
        n = np.linalg.norm(d)
        return d / n if n else None

    def placement(eid: int) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
        t = ents.get(eid)
        if t is None or not t[0].startswith("AXIS2_PLACEMENT_3D"):
            return None, None, None
        refs = [int(r) for r in _REF.findall(t[1])]
        loc = point(refs[0]) if refs else None
        ax = direction(refs[1]) if len(refs) > 1 else np.array([0.0, 0.0, 1.0])
        ref = direction(refs[2]) if len(refs) > 2 else None
        return loc, ax, ref

    def radius_of(args: str) -> float | None:
        tail = args.rsplit(",", 1)[-1]
        try:
            return float(tail.strip(" )"))
        except ValueError:
            return None

    roots = [i for i, (t, _) in ents.items() if t in ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS")]
    if not roots:
        raise ImportError_(
            "STEP file contains no solid bodies (MANIFOLD_SOLID_BREP); surfaces-only files cannot be imported"
        )
    bodies = []
    for root in roots[: MAX_PARTS * 2]:
        name_m = re.match(r"\s*'([^']*)'", ents[root][1])
        seen: set[int] = set()
        stack = [root]
        pts: list[np.ndarray] = []
        hints: dict[str, Any] = {"cylinder": [], "cone": [], "sphere": [], "other": 0, "vertices": 0}
        while stack:
            e = stack.pop()
            if e in seen or e not in ents:
                continue
            seen.add(e)
            typ, args = ents[e]
            if typ == "VERTEX_POINT":
                refs = _REF.findall(args)
                p = point(int(refs[0])) if refs else None
                if p is not None:
                    pts.append(p)
                    hints["vertices"] += 1
                continue
            if typ == "CIRCLE":
                refs = _REF.findall(args)
                loc, ax, ref = placement(int(refs[0])) if refs else (None, None, None)
                r = radius_of(args)
                if loc is not None and ax is not None and r:
                    pts += _circle(loc, ax, ref, r)
                continue
            if typ in ("CYLINDRICAL_SURFACE", "CONICAL_SURFACE", "SPHERICAL_SURFACE"):
                refs = _REF.findall(args)
                loc, ax, _ = placement(int(refs[0])) if refs else (None, None, None)
                r = radius_of(args) if typ != "CONICAL_SURFACE" else None
                key = {"CYLINDRICAL_SURFACE": "cylinder", "CONICAL_SURFACE": "cone", "SPHERICAL_SURFACE": "sphere"}[typ]
                hints[key].append((r, None if ax is None else tuple(ax)))
                continue
            if typ in (
                "TOROIDAL_SURFACE",
                "B_SPLINE_SURFACE_WITH_KNOTS",
                "SURFACE_OF_REVOLUTION",
                "SURFACE_OF_LINEAR_EXTRUSION",
            ):
                hints["other"] += 1
            if typ in ("CARTESIAN_POINT", "DIRECTION"):
                continue
            stack.extend(int(r) for r in _REF.findall(args))
        if pts:
            bodies.append(
                Body(
                    name_m.group(1) if name_m and name_m.group(1) else f"solid{len(bodies) + 1}",
                    np.array(pts),
                    hints=hints,
                )
            )
    if any(t == "ITEM_DEFINED_TRANSFORMATION" for t, _ in ents.values()):
        warns.append(
            "the STEP file is an assembly with instance transforms; part placements are taken as stored "
            "(use a FreeCAD worker for exact assembly placement)"
        )
    if len(roots) > MAX_PARTS * 2:
        warns.append(f"only the first {MAX_PARTS * 2} of {len(roots)} solids were read")
    return bodies, _step_units(text)


def _circle(c: np.ndarray, axis: np.ndarray, ref: np.ndarray | None, r: float, n: int = 16) -> list[np.ndarray]:
    """Points on a circle (B-rep vertices alone miss the extent of round faces: a full cylinder has one seam)."""
    a = axis / (np.linalg.norm(axis) or 1.0)
    x = (
        ref
        if ref is not None and abs(float(np.dot(ref, a))) < 0.99
        else (np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0]))
    )
    x = x - np.dot(x, a) * a
    x = x / (np.linalg.norm(x) or 1.0)
    y = np.cross(a, x)
    return [c + r * (math.cos(t) * x + math.sin(t) * y) for t in np.linspace(0, 2 * math.pi, n, endpoint=False)]


# =========================================================================== OpenCASCADE BREP text (inside .FCStd)
def _brep_points(text: str) -> tuple[np.ndarray | None, dict[str, Any]]:
    lines = text.splitlines()
    pts: list[np.ndarray] = []
    hints: dict[str, Any] = {"cylinder": [], "cone": [], "sphere": [], "other": 0, "vertices": 0}

    def section(title: str) -> int:
        for i, ln in enumerate(lines):
            if ln.startswith(title):
                return i
        return -1

    i = section("Curves ")
    if i >= 0:
        for ln in lines[i + 1 : i + 1 + int(lines[i].split()[1])]:
            v = ln.split()
            if v and v[0] == "2" and len(v) >= 14:  # circle: centre, axis, x, y, radius
                c, ax, r = np.array(v[1:4], float), np.array(v[4:7], float), float(v[13])
                pts += _circle(c, ax, np.array(v[7:10], float), r)
    i = section("Surfaces ")
    if i >= 0:
        for ln in lines[i + 1 : i + 1 + int(lines[i].split()[1])]:
            v = ln.split()
            if not v:
                continue
            if v[0] == "2" and len(v) >= 14:
                hints["cylinder"].append((float(v[13]), tuple(float(x) for x in v[4:7])))
            elif v[0] == "3":
                hints["cone"].append((None, tuple(float(x) for x in v[4:7])))
            elif v[0] == "4" and len(v) >= 14:
                hints["sphere"].append((float(v[13]), None))
            elif v[0] not in ("1",):
                hints["other"] += 1
    i = section("TShapes ")
    if i >= 0:
        for k in range(i + 1, len(lines) - 2):
            if lines[k].strip() == "Ve":
                try:
                    pts.append(np.array(lines[k + 2].split()[:3], float))
                    hints["vertices"] += 1
                except ValueError:
                    pass
    # the root shape's location: the last line is "<orientation><tshape index> <location index>"
    loc = np.eye(4)
    j = section("Locations ")
    try:
        root_loc = int(lines[-1].split()[-1]) if lines and lines[-1].split() else 0
    except ValueError:
        root_loc = 0
    if j >= 0 and root_loc > 0:
        mats, k = [], j + 1
        while k < len(lines) and len(mats) < root_loc:
            if lines[k].strip() == "1" and k + 3 < len(lines):
                rows = [np.array(lines[k + r].split()[:4], float) for r in (1, 2, 3)]
                mats.append(np.vstack([*rows, [0, 0, 0, 1]]))
                k += 4
            elif lines[k].strip().startswith("2"):
                mats.append(np.eye(4))  # composed locations are rare at the root; treat as identity
                k += 1
            else:
                k += 1
        if len(mats) >= root_loc:
            loc = mats[root_loc - 1]
    if not pts:
        return None, hints
    p = np.array(pts)
    return p @ loc[:3, :3].T + loc[:3, 3], hints


# =========================================================================== FreeCAD .FCStd
def _quat_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _nearest_axis(d: np.ndarray, tol: float = 1e-3) -> str | None:
    for name, a in _AXES.items():
        if np.allclose(d, a, atol=tol):
            return name
    return None


def _fc_color(value: str | None) -> str | None:
    try:
        v = int(value or "")
    except ValueError:
        return None
    return f"#{(v >> 24) & 255:02x}{(v >> 16) & 255:02x}{(v >> 8) & 255:02x}"


def _parse_fcstd(data: bytes, warns: list[str], _h: float) -> tuple[list[Body], float | None]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_("not a FreeCAD .FCStd file (not a zip archive)") from None
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise ImportError_(f"the .FCStd file unpacks to more than {MAX_UNZIPPED // (1024 * 1024)} MB")
    try:
        doc = ET.fromstring(z.read("Document.xml"))  # noqa: S314 - see import note
    except KeyError:
        raise ImportError_("the .FCStd archive has no Document.xml") from None
    except ET.ParseError as e:
        raise ImportError_(f"Document.xml is not valid XML ({e})") from None
    colors: dict[str, str] = {}
    if "GuiDocument.xml" in z.namelist():
        try:
            gui = ET.fromstring(z.read("GuiDocument.xml"))  # noqa: S314
            for vp in gui.iter("ViewProvider"):
                for prop in vp.iter("Property"):
                    if prop.get("name") == "ShapeColor":
                        pc = prop.find("PropertyColor")
                        c = _fc_color(pc.get("value") if pc is not None else None)
                        if c:
                            colors[vp.get("name", "")] = c
        except ET.ParseError:
            warns.append("GuiDocument.xml is unreadable: colours not imported")
    types = {o.get("name"): o.get("type", "") for o in doc.iter("Object") if o.get("type")}
    data_objs = doc.find("ObjectData")
    bodies: list[Body] = []
    skipped: Counter[str] = Counter()
    consumed: set[str] = set()  # operands of booleans etc. are drawn by their result, not on their own
    props_of: dict[str, dict[str, ET.Element]] = {}
    for o in data_objs if data_objs is not None else []:
        props_of[o.get("name", "")] = {p.get("name", ""): p for p in o.iter("Property")}
    for name, props in props_of.items():
        for key in ("Base", "Tool", "Shapes", "Source", "Group", "Objects", "Links"):
            p = props.get(key)
            if p is not None and types.get(name, "").startswith(("Part::", "PartDesign::")) and key != "Group":
                consumed.update(e.get("value", "") for e in p.iter() if e.tag in ("Link", "LinkSub") and e.get("value"))
                consumed.update(e.get("obj", "") for e in p.iter() if e.get("obj"))

    def num(props: dict[str, ET.Element], key: str) -> float | None:
        p = props.get(key)
        if p is None:
            return None
        for child in p:
            if child.get("value") is not None:
                try:
                    return float(child.get("value", ""))
                except ValueError:
                    return None
        return None

    def text_prop(props: dict[str, ET.Element], key: str) -> str | None:
        p = props.get(key)
        s = p.find("String") if p is not None else None
        return s.get("value") if s is not None else None

    for name, props in props_of.items():
        typ = types.get(name, "")
        if name in consumed:
            continue
        vis = props.get("Visibility")
        vb = vis.find("Bool") if vis is not None else None
        if vb is not None and vb.get("value") == "false":
            continue
        label = text_prop(props, "Label") or name
        pl = props.get("Placement")
        pp = pl.find("PropertyPlacement") if pl is not None else None
        if pp is not None:
            P = np.array([float(pp.get(k, 0)) for k in ("Px", "Py", "Pz")])
            R = _quat_matrix(*(float(pp.get(k, d)) for k, d in (("Q0", 0), ("Q1", 0), ("Q2", 0), ("Q3", 1))))
        else:
            P, R = np.zeros(3), np.eye(3)
        lz = R @ np.array([0.0, 0.0, 1.0])
        axis = _nearest_axis(lz)
        exact: dict[str, Any] | None = None
        if typ == "Part::Box":
            L, W, H = num(props, "Length"), num(props, "Width"), num(props, "Height")
            if None not in (L, W, H):
                corners = np.array([[x, y, zz] for x in (0, L) for y in (0, W) for zz in (0, H)]) @ R.T + P
                if axis == "z":
                    yaw = math.degrees(math.atan2(R[1, 0], R[0, 0]))
                    exact = {"kind": "box", "size": [L, W, H], "at": list(P), "yaw_deg": round(yaw, 6)}
                elif all(_nearest_axis(R[:, k]) for k in range(3)):  # 90-degree turns: still an exact box
                    lo, hi = corners.min(0), corners.max(0)
                    exact = {"kind": "box", "size": list(hi - lo), "at": list(lo)}
                else:
                    bodies.append(Body(label, corners, volume=L * W * H, closed=True, color=colors.get(name)))  # type: ignore[operator]
                    continue
        elif typ in ("Part::Cylinder", "Part::Cone", "Part::Tube", "Part::Sphere"):
            r = num(props, "Radius") or num(props, "Radius1") or num(props, "OuterRadius")
            h = num(props, "Height")
            angle = num(props, "Angle")
            if typ == "Part::Sphere" and r:
                exact = {"kind": "sphere", "radius": r, "at": list(P)}
            elif axis and r and h and (angle is None or abs(angle - 360) < 1e-6):
                exact = {"kind": "cylinder", "radius": r, "height": h, "axis": axis, "at": list(P)}
                if typ == "Part::Cone":
                    r2 = num(props, "Radius2") or 0.0
                    if r2 > r:  # the spec's base is the wider end: flip the axis and move the base
                        top = P + lz * h
                        exact = {
                            "kind": "cone",
                            "radius": r2,
                            "radius2": r,
                            "height": h,
                            "at": list(top),
                            "axis": _nearest_axis(-lz),
                        }
                    else:
                        exact = {"kind": "cone", "radius": r, "radius2": r2, "height": h, "axis": axis, "at": list(P)}
                elif typ == "Part::Tube":
                    ri = num(props, "InnerRadius") or 0.0
                    exact = {"kind": "tube", "radius": r, "inner_radius": ri, "height": h, "axis": axis, "at": list(P)}
                    if not 0 < ri < r:
                        exact = {"kind": "cylinder", "radius": r, "height": h, "axis": axis, "at": list(P)}
        if exact is not None:
            bodies.append(Body(label, exact=exact, color=colors.get(name), closed=True))
            continue
        shape = props.get("Shape")
        part = shape.find("Part") if shape is not None else None
        fname = part.get("file") if part is not None else None
        if not fname or fname not in z.namelist() or not typ.startswith(("Part::", "PartDesign::")):
            if typ and not typ.startswith(("App::", "Sketcher::", "Spreadsheet::", "Part::Part2D")):
                skipped[typ] += 1
            continue
        if typ.startswith("PartDesign::") and typ != "PartDesign::Body":
            continue  # features are drawn by their Body (its Tip)
        pts, hints = _brep_points(z.read(fname).decode("latin-1"))
        if pts is None:
            continue
        bodies.append(Body(label, pts, hints=hints, color=colors.get(name)))
    for t, n in skipped.items():
        warns.append(f"skipped {n} object(s) of type {t} (no solid geometry stored)")
    return bodies, 0.001  # FreeCAD documents are in millimetres


# =========================================================================== DXF (2-D layouts)
_DXF_UNITS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}


def _parse_dxf(data: bytes, warns: list[str], height: float) -> tuple[list[Body], float | None]:
    lines = data.decode("utf-8", "replace").splitlines()
    if len(lines) % 2:
        lines.append("")
    pairs = [(lines[i].strip(), lines[i + 1].strip()) for i in range(0, len(lines) - 1, 2)]
    if not any(c == "0" and v == "SECTION" for c, v in pairs[:50]):
        raise ImportError_("not an ASCII DXF file")
    units = None
    for i, (c, v) in enumerate(pairs):
        if c == "9" and v == "$INSUNITS" and i + 1 < len(pairs):
            with contextlib.suppress(ValueError):
                units = _DXF_UNITS.get(int(pairs[i + 1][1]))
            break
    try:
        start = next(i for i, (c, v) in enumerate(pairs) if c == "2" and v == "ENTITIES")
    except StopIteration:
        raise ImportError_("DXF file has no ENTITIES section") from None
    ents: list[tuple[str, list[tuple[str, str]]]] = []
    cur: tuple[str, list[tuple[str, str]]] | None = None
    for c, v in pairs[start + 1 :]:
        if c == "0":
            if cur:
                ents.append(cur)
            if v == "ENDSEC":
                break
            cur = (v, [])
        elif cur is not None:
            cur[1].append((c, v))
    bodies: list[Body] = []
    ignored: Counter[str] = Counter()
    # heights are in drawing units; `height` (m) is converted once the drawing's units are known (below)
    for typ, kv in ents:
        g: dict[str, list[str]] = {}
        for c, v in kv:
            g.setdefault(c, []).append(v)

        def f(code: str, default: float = 0.0, g=g) -> float:
            try:
                return float(g[code][0])
            except (KeyError, ValueError, IndexError):
                return default

        layer = (g.get("8") or ["0"])[0]
        thick = abs(f("39"))
        if typ == "CIRCLE":
            r = f("40")
            if r > 0:
                bodies.append(
                    Body(
                        f"{layer}_c",
                        exact={
                            "kind": "cylinder",
                            "radius": r,
                            "height": thick or None,
                            "axis": "z",
                            "at": [f("10"), f("20"), f("30")],
                        },
                        group=layer,
                    )
                )
        elif typ == "LWPOLYLINE" and int(f("70")) & 1:
            try:
                xs, ys = [float(x) for x in g.get("10", [])], [float(y) for y in g.get("20", [])]
            except ValueError:
                continue
            if len(xs) >= 3 and len(xs) == len(ys):
                elev = f("38")
                pts = np.array([[x, y, elev] for x, y in zip(xs, ys)])
                ex = _rect(pts)
                if ex is not None:
                    ex["size"][2] = thick or None
                    bodies.append(Body(f"{layer}_r", exact=ex, group=layer))
                else:
                    bodies.append(Body(f"{layer}_p", pts, hints={"extrude": thick or None}, group=layer))
        else:
            ignored[typ] += 1
    for t, n in ignored.most_common(6):
        warns.append(
            f"ignored {n} DXF {t} entit{'y' if n == 1 else 'ies'} (only circles and closed polylines become solids)"
        )
    if not bodies:
        raise ImportError_("the DXF has no circles or closed polylines to extrude")
    scale = units or (0.001 if _extent(bodies) > 200 else 1.0)
    for b in bodies:  # fill the default extrusion height in drawing units
        h = height / scale
        if b.exact is not None:
            if b.exact["kind"] == "cylinder" and not b.exact["height"]:
                b.exact["height"] = h
            if b.exact["kind"] == "box" and not b.exact["size"][2]:
                b.exact["size"][2] = h
        elif b.points is not None:
            hh = b.hints.pop("extrude", None) or h
            b.points = np.vstack([b.points, b.points + np.array([0.0, 0.0, hh])])
            b.closed = False
    if units is None:
        warns.append("the DXF does not declare $INSUNITS: units were guessed (set them explicitly if wrong)")
    return bodies, units


def _rect(pts: np.ndarray) -> dict[str, Any] | None:
    """A closed 4-vertex polyline with right angles -> exact box (with yaw)."""
    p = pts[:, :2]
    if len(p) == 5 and np.allclose(p[0], p[-1]):
        p = p[:4]
    if len(p) != 4:
        return None
    e = np.roll(p, -1, axis=0) - p
    lens = np.linalg.norm(e, axis=1)
    if lens.min() <= 0 or abs(np.dot(e[0], e[1])) > 1e-6 * lens[0] * lens[1] or not np.allclose(e[0], -e[2]):
        return None
    yaw = math.degrees(math.atan2(e[0][1], e[0][0]))
    at = p[0]
    if (
        e[0][0] * e[1][1] - e[0][1] * e[1][0] < 0
    ):  # clockwise: start from the next corner so the box extends to +local y
        at, yaw = p[1], math.degrees(math.atan2(e[1][1], e[1][0]))
        lens = np.roll(lens, -1)
    return {
        "kind": "box",
        "size": [float(lens[0]), float(lens[1]), None],
        "at": [float(at[0]), float(at[1]), float(pts[0, 2])],
        "yaw_deg": round(yaw, 6),
    }


# =========================================================================== fitting
def _to_z_up(p: np.ndarray, up: str) -> np.ndarray:
    return p if up == "z" else np.column_stack([p[:, 0], -p[:, 2], p[:, 1]])


def _axis_to_z_up(axis: str, up: str) -> str:
    if up == "z":
        return axis
    sign = "-" if axis.startswith("-") else ""
    m = {"x": "x", "y": "z", "z": "-y"}[axis.lstrip("-")]
    return (m[1:] if m.startswith("-") else "-" + m) if sign else m


def _fit_body(b: Body, scale: float, up: str) -> tuple[dict[str, Any], str, float | None]:
    """-> (part fields in metres, z-up; method: exact | fitted | envelope; relative volume error)."""
    if b.exact is not None:
        e = dict(b.exact)
        for k in ("radius", "radius2", "inner_radius", "height"):
            if e.get(k) is not None:
                e[k] = e[k] * scale
        at = _to_z_up(np.array([e["at"]], float) * scale, up)[0]
        e["at"] = list(at)
        if "size" in e:
            s = np.array(e["size"], float) * scale
            e["size"] = list(s if up == "z" else [s[0], s[2], s[1]])
            if up != "z":  # a y-up box's min corner maps to a corner with max y: shift back to min
                e["at"][1] -= e["size"][1]
        if "axis" in e:
            e["axis"] = _axis_to_z_up(e["axis"], up)
        return e, "exact", 0.0
    p = _to_z_up(np.asarray(b.points, float) * scale, up)
    vol = b.volume * scale**3 if (b.volume is not None and b.closed) else None
    # yaw of the footprint from its principal axes (only when it makes the envelope clearly tighter)
    xy = p[:, :2] - p[:, :2].mean(0)
    yaw = 0.0
    round_hints = bool(b.hints and (b.hints.get("cylinder") or b.hints.get("cone") or b.hints.get("sphere")))
    if len(xy) >= 3 and not round_hints:  # B-rep vertex samples of round bodies are too sparse for a yaw estimate
        cov = np.cov(xy.T)
        theta = 0.5 * math.atan2(2 * cov[0, 1], cov[0, 0] - cov[1, 1])
        c, s = math.cos(-theta), math.sin(-theta)
        rot = xy @ np.array([[c, -s], [s, c]]).T
        a_rot = np.ptp(rot[:, 0]) * np.ptp(rot[:, 1])
        a_aa = np.ptp(xy[:, 0]) * np.ptp(xy[:, 1])
        if a_rot < 0.97 * a_aa:
            yaw = theta
    c, s = math.cos(-yaw), math.sin(-yaw)
    local = np.column_stack([p[:, 0] * c - p[:, 1] * s, p[:, 0] * s + p[:, 1] * c, p[:, 2]])
    lo, hi = local.min(0), local.max(0)
    d = hi - lo
    centre_local = (lo + hi) / 2

    def world(q):  # local (yaw-aligned) -> world
        cy, sy = math.cos(yaw), math.sin(yaw)
        return [q[0] * cy - q[1] * sy, q[0] * sy + q[1] * cy, q[2]]

    env = float(np.prod(d)) if np.all(d > 0) else 0.0
    fill = vol / env if (vol is not None and env > 0) else None
    hints = b.hints or {}
    cyl_hint = [h for h in hints.get("cylinder", []) if h[0]]
    has_cone = bool(hints.get("cone"))
    has_sphere = bool(hints.get("sphere"))

    def err(v_fit: float) -> float | None:
        return None if vol is None or vol <= 0 else abs(v_fit - vol) / vol

    # which local axis has a (near) square cross-section perpendicular to it?
    best_axis, best_sq = None, 1.0
    for k in range(3):
        o = [j for j in range(3) if j != k]
        sq = abs(d[o[0]] - d[o[1]]) / max(d[o[0]], d[o[1]], 1e-12)
        if sq < best_sq:
            best_axis, best_sq = k, sq
    round_section = best_axis is not None and best_sq < 0.08
    if (has_sphere or (fill is not None and 0.45 < fill < 0.6)) and (np.ptp(d) / max(d.max(), 1e-12) < 0.1):
        r = float(d.mean() / 2)
        return {"kind": "sphere", "radius": r, "at": world(centre_local)}, "fitted", err(4 / 3 * math.pi * r**3)
    if round_section and (cyl_hint or has_cone or (fill is not None and fill < 0.86)):
        k = best_axis
        o = [j for j in range(3) if j != k]
        r = float((d[o[0]] + d[o[1]]) / 4)
        h = float(d[k])
        axis_name = "xyz"[k]
        base = centre_local.copy()
        base[k] = lo[k]
        # radius profile along the axis separates cones from cylinders / tubes
        rad = np.hypot(local[:, o[0]] - centre_local[o[0]], local[:, o[1]] - centre_local[o[1]])
        t = (local[:, k] - lo[k]) / max(h, 1e-12)
        bot, top = rad[t < 0.1], rad[t > 0.9]
        rb = float(bot.max()) if len(bot) else r
        rt = float(top.max()) if len(top) else r
        if has_cone or abs(rb - rt) > 0.15 * max(rb, rt):
            if rb >= rt:
                part = {
                    "kind": "cone",
                    "radius": rb,
                    "radius2": rt if rt > DIM_MIN else 0.0,
                    "height": h,
                    "axis": axis_name,
                    "at": world(base),
                }
            else:
                top_c = base.copy()
                top_c[k] = hi[k]
                part = {
                    "kind": "cone",
                    "radius": rt,
                    "radius2": rb if rb > DIM_MIN else 0.0,
                    "height": h,
                    "axis": "-" + axis_name,
                    "at": world(top_c),
                }
            _yaw(part, yaw)
            r1, r2 = part["radius"], part["radius2"]
            return part, "fitted", err(math.pi * h / 3 * (r1 * r1 + r1 * r2 + r2 * r2))
        radii = sorted({round(h_[0] * scale, 6) for h_ in cyl_hint})
        ri = None
        if len(radii) >= 2 and radii[0] < radii[-1] * 0.98:
            ri = radii[0]
        elif fill is not None and fill < 0.70:
            ri = r * math.sqrt(max(0.0, 1 - fill / (math.pi / 4)))
        if ri is not None and DIM_MIN < ri < r * 0.98:
            part = {"kind": "tube", "radius": r, "inner_radius": ri, "height": h, "axis": axis_name, "at": world(base)}
            _yaw(part, yaw)
            return part, "fitted", err(math.pi * (r * r - ri * ri) * h)
        part = {"kind": "cylinder", "radius": r, "height": h, "axis": axis_name, "at": world(base)}
        _yaw(part, yaw)
        return part, "fitted", err(math.pi * r * r * h)
    planar_box = hints.get("vertices") == 8 and not (
        hints.get("cylinder") or hints.get("cone") or hints.get("sphere") or hints.get("other")
    )
    method = "fitted" if (fill is not None and fill > 0.86) or planar_box else "envelope"
    part = {"kind": "box", "size": [float(x) for x in d], "at": world(lo)}
    if abs(yaw) > 1e-9:
        part["yaw_deg"] = math.degrees(yaw)
    return part, method, err(env)


def _yaw(part: dict[str, Any], yaw: float) -> None:
    """A horizontal axis found in the yawed frame: the spec turns the part about the vertical through `at`."""
    if part["axis"].lstrip("-") != "z" and abs(yaw) > 1e-9:
        part["yaw_deg"] = math.degrees(yaw)


def _safe_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name or "part").strip("_") or "part"
    if not base[0].isalpha():
        base = "p_" + base
    base = base[:28]
    pid, n = base, 2
    while pid in used:
        pid = f"{base[: 28 - len(str(n)) - 1]}_{n}"
        n += 1
    used.add(pid)
    return pid


def _fit_all(bodies: list[Body], scale: float, up: str, ground: bool, warns: list[str]):
    fitted = []
    for b in bodies:
        try:
            part, method, e = _fit_body(b, scale, up)
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            warns.append(f"{b.name}: could not be fitted and was skipped")
            continue
        fitted.append((b, part, method, e))

    # drop parts below the spec's smallest dimension (fasteners, chamfer slivers, unit mistakes)
    def dims(p):
        if p["kind"] == "box":
            return p["size"]
        return [v for v in (p.get("radius"), p.get("height"), p.get("inner_radius")) if v is not None]

    tiny = [f for f in fitted if min(dims(f[1])) < DIM_MIN]
    fitted = [f for f in fitted if min(dims(f[1])) >= DIM_MIN]
    if tiny:
        warns.append(f"dropped {len(tiny)} body(ies) smaller than {DIM_MIN * 1000:g} mm")
    if len(fitted) > MAX_PARTS:

        def size(f):
            return float(np.prod(dims(f[1]))) if dims(f[1]) else 0.0

        fitted.sort(key=size, reverse=True)
        warns.append(f"kept the {MAX_PARTS} largest of {len(fitted)} bodies (spec limit)")
        fitted = fitted[:MAX_PARTS]
    # translate: centre the footprint on the origin, and put the lowest point on the ground
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for _, p, _, _ in fitted:
        a, bmin, bmax = np.array(p["at"]), None, None
        if p["kind"] == "box":
            bmin, bmax = a, a + np.array(p["size"])
        elif p["kind"] == "sphere":
            bmin, bmax = a - p["radius"], a + p["radius"]
        else:
            ax = np.array(_AXES[p.get("axis", "z")], float)
            end = a + ax * p["height"]
            r = p["radius"]
            bmin, bmax = np.minimum(a, end) - r * (1 - np.abs(ax)), np.maximum(a, end) + r * (1 - np.abs(ax))
        lo, hi = np.minimum(lo, bmin), np.maximum(hi, bmax)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2] if ground else 0.0])
    if np.max(hi - lo) > 2 * COORD_LIMIT * 0.98:
        raise ImportError_(f"the model is {np.max(hi - lo):.0f} m across after unit conversion - check the units")
    used: set[str] = set()
    parts, methods, errors = [], Counter(), []
    for b, p, method, e in fitted:
        p["at"] = [round(float(x + s), 6) for x, s in zip(p["at"], shift)]
        for k in ("radius", "radius2", "inner_radius", "height"):
            if k in p and p[k] is not None:
                p[k] = round(float(p[k]), 6)
        if "size" in p:
            p["size"] = [round(float(x), 6) for x in p["size"]]
        if "yaw_deg" in p:
            p["yaw_deg"] = round(((p["yaw_deg"] + 180) % 360) - 180, 4)
        grp = re.sub(r"[^A-Za-z0-9_]", "_", b.group)[:32] or "imported"
        parts.append(
            {
                "id": _safe_id(b.name, used),
                **p,
                "color": b.color or KIND_COLORS[p["kind"]],
                "group": grp,
                **({"opacity": 0.6} if method == "envelope" else {}),
            }
        )
        methods[method] += 1
        if e is not None:
            errors.append(e)
    stats = {
        "parts": len(parts),
        "kinds": dict(Counter(p["kind"] for p in parts)),
        "methods": dict(methods),
        "mean_volume_error": round(float(np.mean(errors)), 4) if errors else None,
        "bbox_m": [round(float(x), 3) for x in (*(lo + shift), *(hi + shift))],
    }
    if methods.get("envelope"):
        warns.append(
            f"{methods['envelope']} part(s) are envelope boxes (shown semi-transparent): the source shape is irregular"
        )
    return parts, stats


# =========================================================================== exact measurement in FreeCAD
_MEASURE = """\
import Part, json, base64, tempfile, os
_out = []
_tmp = tempfile.mkdtemp(prefix="duosim-import-")
try:
    _p = os.path.join(_tmp, "in" + {ext!r})
    with open(_p, "wb") as fh:
        fh.write(base64.b64decode({b64!r}))
    _sh = Part.Shape()
    _sh.read(_p)
    for i, s in enumerate(_sh.Solids[:{limit}]):
        bb = s.BoundBox
        hints = {{"cylinder": [], "cone": [], "sphere": [], "other": 0}}
        for f in s.Faces:
            n = type(f.Surface).__name__
            if n == "Cylinder":
                hints["cylinder"].append((f.Surface.Radius, tuple(f.Surface.Axis)))
            elif n == "Cone":
                hints["cone"].append((None, tuple(f.Surface.Axis)))
            elif n == "Sphere":
                hints["sphere"].append((f.Surface.Radius, None))
            elif n != "Plane":
                hints["other"] += 1
        pts = [tuple(v.Point) for v in s.Vertexes[:4000]]
        for e in s.Edges:
            if type(e.Curve).__name__ == "Circle":
                c, r, ax = e.Curve.Center, e.Curve.Radius, e.Curve.Axis
                for k in range(8):
                    q = e.valueAt(e.FirstParameter + (e.LastParameter - e.FirstParameter) * k / 8.0)
                    pts.append((q.x, q.y, q.z))
        _out.append({{"name": "solid%d" % (i + 1), "volume": s.Volume, "points": pts, "hints": hints,
                      "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]}})
finally:
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)
print("IMPORT_REPORT " + json.dumps(_out))
"""


def measure_script(data: bytes, ext: str, limit: int = MAX_PARTS) -> str:
    """FreeCAD script that reads a STEP / IGES / BREP file and reports each solid.  The file only ever travels as
    a base64 string literal: nothing from it is formatted into code."""
    import base64

    if ext not in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
        raise ImportError_("only STEP, IGES and BREP are measured in FreeCAD")
    return _MEASURE.format(ext=ext, b64=base64.b64encode(data).decode(), limit=int(limit))


def bodies_from_report(report: list[dict[str, Any]]) -> list[Body]:
    out = []
    for r in report:
        pts = np.array(r["points"], float) if r.get("points") else None
        bb = r.get("bbox")
        if bb and pts is None:
            corners = np.array([[x, y, z] for x in (bb[0], bb[3]) for y in (bb[1], bb[4]) for z in (bb[2], bb[5])])
            pts = corners if pts is None else np.vstack([pts, corners])
        vol = r.get("volume")
        out.append(
            Body(
                r.get("name", "solid"),
                pts,
                abs(vol) if vol else None,
                closed=vol is not None,
                hints=r.get("hints") or {},
            )
        )
    return out


def measure_with_worker(client, data: bytes, filename: str) -> list[Body]:
    from .builder import _marker_json

    ext = PurePath(filename).suffix.lower()
    out = client.execute(measure_script(data, ext), slow=True)
    return bodies_from_report(_marker_json(out, "IMPORT_REPORT"))
