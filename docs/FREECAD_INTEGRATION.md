# FreeCAD ↔ FreeCAD-MCP ↔ DuoSim: integration and validation

This is how DuoSim talks to [FreeCAD](https://github.com/FreeCAD/FreeCAD) through the
[freecad-mcp](https://github.com/neka-nat/freecad-mcp) addon, what was verified against the
*real* programs, and what to know before running it in production.

## Architecture

```
                         ┌────────────────────────── FreeCAD 1.1 (GUI build, virtual display) ───────────────────────┐
  MCP clients            │  FreeCAD-MCP addon  (vendored: third_party/freecad-mcp, v0.1.26, protocol 1)               │
  (Claude Desktop,  ──►  │  XML-RPC server on 127.0.0.1:<port>   Bearer token · IP allow-list · browser-request guard │
   Claude Code, ...)     │  every call is queued onto FreeCAD's GUI thread (execute_code, screenshots, documents)     │
        │ stdio          └──────────────▲──────────────────────────▲────────────────────────────▲────────────────────┘
        ▼                               │ XML-RPC                  │ XML-RPC                    │ XML-RPC
  freecad-mcp (PyPI 0.1.26,       FreeCADBridge              DesignBuilder                  FreeCADWorker
  port 9875 hard-coded)           live kiln model            spec → solids → verified       starts/stops the above
        │                         (duosim/freecad_bridge)    (duosim/design/builder)        (duosim/freecad_worker)
        └─ same FreeCAD session ──────────┴───────────────────────────┴──────────────────────────┘
```

| Component | File | Role |
|---|---|---|
| `FreeCADClient` | `duosim/freecad_bridge.py` | Authenticated XML-RPC client, protocol/version handshake, `diagnose()`, screenshots |
| `FreeCADBridge` | `duosim/freecad_bridge.py` | The live kiln model: build once, push simulator state ~1 Hz, `readback()` to measure it |
| `FreeCADWorker` | `duosim/freecad_worker.py` | Starts FreeCAD + Xvfb in a sandboxed `HOME` with the addon, token and port |
| `DesignBuilder` | `duosim/design/builder.py` | Validated spec → FreeCAD geometry → measured against closed-form values |
| addon | `third_party/freecad-mcp/` | Vendored unmodified (MIT), pinned commit, see `PROVENANCE.md` |

### What the addon needs (verified, not assumed)

* **The GUI build of FreeCAD.** `rpc_server.py` imports `FreeCADGui` and Qt and drives a Qt timer, so
  `freecadcmd` is not enough. On a server run FreeCAD on **Xvfb** with Mesa software GL; this also makes
  `get_active_screenshot` work. `FreeCADWorker` does exactly this (≈10 s to ready, ≈0.6–0.7 GB RSS each).
* **A fixed port in the upstream MCP server.** `freecad-mcp` 0.1.26 hard-codes `port=9875`
  (`server.py: FreeCADConnection(host=..., port=9875)`, no `--port`). Its leg can therefore only reach a worker on
  9875 — run one worker per container, or tunnel. DuoSim's own client accepts any port.
* **The addon's auto-start is also 9875 only.** `FreeCADWorker` starts the server from a startup macro on the
  port you choose, so several workers can share a host (used by the fault-injection tests).
* **A token for anything non-local.** The RPC endpoint executes arbitrary Python (`execute_code`). The addon
  supports `Authorization: Bearer <token>`; DuoSim sends it from `FREECAD_MCP_TOKEN` /
  `FREECAD_MCP_TOKEN_FILE` / `--freecad-token`. `FreeCADWorker --remote` refuses to start without one.
  The addon also rejects any request carrying an `Origin` header (a web page cannot call it) and, when bound to
  loopback, any non-localhost `Host`.
* **Screenshots capture the *active* document.** With several documents open, activate yours first
  (`FreeCADClient.screenshot(doc=...)` does).

## What was validated, and how

`tests/integration/` (marker `freecad`, opt-in) starts a real FreeCAD 1.1.4 process and talks to it over the real
protocol and through the real upstream MCP server. **No mocks.** 59 tests (+1 opt-in live-model test), all passing:

| Suite | Checks |
|---|---|
| `test_01_connection` (11) | real GUI FreeCAD ≥ 1.0, code runs on the GUI/main thread; addon version = vendored copy = `EXPECTED_PROTOCOL_VERSION`; token enforced (none / wrong → 401, secret never echoed); `Origin` → 403, rebinding `Host` → 403, wrong content-type → 415; Python errors returned as errors and the GUI survives; shared script namespace; exact OCCT volumes; screenshots show geometry (with an empty-document negative control) |
| `test_02_mcp_server` (9) | upstream `freecad-mcp==0.1.26` over stdio: handshake, tool catalogue, no version warning; **cross-tool consistency**: geometry created through MCP is measured identically by DuoSim's client and vice-versa (volume vs π·r²·h), edits visible both ways, shared namespace, PNG from `get_view` |
| `test_03_live_bridge` (8) | after pushing real simulator states, FreeCAD is **read back** and compared: 30 shell-scanner colours, 5 cyclone + calciner colours, 18 clinker-bed heights and colours, flame cone length (mm), kiln rotation angle = ∫rpm·dt, drive colours; a disturbance (coal LHV drop + ID-fan trip) changes the model and the rendered view; a lost document is rebuilt automatically; the HMI command `POST /api/cmd freecad build` drives the whole chain |
| `test_04_plant_design` (4) | `tools/freecad/build_plant3d.py` in real FreeCAD: 149 parts, 0 clashes, regenerated mesh equals the committed `plant3d.json` (vertex sets and surface areas, 149/149), binding names present |
| `test_05_resilience` (2) | `SIGKILL` FreeCAD → clear "not reachable", restart → recovers and rebuilds; status stays responsive while the GUI thread is busy |
| `test_06_design_build` (25 + 1 opt-in) | every primitive × every axis vs closed form; STEP round-trip volume; mesh bounding box; real-measurement negative control; the design API end to end on a real worker; the largest valid design builds in < 60 s; (opt-in `llm`) a real Gemini answer verified by FreeCAD |

```bash
# one-off: FreeCAD 1.1.4 AppImage (checksum from the release page), extracted - no FUSE needed
curl -fL -o fc.AppImage https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/FreeCAD_1.1.4-Linux-x86_64-py311.AppImage
echo "f6dc6ba676e5ac96a565ebc8d657232f94c6158e85b4352141bd1a46f6b43434  fc.AppImage" | sha256sum -c -
chmod +x fc.AppImage && ./fc.AppImage --appimage-extract && mv squashfs-root /opt/freecad-1.1.4
sudo apt install xvfb libgl1-mesa-dri libxcb-cursor0 ...     # see .github/workflows/freecad-integration.yml

DUOSIM_FREECAD=/opt/freecad-1.1.4 uv run pytest -m "freecad and not llm" tests/integration -v   # ~3 min
DUOSIM_FREECAD=/opt/freecad-1.1.4 uv run pytest -m "freecad and llm" ...             # +1 live model call
```

The default `pytest` run skips these (hermetic and fast); CI runs them in
`.github/workflows/freecad-integration.yml` (nightly, on demand, and on PRs touching the integration).

### Defects found by validating against the real thing

These were invisible while the bridge was only ever tested against a mock written by the same author:

1. **The bridge could not talk to a token-protected addon** (HTTP 401) — no auth support at all. Fixed.
2. **`GUI_DISPATCH_STUCK`** (the addon's second failure mode) was not recognised and surfaced as a raw message. Fixed.
3. No protocol/version check; a mismatching addon would fail obscurely. `diagnose()` now reports it.
4. No way to *measure* the live model — "no exception" was the only evidence. `readback()` + `test_03` fix that.
5. `Part.Compound` has no `CenterOfMass` (boolean results) — caught by the design verifier on first real run.
6. Triangulation connectivity of the exported mesh differs run to run; the asset check compares geometry
   (vertex sets, surface area), not indices.
7. **A valid design could wedge the shared worker.** Pipes are built by fusing one cylinder per run and one
   sphere per elbow, ~0.19 s per waypoint; 33 pipes x 24 points (the old limit) exceeded the addon's 90 s GUI
   budget, and a started GUI task *cannot be cancelled*. Total waypoints are now capped (120); the worst valid
   design (300 parts) builds in ~32 s, enforced by a regression test. (`multiFuse` is faster but silently
   returned an empty shape in FreeCAD 1.1.4, so it is not used.)
8. Deployment: the worker's non-root user could not read a file-based Docker secret (Compose ignores
   `uid`/`mode` for them); the images start as root only to read it, then drop privileges with `setpriv`.

### Known limitations (honest list)

* **Two geometries.** The live model (`FC_MODULE_SRC`, 92 objects) and the detailed design
  (`tools/freecad/build_plant3d.py`, 149 parts, drives the web 3-D view) are separate code with partly different
  dimensions. They can drift; unifying them (bind live state to the design script's named parts, as
  `plant3d.js` does) is the obvious next step.
* One worker = one FreeCAD session; FreeCAD serialises work on its GUI thread. Throughput scales by adding
  workers (containers), which this repository sets up but does not auto-scale.
* Tested on Linux with FreeCAD 1.1.4 + addon 0.1.26 only. The addon documents Windows/macOS/Flatpak paths;
  they were not exercised.
* The upstream MCP server's tools return optional screenshots; with `--only-text-feedback` they are omitted.

## Running it

```bash
# 1. a worker (foreground; prints its URL, writes the token to <home>/rpc_token)
python -m duosim.freecad_worker start --port 9875

# 2. the app, pointed at it
FREECAD_MCP_TOKEN=$(cat .duosim/freecad-worker/9875/rpc_token) uv run duosim --freecad 127.0.0.1:9875

# 3. check the link at any time
curl localhost:8000/api/freecad/health?deep=true
```

Use it from an MCP client against the *same* FreeCAD (Claude Desktop / Claude Code):

```json
{ "mcpServers": { "freecad": {
    "command": "uvx", "args": ["freecad-mcp==0.1.26", "--host", "127.0.0.1"],
    "env": { "FREECAD_MCP_TOKEN": "<contents of rpc_token>" } } } }
```

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `requires an auth token` | set `FREECAD_MCP_TOKEN` (or `--freecad-token`) to the addon's token |
| `not reachable` | worker not running / wrong port; the MCP server only ever uses 9875 |
| `GUI thread is not picking it up` | a mouse button registered as held or a modal dialog is open (desktop FreeCAD) |
| `GUI_DISPATCH_STUCK` | a long GUI task cannot be cancelled; wait or restart the worker |
| blank screenshot | the document is not active (`screenshot(doc=...)`), or no GL (install `libgl1-mesa-dri`) |
| `protocol_ok: false` | addon and bridge versions differ — update both together (`PROVENANCE.md`) |
