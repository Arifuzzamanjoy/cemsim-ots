"""HTTP API for design-by-prompt and the tenant layer.  Included by `duosim.server`.

    GET  /api/design/status              providers + FreeCAD worker health
    POST /api/design/generate            {prompt, provider?, build?}  -> spec (+ verified FreeCAD build)
    POST /api/design/build               {spec}                       -> build an edited / hand-written spec
    GET  /api/design                     this workspace's designs
    GET  /api/design/{id}                one design
    GET  /api/design/{id}/file/{kind}    mesh | step | fcstd | png | spec

Every route needs `X-API-Key` (or `Authorization: Bearer`) unless DUOSIM_DEV_OPEN=1, because
generation spends model credits and a build runs on the shared FreeCAD worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .design.builder import BuildResult, DesignBuilder
from .design.llm import load_dotenv, providers_from_env
from .design.service import DesignError, DesignService
from .design.spec import SpecError, parse_spec
from .freecad_bridge import FreeCADClient, explain
from .platform.store import ID_RE, QuotaExceeded, Store, Workspace

log = logging.getLogger("duosim.api_design")
router = APIRouter(prefix="/api/design")

MAX_BODY = 1_000_000  # bytes
MEDIA = {
    "mesh": "application/json",
    "spec": "application/json",
    "step": "model/step",
    "fcstd": "application/octet-stream",
    "png": "image/png",
}

_bridge_getter: Callable[[], Any] = lambda: None  # noqa: E731 - replaced by server.configure()
_STORE: Store | None = None
_SERVICE: DesignService | None = None
_BUILDERS: dict[tuple[str, int], DesignBuilder] = {}
_ENV_LOADED = False


def configure(get_bridge: Callable[[], Any]) -> None:
    """server.py hands over a getter for its (optional) FreeCAD bridge so designs are built in
    the same worker as the live kiln model."""
    global _bridge_getter
    _bridge_getter = get_bridge


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store()
    return _STORE


def get_service() -> DesignService:
    global _SERVICE, _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(os.environ.get("DUOSIM_ENV_FILE", ".env"))
        _ENV_LOADED = True
    if _SERVICE is None:
        _SERVICE = DesignService(providers_from_env(), os.environ.get("DUOSIM_DEFAULT_PROVIDER"))
    return _SERVICE


def get_client() -> FreeCADClient | None:
    """The FreeCAD worker to build in, or None.  Same worker as --freecad when that is set."""
    b = _bridge_getter()
    if b is not None:
        return FreeCADClient(b.host, b.port, b.token)
    url = os.environ.get("DUOSIM_FREECAD_URL", "").strip()
    if url:
        host, _, port = url.rpartition(":")
        return FreeCADClient(host or "127.0.0.1", int(port))
    return None


def get_builder() -> DesignBuilder | None:
    c = get_client()
    if c is None:
        return None
    key = (c.host, c.port)
    if key not in _BUILDERS:
        _BUILDERS[key] = DesignBuilder(c)
    else:
        _BUILDERS[key].client = c  # token may have rotated
    return _BUILDERS[key]


# ------------------------------------------------------------------ auth
def _presented_key(request: Request) -> str | None:
    k = request.headers.get("x-api-key")
    if k:
        return k.strip()
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


class _TooLarge(Exception):
    pass


_TOO_LARGE_BODY = b'{"detail":"request body too large"}'


class BodyLimitMiddleware:
    """Refuse oversized request bodies on the design / platform API *while streaming*, so chunked
    uploads (no Content-Length) cannot make the server buffer arbitrary amounts.

    The 413 is sent the moment the limit is crossed: FastAPI turns exceptions raised while it reads
    the body into a generic 400, so raising alone would hide the real reason."""

    PREFIXES = ("/api/design", "/api/platform")

    def __init__(self, app, limit: int = MAX_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.PREFIXES):
            return await self.app(scope, receive, send)
        state = {"total": 0, "started": False, "refused": False}

        async def refuse():
            state["refused"] = True
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
                        (b"connection", b"close"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})

        declared = dict(scope.get("headers") or []).get(b"content-length", b"0")
        if declared.isdigit() and int(declared) > self.limit:
            return await refuse()

        async def limited_receive():
            msg = await receive()
            if msg["type"] == "http.request":
                state["total"] += len(msg.get("body", b""))
                if state["total"] > self.limit and not state["started"] and not state["refused"]:
                    await refuse()
                    raise _TooLarge
            return msg

        async def guarded_send(msg):
            if state["refused"]:  # the app's follow-up error response must not reach the wire
                return
            state["started"] = state["started"] or msg["type"] == "http.response.start"
            await send(msg)

        with contextlib.suppress(_TooLarge):  # the 413 has already been sent
            await self.app(scope, limited_receive, guarded_send)


PUBLIC_NAME = "public"
# (everyone per UTC day, one client IP per UTC hour); env DUOSIM_PUBLIC_DAILY_<KIND>S / _HOURLY_<KIND>S override
PUBLIC_LIMITS = {"generate": (20, 5), "build": (60, 15), "session": (40, 10)}


def public_demo() -> bool:
    """Anonymous visitors may use the Studio with small shared caps (DUOSIM_PUBLIC_DEMO=1)."""
    return os.environ.get("DUOSIM_PUBLIC_DEMO") == "1"


def _client_ip(request: Request) -> str:
    if os.environ.get("DUOSIM_TRUST_FORWARDED") == "1":  # only behind a proxy that sets it
        xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if xff:
            return xff[:45]
    return request.client.host if request.client else "unknown"


def workspace(request: Request) -> Workspace:
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(400, "bad Content-Length") from None
    if declared > MAX_BODY:
        raise HTTPException(413, "request body too large")
    store = get_store()
    if os.environ.get("DUOSIM_DEV_OPEN") == "1":
        ws = store.workspace("dev")
        assert ws is not None
        return ws
    key = _presented_key(request)
    if key:  # a key that is presented must be valid: no silent downgrade to anonymous
        ws = store.authenticate(key)
        if ws is None:
            raise HTTPException(401, "that API key is not valid (platform keys start with ds_)")
        return ws
    if public_demo():
        base = store.workspace(PUBLIC_NAME)
        assert base is not None
        return Workspace(base.id, base.name, scope=_client_ip(request))
    hmac.compare_digest("x" * 8, "y" * 8)
    raise HTTPException(401, "API key required: send X-API-Key (create one: python -m duosim.platform keys create)")


def _limit(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def public_limit(kind: str, per: str) -> int:
    """Cap for anonymous visitors: per='daily' (all visitors together) or 'hourly' (one IP)."""
    day, hour = PUBLIC_LIMITS[kind]
    noun = {"generate": "GENERATIONS", "build": "BUILDS", "session": "SESSIONS"}[kind]
    return _limit(f"DUOSIM_PUBLIC_{per.upper()}_{noun}", day if per == "daily" else hour)


def _take(ws: Workspace, kind: str, limit: int) -> list[tuple[str, str, str]]:
    """Count one use against the caller's caps.  Returns the counters touched, for `_refund`."""
    st = get_store()
    day = time.strftime("%Y-%m-%d", time.gmtime())
    touched: list[tuple[str, str, str]] = []
    try:
        if ws.name == PUBLIC_NAME:
            # the visitor's own hourly allowance first, then the shared daily budget
            hour = (f"ip:{ws.scope}", time.strftime("%Y-%m-%dT%H", time.gmtime()), kind)
            st.take_scoped(*hour[:2], kind, public_limit(kind, "hourly"), "hourly")
            touched.append(hour)
            st.take_scoped(ws.id, day, kind, public_limit(kind, "daily"), "daily")
            touched.append((ws.id, day, kind))
        else:
            st.take(ws, kind, limit)
            touched.append((ws.id, day, kind))
    except QuotaExceeded as e:
        for t in touched:  # the half that succeeded must not be left counted
            st.refund_scoped(*t)
        hint = " - the public demo is rate limited; use your own API key" if ws.name == PUBLIC_NAME else ""
        raise HTTPException(429, str(e) + hint) from None
    return touched


