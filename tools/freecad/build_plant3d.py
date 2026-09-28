"""FreeCAD design script for the CemSim 3-D plant (run inside FreeCAD 1.x).

Builds a to-scale 3000 t/d kiln line - preheater tower with steel structure,
5 cyclones with tangential inlets, calciner with gooseneck, rotary kiln on 3
piers (tyres, support rollers, girth gear, drive), inlet chamber, hood, burner,
tertiary-air duct, grate cooler with fans, vent system, ID fan and stack - and
exports a tessellated, named mesh file for the browser 3-D view:

    export_json(path)  ->  {"meta": {...anchors...}, "objects": [...]}

Coordinates here: mm, x along the kiln axis (feed end -> burner), y lateral, z up.
The export converts to metres in three.js axes (X = x, Y = z, Z = -y) and
quantises to centimetres.  Object names drive live bindings in plant3d.js:
KilnSegNN (shell scanner), Kiln*/Tyre*/GirthGear (rotating group), CycS1..5,
Calciner, *Fan*, CoolFanN, ...

Usage from FreeCAD (or via the FreeCAD MCP execute_code):
    exec(open(r"C:\\Users\\Public\\cemsim\\build_plant3d.py").read())
    build(); export_json(r"C:\\Users\\Public\\cemsim\\plant3d.json")
"""

import itertools
import json
import math

import FreeCAD as App
import Part

V = App.Vector
DOC = "CemSimPlant3D"

# ---------------------------------------------------------------- design data
KILN_L, KILN_R, SLOPE = 62000.0, 2200.0, math.atan(0.035)
Z_OUT = 7000.0  # kiln axis height at the outlet
DIR = V(math.cos(SLOPE), 0, -math.sin(SLOPE))  # inlet -> outlet
P_IN = V(0, 0, Z_OUT + KILN_L * math.sin(SLOPE))  # kiln axis at the feed end
N_SEG = 30
X_R, X_L, X_CH = -12000.0, -24000.0, -18000.0  # cyclone columns and riser channel
CYC_R, CYC_H, CONE_H, VF_R = 3000.0, 6500.0, 7000.0, 1300.0
STAGES = {
    "S1": (X_R, 84000.0),
    "S2": (X_L, 71000.0),
    "S3": (X_R, 58000.0),
    "S4": (X_L, 45000.0),
    "S5": (X_R - 1500.0, 30000.0),
}  # (x, cylinder bottom z)
# tangential inlet side: S1-S4 face the central riser channel, S5 faces the calciner
INLET_SIDE = {"S1": "L", "S2": "R", "S3": "L", "S4": "R", "S5": "R"}
CAL_X, CAL_R, CAL_Z0, CAL_Z1 = -1500.0, 3200.0, 17000.0, 42000.0
DUCT_R = 1400.0

COL = {
    "steel": (0.64, 0.66, 0.68),
    "duct": (0.60, 0.57, 0.52),
    "shell": (0.33, 0.33, 0.35),
    "concrete": (0.74, 0.72, 0.68),
    "structure": (0.30, 0.40, 0.55),
    "fan": (0.22, 0.50, 0.32),
    "motor": (0.20, 0.33, 0.55),
    "meal": (0.72, 0.55, 0.50),
    "air": (0.45, 0.55, 0.72),
    "dark": (0.14, 0.14, 0.15),
    "ground": (0.50, 0.52, 0.47),
    "refractory": (0.70, 0.62, 0.55),
    "coal": (0.25, 0.22, 0.20),
    "yellow": (0.85, 0.72, 0.15),
}
PARTS = []  # (name, shape, color, group, opacity)
META = {}


def m3(v):
    """Point (mm, z-up) -> three.js metres (X, Y-up, Z = -y) for the metadata."""
    v = list(v)
    return [v[0] / 1000.0, v[2] / 1000.0, -v[1] / 1000.0]


def add(name, shape, color="steel", group="static", opacity=1.0):
    PARTS.append((name, shape, COL.get(color, color), group, opacity))


