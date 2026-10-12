"""FreeCAD <-> FreeCAD-MCP addon <-> CemSim client: the RPC link itself.

Everything here runs against a real FreeCAD GUI process with the real addon.
"""

import http.client
import re
import xmlrpc.client
from pathlib import Path

import pytest

from cemsim.freecad_bridge import EXPECTED_PROTOCOL_VERSION, FreeCADClient

from .pngstat import decode, non_background_fraction

pytestmark = pytest.mark.freecad

ADDON_VERSION_FILE = (
    Path(__file__).resolve().parents[2] / "third_party/freecad-mcp/addon/FreeCADMCP/rpc_server/version.py"
)
XML_PING = '<?xml version="1.0"?><methodCall><methodName>ping</methodName><params/></methodCall>'


def _post(worker, headers: dict[str, str]) -> tuple[int, str]:
    c = http.client.HTTPConnection(worker.host, worker.port, timeout=15)
    try:
        c.request("POST", "/RPC2", XML_PING, headers)
        r = c.getresponse()
        return r.status, r.read().decode(errors="replace")
    finally:
        c.close()


def test_real_freecad_gui_is_running(client):
    out = client.execute(
        "import FreeCAD, threading\n"
        "print('ver', '.'.join(FreeCAD.Version()[:3]))\n"
        "print('gui', FreeCAD.GuiUp)\n"
        "print('main', threading.current_thread() is threading.main_thread())\n"
    )
    ver = re.search(r"ver (\d+)\.(\d+)", out)
    assert ver and (int(ver[1]), int(ver[2])) >= (1, 0), out
    assert "gui 1" in out, "addon must run in the GUI build (FreeCADGui + Qt)"
    assert "main True" in out, "execute_code must run on FreeCAD's GUI/main thread"


def test_addon_version_matches_vendored_copy_and_bridge(client):
    st = client.rpc_status()
    src = ADDON_VERSION_FILE.read_text()
    vendored_version = re.search(r'__version__\s*=\s*"([^"]+)"', src)[1]
    vendored_protocol = int(re.search(r"PROTOCOL_VERSION\s*=\s*(\d+)", src)[1])
    assert st["success"] is True and st["rpc_server"] == "running"
    assert st["addon_version"] == vendored_version
    assert st["protocol_version"] == vendored_protocol == EXPECTED_PROTOCOL_VERSION
    assert st["gui_dispatch"]["state"] == "healthy"


def test_diagnose_reports_a_healthy_connection(client):
    d = client.diagnose(deep=True)
    assert d["ok"], d["problems"]
    assert d["reachable"] and d["authenticated"] and d["protocol_ok"]
    assert d["gui_state"] == "healthy"
    assert isinstance(d["documents"], list)
    assert d["latency_ms"] < 500


@pytest.mark.parametrize("token", ["", "not-the-token"])
def test_token_is_enforced(worker, token):
    bad = FreeCADClient(worker.host, worker.port, token)
    with pytest.raises(xmlrpc.client.ProtocolError) as e:
        bad.ping()
    assert e.value.errcode == 401
    d = bad.diagnose()
    assert d["reachable"] is True and d["authenticated"] is False and not d["ok"]
    assert "token" in d["problems"][0]
    if token:  # the secret we sent must not be echoed into error text
        assert token not in str(e.value) and token not in repr(d)


def test_token_never_appears_in_client_error_text(worker):
    c = FreeCADClient(worker.host, worker.port, worker.token)
    c.port = 1  # nothing listens here
    c.url = f"http://{c.host}:1"
    d = c.diagnose()
    assert not d["reachable"]
    assert worker.token not in repr(d) and worker.token not in repr(c.__dict__.get("url"))


def test_browser_requests_are_refused_even_with_a_valid_token(worker):
    auth = {"Authorization": f"Bearer {worker.token}", "Content-Type": "text/xml"}
    assert _post(worker, auth)[0] == 200  # control: the same request without Origin works
    assert _post(worker, {**auth, "Origin": "http://evil.example"})[0] == 403  # CSRF
    assert _post(worker, {**auth, "Host": "evil.example"})[0] == 403  # DNS rebinding
    assert _post(worker, {**auth, "Content-Type": "application/x-www-form-urlencoded"})[0] == 415


def test_python_errors_come_back_as_errors_and_the_gui_survives(client):
    with pytest.raises(RuntimeError, match="boom-4711"):
        client.execute("raise ValueError('boom-4711')")
    assert client.diagnose()["ok"]
    assert client.execute("print(6*7)").strip() == "42"


def test_script_namespace_persists_between_calls(client):
    client.execute("cemsim_probe_value = 41")
    assert client.execute("print(cemsim_probe_value + 1)").strip() == "42"


def test_geometry_kernel_gives_exact_volumes(client):
    out = client.execute(
        "import Part, math\n"
        "b = Part.makeBox(10, 20, 30); c = Part.makeCylinder(5, 10)\n"
        "print(round(b.Volume, 6), round(c.Volume, 6), round(math.pi * 25 * 10, 6))\n"
    )
    box, cyl, ref = (float(x) for x in out.split())
    assert box == pytest.approx(6000.0)
    assert cyl == pytest.approx(ref, rel=1e-9)


def test_screenshot_shows_real_geometry(client, doc_name, cleanup_docs):
    cleanup_docs.append(doc_name)
    client.execute(
        "import FreeCAD, Part\n"
        f"d = FreeCAD.newDocument({doc_name!r})\n"
        "o = d.addObject('Part::Feature', 'B'); o.Shape = Part.makeBox(40, 30, 20); d.recompute()\n"
        "import FreeCADGui; FreeCADGui.ActiveDocument = FreeCADGui.getDocument(d.Name)\n"
    )
    png = client.screenshot("Isometric", 640, 480)
    assert png is not None, "no screenshot (3-D view unavailable)"
    w, h, _, _ = decode(png)
    assert (w, h) == (640, 480)
    frac = non_background_fraction(png)
    assert frac > 0.005, f"3-D view looks blank (only {frac:.4%} non-background pixels)"

    # negative control: the same view of an empty document must look blank, otherwise the
    # check above proves nothing
    empty = doc_name + "E"
    cleanup_docs.append(empty)
    client.execute(
        "import FreeCAD, FreeCADGui\n"
        f"d = FreeCAD.newDocument({empty!r}); FreeCADGui.ActiveDocument = FreeCADGui.getDocument(d.Name)\n"
    )
    blank = client.screenshot("Isometric", 640, 480)
    assert blank is not None and non_background_fraction(blank) < frac / 5
