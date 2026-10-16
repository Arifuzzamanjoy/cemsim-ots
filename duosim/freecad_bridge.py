"""Live 3-D visualisation of the kiln line in FreeCAD (via the FreeCAD-MCP XML-RPC addon).

The bridge installs a small helper module (`duosim_fc`) inside FreeCAD once,
builds a to-scale model of the line (mm, x along the kiln axis, z up) and then
pushes a compact state dict ~1 Hz:

  * kiln shell: 30 segments coloured with the shell-scanner palette (real T_sh)
  * kiln rotation: marker stripes rotate at the actual kiln speed
  * flame: cone length = model flame length, colour/brightness from gas peak T
  * cyclones S1..S4 + calciner: colour from stage temperature
  * cooler: 18 clinker-bed slices, height = bed depth, colour = incandescence
  * fans / drives: green running, grey stopped, red fault

Two layers, so other features can reuse the transport without the kiln model:

  * `FreeCADClient`  - authenticated, version-checked XML-RPC access to the addon
                       (execute_code, screenshots, documents, deep `diagnose()`)
  * `FreeCADBridge`  - the live kiln-line model on top of it (build / push / readback)

Authentication: if the addon has an auth token set (required for any remote use), pass
`token=` or set FREECAD_MCP_TOKEN; it is sent as an `Authorization: Bearer` header and is
never placed in a URL or log line.

Usage: python -m duosim.server --freecad localhost:9875   (WSL: use the Windows host IP)
"""

from __future__ import annotations

import json
import os
import time
import xmlrpc.client

# RPC contract of the vendored addon (third_party/freecad-mcp, PROTOCOL_VERSION in rpc_server/version.py)
EXPECTED_PROTOCOL_VERSION = 1
TOKEN_ENV = "FREECAD_MCP_TOKEN"  # noqa: S105 - the *name* of the env var, not a secret
TOKEN_FILE_ENV = "FREECAD_MCP_TOKEN_FILE"  # noqa: S105 - Docker/Kubernetes-secret style: path to a file
GEOMETRY_ID = "duosim-kiln-line-v1"  # bump when FC_MODULE_SRC.build() changes the model

FC_MODULE_SRC = r"""
import math, FreeCAD as App, Part
try:
    import FreeCADGui as Gui
except Exception:
    Gui = None
V = App.Vector
DOC = "DuoSimKilnLine"
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

def _rgb(name):
    o = App.getDocument(DOC).getObject(name)
    if o is None or not (Gui and o.ViewObject):
        return None
    return [round(float(c), 4) for c in o.ViewObject.ShapeColor[:3]]

def _zlen(name):
    o = App.getDocument(DOC).getObject(name)
    return None if o is None else round(o.Shape.BoundBox.ZLength, 3)

def readback():
    # Measured state of the model inside FreeCAD (geometry and colours as they really are),
    # used to validate that what the simulator sent is what FreeCAD holds.
    d = App.getDocument(DOC)
    flame_len = None
    f = d.getObject("Flame")
    if f is not None:
        c = [x.CenterOfMass for x in f.Shape.Faces if x.Surface.__class__.__name__ == "Plane"]
        if len(c) == 2:
            flame_len = round(c[0].distanceToPoint(c[1]), 3)
    roll = None
    s0 = d.getObject("Stripe0")
    if s0 is not None:
        align = App.Rotation(V(1, 0, 0), AXIS)
        roll = round(align.inverted().multiply(s0.Placement.Rotation).toEuler()[2], 3)
    drives = ["IDFan", "VentFan", "PAFan", "KilnDrive"] + ["CoolFan%d" % i for i in range(1, 7)]
    return {
        "geometry": GEOMETRY_ID,
        "n_objects": len(d.Objects),
        "gui": bool(Gui),
        "shell_rgb": [_rgb("KilnSeg%02d" % i) for i in range(NSEG)],
        "stripe0_deg": roll,
        "flame_len_mm": flame_len,
        "flame_rgb": _rgb("Flame"),
        "stage_rgb": {n: _rgb("Cyc" + n) for n in ("S1", "S2", "S3", "S4", "S5")},
        "calciner_rgb": _rgb("Calciner"),
        "bed_h_mm": [_zlen("ClkBed%02d" % i) for i in range(18)],
        "bed_rgb": [_rgb("ClkBed%02d" % i) for i in range(18)],
        "drive_rgb": {n: _rgb(n) for n in drives},
    }
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


def _env_token() -> str:
    """Token from FREECAD_MCP_TOKEN, else from the file named by FREECAD_MCP_TOKEN_FILE."""
    t = os.environ.get(TOKEN_ENV, "")
    if t:
        return t
    f = os.environ.get(TOKEN_FILE_ENV, "")
    if f:
        try:
            return open(f, encoding="utf-8").read().strip()
        except OSError:
            return ""
    return ""


class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a socket timeout (a hung FreeCAD must not block us forever)
    and optional extra headers (the Bearer token)."""

    def __init__(self, timeout, headers=()):
        super().__init__(headers=list(headers))
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
    if isinstance(err, xmlrpc.client.ProtocolError):
        if err.errcode == 401:
            return (
                "FreeCAD RPC server requires an auth token: set FREECAD_MCP_TOKEN (or --freecad-token) "
                "to the token configured in the FreeCAD MCP addon."
            )
        if err.errcode == 403:
            return f"FreeCAD RPC server refused the request ({err.errmsg}). Check the addon's allowed IPs."
        return f"FreeCAD RPC server answered HTTP {err.errcode} {err.errmsg}"
    if "GUI_DISPATCH_STUCK" in s or "still running on FreeCAD's GUI thread" in s:
        return (
            "FreeCAD's GUI thread is stuck in a long operation (the addon cannot cancel it) - "
            "wait for it to finish or restart FreeCAD."
        )
    if "waiting for" in s and "to start" in s:
        return GUI_STALLED_HINT
    if isinstance(err, (ConnectionRefusedError, OSError)) and "timed out" not in s:
        return f"FreeCAD RPC server not reachable ({s}). Is FreeCAD running with the MCP addon RPC server started?"
    if "timed out" in s:
        return "FreeCAD did not answer in time (busy or hung)."
    return s[:300]


