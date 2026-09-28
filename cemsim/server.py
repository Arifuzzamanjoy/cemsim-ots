"""FastAPI server: runs the simulator in real time and serves the operator HMI.

  GET  /                      HMI (static web app)
  WS   /ws                    status stream (~2 Hz) + command channel
  GET  /api/trend?tags=a,b&minutes=60
  GET  /api/events, /api/alarm_history, /api/heat_balance, /api/session, /api/snapshots
  POST /api/cmd               {"type": ..., ...}   (same payloads as the WS command channel)

Run:  uv run cemsim  [--port 8000] [--freecad host:port]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .sim import TREND_TAGS, CommandError, Simulator, _num
from .trainer import DISTURBANCES

WEB = Path(__file__).resolve().parent / "web"
log = logging.getLogger("cemsim")

SIM: Simulator | None = None
LOCK = asyncio.Lock()
BRIDGE_LOCK = asyncio.Lock()
_BG_TASKS: set = set()
CLIENTS: set[WebSocket] = set()
BRIDGE = None


def _clean(o):
    """Make numpy / nan values JSON-safe."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def get_sim() -> Simulator:
    global SIM
    if SIM is None:
        SIM = Simulator()
        names = [s["name"] for s in Simulator.list_files()]
        if "nominal" in names:
            SIM.load_file("nominal")
        else:
            SIM.set_all_running()
            SIM.run(60)
        SIM.running = False
    return SIM


# ---------------------------------------------------------------------------
async def sim_loop():
    """Real-time pacing: advance speed x wall time in dt steps."""
    sim = get_sim()
    last = time.perf_counter()
    debt = 0.0
    while True:
        await asyncio.sleep(0.1)
        now = time.perf_counter()
        wall = now - last
        last = now
        if not sim.running:
            debt = 0.0
            continue
        debt += wall * sim.speed
        n = int(debt / sim.dt)
        n = min(n, int(max(sim.speed, 1) * 2))  # never fall more than ~2 s behind
        async with LOCK:
            t0 = time.perf_counter()
            for _ in range(n):
                sim.step()
                if time.perf_counter() - t0 > 0.5:  # keep the event loop responsive
                    break
        debt -= n * sim.dt
        debt = min(debt, sim.dt * 5)


async def broadcast_loop():
    while True:
        await asyncio.sleep(0.5)
        if not CLIENTS:
            continue
        sim = get_sim()
        async with LOCK:
            msg = json.dumps(
                _clean({"type": "status", **sim.status(), "freecad": BRIDGE.status if BRIDGE is not None else None})
            )
        dead = []
        for ws in list(CLIENTS):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)


async def freecad_loop():
    """Live pushes while the operator wants live mode.  A missing model is rebuilt by
    the bridge itself; failures back off 15 s and retry, never cancelling the intent."""
    from .freecad_bridge import explain

    while True:
        await asyncio.sleep(1.0)
        if BRIDGE is None or not BRIDGE.want_live or BRIDGE.building:
            continue
        if time.monotonic() < BRIDGE.fail_until:
            continue
        sim = get_sim()
        async with LOCK:
            st = BRIDGE.state_from_sim(sim)
        try:
            async with BRIDGE_LOCK:
                await asyncio.to_thread(BRIDGE.push, st)
            if BRIDGE.status["state"] != "live":
                BRIDGE.set_status("live", "3-D view updating every second")
        except Exception as e:  # FreeCAD closed/busy: report, back off, keep simulating
            BRIDGE.last_error = str(e)
            BRIDGE.fail_until = time.monotonic() + 15.0
            BRIDGE.set_status("error", explain(e) + " (retrying every 15 s)")
            log.warning("FreeCAD push failed: %s", e)


async def _freecad_build_task():
    from .freecad_bridge import explain

    BRIDGE.building = True
    try:
        async with BRIDGE_LOCK:
            await asyncio.to_thread(BRIDGE.build)
        BRIDGE.fail_until = 0.0
        BRIDGE.set_status(
            "live" if BRIDGE.want_live else "off",
            "3-D model built" + (" - live updates on" if BRIDGE.want_live else ""),
        )
    except Exception as e:
        BRIDGE.fail_until = time.monotonic() + 15.0  # live mode (if wanted) retries later
        BRIDGE.set_status("error", explain(e))
        log.warning("FreeCAD build failed: %s", e)
    finally:
        BRIDGE.building = False


@asynccontextmanager
async def lifespan(_app):
    get_sim()
    tasks = [asyncio.create_task(f()) for f in (sim_loop, broadcast_loop, freecad_loop)]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="CemSim kiln-line training simulator", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)  # 3-D model json 890 kB -> ~200 kB


# ---------------------------------------------------------------------------
async def handle_cmd(c):
    """Execute one command; never raises - returns {"ok": False, "msg": ...} instead."""
    if not isinstance(c, dict):
        return {"ok": False, "msg": "command must be a JSON object"}
    try:
        return await _handle_cmd(c)
    except CommandError as e:
        return {"ok": False, "msg": str(e)}
    except KeyError as e:
        return {"ok": False, "msg": f"missing field {e}"}
    except Exception as e:  # keep the simulator and the socket alive
        log.exception("command failed: %s", c)
        return {"ok": False, "msg": f"internal error: {type(e).__name__}: {e}"}


