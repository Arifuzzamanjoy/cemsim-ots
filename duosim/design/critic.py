"""Deterministic geometric critique of a design spec: the "verifier" half of generate -> critique -> repair.

The FreeCAD build already proves that every part is *built* exactly as specified.  This module checks
whether what is specified is *physically plausible* - things a language model often gets wrong and a
schema validator cannot see:

  floating       a part above the ground that touches nothing (no support, no attachment)
  below_ground   a part sunk deeper than a foundation would be
  open_pipe      a pipe end that is not connected to any equipment
  duplicate      two parts occupying the same space (copy/paste artefacts)
  islands        groups of parts that do not touch each other (information)
  scale          overall dimensions far from the planned ones
  coverage       planned components that do not appear in the design

Every finding names the parts involved and says how to fix it, so it can be fed back to the model verbatim.
All checks work on axis-aligned bounding boxes (yaw applied), which keeps them O(n^2) on <= 300 parts.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from .spec import AXES, Part, Spec

TOUCH_TOL = 0.05  # m: parts closer than this touch
WEIGHTS = {"error": 15, "warn": 5, "info": 0}


@dataclass
class Finding:
    rule: str
    severity: str  # error | warn | info
    message: str
    parts: list[str] = field(default_factory=list)
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def line(self) -> str:
        return f"[{self.severity}] {self.message}" + (f" Fix: {self.fix}" if self.fix else "")


@dataclass
class Critique:
    score: int
    findings: list[Finding]
    bbox_m: list[float]
    islands: int

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "errors": self.errors,
            "warnings": sum(f.severity == "warn" for f in self.findings),
            "islands": self.islands,
            "bbox_m": self.bbox_m,
            "findings": [f.to_dict() for f in self.findings],
        }

    def feedback(self, limit: int = 25) -> str:
        keep = [f for f in self.findings if f.severity != "info"][:limit]
        return "\n".join(f"- {f.line()}" for f in keep)


def _rot(v: np.ndarray, yaw_deg: float) -> np.ndarray:
    if not yaw_deg:
        return v
    a = math.radians(yaw_deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2]])


def aabb(p: Part) -> np.ndarray:
    """[xmin, ymin, zmin, xmax, ymax, zmax] in metres, yaw about the vertical through `at` applied."""
    at = np.array(p.at, float)
    if p.kind == "pipe":
        pts = np.array(p.path, float)
        r = p.radius or 0.0
        return np.concatenate([pts.min(0) - r, pts.max(0) + r])
    if p.kind == "sphere":
        r = p.radius or 0.0
        return np.concatenate([at - r, at + r])
    if p.kind == "box":
        sx, sy, sz = p.size or (0.0, 0.0, 0.0)
        corners = np.array([[x, y, z] for x in (0, sx) for y in (0, sy) for z in (0, sz)], float)
        pts = np.array([at + _rot(c, p.yaw_deg) for c in corners])
        return np.concatenate([pts.min(0), pts.max(0)])
    d = _rot(np.array(AXES[p.axis], float), p.yaw_deg)
    h, r = p.height or 0.0, max(p.radius or 0.0, p.radius2 or 0.0)
    end = at + d * h
    ext = r * np.sqrt(np.clip(1 - d**2, 0, 1))
    return np.concatenate([np.minimum(at, end) - ext, np.maximum(at, end) + ext])


def _gap(a: np.ndarray, b: np.ndarray) -> float:
    """Largest separation along any axis (<= 0 when the boxes overlap or touch)."""
    return float(np.max(np.maximum(a[:3] - b[3:], b[:3] - a[3:])))


def _overlap_ratio(a: np.ndarray, b: np.ndarray) -> float:
    lo, hi = np.maximum(a[:3], b[:3]), np.minimum(a[3:], b[3:])
    inter = float(np.prod(np.clip(hi - lo, 0, None)))
    va, vb = float(np.prod(a[3:] - a[:3])), float(np.prod(b[3:] - b[:3]))
    union = va + vb - inter
    return inter / union if union > 0 else 0.0


def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) > 2}


def critique(spec: Spec, plan: dict[str, Any] | None = None) -> Critique:
    parts = spec.parts
    boxes = np.array([aabb(p) for p in parts]) if parts else np.zeros((0, 6))
    n = len(parts)
    findings: list[Finding] = []
    # pairwise gaps (vectorised): gap[i, j] <= TOUCH_TOL means i and j touch
    if n:
        lo, hi = boxes[:, :3], boxes[:, 3:]
        sep = np.maximum(lo[:, None, :] - hi[None, :, :], lo[None, :, :] - hi[:, None, :]).max(-1)
        touch = sep <= TOUCH_TOL
        np.fill_diagonal(touch, False)
    else:
        touch = np.zeros((0, 0), bool)

    for i, p in enumerate(parts):
        b = boxes[i]
        if b[2] < -10:
            findings.append(
                Finding(
                    "below_ground",
                    "error",
                    f"{p.id} reaches {b[2]:.1f} m below ground.",
                    [p.id],
                    "Raise it: equipment stands on the ground (z = 0); only foundations go slightly below.",
                )
            )
        elif b[2] < -1.0:
            findings.append(
                Finding(
                    "below_ground",
                    "warn",
                    f"{p.id} reaches {b[2]:.2f} m below ground.",
                    [p.id],
                    "Keep only foundations and pits below z = 0.",
                )
            )
        if p.kind != "pipe" and b[2] > TOUCH_TOL and not touch[i].any():
            sev = "error" if b[2] > 0.5 else "warn"
            findings.append(
                Finding(
                    "floating",
                    sev,
                    f"{p.id} floats {b[2]:.2f} m above the ground touching nothing.",
                    [p.id],
                    "Support it on the ground, a structure or the equipment it belongs to (make the boxes touch).",
                )
            )
        if p.kind == "pipe" and len(p.path) >= 2:
            r = p.radius or 0.0
            for end_name, q in (("start", p.path[0]), ("end", p.path[-1])):
                qb = np.array([*q, *q], float)
                others = [j for j in range(n) if j != i]
                near = any(_gap(qb, boxes[j]) <= 2 * r + 0.15 for j in others)
                if not near:
                    findings.append(
                        Finding(
                            "open_pipe",
                            "warn",
                            f"pipe {p.id} {end_name} {[round(x, 2) for x in q]} connects to nothing.",
                            [p.id],
                            "Move the end point onto the nozzle / duct / equipment it should connect to.",
                        )
                    )
    for i in range(n):
        for j in range(i + 1, n):
            if parts[i].kind == parts[j].kind and _overlap_ratio(boxes[i], boxes[j]) > 0.95:
                findings.append(
                    Finding(
                        "duplicate",
                        "warn",
                        f"{parts[i].id} and {parts[j].id} occupy the same space.",
                        [parts[i].id, parts[j].id],
                        "Remove the duplicate or move it to its own position.",
                    )
                )
    # connected groups of touching parts
    seen, islands, sizes = np.zeros(n, bool), 0, []
    for s in range(n):
        if seen[s]:
            continue
        islands += 1
        stack, k = [s], 0
        seen[s] = True
        while stack:
            u = stack.pop()
            k += 1
            for v in np.nonzero(touch[u])[0]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(int(v))
        sizes.append(k)
    if islands > 1:
        findings.append(
            Finding(
                "islands",
                "info",
                f"the design has {islands} separate groups of parts (sizes {sorted(sizes, reverse=True)[:8]}).",
                fix="Connect related equipment with ducts, pipes, conveyors or a common slab where they belong together.",
            )
        )
    bbox = [float(x) for x in (*boxes[:, :3].min(0), *boxes[:, 3:].max(0))] if n else [0.0] * 6
    if plan:
        ov = plan.get("overall") or {}
        ext = sorted([bbox[3] - bbox[0], bbox[4] - bbox[1]], reverse=True)
        want = sorted(
            [v for v in (ov.get("length_m"), ov.get("width_m")) if isinstance(v, (int, float)) and v > 0], reverse=True
        )
        checks = list(zip(ext, want, ("length", "width")))
        if isinstance(ov.get("height_m"), (int, float)) and ov["height_m"] > 0:
            checks.append((bbox[5] - max(bbox[2], 0.0), ov["height_m"], "height"))
        for got, exp, what in checks:
            if exp and not 0.6 <= got / exp <= 1.6:
                findings.append(
                    Finding(
                        "scale",
                        "warn",
                        f"overall {what} is {got:.1f} m but the plan says about {exp:.1f} m.",
                        fix=f"Rescale the layout so its {what} matches the brief.",
                    )
                )
        words = set().union(*(_words(p.id + " " + p.group) for p in parts)) if parts else set()
        for comp in (plan.get("components") or [])[:30]:
            name = str(comp.get("name", "")) if isinstance(comp, dict) else str(comp)
            w = _words(name)
            if w and not (w & words) and not any(any(x in y or y in x for y in words) for x in w):
                findings.append(
                    Finding(
                        "coverage",
                        "warn",
                        f"planned component '{name}' is not recognisable in the design.",
                        fix=f"Add '{name}' (use its name in the part ids or group).",
                    )
                )
    # each rule counts at most 4 times, so one systematic mistake does not drown every other signal
    per_rule: dict[str, list[int]] = {}
    for f in findings:
        per_rule.setdefault(f.rule, []).append(WEIGHTS[f.severity])
    penalty = sum(sum(sorted(w, reverse=True)[:4]) for w in per_rule.values())
    return Critique(max(0, 100 - penalty), findings, [round(x, 3) for x in bbox], islands)