def _refund(tokens: list[tuple[str, str, str]]) -> None:
    st = get_store()
    for t in tokens:
        st.refund_scoped(*t)


WS = Annotated[Workspace, Depends(workspace)]  # the authenticated tenant of this request


# ------------------------------------------------------------------ models
class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    provider: str | None = Field(default=None, max_length=32)
    build: bool = True


class BuildIn(BaseModel):
    spec: dict[str, Any]
    prompt: str = Field(default="", max_length=2000)


# ------------------------------------------------------------------ helpers
async def _build(spec) -> tuple[BuildResult | None, str | None]:
    builder = get_builder()
    if builder is None:
        return (
            None,
            "no FreeCAD worker configured (start the server with --freecad host:port or set DUOSIM_FREECAD_URL)",
        )
    try:
        return await asyncio.to_thread(builder.build, spec), None
    except Exception as e:
        log.warning("design build failed: %s", e)
        return None, explain(e)


def _persist(ws: Workspace, prompt, provider, model, spec_dict, res: BuildResult | None) -> str:
    arts: dict[str, bytes] = {}
    if res is not None:
        arts.update(res.artifacts)
        if res.png:
            arts["png"] = res.png
    return get_store().save_design(
        ws,
        prompt=prompt,
        provider=provider,
        model=model,
        spec=spec_dict,
        verified=None if res is None else res.ok,
        summary=None if res is None else res.summary(),
        artifacts=arts,
    )