def axis_pt(s):
    """Point on the kiln axis at distance s (mm) from the feed end."""
    return P_IN + DIR * s


def tube(name, pts, r, color="duct"):
    """Orthogonal duct through waypoints: cylinders + spherical elbows."""
    shapes = []
    for a, b in itertools.pairwise(pts):
        a, b = V(*a), V(*b)
        L = (b - a).Length
        if L > 1:
            shapes.append(Part.makeCylinder(r, L, a, b - a))
    for p in pts[1:-1]:
        shapes.append(Part.makeSphere(r, V(*p)))
    s = shapes[0]
    for x in shapes[1:]:
        s = s.fuse(x)
    add(name, s.removeSplitter() if hasattr(s, "removeSplitter") else s, color)


def box(name, x0, y0, z0, dx, dy, dz, color="steel", group="static", opacity=1.0):
    add(name, Part.makeBox(dx, dy, dz, V(x0, y0, z0)), color, group, opacity)


# ---------------------------------------------------------------- equipment
def build_ground():
    box("Ground", -60000, -40000, -400, 175000, 80000, 400, "ground")


def build_kiln():
    seg = KILN_L / N_SEG
    for i in range(N_SEG):
        add(f"KilnSeg{i:02d}", Part.makeCylinder(KILN_R, seg * 0.99, axis_pt(i * seg), DIR), "shell", "kiln_rot")
    for j in range(4):  # rotation marker stripes
        ang = j * math.pi / 2
        off = V(0, math.cos(ang) * (KILN_R + 20), math.sin(ang) * (KILN_R + 20))
        b = Part.makeCylinder(90, KILN_L * 0.9, axis_pt(KILN_L * 0.05) + off, DIR)
        add(f"KilnStripe{j}", b, "dark", "kiln_rot")
    piers = (10000.0, 32000.0, 54000.0)
    for n, s in enumerate(piers):
        c = axis_pt(s - 450)
        ring = Part.makeCylinder(KILN_R + 450, 900, c, DIR).cut(Part.makeCylinder(KILN_R, 900, c, DIR))
        add(f"Tyre{n}", ring, "dark", "kiln_rot")
        a = axis_pt(s)
        for k, sy in enumerate((-1, 1)):  # support rollers at +-30 deg
            rc = a + V(0, sy * 3350 * math.sin(math.radians(30)), -3350 * math.cos(math.radians(30)))
            add(f"Roller{n}{'ab'[k]}", Part.makeCylinder(700, 1000, rc - DIR * 500, DIR), "dark")
        top = a.z - 3350 * math.cos(math.radians(30)) - 900
        add(f"Pier{n}", Part.makeBox(3200, 7600, top, V(a.x - 1600, -3800, 0)), "concrete")
    g = axis_pt(24000 - 300)
    gear = Part.makeCylinder(KILN_R + 900, 600, g, DIR).cut(Part.makeCylinder(KILN_R, 600, g, DIR))
    add("GirthGear", gear, "dark", "kiln_rot")
    gp = axis_pt(24000)
    add("KilnPinion", Part.makeCylinder(500, 800, gp + V(0, KILN_R + 1400, -900) - DIR * 400, DIR), "dark")
    box("KilnGearbox", gp.x - 1400, KILN_R + 2100, 0, 2800, 2400, gp.z - 1500, "motor")
    add("KilnDriveMotor", Part.makeCylinder(700, 2200, V(gp.x + 1400, KILN_R + 3300, gp.z - 2600), V(1, 0, 0)), "motor")
    META["kiln"] = {
        "inlet": m3(P_IN),
        "outlet": m3(axis_pt(KILN_L)),
        "radius": KILN_R / 1000.0,
        "n_seg": N_SEG,
        "piers": [m3(axis_pt(s)) for s in piers],
    }


