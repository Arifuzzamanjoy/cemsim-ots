"""The upstream `freecad-mcp` MCP server (PyPI, stdio) against the same FreeCAD that CemSim uses.

Validates the second leg of the integration: an MCP client (Claude Desktop, Claude Code,
Gemini CLI, ...) -> freecad-mcp -> XML-RPC -> addon -> FreeCAD, and that this leg and CemSim's
own client see one and the same FreeCAD session.
"""

import base64
import json
import math

import pytest

from .conftest import MCP_SERVER_VERSION

pytestmark = pytest.mark.freecad

REQUIRED_TOOLS = {
    "create_document",
    "list_documents",
    "create_object",
    "edit_object",
    "delete_object",
    "get_objects",
    "get_object",
    "get_view",
    "execute_code",
    "execute_code_async",
    "get_async_status",
    "get_rpc_status",
    "reload_document",
}


def _json(text: str):
    """First JSON value in a tool's text reply."""
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    assert start >= 0, f"no JSON in: {text[:200]!r}"
    return json.JSONDecoder().raw_decode(text[start:])[0]


def test_server_handshake_and_tool_catalogue(mcp):
    assert mcp.server_info.get("name") == "FreeCADMCP"
    names = {t["name"] for t in mcp.list_tools()}
    missing = REQUIRED_TOOLS - names
    assert not missing, f"upstream {MCP_SERVER_VERSION} no longer offers: {sorted(missing)}"


def test_every_tool_has_a_json_schema(mcp):
    for t in mcp.list_tools():
        assert t.get("description"), t["name"]
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_mcp_reports_a_matching_addon_without_version_warning(mcp):
    r = mcp.call_tool("get_rpc_status")
    assert not r["is_error"], r["text"]
    assert "Warning" not in r["text"], f"MCP server complains about the addon: {r['text'][:300]}"
    st = _json(r["text"])
    assert st["addon_version"] == MCP_SERVER_VERSION
    assert st["gui_dispatch"]["state"] == "healthy"


def test_mcp_creates_geometry_that_cemsim_client_measures(mcp, client, doc_name, cleanup_docs):
    cleanup_docs.append(doc_name)
    r = mcp.call_tool("create_document", {"name": doc_name})
    assert not r["is_error"], r["text"]
    assert doc_name in client.list_documents()  # same FreeCAD session seen from CemSim's client

    r = mcp.call_tool(
        "create_object",
        {
            "doc_name": doc_name,
            "obj_type": "Part::Box",
            "obj_name": "McpBox",
            "obj_properties": {"Length": 20.0, "Width": 10.0, "Height": 5.0},
            "include_screenshot": False,
        },
    )
    assert not r["is_error"], r["text"]
    vol = client.execute(f"import FreeCAD\nprint(FreeCAD.getDocument({doc_name!r}).McpBox.Shape.Volume)")
    assert float(vol) == pytest.approx(20 * 10 * 5)


def test_cemsim_creates_geometry_that_mcp_reads_back(mcp, client, doc_name, cleanup_docs):
    cleanup_docs.append(doc_name)
    client.execute(
        "import FreeCAD, Part\n"
        f"d = FreeCAD.newDocument({doc_name!r})\n"
        "c = d.addObject('Part::Cylinder', 'CemsimCyl'); c.Radius = 3; c.Height = 7; d.recompute()\n"
    )
    r = mcp.call_tool("get_objects", {"doc_name": doc_name, "include_screenshot": False})
    assert not r["is_error"], r["text"]
    objs = _json(r["text"])
    cyl = next(o for o in objs if o["Name"] == "CemsimCyl")
    assert cyl["TypeId"] == "Part::Cylinder"
    # Quantity properties are serialised through str(): "3.0 mm"
    assert float(str(cyl["Properties"]["Radius"]).split()[0]) == pytest.approx(3.0)
    assert float(str(cyl["Properties"]["Height"]).split()[0]) == pytest.approx(7.0)
    assert cyl["Shape"]["Volume"] == pytest.approx(math.pi * 3.0**2 * 7.0, rel=1e-9)  # analytic, via MCP


def test_mcp_edit_is_visible_to_cemsim(mcp, client, doc_name, cleanup_docs):
    cleanup_docs.append(doc_name)
    mcp.call_tool("create_document", {"name": doc_name})
    mcp.call_tool(
        "create_object",
        {
            "doc_name": doc_name,
            "obj_type": "Part::Box",
            "obj_name": "EditMe",
            "obj_properties": {"Length": 1.0, "Width": 1.0, "Height": 1.0},
            "include_screenshot": False,
        },
    )
    r = mcp.call_tool(
        "edit_object",
        {
            "doc_name": doc_name,
            "obj_name": "EditMe",
            "obj_properties": {"Height": 9.0},
            "include_screenshot": False,
        },
    )
    assert not r["is_error"], r["text"]
    vol = client.execute(f"import FreeCAD\nprint(FreeCAD.getDocument({doc_name!r}).EditMe.Shape.Volume)")
    assert float(vol) == pytest.approx(9.0)


def test_mcp_and_cemsim_share_the_script_namespace(mcp, client):
    r = mcp.call_tool("execute_code", {"code": "cemsim_shared_probe = 1234", "include_screenshot": False})
    assert not r["is_error"], r["text"]
    assert client.execute("print(cemsim_shared_probe)").strip() == "1234"


def test_mcp_execute_code_error_is_reported_not_swallowed(mcp):
    r = mcp.call_tool("execute_code", {"code": "raise KeyError('mcp-err-99')", "include_screenshot": False})
    assert "mcp-err-99" in r["text"]
    assert r["is_error"] or "rror" in r["text"]


def test_mcp_view_returns_a_png(mcp, client, doc_name, cleanup_docs):
    cleanup_docs.append(doc_name)
    client.execute(
        "import FreeCAD, Part, FreeCADGui\n"
        f"d = FreeCAD.newDocument({doc_name!r})\n"
        "o = d.addObject('Part::Feature', 'V'); o.Shape = Part.makeBox(5, 5, 5); d.recompute()\n"
        "FreeCADGui.ActiveDocument = FreeCADGui.getDocument(d.Name)\n"
    )
    r = mcp.call_tool("get_view", {"view_name": "Front"})
    assert r["images"], f"no image returned: {r['text'][:200]}"
    assert base64.b64decode(r["images"][0])[:8] == b"\x89PNG\r\n\x1a\n"
