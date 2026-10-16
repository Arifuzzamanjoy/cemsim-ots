"""Design spec: a small, strictly validated JSON description of an equipment / plant layout.

The language model never writes code.  It writes *this* JSON; `parse_spec` turns anything that
is not exactly this shape into a list of precise error messages (which are fed back to the
model for one repair round), and `duosim.design.builder` turns the validated numbers into
FreeCAD calls.  Units: metres in the spec (millimetres in FreeCAD).  Z is up.

Parts
-----
box       size [dx, dy, dz]            at = minimum corner
cylinder  radius, height, axis         at = centre of the base
cone      radius, radius2, height, axis   (frustum or cone; radius2 = 0 -> pointed)  at = centre of base
sphere    radius                       at = centre
tube      radius (outer), inner_radius, height, axis   (hollow cylinder)  at = centre of base
pipe      radius, path [[x, y, z], ...]  straight runs with spherical elbows

Every part also has: id (unique), color "#RRGGBB", opacity 0.05..1, group (free label), yaw_deg
(rotation about the vertical line through `at`).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Any

KINDS = ("box", "cylinder", "cone", "sphere", "tube", "pipe")
AXES = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
    "-x": (-1.0, 0.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "-z": (0.0, 0.0, -1.0),
}

MAX_PARTS = 300
MAX_PATH_POINTS = 24
# A pipe is built by fusing one cylinder per run and one sphere per elbow, ~0.19 s per waypoint in FreeCAD 1.1
# (measured).  The cap keeps the worst valid design to tens of seconds: a build occupies the shared GUI thread
# and cannot be cancelled once started.
MAX_TOTAL_PATH_POINTS = 120
COORD_LIMIT = 1000.0  # m, any coordinate
DIM_MIN, DIM_MAX = 0.005, 500.0  # m, any length / radius
MAX_NAME = 80

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_GROUP = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_COLOR = "#9aa3ad"

# which numeric/option fields each kind needs (besides the common ones)
REQUIRED = {
    "box": ("size", "at"),
    "cylinder": ("radius", "height", "at"),
    "cone": ("radius", "height", "at"),
    "sphere": ("radius", "at"),
    "tube": ("radius", "inner_radius", "height", "at"),
    "pipe": ("radius", "path"),
}
_KNOWN = {
    "id",
    "kind",
    "color",
    "opacity",
    "group",
    "yaw_deg",
    "at",
    "size",
    "radius",
    "radius2",
    "inner_radius",
    "height",
    "axis",
    "path",
    "note",
}


class SpecError(ValueError):
    """The spec is not valid; `.errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:6]) + (f" (+{len(errors) - 6} more)" if len(errors) > 6 else ""))
        self.errors = errors


@dataclass
class Part:
    id: str
    kind: str
    color: str = DEFAULT_COLOR
    opacity: float = 1.0
    group: str = "main"
    yaw_deg: float = 0.0
    at: tuple[float, float, float] = (0.0, 0.0, 0.0)
    size: tuple[float, float, float] | None = None
    radius: float | None = None
    radius2: float | None = None
    inner_radius: float | None = None
    height: float | None = None
    axis: str = "z"
    path: list[tuple[float, float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "color": self.color,
            "opacity": self.opacity,
            "group": self.group,
        }
        if self.yaw_deg:
            d["yaw_deg"] = self.yaw_deg
        if self.kind != "pipe":
            d["at"] = list(self.at)
        if self.kind in ("cylinder", "cone", "tube"):
            d["axis"] = self.axis
        for k in ("size", "radius", "radius2", "inner_radius", "height"):
            v = getattr(self, k)
            if v is not None:
                d[k] = list(v) if isinstance(v, tuple) else v
        if self.kind == "pipe":
            d["path"] = [list(p) for p in self.path]
        return d


@dataclass
class Spec:
    name: str
    description: str
    parts: list[Part]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parts": [p.to_dict() for p in self.parts]}


# --------------------------------------------------------------------------- parsing
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _vec3(v: Any, where: str, errs: list[str], lim: float = COORD_LIMIT) -> tuple[float, float, float] | None:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        errs.append(f"{where}: must be an array of 3 numbers [x, y, z] in metres")
        return None
    out = []
    for c in v:
        f = _num(c)
        if f is None:
            errs.append(f"{where}: {c!r} is not a finite number")
            return None
        if abs(f) > lim:
            errs.append(f"{where}: {f:g} m is outside +-{lim:g} m")
            return None
        out.append(f)
    return (out[0], out[1], out[2])


def _len(p: dict, key: str, pid: str, errs: list[str], zero_ok: bool = False) -> float | None:
    f = _num(p.get(key))
    if f is None:
        errs.append(f"{pid}.{key}: must be a number (metres)")
        return None
    lo = 0.0 if zero_ok else DIM_MIN
    if f < lo or f > DIM_MAX:
        errs.append(f"{pid}.{key}: {f:g} m must be within [{lo:g}, {DIM_MAX:g}] m")
        return None
    return f


def parse_spec(raw: Any) -> tuple[Spec, list[str]]:
    """Validate a decoded JSON value.  Returns (spec, warnings) or raises SpecError."""
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(raw, dict):
        raise SpecError(["the design must be a JSON object with a 'parts' array"])
    name = str(raw.get("name") or "Untitled design").strip()[:MAX_NAME] or "Untitled design"
    desc = str(raw.get("description") or "").strip()[:500]
    parts_raw = raw.get("parts")
    if not isinstance(parts_raw, list) or not parts_raw:
        raise SpecError(["'parts' must be a non-empty array"])
    if len(parts_raw) > MAX_PARTS:
        raise SpecError([f"too many parts: {len(parts_raw)} (limit {MAX_PARTS})"])

    parts: list[Part] = []
    seen: set[str] = set()
    path_points = 0
    for i, p in enumerate(parts_raw):
        if not isinstance(p, dict):
            errs.append(f"parts[{i}]: must be an object")
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not _ID.match(pid):
            errs.append(f"parts[{i}].id: {pid!r} must match [A-Za-z][A-Za-z0-9_]{{0,31}}")
            pid = f"parts{i}"
        elif pid in seen:
            errs.append(f"parts[{i}].id: duplicate id {pid!r}")
        seen.add(pid)
        kind = p.get("kind")
        if kind not in KINDS:
            errs.append(f"{pid}.kind: {kind!r} must be one of {', '.join(KINDS)}")
            continue
        for k in p:
            if k not in _KNOWN:
                warns.append(f"{pid}: ignored unknown field {k!r}")
        for k in REQUIRED[kind]:
            if k not in p:
                errs.append(f"{pid}: {kind} needs {k!r}")
        part = Part(id=pid, kind=kind)

        color = p.get("color", DEFAULT_COLOR)
        if isinstance(color, str) and _COLOR.match(color):
            part.color = color.lower()
        else:
            warns.append(f"{pid}.color: {color!r} is not #RRGGBB, using {DEFAULT_COLOR}")
        op = p.get("opacity", 1.0)
        f = _num(op)
        if f is None or not 0.05 <= f <= 1.0:
            warns.append(f"{pid}.opacity: {op!r} outside [0.05, 1], using 1")
        else:
            part.opacity = f
        grp = p.get("group", "main")
        part.group = grp if isinstance(grp, str) and _GROUP.match(grp) else "main"
        yaw = p.get("yaw_deg", 0.0)
        f = _num(yaw)
        if f is None or abs(f) > 360.0:
            errs.append(f"{pid}.yaw_deg: {yaw!r} must be a number within +-360")
        else:
            part.yaw_deg = f

        if "at" in p and kind != "pipe":
            v = _vec3(p["at"], f"{pid}.at", errs)
            if v:
                part.at = v
        if kind in ("cylinder", "cone", "tube"):
            ax = p.get("axis", "z")
            if ax not in AXES:
                errs.append(f"{pid}.axis: {ax!r} must be one of {', '.join(AXES)}")
            else:
                part.axis = ax
        if kind == "box" and "size" in p:
            v = _vec3(p["size"], f"{pid}.size", errs, DIM_MAX)
            if v:
                if min(v) < DIM_MIN:
                    errs.append(f"{pid}.size: every side must be >= {DIM_MIN:g} m")
                else:
                    part.size = v
        if kind in ("cylinder", "cone", "sphere", "tube", "pipe") and "radius" in p:
            part.radius = _len(p, "radius", pid, errs)
        if kind in ("cylinder", "cone", "tube") and "height" in p:
            part.height = _len(p, "height", pid, errs)
        if kind == "cone":  # base radius is already > 0 above, so only the top may be 0 (pointed)
            part.radius2 = _len(p, "radius2", pid, errs, zero_ok=True) if "radius2" in p else 0.0
        if kind == "tube" and "inner_radius" in p:
            part.inner_radius = _len(p, "inner_radius", pid, errs)
            if part.inner_radius is not None and part.radius is not None and part.inner_radius >= part.radius:
                errs.append(f"{pid}: inner_radius {part.inner_radius:g} must be < radius {part.radius:g}")
        if kind == "pipe" and "path" in p:
            pts = p["path"]
            if not isinstance(pts, list) or not 2 <= len(pts) <= MAX_PATH_POINTS:
                errs.append(f"{pid}.path: needs 2..{MAX_PATH_POINTS} points")
            else:
                path_points += len(pts)
                got = [_vec3(q, f"{pid}.path[{j}]", errs) for j, q in enumerate(pts)]
                if all(got):
                    for a, b in itertools.pairwise(got):
                        if math.dist(a, b) < DIM_MIN:  # type: ignore[arg-type]
                            errs.append(f"{pid}.path: consecutive points must differ by >= {DIM_MIN:g} m")
                            break
                    else:
                        part.path = got  # type: ignore[assignment]
        parts.append(part)

    if path_points > MAX_TOTAL_PATH_POINTS:
        errs.append(f"too many pipe waypoints in total: {path_points} (limit {MAX_TOTAL_PATH_POINTS})")
    if errs:
        raise SpecError(errs)
    return Spec(name=name, description=desc, parts=parts), warns


# --------------------------------------------------------------------------- analytic reference
def axis_vec(part: Part) -> tuple[float, float, float]:
    return AXES[part.axis]


def analytic_volume(part: Part) -> float | None:
    """Exact volume in m^3 for the primitives (None for pipes: the fused union has no closed form)."""
    k = part.kind
    if k == "box":
        assert part.size
        return part.size[0] * part.size[1] * part.size[2]
    if k == "sphere":
        assert part.radius is not None
        return 4.0 / 3.0 * math.pi * part.radius**3
    if k == "cylinder":
        assert part.radius is not None and part.height is not None
        return math.pi * part.radius**2 * part.height
    if k == "cone":
        assert part.radius is not None and part.radius2 is not None and part.height is not None
        r1, r2, h = part.radius, part.radius2, part.height
        return math.pi * h / 3.0 * (r1 * r1 + r1 * r2 + r2 * r2)
    if k == "tube":
        assert part.radius is not None and part.inner_radius is not None and part.height is not None
        return math.pi * (part.radius**2 - part.inner_radius**2) * part.height
    return None


def analytic_centroid(part: Part) -> tuple[float, float, float] | None:
    """Centre of mass in metres (after yaw), None for pipes."""
    k = part.kind
    ax, ay, az = part.at
    if k == "box":
        assert part.size
        c = (ax + part.size[0] / 2, ay + part.size[1] / 2, az + part.size[2] / 2)
    elif k == "sphere":
        c = (ax, ay, az)
    elif k in ("cylinder", "tube"):
        assert part.height is not None
        d = axis_vec(part)
        c = (ax + d[0] * part.height / 2, ay + d[1] * part.height / 2, az + d[2] * part.height / 2)
    elif k == "cone":
        assert part.radius is not None and part.radius2 is not None and part.height is not None
        r1, r2, h = part.radius, part.radius2, part.height
        t = h * (r1 * r1 + 2 * r1 * r2 + 3 * r2 * r2) / (4 * (r1 * r1 + r1 * r2 + r2 * r2))
        d = axis_vec(part)
        c = (ax + d[0] * t, ay + d[1] * t, az + d[2] * t)
    else:
        return None
    if part.yaw_deg:  # rotate about the vertical line through `at`
        a = math.radians(part.yaw_deg)
        dx, dy = c[0] - ax, c[1] - ay
        c = (ax + dx * math.cos(a) - dy * math.sin(a), ay + dx * math.sin(a) + dy * math.cos(a), c[2])
    return c