def build_inlet_and_calciner():
    box("InletChamber", -6500, -3600, 3000, 7000, 7200, 12500, "refractory")
    # calciner: conical bottom on the inlet chamber, cylinder, domed top
    add(
        "Calciner",
        Part.makeCone(1800, CAL_R, CAL_Z0 - 15500, V(CAL_X, 0, 15500)).fuse(
            Part.makeCylinder(CAL_R, CAL_Z1 - CAL_Z0, V(CAL_X, 0, CAL_Z0))
        ),
        "steel",
    )
    META["calciner"] = {"top": m3((CAL_X, 0, CAL_Z1)), "mid": m3((CAL_X, 0, 30000)), "radius": CAL_R / 1000.0}


def cyclone(name, x, z0, inlet_side):
    """Cylinder + cone + vortex finder + rectangular tangential inlet."""
    body = Part.makeCylinder(CYC_R, CYC_H, V(x, 0, z0))
    cone = Part.makeCone(600, CYC_R, CONE_H, V(x, 0, z0 - CONE_H))
    vf = Part.makeCylinder(VF_R, 2000, V(x, 0, z0 + CYC_H))
    sgn = -1 if inlet_side == "L" else 1
    inlet = Part.makeBox(
        2600, 1800, 3000, V(x + sgn * CYC_R - (2600 if sgn < 0 else 0), CYC_R - 1800, z0 + CYC_H - 3200)
    )
    add(f"Cyc{name}", body.fuse(cone).fuse(vf).fuse(inlet), "steel")
    return {
        "out": (x, 0, z0 + CYC_H + 2000),
        "in": (x + sgn * (CYC_R + 2600), CYC_R - 900, z0 + CYC_H - 1700),
        "tip": (x, 0, z0 - CONE_H),
    }


