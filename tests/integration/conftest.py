"""Fixtures for the real-FreeCAD integration suite (marker: freecad).

These tests start an actual FreeCAD GUI process (virtual display) with the vendored
FreeCAD-MCP addon and talk to it over the real RPC protocol - and through the real upstream
`freecad-mcp` MCP server.  Nothing is mocked.

They are opt-in because they need ~3 GB of FreeCAD and take a couple of minutes:

    DUOSIM_FREECAD=/opt/freecad-1.1.4 uv run pytest -m freecad tests/integration -v

See docs/FREECAD_INTEGRATION.md.
"""

from __future__ import annotations

import os
import shutil
import socket
import uuid

import pytest

from duosim.freecad_bridge import FreeCADClient
from duosim.freecad_worker import FreeCADWorker, find_freecad

from .mcp_stdio import MCPStdioClient

# upstream's MCP server hard-codes the RPC port (freecad_mcp/server.py: port=9875)
MCP_RPC_PORT = 9875
MCP_SERVER_VERSION = "0.1.26"  # must equal the vendored addon (third_party/freecad-mcp/PROVENANCE.md)


def pytest_collection_modifyitems(config, items):
    expr = config.getoption("-m") or ""
    for marker, why in (
        ("freecad", "real-FreeCAD suite is opt-in: run with  -m freecad"),
        ("llm", "live model-API test is opt-in (spends credits): run with  -m llm"),
    ):
        if marker in expr:
            continue
        skip = pytest.mark.skip(reason=why)
        for it in items:
            if marker in it.keywords:
                it.add_marker(skip)


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="session")
def worker(tmp_path_factory):
    if find_freecad() is None:
        pytest.skip("FreeCAD not found (set DUOSIM_FREECAD to an extracted AppImage dir)")
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed (apt install xvfb)")
    if not _port_free(MCP_RPC_PORT):
        pytest.skip(f"port {MCP_RPC_PORT} is busy (another FreeCAD RPC server?)")
    w = FreeCADWorker(port=MCP_RPC_PORT, home=tmp_path_factory.mktemp("fcworker"))
    w.start()
    yield w
    keep = os.environ.get("DUOSIM_WORKER_LOG_DIR")
    if keep and w.log_path.exists():  # CI uploads this as an artifact
        os.makedirs(keep, exist_ok=True)
        shutil.copy(w.log_path, os.path.join(keep, "freecad.log"))
    w.stop()


@pytest.fixture(scope="session")
def client(worker) -> FreeCADClient:
    return worker.client()


@pytest.fixture(scope="session")
def mcp(worker, tmp_path_factory):
    """The real upstream MCP server (PyPI freecad-mcp) speaking stdio, connected to the worker."""
    if shutil.which("uvx") is None:
        pytest.skip("uvx not installed (needed to run the upstream freecad-mcp server)")
    log = str(tmp_path_factory.mktemp("mcp") / "server.log")
    c = MCPStdioClient(
        ["uvx", "--from", f"freecad-mcp=={MCP_SERVER_VERSION}", "freecad-mcp", "--host", "127.0.0.1"],
        env={"FREECAD_MCP_TOKEN": worker.token},
        stderr_path=log,
    )
    c.start()
    try:
        c.initialize()
    except Exception:
        c.close()
        raise
    yield c
    c.close()


@pytest.fixture
def doc_name():
    """A unique document name; closed again after the test."""
    return "T" + uuid.uuid4().hex[:8]


@pytest.fixture
def cleanup_docs(client):
    names: list[str] = []
    yield names
    for n in names:
        try:
            client.execute(f"import FreeCAD\nif {n!r} in FreeCAD.listDocuments(): FreeCAD.closeDocument({n!r})")
        except Exception:  # best effort
            pass