async def _handle_freecad(c: dict):
    """FreeCAD commands never block: the build runs as a background task and its
    progress/result is published in the status stream and in this reply."""
    cmd_ = c.get("cmd")
    if cmd_ not in (None, "build", "live"):
        raise CommandError("freecad cmd must be build or live")
    if BRIDGE is None:
        return {"ok": False, "msg": "FreeCAD bridge not configured (start the server with --freecad host:port)"}
    on = bool(c.get("on", True))
    if cmd_ == "build":
        if BRIDGE.building:
            return {"ok": False, "msg": "FreeCAD build already in progress", "freecad": BRIDGE.status}
        BRIDGE.want_live = on
        BRIDGE.building = True  # set before the task starts: no race
        BRIDGE.set_status("checking", f"contacting {BRIDGE.url}")
        task = asyncio.create_task(_freecad_build_task())
        _BG_TASKS.add(task)  # keep a reference until done
        task.add_done_callback(_BG_TASKS.discard)
        return {"ok": True, "msg": "FreeCAD build started", "freecad": BRIDGE.status}
    BRIDGE.want_live = on
    BRIDGE.fail_until = 0.0
    if on:
        if not BRIDGE.building and BRIDGE.status["state"] != "live":
            BRIDGE.set_status("checking", "starting live updates (model is built automatically if missing)")
    elif not BRIDGE.building:
        BRIDGE.set_status("off", "live updates off")
    return {"ok": True, "msg": "FreeCAD live " + ("on" if on else "off"), "freecad": BRIDGE.status}


async def _handle_cmd(c: dict):
    sim = get_sim()
    t = c.get("type")
    if t == "freecad":
        return await _handle_freecad(c)
    async with LOCK:
        if t == "run":
            sim.running = bool(c.get("on", True))
            sim.log("TRAINER", "SIM.RUN", sim.running, "Running State Simulation Session")
        elif t == "speed":
            sim.speed = min(max(_num(c["value"], "speed"), 0.1), 60.0)
            sim.log("TRAINER", "SIM.SPEED", sim.speed, "Quick Motion State Simulation Session")
        elif t == "group":
            ok, why = sim.cmd_group(c["tag"], c["cmd"])
            return {"ok": ok, "msg": why}
        elif t == "drive":
            sim.cmd_drive(c["tag"], c["cmd"], c.get("value"))
        elif t == "pid":
            sim.cmd_pid(c["tag"], c["field"], c["value"])
        elif t == "ack":
            sim.alarms.ack(c.get("id"))
            sim.log("OPERATOR", "ALARM.ACK", c.get("id") or "ALL", "alarm acknowledge")
        elif t == "dist":
            sim.cmd_disturbance(c["id"], c.get("value"))
        elif t == "snapshot_save":
            name = "".join(ch for ch in str(c.get("name", "snap")) if ch.isalnum() or ch in "-_")[:60] or "snap"
            sim.save_file(name, str(c.get("comment", ""))[:200])
            sim.log("TRAINER", "SNAPSHOT", "SAVE", name)
        elif t == "snapshot_load":
            sim.load_file(c["name"])
        elif t == "session":
            if c["cmd"] not in ("start", "stop"):
                raise CommandError("session cmd must be start or stop")
            if c["cmd"] == "start":
                sim.session.start(sim.plant.t)
            else:
                sim.session.stop(sim.plant.t)
            sim.log("TRAINER", "SESSION", c["cmd"].upper(), "performance monitoring")
        else:
            return {"ok": False, "msg": f"unknown command {t}"}
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_text(
        json.dumps(
            _clean(
                {
                    "type": "meta",
                    "disturbances": DISTURBANCES,
                    "trend_tags": TREND_TAGS,
                    "snapshots": Simulator.list_files(),
                    "freecad": BRIDGE is not None,
                }
            )
        )
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "ack", "ok": False, "msg": "invalid JSON"}))
                continue
            res = await handle_cmd(msg)
            await ws.send_text(json.dumps({"type": "ack", "req": msg, **(res or {})}))
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(ws)


@app.post("/api/cmd")
async def api_cmd(c: dict):
    return JSONResponse(_clean(await handle_cmd(c)))


@app.get("/api/trend")
async def api_trend(tags: str, minutes: float = 60.0):
    sim = get_sim()
    tl = [t for t in tags.split(",") if t in TREND_TAGS]
    async with LOCK:
        t_end = sim.plant.t
        rows = [r for r in sim.history if r["t"] >= t_end - minutes * 60]
    return JSONResponse(_clean({"t": [r["t"] for r in rows], **{t: [r[t] for r in rows] for t in tl}}))


@app.get("/api/events")
async def api_events(n: int = 300):
    return JSONResponse(_clean(get_sim().events.rows[-n:]))


@app.get("/api/alarm_history")
async def api_alarm_hist(n: int = 300):
    return JSONResponse(_clean(list(get_sim().alarms.history)[-n:]))


@app.get("/api/heat_balance")
async def api_hb():
    sim = get_sim()
    async with LOCK:
        return JSONResponse(_clean(sim.heat_balance()))


@app.get("/api/session")
async def api_session():
    return JSONResponse(_clean(get_sim().session.result()))


@app.get("/api/snapshots")
async def api_snaps():
    return JSONResponse(Simulator.list_files())


@app.get("/api/lab")
async def api_lab():
    return JSONResponse(_clean(get_sim().lab.results[-50:]))


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/3d")
async def plant3d():
    """Live 3-D plant (three.js); geometry designed in FreeCAD, no FreeCAD at runtime."""
    return FileResponse(WEB / "plant3d.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


def main():
    global BRIDGE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--freecad", default="", help="host:port of the FreeCAD MCP XML-RPC server")
    a = ap.parse_args()
    if a.freecad:
        from .freecad_bridge import FreeCADBridge

        h, p = a.freecad.split(":")
        BRIDGE = FreeCADBridge(h, int(p))
    import uvicorn

    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
