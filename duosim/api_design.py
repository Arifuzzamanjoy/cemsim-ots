"""HTTP API for design-by-prompt / by-image / by-CAD and the tenant layer.  Included by `duosim.server`.

    GET    /api/design/status             providers, agent modes, credits, FreeCAD worker health
    POST   /api/design/generate           {prompt, images?, provider?, mode?, build?}  -> agent run (+ verified build)
    POST   /api/design/build              {spec}          -> build an edited / hand-written spec
    POST   /api/design/import?filename=   raw file body   -> CAD / mesh import, primitive fit (+ refine, + build)
    GET    /api/design                    this workspace's designs
    GET    /api/design/{id}               one design
    DELETE /api/design/{id}               delete a design and its files
    GET    /api/design/{id}/file/{kind}   mesh | step | fcstd | png | spec

Authentication: `X-API-Key` (or `Authorization: Bearer`), or a signed-in browser session (cookie + the
`X-DuoSim-CSRF: 1` header on writes), or - when enabled - the rate-limited anonymous public demo.
DUOSIM_DEV_OPEN=1 disables authentication for local development only.

Billing: every operation is priced in credits (`duosim.saas.plans`) and debited atomically before the work
starts; when the model provider or the FreeCAD worker fails through no fault of the caller the charge is
refunded.  Daily caps still apply on top as abuse protection.
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

from .design.agent import MODES, DesignAgent
from .design.builder import BuildResult, DesignBuilder
from .design.cad_import import FORMATS, WORKER_FORMATS, ImportError_, import_cad, measure_with_worker
from .design.inputs import MAX_IMAGE_BYTES, MAX_IMAGES, ImageError, parse_images
from .design.llm import load_dotenv, provider_from_secret, providers_from_env
from .design.service import DesignError, DesignService
from .design.spec import SpecError, parse_spec
from .freecad_bridge import FreeCADClient, explain
from .platform.store import ID_RE, QuotaExceeded, Store, Workspace

log = logging.getLogger("duosim.api_design")
router = APIRouter(prefix="/api/design", tags=["design"])

MAX_BODY = 1_000_000  # bytes, every JSON route
MAX_GENERATE_BODY = MAX_IMAGES * MAX_IMAGE_BYTES * 4 // 3 + 1_000_000  # reference images travel base64-encoded
MEDIA = {
    "mesh": "application/json",
    "spec": "application/json",
    "step": "model/step",
    "fcstd": "application/octet-stream",
    "png": "image/png",
}
SESSION_COOKIE = "duosim_session"
CSRF_HEADER = "x-duosim-csrf"

_bridge_getter: Callable[[], Any] = lambda: None  # noqa: E731 - replaced by server.configure()
_STORE: Store | None = None
_SERVICE: DesignService | None = None
_BUILDERS: dict[tuple[str, int], DesignBuilder] = {}
_ENV_LOADED = False


def max_import_bytes() -> int:
    return _limit("DUOSIM_MAX_IMPORT_MB", 50) * 1024 * 1024


def body_limit(path: str) -> int:
    if path == "/api/design/import":
        return max_import_bytes()
    if path == "/api/design/generate":
        return MAX_GENERATE_BODY
    return MAX_BODY


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


def get_saas():
    from .saas import saas_for

    return saas_for(get_store())


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
    """Refuse oversized request bodies on the API *while streaming*, so chunked uploads (no
    Content-Length) cannot make the server buffer arbitrary amounts.  The limit depends on the route
    (`body_limit`): 1 MB for JSON, more for reference images and CAD uploads.

    The 413 is sent the moment the limit is crossed: FastAPI turns exceptions raised while it reads
    the body into a generic 400, so raising alone would hide the real reason."""

    PREFIXES = ("/api/",)

    def __init__(self, app, limit: int | None = None):
        self.app, self.fixed = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.PREFIXES):
            return await self.app(scope, receive, send)
        limit = self.fixed if self.fixed is not None else body_limit(scope["path"])
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
        if declared.isdigit() and int(declared) > limit:
            return await refuse()

        async def limited_receive():
            msg = await receive()
            if msg["type"] == "http.request":
                state["total"] += len(msg.get("body", b""))
                if state["total"] > limit and not state["started"] and not state["refused"]:
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
PUBLIC_LIMITS = {"generate": (20, 5), "build": (60, 15), "session": (40, 10), "import": (40, 10)}


def public_demo() -> bool:
    """Anonymous visitors may use the Studio with small shared caps (DUOSIM_PUBLIC_DEMO=1)."""
    return os.environ.get("DUOSIM_PUBLIC_DEMO") == "1"


def _client_ip(request: Request) -> str:
    if os.environ.get("DUOSIM_TRUST_FORWARDED") == "1":  # only behind a proxy that sets it
        xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if xff:
            return xff[:45]
    return request.client.host if request.client else "unknown"


def _active(ws: Workspace) -> Workspace:
    if get_saas().workspace_status(ws.id) != "active":
        raise HTTPException(403, "this workspace is suspended - contact support")
    return ws


def workspace(request: Request) -> Workspace:
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(400, "bad Content-Length") from None
    if declared > body_limit(request.url.path):
        raise HTTPException(413, "request body too large")
    store = get_store()
    if os.environ.get("DUOSIM_DEV_OPEN") == "1":
        ws = store.workspace("dev")
        assert ws is not None
        return Workspace(ws.id, ws.name, via="dev")
    key = _presented_key(request)
    if key:  # a key that is presented must be valid: no silent downgrade to anonymous
        ws = store.authenticate(key)
        if ws is None:
            raise HTTPException(401, "that API key is not valid (platform keys start with ds_)")
        return _active(ws)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        saas = get_saas()
        sess = saas.session(cookie)
        if sess is not None and sess["workspace_id"]:
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
                raise HTTPException(403, "missing X-DuoSim-CSRF header")
            base = saas.workspace_obj(sess["workspace_id"])
            if base is not None:
                role = saas.role(sess["user_id"], base.id) or ""
                return _active(Workspace(base.id, base.name, user_id=sess["user_id"], role=role, via="session"))
    if public_demo():
        base = store.workspace(PUBLIC_NAME)
        assert base is not None
        return Workspace(base.id, base.name, scope=_client_ip(request), via="public")
    hmac.compare_digest("x" * 8, "y" * 8)
    raise HTTPException(401, "sign in or send an API key (X-API-Key: ds_...)")


def _limit(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def public_limit(kind: str, per: str) -> int:
    """Cap for anonymous visitors: per='daily' (all visitors together) or 'hourly' (one IP)."""
    day, hour = PUBLIC_LIMITS[kind]
    noun = {"generate": "GENERATIONS", "build": "BUILDS", "session": "SESSIONS", "import": "IMPORTS"}[kind]
    return _limit(f"DUOSIM_PUBLIC_{per.upper()}_{noun}", day if per == "daily" else hour)


def _plan_of(ws: Workspace):
    from .saas import plans

    if ws.via in ("public", "dev"):
        return None
    info = get_saas().workspace_info(ws.id)
    return plans.plan(info["plan"] if info else None)


def daily_limit(ws: Workspace, name: str, default: int) -> int:
    """Abuse cap per workspace and UTC day; paid plans get ten times the free cap unless set explicitly."""
    if name in os.environ:
        return _limit(name, default)
    p = _plan_of(ws)
    return default * 10 if p is not None and (p.price_usd is None or p.price_usd > 0) else default


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
        hint = " - the public demo is rate limited; sign up or use your own API key" if ws.name == PUBLIC_NAME else ""
        raise HTTPException(429, str(e) + hint) from None
    return touched


def _refund(tokens: list[tuple[str, str, str]]) -> None:
    st = get_store()
    for t in tokens:
        st.refund_scoped(*t)


# ------------------------------------------------------------------ credits
def charges_credits(ws: Workspace) -> bool:
    from .saas.core import credits_enabled

    return credits_enabled() and ws.via in ("key", "session")


def charge(ws: Workspace, amount: int, op: str, ref: str | None = None) -> int:
    """Debit credits (0 = nothing charged).  402 with the price and balance when the workspace cannot pay."""
    if not charges_credits(ws) or amount <= 0:
        return 0
    from .saas.core import Forbidden, InsufficientCredits

    try:
        return get_saas().charge(ws.id, amount, op, ws.user_id, ref)
    except InsufficientCredits as e:
        raise HTTPException(
            402, f"{e} - buy credits or upgrade the plan (Account > Billing)", headers={"X-Credits-Needed": str(amount)}
        ) from None
    except Forbidden as e:
        raise HTTPException(403, str(e)) from None


def refund(ws: Workspace, ledger_id: int, note: str) -> None:
    if ledger_id:
        get_saas().refund(ws.id, ledger_id, note)


def balance(ws: Workspace) -> int | None:
    return get_saas().balance(ws.id) if charges_credits(ws) else None


def require_feature(ws: Workspace, feature: str, what: str) -> None:
    p = _plan_of(ws)
    if p is not None and feature not in p.features:
        raise HTTPException(402, f"{what} needs a plan with '{feature}' - upgrade at Account > Billing")


def emit(ws: Workspace, event: str, data: dict[str, Any]) -> None:
    if ws.via in ("key", "session"):
        from .saas.webhooks import emit as _emit

        _emit(get_saas(), ws.id, event, data)


WS = Annotated[Workspace, Depends(workspace)]  # the authenticated tenant of this request


# ------------------------------------------------------------------ models
class GenerateIn(BaseModel):
    prompt: str = Field(default="", max_length=2000)
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    provider: str | None = Field(default=None, max_length=40)
    mode: str = Field(default="fast", max_length=10)
    build: bool = True


class BuildIn(BaseModel):
    spec: dict[str, Any]
    prompt: str = Field(default="", max_length=2000)


# ------------------------------------------------------------------ helpers
def _builder_fn():
    builder = get_builder()
    if builder is None:
        return None

    def run(spec):
        try:
            return builder.build(spec)
        except Exception as e:
            log.warning("design build failed: %s", e)
            raise RuntimeError(explain(e)) from None

    return run


async def _build(spec) -> tuple[BuildResult | None, str | None]:
    builder = get_builder()
    if builder is None:
        return None, NO_WORKER
    try:
        return await asyncio.to_thread(builder.build, spec), None
    except Exception as e:
        log.warning("design build failed: %s", e)
        return None, explain(e)


NO_WORKER = "no FreeCAD worker configured (start the server with --freecad host:port or set DUOSIM_FREECAD_URL)"


def _persist(ws: Workspace, prompt, provider, model, spec_dict, res: BuildResult | None, summary_extra=None) -> str:
    arts: dict[str, bytes] = {}
    if res is not None:
        arts.update(res.artifacts)
        if res.png:
            arts["png"] = res.png
    summary = None if res is None else res.summary()
    if summary_extra:
        summary = {**(summary or {}), **summary_extra}
    return get_store().save_design(
        ws,
        prompt=prompt,
        provider=provider,
        model=model,
        spec=spec_dict,
        verified=None if res is None else res.ok,
        summary=summary,
        artifacts=arts,
    )


def _build_json(res: BuildResult | None, err: str | None) -> dict[str, Any]:
    if res is None:
        return {"built": False, "reason": err}
    return {"built": True, **res.summary(), "checks": [c.to_dict() for c in res.checks]}


def resolve_provider(ws: Workspace, name: str | None):
    """(provider, byok).  A workspace's own provider of that name wins over the platform's."""
    svc = get_service()
    if name and ws.via in ("key", "session"):
        secret = get_saas().provider_secret(ws.id, name)
        if secret is not None:
            return provider_from_secret(secret), True
    pname = name or svc.default
    if pname is None:
        raise HTTPException(
            503, "no model provider configured (set GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY)"
        )
    prov = svc.providers.get(pname)
    if prov is None:
        raise HTTPException(422, f"unknown provider {pname!r}")
    return prov, False


def _modes_for(ws: Workspace) -> list[str]:
    if ws.via == "public":
        return ["fast", "standard"]
    p = _plan_of(ws)
    if p is not None and "agent_deep" not in p.features:
        return ["fast", "standard"]
    return list(MODES)


# ------------------------------------------------------------------ routes
@router.get("/status")
async def status(ws: WS):
    from .saas import plans

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
    providers = [
        {**p, "vision": bool(getattr(svc.providers[p["name"]], "vision", False)), "byok": False}
        for p in svc.available()
    ]
    if ws.via in ("key", "session"):
        for k in get_saas().provider_keys(ws.id):
            providers.append(
                {
                    "name": k["name"],
                    "model": k["model"] or k["type"],
                    "default": False,
                    "vision": k["vision"],
                    "byok": True,
                }
            )
    p = _plan_of(ws)
    return {
        "workspace": ws.name,
        "public": pub,
        "via": ws.via,
        "public_demo_enabled": public_demo(),
        "providers": providers,
        "modes": _modes_for(ws),
        "plan": p.to_dict() if p else None,
        "credits": balance(ws),
        "costs": plans.credit_costs(),
        "import_formats": sorted(FORMATS) + (sorted(WORKER_FORMATS) if client is not None else []),
        "max_import_mb": max_import_bytes() // (1024 * 1024),
        "freecad": fc,
        "usage": {
            "generate": {
                "used": st.used_today(ws, "generate"),
                "limit": public_limit("generate", "daily") if pub else daily_limit(ws, "DUOSIM_DAILY_GENERATIONS", 50),
            },
            "build": {
                "used": st.used_today(ws, "build"),
                "limit": public_limit("build", "daily") if pub else daily_limit(ws, "DUOSIM_DAILY_BUILDS", 200),
            },
        },
    }


@router.post("/generate")
async def generate(body: GenerateIn, ws: WS):
    from .saas.plans import credit_costs, generate_cost

    if body.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(MODES)}")
    if body.mode not in _modes_for(ws):
        raise HTTPException(402, f"the {body.mode} agent needs a higher plan - upgrade at Account > Billing")
    try:
        images = parse_images(body.images)
    except ImageError as e:
        raise HTTPException(422, str(e)) from None
    if images:
        require_feature(ws, "image_input", "Design from images")
    if not body.prompt.strip() and not images:
        raise HTTPException(422, "describe what to design, or attach a reference image")
    prov, byok = resolve_provider(ws, body.provider)
    if images and not getattr(prov, "vision", False):
        raise HTTPException(
            422, f"{prov.name} ({prov.model}) does not accept images - choose a vision-capable provider"
        )
    builder = _builder_fn() if body.build else None
    tokens = _take(ws, "generate", daily_limit(ws, "DUOSIM_DAILY_GENERATIONS", 50))
    cost = generate_cost(body.mode, len(images), byok)
    try:
        lid = charge(ws, cost, f"generate.{body.mode}")
    except HTTPException:
        _refund(tokens)
        raise
    build_tokens: list = []
    build_lid = 0
    if builder is not None:
        try:
            build_tokens = _take(ws, "build", daily_limit(ws, "DUOSIM_DAILY_BUILDS", 200))
            build_lid = charge(ws, credit_costs()["build"], "build")
        except HTTPException:
            _refund(tokens)
            refund(ws, lid, "build could not be paid")
            raise
    agent = DesignAgent(prov, mode=body.mode, builder=builder)
    try:
        res = await asyncio.to_thread(agent.run, body.prompt, images)
    except ValueError as e:  # bad prompt: it never reached the model
        _refund(tokens + build_tokens)
        refund(ws, lid, "rejected input")
        refund(ws, build_lid, "rejected input")
        raise HTTPException(422, str(e)) from None
    except DesignError as e:
        _refund(build_tokens)
        refund(ws, build_lid, "no design to build")
        if e.upstream:  # the model API failed, not the request: do not charge the caller
            _refund(tokens)
            refund(ws, lid, "model provider failure")
        emit(ws, "design.failed", {"reason": str(e), "mode": body.mode})
        return JSONResponse(
            {"detail": str(e), "errors": e.errors, "trace": [s.to_dict() for s in agent.trace]},
            status_code=502 if e.upstream else 422,
        )
    spec_dict = res.spec.to_dict()
    err = None
    if builder is None:
        err = "build not requested" if not body.build else NO_WORKER
    elif res.build is None:
        err = res.build_error or NO_WORKER
        _refund(build_tokens)
        refund(ws, build_lid, "FreeCAD worker failure")
    prompt_label = body.prompt or f"(from {len(images)} image{'s' if len(images) != 1 else ''})"
    did = _persist(
        ws,
        prompt_label,
        res.provider,
        res.model,
        spec_dict,
        res.build,
        {"agent": {"mode": res.mode, "critic_score": res.critique.score, "judge": res.judge}},
    )
    out = {
        "id": did,
        "spec": spec_dict,
        "warnings": res.warnings,
        "provider": res.provider,
        "model": res.model,
        "byok": byok,
        "attempts": res.llm_calls,
        "latency_s": res.latency_s,
        "usage": res.usage,
        "agent": res.summary(),
        "build": _build_json(res.build, err),
        "credits": {
            "charged": (cost if lid else 0) + (credit_costs()["build"] if build_lid and res.build else 0),
            "balance": balance(ws),
        },
    }
    emit(
        ws,
        "design.completed",
        {
            "id": did,
            "name": spec_dict["name"],
            "parts": len(spec_dict["parts"]),
            "verified": None if res.build is None else res.build.ok,
            "mode": res.mode,
        },
    )
    return out


@router.post("/build")
async def build(body: BuildIn, ws: WS):
    from .saas.plans import credit_costs

    try:
        spec, warns = parse_spec(body.spec)
    except SpecError as e:
        return JSONResponse({"detail": "invalid design", "errors": e.errors}, status_code=422)
    tokens = _take(ws, "build", daily_limit(ws, "DUOSIM_DAILY_BUILDS", 200))
    try:
        lid = charge(ws, credit_costs()["build"], "build") if get_builder() is not None else 0
    except HTTPException:
        _refund(tokens)
        raise
    res, err = await _build(spec)
    if res is None:
        _refund(tokens)
        refund(ws, lid, "FreeCAD worker failure")
    did = _persist(ws, body.prompt or "(edited spec)", None, None, spec.to_dict(), res)
    if res is not None:
        emit(ws, "design.completed", {"id": did, "name": spec.name, "parts": len(spec.parts), "verified": res.ok})
    return {
        "id": did,
        "spec": spec.to_dict(),
        "warnings": warns,
        "build": _build_json(res, err),
        "credits": {"balance": balance(ws)},
    }


@router.post("/import")
async def import_file(
    request: Request,
    ws: WS,
    filename: str,
    units: str = "auto",
    up: str = "auto",
    height: float = 3.0,
    build: bool = False,
    refine: str = "",
    mode: str = "fast",
    provider: str | None = None,
):
    """Upload the raw file as the request body (any Content-Type); `filename` gives its type."""
    from .saas.plans import credit_costs, generate_cost

    require_feature(ws, "cad_import", "CAD import")
    if len(filename) > 200 or len(refine) > 2000:
        raise HTTPException(422, "filename or refine text too long")
    if not 0.01 <= height <= 500:
        raise HTTPException(422, "height must be between 0.01 and 500 m")
    data = await request.body()
    if not data:
        raise HTTPException(422, "the request body is empty: send the file's bytes")
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    prov, byok = None, False
    if refine.strip():  # validate everything that can fail before anything is charged
        if mode not in MODES or mode not in _modes_for(ws):
            raise HTTPException(402 if mode in MODES else 422, f"the {mode} agent is not available on this plan")
        prov, byok = resolve_provider(ws, provider)
    client = get_client()
    tokens = _take(
        ws,
        "import",
        public_limit("import", "daily") if ws.via == "public" else daily_limit(ws, "DUOSIM_DAILY_IMPORTS", 100),
    )
    try:
        lid = charge(ws, credit_costs()["import"], "import")
    except HTTPException:
        _refund(tokens)
        raise
    bodies, note = None, None
    if client is not None and ext in (".step", ".stp", *WORKER_FORMATS):
        try:  # exact OpenCASCADE measurement when a worker is available
            bodies = await asyncio.to_thread(measure_with_worker, client, data, filename)
            note = "measured exactly by OpenCASCADE in the FreeCAD worker"
        except Exception as e:
            log.warning("worker import failed, falling back to the text parser: %s", e)
            note = f"FreeCAD worker unavailable ({explain(e)}); read by the built-in parser"
    try:
        imp = await asyncio.to_thread(import_cad, data, filename, units=units, up=up, height=height, bodies=bodies)
    except ImportError_ as e:
        _refund(tokens)
        refund(ws, lid, "file could not be imported")
        raise HTTPException(422, str(e)) from None
    except SpecError as e:  # pragma: no cover - the fitter only emits valid specs; keep the guard honest
        _refund(tokens)
        refund(ws, lid, "importer produced an invalid spec")
        return JSONResponse({"detail": "import produced an invalid design", "errors": e.errors}, status_code=500)
    report = {**imp.report, **({"note": note} if note else {})}
    spec_dict, warnings, agent_summary, prov_name, model = imp.spec, list(imp.warnings), None, None, None
    if prov is not None:
        g_tokens = _take(ws, "generate", daily_limit(ws, "DUOSIM_DAILY_GENERATIONS", 50))
        g_lid = charge(ws, generate_cost(mode, 0, byok), f"generate.{mode}")
        agent = DesignAgent(prov, mode=mode)
        try:
            res = await asyncio.to_thread(agent.run, refine, None, imp.spec)
            spec_dict, warnings, agent_summary = res.spec.to_dict(), warnings + res.warnings, res.summary()
            prov_name, model = res.provider, res.model
        except (DesignError, ValueError) as e:
            if getattr(e, "upstream", False) or isinstance(e, ValueError):
                _refund(g_tokens)
                refund(ws, g_lid, "model provider failure")
            warnings.append(f"refinement failed, the imported model is returned unchanged: {e}")
    built, err = None, "build not requested"
    if build:
        spec_obj, _ = parse_spec(spec_dict)
        b_tokens = _take(ws, "build", daily_limit(ws, "DUOSIM_DAILY_BUILDS", 200))
        b_lid = charge(ws, credit_costs()["build"], "build") if get_builder() is not None else 0
        built, err = await _build(spec_obj)
        if built is None:
            _refund(b_tokens)
            refund(ws, b_lid, "FreeCAD worker failure")
    did = _persist(
        ws,
        f"import {filename}" + (f": {refine}" if refine else ""),
        prov_name,
        model,
        spec_dict,
        built,
        {"import": report},
    )
    emit(ws, "import.completed", {"id": did, "filename": filename, "parts": len(spec_dict["parts"])})
    return {
        "id": did,
        "spec": spec_dict,
        "warnings": warnings,
        "import": report,
        "agent": agent_summary,
        "build": _build_json(built, err),
        "credits": {"balance": balance(ws)},
    }


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


@router.delete("/{design_id}", status_code=204)
async def delete_design(design_id: str, ws: WS):
    if ws.name == PUBLIC_NAME or not ID_RE.match(design_id) or not get_store().delete_design(ws, design_id):
        raise HTTPException(404, "no such design")


@router.get("/{design_id}/file/{kind}")
async def design_file(design_id: str, kind: str, ws: WS):
    p = get_store().artifact_path(ws, design_id, kind)
    if p is None:
        raise HTTPException(404, "no such file")
    return FileResponse(p, media_type=MEDIA.get(kind, "application/octet-stream"), filename=p.name)