def build_preheater():
    ports = {n: cyclone(n, x, z0, INLET_SIDE[n]) for n, (x, z0) in STAGES.items()}
    # gooseneck: calciner roof -> up -> over -> down -> into S5's tangential inlet face
    ix, iy, iz = ports["S5"]["in"]
    xd = ix + 1300  # down-leg, clear of the calciner shell
    assert xd + 1600 < CAL_X - CAL_R, "gooseneck down-leg clashes with the calciner"
    tube(
        "DuctCalS5",
        [
            (CAL_X, 0, CAL_Z1 - 200),
            (CAL_X, 0, CAL_Z1 + 2000),
            (xd, 0, CAL_Z1 + 2000),
            (xd, 0, iz),
            (xd, iy, iz),
            (ix, iy, iz),
        ],
        1600,
    )
    META["cyclones"] = {
        n: {"center": m3((STAGES[n][0], 0, STAGES[n][1] + CYC_H / 2)), "radius": CYC_R / 1000.0} for n in STAGES
    }
    # gas risers: roof outlet of the lower stage -> up -> central channel -> side inlet of the next
    order = ["S5", "S4", "S3", "S2", "S1"]
    for lo, hi in itertools.pairwise(order):
        ox, _, oz = ports[lo]["out"]
        ix, iy, iz = ports[hi]["in"]
        tube(
            f"Duct{lo}{hi}",
            [(ox, 0, oz - 300), (ox, 0, oz + 1500), (X_CH, 0, oz + 1500), (X_CH, 0, iz), (X_CH, iy, iz), (ix, iy, iz)],
            DUCT_R,
        )
    # S1 -> down-comer -> ID fan -> stack
    ox, _, oz = ports["S1"]["out"]
    tube(
        "DuctS1Fan",
        [(ox, 0, oz - 300), (ox, 0, oz + 2500), (-38000, 0, oz + 2500), (-38000, 0, 7000), (-38000, 3500, 7000)],
        1700,
    )
    add("IDFan", Part.makeCylinder(3000, 2600, V(-38000, 3500, 4200), V(0, 1, 0)), "fan")
    add("IDFanMotor", Part.makeCylinder(900, 2600, V(-38000, 6100, 4200), V(0, 1, 0)), "motor")
    tube("DuctFanStack", [(-38000, 4800, 4200), (-38000, 4800, 1500), (-46000, 4800, 1500), (-46000, 0, 1500)], 1500)
    add("Stack", Part.makeCylinder(2300, 90000, V(-46000, 0, 0)), "concrete")
    # meal dip pipes: cone tip -> riser feeding the next stage (S4 -> calciner, S5 -> inlet chamber)
    # meal falls by gravity: the junction is 1.5 m BELOW the cone tip and must lie on
    # the vertical riser that feeds the next stage down (riser = lower stage outlet -> inlet)
    riser_of = {"S2": ("S3", "S2"), "S3": ("S4", "S3"), "S4": ("S5", "S4")}
    for up, lo in (("S1", "S2"), ("S2", "S3"), ("S3", "S4")):
        tx, _, tz = ports[up]["tip"]
        zr = tz - 1500
        r_lo, r_hi = riser_of[lo]
        z_bot, z_top = ports[r_lo]["out"][2] + 1500, ports[r_hi]["in"][2]
        assert z_bot < zr < z_top, f"meal {up}: junction {zr} outside riser {z_bot}..{z_top}"
        tube(f"Meal{up}", [(tx, 0, tz), (tx, 0, zr), (X_CH, 0, zr)], 350, "meal")
    tx, _, tz = ports["S4"]["tip"]
    tube(
        "MealS4",
        [(tx, 0, tz), (tx, 0, 22000), (tx, -4200, 22000), (CAL_X - CAL_R, -4200, 22000), (CAL_X - CAL_R, 0, 22000)],
        350,
        "meal",
    )
    tx, _, tz = ports["S5"]["tip"]
    tube("MealS5", [(tx, 0, tz), (tx, 0, 16500), (-5200, 0, 16500), (-5200, 0, 15500)], 450, "meal")
    # kiln feed: bucket elevator -> riser S2->S1
    box("FeedElevator", -36000, 9000, 0, 2400, 2400, 92000, "structure")
    fz = ports["S1"]["in"][2] - 2500
    tube("KilnFeedLine", [(-34800, 10200, fz), (X_CH, 10200, fz), (X_CH, 1400, fz)], 350, "meal")
    META["feed"] = m3((X_CH, 0, fz))
    # steel structure: 6 columns, ring beams, bracing
    xs, ys = (-30000.0, -18000.0, -6500.0), (-7500.0, 7500.0)
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            box(f"Column{i}{j}", x - 450, y - 450, 0, 900, 900, 98000, "structure")
    for k, z in enumerate((27000, 42000, 55000, 68000, 81000, 95000)):
        for j, y in enumerate(ys):
            box(f"BeamX{k}{j}", xs[0], y - 300, z, xs[-1] - xs[0], 600, 700, "structure")
        for i, x in enumerate(xs):
            box(f"BeamY{k}{i}", x - 300, ys[0], z, 600, ys[1] - ys[0], 700, "structure")
        box(f"Floor{k}", xs[0], ys[0], z + 700, xs[-1] - xs[0], ys[1] - ys[0], 60, "structure", opacity=0.35)
    META["tower"] = {"x": [xs[0] / 1000.0, xs[-1] / 1000.0], "top": 98.0}
    META["id_fan"] = m3((-38000, 3500, 4200))
    META["stack_top"] = m3((-46000, 0, 90000))
    META["ph_exit"] = m3((-38000, 0, 96000))


