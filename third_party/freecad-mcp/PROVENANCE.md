# freecad-mcp addon (vendored)

| | |
|---|---|
| Upstream | https://github.com/neka-nat/freecad-mcp |
| Version | v0.1.26 (addon `__version__` 0.1.26, `PROTOCOL_VERSION` 1) |
| Commit | `4751f953636f46b825424320c2f76e11159dd8c0` |
| License | MIT (see `LICENSE`) |
| Subset | `addon/FreeCADMCP` only - the part that runs *inside* FreeCAD |

The external MCP server (`freecad-mcp` on PyPI, Python >= 3.12) is **not** vendored; it is
run from PyPI at the same version (`uvx freecad-mcp==0.1.26`). Keeping both at the same version
is what the protocol handshake in `get_rpc_status` checks.

The files are unmodified. To upgrade: replace `addon/FreeCADMCP` from the new tag, update
this file and `duosim/freecad_bridge.py::EXPECTED_PROTOCOL_VERSION`, then run
`pytest -m freecad` (see `docs/FREECAD_INTEGRATION.md`).