def _build_json(res: BuildResult | None, err: str | None) -> dict[str, Any]:
    if res is None:
        return {"built": False, "reason": err}
    return {"built": True, **res.summary(), "checks": [c.to_dict() for c in res.checks]}


# ------------------------------------------------------------------ routes
@router.get("/status")
async def status(ws: WS):
    svc = get_service()
    client = get_client()
    fc: dict[str, Any] = {"configured": False}
    if client is not None:
        rep = await asyncio.to_thread(client.diagnose)
        fc = {
            "configured": True,
            "ok": rep["ok"],
            "problems": rep["problems"],
            "addon_version": rep.get("addon_version"),
        }
    st = get_store()
    pub = ws.name == PUBLIC_NAME
    return {
        "workspace": ws.name,
        "public": pub,
        "public_demo_enabled": public_demo(),
        "providers": svc.available(),
        "freecad": fc,
        "usage": {
            "generate": {
                "used": st.used_today(ws, "generate"),
                "limit": public_limit("generate", "daily") if pub else _limit("DUOSIM_DAILY_GENERATIONS", 50),
            },
            "build": {
                "used": st.used_today(ws, "build"),
                "limit": public_limit("build", "daily") if pub else _limit("DUOSIM_DAILY_BUILDS", 200),
            },
        },
    }


@router.post("/generate")
async def generate(body: GenerateIn, ws: WS):
    tokens = _take(ws, "generate", _limit("DUOSIM_DAILY_GENERATIONS", 50))
    svc = get_service()
    try:
        gen = await asyncio.to_thread(svc.generate, body.prompt, body.provider)
    except ValueError as e:  # bad prompt: it never reached the model
        _refund(tokens)
        raise HTTPException(422, str(e)) from None
    except DesignError as e:
        if e.upstream:  # the model API failed, not the request: do not charge the caller
            _refund(tokens)
        return JSONResponse({"detail": str(e), "errors": e.errors}, status_code=502 if e.upstream else 422)
    spec_dict = gen.spec.to_dict()
    res, err = (None, "build not requested")
    if body.build:
        _take(ws, "build", _limit("DUOSIM_DAILY_BUILDS", 200))
        res, err = await _build(gen.spec)
    did = _persist(ws, body.prompt, gen.provider, gen.model, spec_dict, res)
    return {
        "id": did,
        "spec": spec_dict,
        "warnings": gen.warnings,
        "provider": gen.provider,
        "model": gen.model,
        "attempts": gen.attempts,
        "latency_s": gen.latency_s,
        "usage": gen.usage,
        "build": _build_json(res, err),
    }


@router.post("/build")
async def build(body: BuildIn, ws: WS):
    try:
        spec, warns = parse_spec(body.spec)
    except SpecError as e:
        return JSONResponse({"detail": "invalid design", "errors": e.errors}, status_code=422)
    _take(ws, "build", _limit("DUOSIM_DAILY_BUILDS", 200))
    res, err = await _build(spec)
    did = _persist(ws, body.prompt or "(edited spec)", None, None, spec.to_dict(), res)
    return {"id": did, "spec": spec.to_dict(), "warnings": warns, "build": _build_json(res, err)}


@router.get("")
async def list_designs(ws: WS):
    if ws.name == PUBLIC_NAME:  # anonymous visitors share one workspace: do not list each other's prompts
        return {"designs": []}
    return {"designs": get_store().list_designs(ws)}


@router.get("/{design_id}")
async def get_design(design_id: str, ws: WS):
    d = get_store().get_design(ws, design_id) if ID_RE.match(design_id) else None
    if d is None:
        raise HTTPException(404, "no such design")
    return d


@router.get("/{design_id}/file/{kind}")
async def design_file(design_id: str, kind: str, ws: WS):
    p = get_store().artifact_path(ws, design_id, kind)
    if p is None:
        raise HTTPException(404, "no such file")
    return FileResponse(p, media_type=MEDIA.get(kind, "application/octet-stream"), filename=p.name)