def build_hood_burner_ta():
    xo = axis_pt(KILN_L).x
    box("KilnHood", xo - 1500, -4200, 2500, 5500, 8400, 13500, "refractory")
    tip = axis_pt(KILN_L - 1200)
    add("BurnerPipe", Part.makeCylinder(420, 14000, tip, DIR), "dark")
    # firing floor: elevated slab above the cooler (burner carriage level), columns outside the cooler
    box("BurnerPlatform", xo + 5000, -4600, 5700, 12000, 9200, 300, "structure")
    for i, dx in enumerate((5200, 16600)):
        for j, y in enumerate((-4400, 4000)):
            box(f"PlatformColumn{i}{j}", xo + dx, y, 0, 400, 400, 5700, "structure")
    add("PAFan", Part.makeCylinder(1100, 1300, V(xo + 11500, -4000, 7200), V(0, 1, 0)), "fan")
    box("CoalBinKiln", xo + 9000, 6500, 0, 4000, 4000, 16000, "coal")
    META["burner"] = {"tip": m3(tip), "dir": [-DIR.x, -DIR.z, DIR.y]}  # flame points into the kiln
    # tertiary air duct: hood roof -> over the kiln -> calciner side
    tube("TADuct", [(xo + 1000, 0, 15500), (xo + 1000, 0, 21000), (CAL_X + CAL_R - 100, 0, 21000)], 1300, "air")
    box("TADamper", 30000 - 700, -1700, 21000 - 1700, 1400, 3400, 3400, "yellow")
    add("CoalLineCal", Part.makeCylinder(250, 60000, V(CAL_X + CAL_R, 1800, 23500), V(1, 0, 0)), "coal")
    META["hood"] = m3((xo + 1000, 0, 9000))
    META["ta_duct"] = m3((20000, 0, 21000))


def build_cooler():
    x0 = axis_pt(KILN_L).x + 1500
    L, W = 20000.0, 5200.0
    H = 5500.0  # below the burner pipe / firing floor
    add(
        "CoolerCasing",
        Part.makeBox(L, W, H, V(x0, -W / 2, 0)).cut(
            Part.makeBox(L - 400, W - 400, H - 200, V(x0 + 200, -W / 2 + 200, 200))
        ),
        "steel",
        opacity=0.30,
    )
    box("CoolerGrate", x0 + 200, -W / 2 + 200, 1900, L - 400, W - 400, 150, "dark")
    bounds = ((0, 2), (2, 5), (5, 8), (8, 11), (11, 14), (14, 18))
    for i, (a, b) in enumerate(bounds):
        cx = x0 + 1000 + (a + b) / 2 * 1000
        add(f"CoolFan{i + 1}", Part.makeCylinder(900, 1200, V(cx, W / 2 + 1800, 1000), V(0, 1, 0)), "fan")
        tube(f"CoolAirDuct{i + 1}", [(cx, W / 2 + 1800, 1000), (cx, 0, 1000), (cx, 0, 1700)], 450, "air")
    tube("VentDuct", [(x0 + L - 2500, 0, H - 100), (x0 + L - 2500, 0, 12000), (x0 + L + 6000, 0, 12000)], 1300, "air")
    add("VentFan", Part.makeCylinder(1700, 1800, V(x0 + L + 6000, -900, 12000), V(0, 1, 0)), "fan")
    # cooler dust filter beside the clinker line (clear of the pan conveyor), fed from the vent fan outlet
    box("CoolerFilter", x0 + L + 2000, -14000, 0, 8000, 8000, 15000, "steel")
    tube("DuctVentFilter", [(x0 + L + 6000, -900, 12000), (x0 + L + 6000, -6100, 12000)], 1100, "air")
    box("ClinkerCrusher", x0 + L, -2000, 0, 2600, 4000, 3000, "dark")
    conv = Part.makeBox(22000, 2200, 600, V(x0 + L + 2600, -1100, 1200))
    add("PanConveyor", conv, "structure")
    META["cooler"] = {
        "x0": (x0 + 1000) / 1000.0,
        "x1": (x0 + 19000) / 1000.0,
        "z": [-(W - 800) / 2000.0, (W - 800) / 2000.0],
        "y_grate": 2.05,
        "cells": 18,
        "vent_fan": m3((x0 + L + 6000, 0, 12000)),
        "clinker_out": m3((x0 + L, 0, 3000)),
    }


