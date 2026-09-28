"""Live 3-D visualisation of the kiln line in FreeCAD (via the FreeCAD-MCP XML-RPC addon).

The bridge installs a small helper module (`cemsim_fc`) inside FreeCAD once,
builds a to-scale model of the line (mm, x along the kiln axis, z up) and then
pushes a compact state dict ~1 Hz:

  * kiln shell: 30 segments coloured with the shell-scanner palette (real T_sh)
  * kiln rotation: marker stripes rotate at the actual kiln speed
  * flame: cone length = model flame length, colour/brightness from gas peak T
  * cyclones S1..S4 + calciner: colour from stage temperature
  * cooler: 18 clinker-bed slices, height = bed depth, colour = incandescence
  * fans / drives: green running, grey stopped, red fault

Usage: python -m cemsim.server --freecad localhost:9875   (WSL: use the Windows host IP)
"""

from __future__ import annotations

import json
import time
import xmlrpc.client

FC_MODULE_SRC = r"""
import math, FreeCAD as App, Part
try:
    import FreeCADGui as Gui
except Exception:
    Gui = None
V = App.Vector
DOC = "CemSimKilnLine"
KILN_L, KILN_D, KILN_SLOPE = 62000.0, 4400.0, math.atan(0.035)
NSEG = 30
Z_OUT = 9000.0                       # kiln axis height at the outlet
AXIS = V(-math.cos(KILN_SLOPE), 0, math.sin(KILN_SLOPE))   # from outlet towards inlet
OUT = V(KILN_L, 0, Z_OUT)
state = {"phi": 0.0, "base": {}}

def kiln_point(x_from_inlet):
    return OUT + AXIS * (KILN_L - x_from_inlet)

def _doc():
    d = App.listDocuments().get(DOC)
    if d is None:
        d = App.newDocument(DOC)
    return d

def _add(d, name, shape, color=(0.6, 0.62, 0.65), transp=0):
    o = d.getObject(name)
    if o is None:
        o = d.addObject("Part::Feature", name)
    o.Shape = shape
    if Gui and o.ViewObject:
        o.ViewObject.ShapeColor = color
        o.ViewObject.Transparency = transp
    return o

def _place_stripe(o, ang):
    align = App.Rotation(V(1, 0, 0), AXIS)
    o.Placement = App.Placement(OUT, align).multiply(App.Placement(V(0, 0, 0), App.Rotation(V(1, 0, 0), ang)))

def _cyl(r, h, base, direction):
    return Part.makeCylinder(r, h, base, direction)

def _cyclone(d, name, x, y, z, r=3200.0):
    body = _cyl(r, 7000, V(x, y, z), V(0, 0, 1))
    cone = Part.makeCone(600, r, 7500, V(x, y, z - 7500), V(0, 0, 1))
    roof = _cyl(1300, 2500, V(x, y, z + 7000), V(0, 0, 1))
    return _add(d, name, body.fuse(cone).fuse(roof))

def build():
    d = _doc()
    for o in list(d.Objects):
        d.removeObject(o.Name)
    dx = KILN_L / NSEG
    for i in range(NSEG):
        base = kiln_point(i * dx)
        _add(d, "KilnSeg%02d" % i, _cyl(KILN_D / 2, dx * 0.985, base, AXIS * -1))
    # rotation marker stripes: built along local +X, placed on the kiln axis
    for j in range(4):
        b = Part.makeBox(KILN_L * 0.93, 300, 140)
        b.translate(V(1500, -150, KILN_D / 2 - 30))
        o = _add(d, "Stripe%d" % j, b, (0.12, 0.12, 0.12))
        state["base"][o.Name] = j * 90.0
        _place_stripe(o, j * 90.0)
    # tyres, girth gear, piers
    for n, xi in enumerate((10000.0, 32000.0, 54000.0)):
        c = kiln_point(xi) + AXIS * 400
        ring = _cyl(KILN_D / 2 + 450, 800, c, AXIS * -1).cut(_cyl(KILN_D / 2, 800, c, AXIS * -1))
        _add(d, "Tyre%d" % n, ring, (0.25, 0.25, 0.28))
        p = kiln_point(xi)
        pier = Part.makeBox(3000, 7000, p.z - KILN_D / 2 - 600)
        pier.translate(V(p.x - 1500, -3500, 0))
        _add(d, "Pier%d" % n, pier, (0.75, 0.72, 0.66))
    g = kiln_point(24000.0) + AXIS * 300
    gear = _cyl(KILN_D / 2 + 700, 600, g, AXIS * -1).cut(_cyl(KILN_D / 2, 600, g, AXIS * -1))
    _add(d, "GirthGear", gear, (0.2, 0.2, 0.2))
    gp = kiln_point(24000.0)
    _add(d, "KilnDrive", Part.makeBox(2500, 2000, 1800, V(gp.x - 1250, KILN_D / 2 + 1500, gp.z - 3500)), (0.6, 0.6, 0.6))
    # inlet chamber + calciner + preheater tower
    pin = kiln_point(0.0)
    _add(d, "InletChamber", Part.makeBox(6000, 7000, 11000, V(pin.x - 5500, -3500, pin.z - 4500)))
    _add(d, "Calciner", _cyl(3000, 26000, V(pin.x - 2500, 0, pin.z + 6500), V(0, 0, 1)))
    tx = pin.x - 16000
    stages = [("S5", tx + 4000, 0, pin.z + 26000), ("S4", tx - 4000, 0, pin.z + 38000),
              ("S3", tx + 4000, 0, pin.z + 51000), ("S2", tx - 4000, 0, pin.z + 64000),
              ("S1", tx + 4000, 0, pin.z + 77000)]
    for name, x, y, z in stages:
        _cyclone(d, "Cyc" + name, x, y, z)
    # risers (gas ducts)
    prev = V(pin.x - 2500, 0, pin.z + 32500)
    for name, x, y, z in stages:
        top = V(x, y, z + 9500)
        _add(d, "Duct" + name, Part.makeCylinder(1400, prev.distanceToPoint(top), prev, top - prev), (0.78, 0.74, 0.42))
        prev = top
    # tower frame (4 columns)
    for i, (cx, cy) in enumerate(((tx - 9000, -7000), (tx + 9000, -7000), (tx - 9000, 7000), (tx + 9000, 7000))):
        _add(d, "Column%d" % i, Part.makeBox(800, 800, pin.z + 92000, V(cx, cy, 0)), (0.55, 0.55, 0.55))
    # ID fan + stack
    _add(d, "IDFan", _cyl(2500, 2500, V(tx - 20000, 0, 3000), V(0, 1, 0)))
    s1top = prev
    fan = V(tx - 20000, 1250, 5500)
    _add(d, "ExitDuct", Part.makeCylinder(1400, s1top.distanceToPoint(fan), s1top, fan - s1top), (0.78, 0.74, 0.42))
    _add(d, "Stack", _cyl(2200, 90000, V(tx - 30000, 0, 0), V(0, 0, 1)), (0.7, 0.7, 0.72))
    # hood, burner, flame
    _add(d, "Hood", Part.makeBox(5000, 8000, 12000, V(KILN_L - 500, -4000, Z_OUT - 7000)), (0.6, 0.62, 0.65), 65)
    tip = kiln_point(KILN_L - 1500)
    _add(d, "BurnerPipe", Part.makeCylinder(350, 12000, tip, AXIS * -1), (0.3, 0.3, 0.3))
    _add(d, "Flame", Part.makeCone(900, 150, 10000, tip, AXIS), (1.0, 0.6, 0.05), 25)
    _add(d, "PAFan", _cyl(900, 1200, V(KILN_L + 13000, 0, Z_OUT - 1500), V(0, 1, 0)))
    # cooler
    cx0, cz = KILN_L + 2500, 2500.0
    _add(d, "CoolerCasing", Part.makeBox(18500, 5200, 6500, V(cx0, -2600, 0)), (0.62, 0.64, 0.66), 70)
    for i in range(18):
        _add(d, "ClkBed%02d" % i, Part.makeBox(980, 4000, 600, V(cx0 + 250 + i * 1000, -2000, cz)), (0.5, 0.3, 0.2))
    bounds = ((0, 2), (2, 5), (5, 8), (8, 11), (11, 14), (14, 18))
    for i, (a, b) in enumerate(bounds):
        x = cx0 + 250 + (a + b) / 2 * 1000
        _add(d, "CoolFan%d" % (i + 1), _cyl(700, 1400, V(x, 3200, 700), V(0, 1, 0)))
    _add(d, "VentFan", _cyl(1300, 1600, V(cx0 + 17000, 3000, 9000), V(0, 1, 0)))
    ta0 = V(KILN_L + 3500, 0, 12500)
    ta1 = V(pin.x - 2500, 0, pin.z + 14000)
    _add(d, "TADuct", Part.makeCylinder(1300, ta0.distanceToPoint(ta1), ta0, ta1 - ta0), (0.55, 0.62, 0.8))
    d.recompute()
    if Gui:
        try:
            v = Gui.ActiveDocument.ActiveView
            v.viewIsometric()
            Gui.SendMsgToActiveView("ViewFit")
        except Exception:
            pass
    return len(d.Objects)

def _scan(T, lo=150.0, hi=450.0):
    x = max(0.0, min(1.0, (T - lo) / (hi - lo)))
    stops = [(0, (0.12, 0.24, 0.75)), (0.35, (0.16, 0.75, 0.35)), (0.6, (0.94, 0.86, 0.16)),
             (0.8, (0.96, 0.51, 0.08)), (1.0, (0.86, 0.12, 0.12))]
    for i in range(1, len(stops)):
        if x <= stops[i][0]:
            a, ca = stops[i - 1]; b, cb = stops[i]; f = (x - a) / (b - a)
            return tuple(ca[k] + f * (cb[k] - ca[k]) for k in range(3))
    return stops[-1][1]

def _glow(T):
    x = max(0.0, min(1.0, (T - 150.0) / 1250.0))
    return (0.35 + 0.65 * min(1.0, x * 1.6), 0.27 + 0.73 * max(0.0, x - 0.35) / 0.65, 0.27 + 0.5 * max(0.0, x - 0.8) / 0.2)

def _heat(T, lo=250.0, hi=1100.0):
    x = max(0.0, min(1.0, (T - lo) / (hi - lo)))
    return (0.55 + 0.45 * x, 0.6 - 0.35 * x, 0.65 - 0.55 * x)

def _col(name, c, transp=None):
    d = App.getDocument(DOC)
    o = d.getObject(name)
    if o is not None and Gui and o.ViewObject:
        o.ViewObject.ShapeColor = tuple(float(v) for v in c)
        if transp is not None:
            o.ViewObject.Transparency = int(transp)

def update(s):
    d = App.getDocument(DOC)
    for i, T in enumerate(s["shell"]):
        _col("KilnSeg%02d" % i, _scan(T))
    # rotation
    state["phi"] = (state["phi"] + s["rpm"] * 6.0 * s["dt_sim"]) % 360.0
    for name, off in state["base"].items():
        o = d.getObject(name)
        if o is not None:
            _place_stripe(o, state["phi"] + off)
    # flame
    f = d.getObject("Flame")
    if f is not None:
        L = max(1.0, s["flame_m"]) * 1000.0 if s["burner_on"] else 1.0
        tip = kiln_point(KILN_L - 1500)
        f.Shape = Part.makeCone(900 + 400 * min(1.0, s["coal_k"] / 8.0), 150, L, tip, AXIS)
        hot = max(0.0, min(1.0, (s["T_gas_max"] - 1500.0) / 700.0))
        _col("Flame", (1.0, 0.45 + 0.5 * hot, 0.05 + 0.6 * hot * hot), 20 if s["burner_on"] else 95)
    for name, T in s["stages"].items():
        _col("Cyc" + name, _heat(T))
    _col("Calciner", _heat(s["T_cal"]))
    cz = 2500.0
    x0 = KILN_L + 2500
    for i, T in enumerate(s["cooler_T"]):
        o = d.getObject("ClkBed%02d" % i)
        if o is None:
            continue
        h = max(50.0, s["bed_m"][min(i * 6 // 18, 5)] * 1000.0)
        o.Shape = Part.makeBox(980, 4000, h, V(x0 + 250 + i * 1000, -2000, cz))
        _col("ClkBed%02d" % i, _glow(T))
    run = {"RUNNING": (0.18, 0.7, 0.29), "FAULT": (0.85, 0.15, 0.15)}
    for name, st in s["drives"].items():
        _col(name, run.get(st, (0.6, 0.62, 0.65)))
    d.recompute()
    return True
"""


