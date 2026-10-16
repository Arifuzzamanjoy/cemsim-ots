"""Retrieval library of equipment patterns (retrieval-augmented generation for the design agent).

Each template is a small, valid spec fragment that shows *how* a recognisable piece of equipment is built
from the primitives, with realistic proportions.  The agent retrieves the few that match the brief and
plan (BM25 over names, keywords and descriptions) and shows them to the model as reference patterns:
retrieval of worked examples is what makes small primitive vocabularies produce recognisable equipment.
Every template is validated by the test-suite with `parse_spec`.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    keywords: tuple[str, ...]
    description: str
    parts: tuple[dict[str, Any], ...]

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parts": [dict(p) for p in self.parts]}

    def compact(self) -> str:
        return json.dumps(self.spec(), separators=(",", ":"))


GREY, STEEL, BLUE, DUCT, DRIVE, HOT, CONCRETE = (
    "#8d9399",
    "#4a5058",
    "#6f8fb5",
    "#d4ac0d",
    "#2f9e44",
    "#c0563a",
    "#a8a8a0",
)

TEMPLATES: tuple[Template, ...] = (
    Template(
        "cyclone",
        "Cyclone separator",
        ("cyclone", "separator", "preheater", "dust", "gas", "solids", "stage"),
        "Cylinder barrel with a cone below (axis -z), a gas outlet (vortex finder) on top, a tangential inlet duct and a dip pipe for solids.",
        (
            {
                "id": "cy_barrel",
                "kind": "cylinder",
                "radius": 2.5,
                "height": 5,
                "at": [0, 0, 12],
                "color": BLUE,
                "group": "cyclone",
            },
            {
                "id": "cy_cone",
                "kind": "cone",
                "radius": 2.5,
                "radius2": 0.5,
                "height": 7,
                "axis": "-z",
                "at": [0, 0, 12],
                "color": BLUE,
                "group": "cyclone",
            },
            {
                "id": "cy_outlet",
                "kind": "cylinder",
                "radius": 1.2,
                "height": 2,
                "at": [0, 0, 17],
                "color": DUCT,
                "group": "cyclone",
            },
            {
                "id": "cy_inlet",
                "kind": "box",
                "size": [3, 1.4, 2.6],
                "at": [-1.0, -3.9, 14],
                "color": DUCT,
                "group": "cyclone",
            },
            {
                "id": "cy_dip",
                "kind": "pipe",
                "radius": 0.3,
                "path": [[0, 0, 5], [0, 0, 1]],
                "color": DUCT,
                "group": "cyclone",
            },
            {
                "id": "cy_legs",
                "kind": "box",
                "size": [0.4, 0.4, 12],
                "at": [2.3, -0.2, 0],
                "color": STEEL,
                "group": "structure",
            },
        ),
    ),
    Template(
        "vertical_tank",
        "Vertical storage tank",
        ("tank", "storage", "vessel", "silo", "water", "oil", "fuel", "farm", "bund"),
        "Cylindrical shell on a slab with a conical roof, an outlet nozzle and pipe.",
        (
            {
                "id": "tk_slab",
                "kind": "box",
                "size": [8, 8, 0.4],
                "at": [-4, -4, -0.4],
                "color": CONCRETE,
                "group": "civil",
            },
            {
                "id": "tk_shell",
                "kind": "cylinder",
                "radius": 3,
                "height": 8,
                "at": [0, 0, 0],
                "color": BLUE,
                "group": "tank",
            },
            {
                "id": "tk_roof",
                "kind": "cone",
                "radius": 3,
                "radius2": 0.3,
                "height": 1.2,
                "at": [0, 0, 8],
                "color": BLUE,
                "group": "tank",
            },
            {
                "id": "tk_outlet",
                "kind": "pipe",
                "radius": 0.15,
                "path": [[3, 0, 0.8], [5, 0, 0.8]],
                "color": DUCT,
                "group": "piping",
            },
        ),
    ),
    Template(
        "silo",
        "Silo with hopper",
        ("silo", "bin", "hopper", "cement", "clinker", "storage", "bulk", "meal"),
        "Tall cylinder on a skirt (tube) with a conical hopper (axis -z) inside the skirt and a top-house box.",
        (
            {
                "id": "si_skirt",
                "kind": "tube",
                "radius": 6,
                "inner_radius": 5.6,
                "height": 8,
                "at": [0, 0, 0],
                "color": CONCRETE,
                "group": "silo",
            },
            {
                "id": "si_shell",
                "kind": "cylinder",
                "radius": 6,
                "height": 30,
                "at": [0, 0, 8],
                "color": CONCRETE,
                "group": "silo",
            },
            {
                "id": "si_hopper",
                "kind": "cone",
                "radius": 5.6,
                "radius2": 0.6,
                "height": 5,
                "axis": "-z",
                "at": [0, 0, 8],
                "color": GREY,
                "group": "silo",
            },
            {"id": "si_tophouse", "kind": "box", "size": [6, 4, 3], "at": [-3, -2, 38], "color": GREY, "group": "silo"},
        ),
    ),
    Template(
        "fan",
        "Centrifugal fan with motor",
        ("fan", "blower", "id", "exhaust", "draught", "induced", "motor", "air"),
        "Short wide cylinder (axis y) as the scroll casing, an outlet duct box, a motor box on a base frame.",
        (
            {"id": "fan_base", "kind": "box", "size": [7, 3, 0.5], "at": [-2, -1.5, 0], "color": STEEL, "group": "fan"},
            {
                "id": "fan_casing",
                "kind": "cylinder",
                "radius": 1.8,
                "height": 1.2,
                "axis": "y",
                "at": [0, -0.6, 2.3],
                "color": BLUE,
                "group": "fan",
            },
            {
                "id": "fan_outlet",
                "kind": "box",
                "size": [1.4, 1.2, 2.2],
                "at": [0.4, -0.6, 4.1],
                "color": DUCT,
                "group": "fan",
            },
            {
                "id": "fan_motor",
                "kind": "cylinder",
                "radius": 0.6,
                "height": 1.8,
                "axis": "x",
                "at": [2.8, 0, 1.1],
                "color": DRIVE,
                "group": "drive",
            },
            {
                "id": "fan_shaft",
                "kind": "cylinder",
                "radius": 0.12,
                "height": 1.0,
                "axis": "x",
                "at": [1.8, 0, 1.1],
                "color": STEEL,
                "group": "drive",
            },
        ),
    ),
    Template(
        "stack",
        "Stack / chimney",
        ("stack", "chimney", "flue", "exhaust", "vent"),
        "Tall tube on a foundation with a platform near the top.",
        (
            {
                "id": "st_found",
                "kind": "box",
                "size": [5, 5, 1],
                "at": [-2.5, -2.5, -1],
                "color": CONCRETE,
                "group": "civil",
            },
            {
                "id": "st_shell",
                "kind": "tube",
                "radius": 1.6,
                "inner_radius": 1.4,
                "height": 45,
                "at": [0, 0, 0],
                "color": GREY,
                "group": "stack",
            },
            {
                "id": "st_platform",
                "kind": "tube",
                "radius": 2.6,
                "inner_radius": 1.6,
                "height": 0.2,
                "at": [0, 0, 40],
                "color": STEEL,
                "group": "stack",
            },
        ),
    ),
    Template(
        "rotary_kiln",
        "Rotary kiln on piers",
        ("kiln", "rotary", "dryer", "drum", "tyre", "pier", "girth", "burner", "calciner"),
        "Long horizontal cylinder (axis x) on concrete piers with tyres (tubes), a girth gear and drive, inlet and hood boxes.",
        (
            {
                "id": "k_shell",
                "kind": "cylinder",
                "radius": 2.2,
                "height": 60,
                "axis": "x",
                "at": [0, 0, 7],
                "color": HOT,
                "group": "kiln",
            },
            {
                "id": "k_pier1",
                "kind": "box",
                "size": [3, 6, 4.2],
                "at": [8.5, -3, 0],
                "color": CONCRETE,
                "group": "civil",
            },
            {
                "id": "k_pier2",
                "kind": "box",
                "size": [3, 6, 4.2],
                "at": [28.5, -3, 0],
                "color": CONCRETE,
                "group": "civil",
            },
            {
                "id": "k_pier3",
                "kind": "box",
                "size": [3, 6, 4.2],
                "at": [48.5, -3, 0],
                "color": CONCRETE,
                "group": "civil",
            },
            {
                "id": "k_tyre1",
                "kind": "tube",
                "radius": 2.6,
                "inner_radius": 2.2,
                "height": 0.9,
                "axis": "x",
                "at": [9.55, 0, 7],
                "color": STEEL,
                "group": "kiln",
            },
            {
                "id": "k_tyre2",
                "kind": "tube",
                "radius": 2.6,
                "inner_radius": 2.2,
                "height": 0.9,
                "axis": "x",
                "at": [29.55, 0, 7],
                "color": STEEL,
                "group": "kiln",
            },
            {
                "id": "k_tyre3",
                "kind": "tube",
                "radius": 2.6,
                "inner_radius": 2.2,
                "height": 0.9,
                "axis": "x",
                "at": [49.55, 0, 7],
                "color": STEEL,
                "group": "kiln",
            },
            {
                "id": "k_girth",
                "kind": "tube",
                "radius": 3.0,
                "inner_radius": 2.2,
                "height": 0.4,
                "axis": "x",
                "at": [20, 0, 7],
                "color": STEEL,
                "group": "drive",
            },
            {"id": "k_drive", "kind": "box", "size": [2.5, 2, 2], "at": [19, 2.8, 0], "color": DRIVE, "group": "drive"},
            {"id": "k_inlet", "kind": "box", "size": [3, 6, 7], "at": [-3, -3, 4], "color": GREY, "group": "kiln"},
            {"id": "k_hood", "kind": "box", "size": [3, 7, 8], "at": [60, -3.5, 3], "color": GREY, "group": "kiln"},
        ),
    ),
    Template(
        "bag_filter",
        "Bag filter (baghouse)",
        ("filter", "bag", "baghouse", "dust", "collector", "dedusting", "esp"),
        "Box housing on legs with pyramid-like hoppers (cones, axis -z) below, a clean-gas duct and a fan connection.",
        (
            {"id": "bf_housing", "kind": "box", "size": [8, 4, 6], "at": [-4, -2, 6], "color": BLUE, "group": "filter"},
            {
                "id": "bf_hopper1",
                "kind": "cone",
                "radius": 1.9,
                "radius2": 0.3,
                "height": 2.5,
                "axis": "-z",
                "at": [-2, 0, 6],
                "color": BLUE,
                "group": "filter",
            },
            {
                "id": "bf_hopper2",
                "kind": "cone",
                "radius": 1.9,
                "radius2": 0.3,
                "height": 2.5,
                "axis": "-z",
                "at": [2, 0, 6],
                "color": BLUE,
                "group": "filter",
            },
            {
                "id": "bf_leg1",
                "kind": "box",
                "size": [0.3, 0.3, 6],
                "at": [-4, -2, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "bf_leg2",
                "kind": "box",
                "size": [0.3, 0.3, 6],
                "at": [3.7, -2, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "bf_leg3",
                "kind": "box",
                "size": [0.3, 0.3, 6],
                "at": [-4, 1.7, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "bf_leg4",
                "kind": "box",
                "size": [0.3, 0.3, 6],
                "at": [3.7, 1.7, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "bf_clean_duct",
                "kind": "pipe",
                "radius": 0.6,
                "path": [[4, 0, 11], [7, 0, 11], [7, 0, 3]],
                "color": DUCT,
                "group": "ducts",
            },
        ),
    ),
    Template(
        "belt_conveyor",
        "Belt conveyor on trestles",
        ("conveyor", "belt", "transport", "gallery", "feeder", "trestle"),
        "Long thin box (the belt / gallery) on trestle columns, with head and tail pulleys (cylinders, axis y).",
        (
            {
                "id": "cv_belt",
                "kind": "box",
                "size": [30, 1.2, 0.4],
                "at": [0, -0.6, 4],
                "color": GREY,
                "group": "conveyor",
            },
            {
                "id": "cv_trestle1",
                "kind": "box",
                "size": [0.3, 1.2, 4],
                "at": [5, -0.6, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "cv_trestle2",
                "kind": "box",
                "size": [0.3, 1.2, 4],
                "at": [15, -0.6, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "cv_trestle3",
                "kind": "box",
                "size": [0.3, 1.2, 4],
                "at": [25, -0.6, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "cv_head",
                "kind": "cylinder",
                "radius": 0.4,
                "height": 1.4,
                "axis": "y",
                "at": [30, -0.7, 4.2],
                "color": DRIVE,
                "group": "conveyor",
            },
            {
                "id": "cv_tail",
                "kind": "cylinder",
                "radius": 0.35,
                "height": 1.4,
                "axis": "y",
                "at": [0, -0.7, 4.2],
                "color": STEEL,
                "group": "conveyor",
            },
        ),
    ),
    Template(
        "pump_skid",
        "Pump skid",
        ("pump", "skid", "centrifugal", "motor", "piping", "header", "water"),
        "Base plate with a pump casing (cylinder, axis y), a motor (cylinder, axis x), suction and discharge pipes.",
        (
            {
                "id": "pp_base",
                "kind": "box",
                "size": [3, 1.2, 0.2],
                "at": [0, -0.6, 0],
                "color": STEEL,
                "group": "skid",
            },
            {
                "id": "pp_casing",
                "kind": "cylinder",
                "radius": 0.35,
                "height": 0.4,
                "axis": "y",
                "at": [0.6, -0.2, 0.6],
                "color": BLUE,
                "group": "pump",
            },
            {
                "id": "pp_motor",
                "kind": "cylinder",
                "radius": 0.3,
                "height": 1.2,
                "axis": "x",
                "at": [1.4, 0, 0.6],
                "color": DRIVE,
                "group": "pump",
            },
            {
                "id": "pp_motor_foot",
                "kind": "box",
                "size": [1.2, 0.5, 0.2],
                "at": [1.4, -0.25, 0.2],
                "color": STEEL,
                "group": "pump",
            },
            {
                "id": "pp_pedestal",
                "kind": "box",
                "size": [0.5, 0.5, 0.3],
                "at": [0.35, -0.25, 0.2],
                "color": STEEL,
                "group": "pump",
            },
            {
                "id": "pp_suction",
                "kind": "pipe",
                "radius": 0.1,
                "path": [[0.25, 0, 0.6], [-1.5, 0, 0.6]],
                "color": DUCT,
                "group": "piping",
            },
            {
                "id": "pp_discharge",
                "kind": "pipe",
                "radius": 0.08,
                "path": [[0.6, 0, 0.95], [0.6, 0, 2.0], [-1.5, 0, 2.0]],
                "color": DUCT,
                "group": "piping",
            },
        ),
    ),
    Template(
        "steel_structure",
        "Steel structure with floors",
        ("structure", "frame", "tower", "building", "floor", "platform", "steel", "column", "rack"),
        "Four corner columns (thin boxes) with floor plates (thin boxes) at several levels; equipment sits on the floors.",
        (
            {
                "id": "fr_col1",
                "kind": "box",
                "size": [0.4, 0.4, 20],
                "at": [-5, -5, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "fr_col2",
                "kind": "box",
                "size": [0.4, 0.4, 20],
                "at": [4.6, -5, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "fr_col3",
                "kind": "box",
                "size": [0.4, 0.4, 20],
                "at": [-5, 4.6, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "fr_col4",
                "kind": "box",
                "size": [0.4, 0.4, 20],
                "at": [4.6, 4.6, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "fr_floor1",
                "kind": "box",
                "size": [10, 10, 0.3],
                "at": [-5, -5, 10],
                "color": GREY,
                "group": "structure",
                "opacity": 0.6,
            },
            {
                "id": "fr_floor2",
                "kind": "box",
                "size": [10, 10, 0.3],
                "at": [-5, -5, 19.7],
                "color": GREY,
                "group": "structure",
                "opacity": 0.6,
            },
        ),
    ),
    Template(
        "heat_exchanger",
        "Shell-and-tube heat exchanger",
        ("exchanger", "heat", "cooler", "condenser", "shell", "tube", "heater"),
        "Horizontal cylinder (axis x) with domed heads (spheres), two saddles and four nozzles.",
        (
            {
                "id": "hx_shell",
                "kind": "cylinder",
                "radius": 0.6,
                "height": 5,
                "axis": "x",
                "at": [0, 0, 1.5],
                "color": BLUE,
                "group": "exchanger",
            },
            {"id": "hx_head1", "kind": "sphere", "radius": 0.6, "at": [0, 0, 1.5], "color": BLUE, "group": "exchanger"},
            {"id": "hx_head2", "kind": "sphere", "radius": 0.6, "at": [5, 0, 1.5], "color": BLUE, "group": "exchanger"},
            {
                "id": "hx_saddle1",
                "kind": "box",
                "size": [0.4, 1.0, 1.0],
                "at": [0.8, -0.5, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "hx_saddle2",
                "kind": "box",
                "size": [0.4, 1.0, 1.0],
                "at": [3.8, -0.5, 0],
                "color": STEEL,
                "group": "structure",
            },
            {
                "id": "hx_nozzle_in",
                "kind": "pipe",
                "radius": 0.1,
                "path": [[1.2, 0, 2.1], [1.2, 0, 2.8]],
                "color": DUCT,
                "group": "piping",
            },
            {
                "id": "hx_nozzle_out",
                "kind": "pipe",
                "radius": 0.1,
                "path": [[3.8, 0, 2.1], [3.8, 0, 2.8]],
                "color": DUCT,
                "group": "piping",
            },
        ),
    ),
)

_TOK = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOK.findall(text.lower()) if len(t) > 2]


def _doc(t: Template) -> list[str]:
    return _tokens(" ".join((t.name, t.description, " ".join(t.keywords) * 2)))


def search(query: str, k: int = 3) -> list[Template]:
    """BM25 ranking of the templates for a free-text query (brief + planned component names)."""
    q = Counter(_tokens(query))
    if not q:
        return []
    docs = [_doc(t) for t in TEMPLATES]
    n = len(docs)
    avg = sum(map(len, docs)) / n
    df = Counter(w for d in docs for w in set(d))
    k1, b = 1.4, 0.75
    scored = []
    for t, d in zip(TEMPLATES, docs):
        tf = Counter(d)
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
        if s > 0:
            scored.append((s, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:k]]