def build():
    PARTS.clear()
    META.clear()
    build_ground()
    build_kiln()
    build_inlet_and_calciner()
    build_preheater()
    build_hood_burner_ta()
    build_cooler()
    d = App.listDocuments().get(DOC)
    if d is None:
        d = App.newDocument(DOC)
    for o in list(d.Objects):
        d.removeObject(o.Name)
    for name, shape, color, _group, opacity in PARTS:
        o = d.addObject("Part::Feature", name)
        o.Shape = shape
        if App.GuiUp and o.ViewObject:
            o.ViewObject.ShapeColor = color
            o.ViewObject.Transparency = int((1 - opacity) * 100)
    d.recompute()
    return len(PARTS)


# ---------------------------------------------------------------- design checks
EQUIPMENT = (
    "KilnSeg",
    "Tyre",
    "GirthGear",
    "Pier",
    "Roller",
    "KilnGearbox",
    "KilnDriveMotor",
    "InletChamber",
    "Calciner",
    "Cyc",
    "Stack",
    "IDFan",
    "FeedElevator",
    "KilnHood",
    "BurnerPlatform",
    "PlatformColumn",
    "PAFan",
    "CoalBinKiln",
    "CoolerCasing",
    "CoolFan",
    "VentFan",
    "CoolerFilter",
    "ClinkerCrusher",
    "PanConveyor",
    "Column",
    "BurnerPipe",
)
# intended contacts (welded / seated / passing through a seal)
ALLOWED = [
    ("KilnSeg", "Tyre"),
    ("KilnSeg", "GirthGear"),
    ("KilnSeg", "InletChamber"),
    ("KilnSeg", "KilnHood"),
    ("Tyre", "Roller"),
    ("Roller", "Pier"),
    ("GirthGear", "KilnPinion"),
    ("KilnHood", "CoolerCasing"),
    ("KilnHood", "BurnerPipe"),
    ("KilnSeg", "BurnerPipe"),
    ("InletChamber", "Calciner"),
    ("BurnerPlatform", "PlatformColumn"),
    ("ClinkerCrusher", "CoolerCasing"),
    ("ClinkerCrusher", "PanConveyor"),
    ("BurnerPlatform", "KilnHood"),
    ("KilnGearbox", "KilnDriveMotor"),
]


def clashes(min_vol=1e6):
    """Solid overlaps between equipment items that are not intended contacts (mm^3 > min_vol)."""
    items = [(n, sh) for n, sh, *_ in PARTS if n.startswith(EQUIPMENT)]
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (a, sa), (b, sb) = items[i], items[j]
            if any((a.startswith(x) and b.startswith(y)) or (a.startswith(y) and b.startswith(x)) for x, y in ALLOWED):
                continue
            if not sa.BoundBox.intersect(sb.BoundBox):
                continue
            v = sa.common(sb).Volume
            if v > min_vol:
                out.append((a, b, round(v / 1e9, 3)))
    return out


# ---------------------------------------------------------------- export
def _to3(v):
    """FreeCAD mm (x, y, z-up) -> three.js metres (X, Y-up, Z) quantised to cm."""
    return [round(v[0] / 10.0), round(v[2] / 10.0), round(-v[1] / 10.0)]


def export_json(path, tol=60.0):
    objs = []
    for name, shape, color, group, opacity in PARTS:
        verts, idx = [], []
        for f in shape.Faces:
            pts, tris = f.tessellate(tol)
            base = len(verts) // 3
            for p in pts:
                verts.extend(_to3(p))
            for t in tris:
                idx.extend((base + t[0], base + t[1], base + t[2]))
        objs.append(
            {
                "name": name,
                "color": [round(c, 3) for c in color],
                "group": group,
                "opacity": opacity,
                "v": verts,
                "i": idx,
            }
        )
    meta = json.loads(json.dumps(META))
    meta["units"] = "vertices in cm, three.js axes (Y up); meta in m"
    with open(path, "w") as fh:
        json.dump({"meta": meta, "objects": objs}, fh, separators=(",", ":"))
    return len(objs), sum(len(o["i"]) // 3 for o in objs)