def _native(o):
    """numpy scalars/arrays -> plain Python types (JSON-safe)."""
    if isinstance(o, dict):
        return {k: _native(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_native(v) for v in o]
    if hasattr(o, "item"):
        return o.item()
    return o


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a socket timeout (a hung FreeCAD must not block us forever)."""

    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class NeedsBuild(RuntimeError):
    """The 3-D model / helper module is missing in FreeCAD (restart, closed document)."""


GUI_STALLED_HINT = (
    "FreeCAD received the request but its GUI thread is not picking it up. "
    "Usually a mouse button is registered as held (click once inside the "
    "FreeCAD window), a dialog/menu is open, or FreeCAD is busy. "
    "Then press build again."
)


def explain(err: Exception) -> str:
    """Turn transport/addon errors into an operator-actionable message."""
    s = str(err)
    if isinstance(err, NeedsBuild):
        return "3-D model not present in FreeCAD - building it"
    if "waiting for" in s and "to start" in s:
        return GUI_STALLED_HINT
    if isinstance(err, (ConnectionRefusedError, OSError)) and "timed out" not in s:
        return f"FreeCAD RPC server not reachable ({s}). Is FreeCAD running with the MCP addon RPC server started?"
    if "timed out" in s:
        return "FreeCAD did not answer in time (busy or hung)."
    return s[:300]


class FreeCADBridge:
    """Talks to the FreeCAD-MCP addon over XML-RPC.

    `status` is published to the HMI: state is one of
    off | checking | building | live | error, with a human-readable message.
    """

    def __init__(self, host="127.0.0.1", port=9875):
        self.url = f"http://{host}:{port}"
        self.rpc = xmlrpc.client.ServerProxy(self.url, allow_none=True, transport=_TimeoutTransport(20.0))
        self.rpc_build = xmlrpc.client.ServerProxy(self.url, allow_none=True, transport=_TimeoutTransport(180.0))
        self.rpc_quick = xmlrpc.client.ServerProxy(self.url, allow_none=True, transport=_TimeoutTransport(3.0))
        self.fail_until = 0.0
        self.want_live = False  # operator intent (live on/off)
        self.building = False  # a build is in progress
        self.installed = False
        self.last_error = ""
        self._last_t = None
        self.status = {"state": "off", "msg": "not started", "url": self.url, "pushes": 0}

    def set_status(self, state, msg=""):
        self.status.update(state=state, msg=msg, t=time.time())

    def _exec(self, code, slow=False):
        r = (self.rpc_build if slow else self.rpc).execute_code(code)
        if isinstance(r, dict) and not r.get("success", False):
            err = str(r.get("error") or r.get("message"))
            if "CEMSIM_NEEDS_BUILD" in err:
                raise NeedsBuild(err)
            raise RuntimeError(err)
        return r

    def check(self):
        """Fast reachability check that does not need FreeCAD's GUI thread."""
        try:
            self.rpc_quick.ping()
        except Exception as e:
            raise ConnectionRefusedError(str(e)) from None
        try:
            st = self.rpc_quick.get_rpc_status()
            gd = st.get("gui_dispatch", {}) if isinstance(st, dict) else {}
            if gd.get("state") == "stuck":
                raise RuntimeError(f"FreeCAD GUI is stuck in '{gd.get('operation')}' - restart FreeCAD")
        except xmlrpc.client.Fault:
            pass  # older addon without get_rpc_status

    def install(self):
        code = (
            "import sys, types\n"
            "m = types.ModuleType('cemsim_fc')\n"
            f"exec(compile({FC_MODULE_SRC!r}, 'cemsim_fc', 'exec'), m.__dict__)\n"
            "sys.modules['cemsim_fc'] = m\n"
            "print('cemsim_fc installed')\n"
        )
        self._exec(code, slow=True)
        self.installed = True

    def build(self):
        self.set_status("checking", f"contacting {self.url}")
        self.check()
        self.set_status("building", "building the 3-D kiln line in FreeCAD ...")
        self.install()
        r = self._exec("import sys; print('objects', sys.modules['cemsim_fc'].build())", slow=True)
        self._last_t = None
        return r

    @staticmethod
    def state_from_sim(sim) -> dict:
        k, p = sim.kpi, sim.plant.profiles()
        D = sim.drives
        return {
            "t": sim.plant.t,
            "shell": [round(x, 1) for x in p["T_shell"]],
            "rpm": D["KILN_DRIVE"].pv,
            "flame_m": float(k.get("flame_len_m", 0.0)),
            "burner_on": D["COAL_KILN"].pv > 0.1,
            "coal_k": D["COAL_KILN"].pv,
            "T_gas_max": float(k.get("T_gas_max", 1500.0)),
            "stages": {
                "S1": k.get("T_S1", 0),
                "S2": k.get("T_S2", 0),
                "S3": k.get("T_S3", 0),
                "S4": k.get("T_S4", 0),
                "S5": k.get("T_calciner", 0),
            },
            "T_cal": float(k.get("T_calciner", 0.0)),
            "cooler_T": [round(x, 1) for x in p["cooler_T"]],
            "bed_m": [float(x) for x in sim.plant.cooler.out.get("bed_m", [0.6] * 6)],
            "drives": {
                "IDFan": D["ID_FAN"].state,
                "VentFan": D["VENT_FAN"].state,
                "PAFan": D["PA_FAN"].state,
                "KilnDrive": D["KILN_DRIVE"].state,
                **{f"CoolFan{i}": D[f"CF{i}"].state for i in range(1, 7)},
            },
        }

    def push(self, st: dict):
        """One live update; rebuilds automatically if FreeCAD lost the model."""
        t = st.pop("t")
        st["dt_sim"] = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        payload = json.dumps(_native(st))
        code = (
            "import sys, json, FreeCAD\n"
            "m = sys.modules.get('cemsim_fc')\n"
            "if m is None or 'CemSimKilnLine' not in FreeCAD.listDocuments():\n"
            "    raise RuntimeError('CEMSIM_NEEDS_BUILD')\n"
            f"m.update(json.loads({payload!r}))\n"
        )
        try:
            self._exec(code)
        except NeedsBuild:
            self.build()
            self._exec(code)
        self._last_t = t
        self.status["pushes"] = self.status.get("pushes", 0) + 1


if __name__ == "__main__":  # quick manual test:  python -m cemsim.freecad_bridge
    from .sim import Simulator

    b = FreeCADBridge()
    print(b.build())
    sim = Simulator()
    try:
        sim.load_file("nominal")
    except FileNotFoundError:
        sim.set_all_running()
    for _ in range(5):
        sim.run(10)
        b.push(b.state_from_sim(sim))
        time.sleep(0.5)
    print("ok")