class FreeCADClient:
    """Authenticated XML-RPC access to the FreeCAD-MCP addon.

    Every call uses its own short-lived connection (ServerProxy is not thread-safe, and
    status/ping must stay usable while a GUI-thread request is in flight).
    """

    QUICK = 3.0  # ping / status: they never wait for FreeCAD's GUI thread
    NORMAL = 20.0  # one live update
    SLOW = 180.0  # model build

    def __init__(self, host="127.0.0.1", port=9875, token=None):
        self.host, self.port = host, int(port)
        self.url = f"http://{host}:{self.port}"
        self.token = _env_token() if token is None else token
        self._headers = [("Authorization", f"Bearer {self.token}")] if self.token else []
        self.info: dict = {}  # last get_rpc_status reply

    # -- transport --------------------------------------------------------
    def call(self, method, *args, timeout=NORMAL):
        proxy = xmlrpc.client.ServerProxy(
            self.url, allow_none=True, transport=_TimeoutTransport(timeout, self._headers)
        )
        with proxy as p:
            return getattr(p, method)(*args)

    def execute(self, code, slow=False) -> str:
        """Run Python on FreeCAD's GUI thread; return what it printed.

        Raises NeedsBuild if the code signalled DUOSIM_NEEDS_BUILD, RuntimeError on any
        other failure reported by the addon.
        """
        if slow:
            # ask for a longer run budget when the addon supports it (it reports its ceiling)
            args = (code, 170.0) if self.info.get("max_execute_code_timeout") else (code,)
            r = self.call("execute_code", *args, timeout=self.SLOW)
        else:
            r = self.call("execute_code", code, timeout=self.NORMAL)
        if isinstance(r, dict) and not r.get("success", False):
            err = str(r.get("error") or r.get("message"))
            if "DUOSIM_NEEDS_BUILD" in err:
                raise NeedsBuild(err)
            raise RuntimeError(err)
        msg = r.get("message", "") if isinstance(r, dict) else str(r)
        return msg.partition("Output: ")[2]

    def ping(self) -> bool:
        return bool(self.call("ping", timeout=self.QUICK))

    def rpc_status(self) -> dict:
        st = self.call("get_rpc_status", timeout=self.QUICK)
        self.info = st if isinstance(st, dict) else {}
        return self.info

    def list_documents(self) -> list[str]:
        return list(self.call("list_documents", timeout=5.0))

    def screenshot(self, view="Isometric", width=1280, height=720, focus=None, doc=None) -> bytes | None:
        """PNG of the 3-D view (None if it cannot be captured).

        The addon captures whichever document is *active* in the GUI, and a worker usually has
        several open, so pass `doc` to activate yours first.
        """
        import base64

        if doc:
            self.execute(
                "import FreeCAD, FreeCADGui\n"
                f"FreeCAD.setActiveDocument({doc!r})\n"
                f"FreeCADGui.ActiveDocument = FreeCADGui.getDocument({doc!r})\n"
            )
        b64 = self.call("get_active_screenshot", view, width, height, focus, timeout=30.0)
        return base64.b64decode(b64) if b64 else None

    # -- health -----------------------------------------------------------
    @property
    def protocol_ok(self) -> bool:
        return self.info.get("protocol_version") == EXPECTED_PROTOCOL_VERSION

    def check(self):
        """Fast reachability + health check that does not need FreeCAD's GUI thread."""
        try:
            self.ping()
        except xmlrpc.client.ProtocolError:
            raise  # answered but refused (401/403): keep the type so explain() can say why
        except Exception as e:
            raise ConnectionRefusedError(str(e)) from None
        try:
            st = self.rpc_status()
        except xmlrpc.client.Fault:
            return  # older addon without get_rpc_status
        gd = st.get("gui_dispatch", {})
        if gd.get("state") == "stuck":
            raise RuntimeError(f"FreeCAD GUI is stuck in '{gd.get('operation')}' - restart FreeCAD")

    def diagnose(self, deep=False) -> dict:
        """Structured connection report (never raises).

        `deep` also asks FreeCAD (GUI thread) which documents are open.
        """
        out: dict = {
            "url": self.url,
            "reachable": False,
            "authenticated": None,
            "ok": False,
            "problems": [],
            "t": time.time(),
        }
        t0 = time.perf_counter()
        try:
            self.ping()
        except xmlrpc.client.ProtocolError as e:
            out["reachable"] = True
            out["authenticated"] = False if e.errcode == 401 else None
            out["problems"].append(explain(e))
            return out
        except Exception as e:
            out["problems"].append(explain(e))
            return out
        out.update(reachable=True, authenticated=True, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        try:
            st = self.rpc_status()
        except xmlrpc.client.Fault:
            out["problems"].append("addon predates get_rpc_status: update the FreeCAD MCP addon")
            st = {}
        except Exception as e:
            out["problems"].append(explain(e))
            st = {}
        gd = st.get("gui_dispatch") or {}
        out.update(
            addon_version=st.get("addon_version"),
            protocol_version=st.get("protocol_version"),
            protocol_expected=EXPECTED_PROTOCOL_VERSION,
            gui_state=gd.get("state"),
            gui_operation=gd.get("operation") or None,
            async_jobs_running=len(st.get("async_jobs_running") or []),
        )
        out["protocol_ok"] = self.protocol_ok
        if st and not out["protocol_ok"]:
            out["problems"].append(
                f"addon protocol {st.get('protocol_version')!r} != {EXPECTED_PROTOCOL_VERSION} expected by DuoSim: "
                "update the addon and this bridge together"
            )
        if gd.get("state") == "stuck":
            out["problems"].append(explain(RuntimeError("GUI_DISPATCH_STUCK")))
        elif deep and gd.get("state") == "healthy":
            try:
                out["documents"] = self.list_documents()
            except Exception as e:
                out["problems"].append(explain(e))
        out["ok"] = not out["problems"]
        return out


class FreeCADBridge(FreeCADClient):
    """The live kiln-line model in FreeCAD, driven by the simulator.

    `status` is published to the HMI: state is one of
    off | checking | building | live | error, with a human-readable message.
    """

    DOC = "DuoSimKilnLine"

    def __init__(self, host="127.0.0.1", port=9875, token=None):
        super().__init__(host, port, token)
        self.fail_until = 0.0
        self.want_live = False  # operator intent (live on/off)
        self.building = False  # a build is in progress
        self.installed = False
        self.last_error = ""
        self._last_t = None
        self.status = {"state": "off", "msg": "not started", "url": self.url, "pushes": 0}

    def set_status(self, state, msg=""):
        self.status.update(state=state, msg=msg, t=time.time())

    def install(self):
        src = f"GEOMETRY_ID = {GEOMETRY_ID!r}\n" + FC_MODULE_SRC  # host-side constant is the single source
        code = (
            "import sys, types\n"
            "m = types.ModuleType('duosim_fc')\n"
            f"exec(compile({src!r}, 'duosim_fc', 'exec'), m.__dict__)\n"
            "sys.modules['duosim_fc'] = m\n"
            "print('duosim_fc installed')\n"
        )
        self.execute(code, slow=True)
        self.installed = True

    def build(self) -> str:
        self.set_status("checking", f"contacting {self.url}")
        self.check()
        self.set_status("building", "building the 3-D kiln line in FreeCAD ...")
        self.install()
        out = self.execute("import sys; print('objects', sys.modules['duosim_fc'].build())", slow=True)
        self._last_t = None
        return out.strip()

    def readback(self) -> dict:
        """Measured model state inside FreeCAD (colours, geometry, rotation) - see FC_MODULE_SRC.readback."""
        code = (
            "import sys, json\n"
            "m = sys.modules.get('duosim_fc')\n"
            f"if m is None or {self.DOC!r} not in __import__('FreeCAD').listDocuments():\n"
            "    raise RuntimeError('DUOSIM_NEEDS_BUILD')\n"
            "print(json.dumps(m.readback()))\n"
        )
        return json.loads(self.execute(code).strip().splitlines()[-1])

    def diagnose(self, deep=False) -> dict:
        out = super().diagnose(deep)
        out.update(want_live=self.want_live, building=self.building, state=self.status["state"])
        out["pushes"] = self.status.get("pushes", 0)
        if "documents" in out:
            out["model_present"] = self.DOC in out["documents"]
        return out

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
            "m = sys.modules.get('duosim_fc')\n"
            f"if m is None or {self.DOC!r} not in FreeCAD.listDocuments():\n"
            "    raise RuntimeError('DUOSIM_NEEDS_BUILD')\n"
            f"m.update(json.loads({payload!r}))\n"
        )
        try:
            self.execute(code)
        except NeedsBuild:
            self.build()
            self.execute(code)
        self._last_t = t
        self.status["pushes"] = self.status.get("pushes", 0) + 1


if __name__ == "__main__":  # quick manual test:  python -m duosim.freecad_bridge
    from .sim import Simulator

    b = FreeCADBridge()
    print(json.dumps(b.diagnose(deep=True), indent=2))
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
