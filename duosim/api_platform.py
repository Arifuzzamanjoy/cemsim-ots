"""Generic simulator runtime for the SaaS layer: any registered plug-in, as headless sessions.

    GET    /api/platform/simulators                 installed simulators (manifests)
    GET    /api/platform/simulators/{id}            one manifest
    POST   /api/platform/sessions                   {simulator, seed?}  -> new session
    GET    /api/platform/sessions/{sid}             current tags
    POST   /api/platform/sessions/{sid}/step        {steps}
    POST   /api/platform/sessions/{sid}/command     {name, args}
    DELETE /api/platform/sessions/{sid}

Sessions are per workspace, capped in number and idle time.  The cement HMI at `/` is separate
(its own global simulator); this is the multi-tenant, API-driven path.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .api_design import WS, _limit, _take
from .platform.registry import CommandRejected, SimulatorInstance, default_registry
from .platform.store import Workspace

router = APIRouter(prefix="/api/platform")

MAX_STEPS_PER_CALL = 3600


@dataclass
class Session:
    id: str
    sim_id: str
    instance: SimulatorInstance
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


_SESSIONS: dict[str, dict[str, Session]] = {}  # tenant key -> session id -> Session
_GUARD = threading.Lock()


def _tenant(ws: Workspace) -> str:
    """Sessions belong to a workspace, and inside the shared public workspace to one client IP."""
    return f"{ws.id}:{ws.scope}"


def _idle_ttl(tenant: str) -> float:
    """Anonymous (scoped) visitors lose an abandoned console quickly: a closed tab cannot clean up."""
    anonymous = bool(tenant.partition(":")[2])
    return float(_limit("DUOSIM_PUBLIC_SESSION_IDLE_S", 90) if anonymous else _limit("DUOSIM_SESSION_IDLE_S", 1800))


def _evict(tenant: str) -> None:
    now, ttl = time.time(), _idle_ttl(tenant)
    mine = _SESSIONS.get(tenant, {})
    for sid in [s for s, v in mine.items() if now - v.last_used > ttl]:
        del mine[sid]


def _session(tenant: str, sid: str) -> Session:
    with _GUARD:
        _evict(tenant)
        s = _SESSIONS.get(tenant, {}).get(sid)
    if s is None:
        raise HTTPException(404, "no such session (it may have expired)")
    s.last_used = time.time()
    return s


class SessionIn(BaseModel):
    simulator: str = Field(max_length=40)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class StepIn(BaseModel):
    steps: int = Field(default=1, ge=1, le=MAX_STEPS_PER_CALL)


class CommandIn(BaseModel):
    name: str = Field(max_length=48)
    args: dict[str, Any] = Field(default_factory=dict)


def _view(s: Session) -> dict[str, Any]:
    return {"id": s.id, "simulator": s.sim_id, "t": s.instance.t, "tags": s.instance.tags()}


@router.get("/simulators")
async def simulators(ws: WS):
    return {"simulators": [m.to_dict() for m in default_registry().manifests()]}


@router.get("/simulators/{sim_id}")
async def simulator(sim_id: str, ws: WS):
    try:
        return default_registry().get(sim_id).manifest.to_dict()
    except KeyError:
        raise HTTPException(404, "no such simulator") from None


@router.post("/sessions", status_code=201)
async def create_session(body: SessionIn, ws: WS):
    try:
        plugin = default_registry().get(body.simulator)
    except KeyError:
        raise HTTPException(404, "no such simulator") from None
    cap = _limit("DUOSIM_MAX_SESSIONS", 5)
    with _GUARD:
        _evict(_tenant(ws))
        if len(_SESSIONS.get(_tenant(ws), {})) >= cap:
            raise HTTPException(429, f"at most {cap} concurrent sessions per workspace; delete one first")
    _take(ws, "session", _limit("DUOSIM_DAILY_SESSIONS", 100))
    inst = await asyncio.to_thread(plugin.create, body.seed)
    s = Session(uuid.uuid4().hex[:16], body.simulator, inst)
    with _GUARD:
        _SESSIONS.setdefault(_tenant(ws), {})[s.id] = s
    return _view(s)


@router.get("/sessions/{sid}")
async def get_session(sid: str, ws: WS):
    return _view(_session(_tenant(ws), sid))


@router.post("/sessions/{sid}/step")
async def step(sid: str, body: StepIn, ws: WS):
    s = _session(_tenant(ws), sid)

    def run():
        with s.lock:
            s.instance.step(body.steps)
            return _view(s)

    return await asyncio.to_thread(run)


@router.post("/sessions/{sid}/command")
async def command(sid: str, body: CommandIn, ws: WS):
    s = _session(_tenant(ws), sid)

    def run():
        with s.lock:
            return s.instance.command(body.name, **body.args)

    try:
        res = await asyncio.to_thread(run)
    except CommandRejected as e:
        raise HTTPException(422, str(e)) from None
    except TypeError as e:  # wrong argument names for an adapter that is not SimpleModel
        raise HTTPException(422, f"bad arguments: {e}") from None
    return {"result": res, **_view(s)}


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: str, ws: WS):
    with _GUARD:
        if _SESSIONS.get(_tenant(ws), {}).pop(sid, None) is None:
            raise HTTPException(404, "no such session")


def _reset_for_tests() -> None:  # pragma: no cover - used by the test-suite
    _SESSIONS.clear()
