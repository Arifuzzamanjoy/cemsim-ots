"""Web pages of the product and operational endpoints.

/                 landing page (product, pricing, FAQ)
/app              the Studio (design by text / image / CAD, simulators)      (/design kept as an alias)
/login /signup /forgot /reset /verify     account pages (one page, five modes)
/account          workspace, members, billing & credits, API keys, model providers, webhooks, privacy
/admin            platform administration
/terms /privacy   legal templates (to be reviewed by counsel before going live)
/healthz /readyz  liveness and readiness probes
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

WEB = Path(__file__).resolve().parent / "web"
router = APIRouter(include_in_schema=False)


def _page(name: str) -> FileResponse:
    return FileResponse(WEB / name, headers={"Cache-Control": "no-cache"})


@router.get("/")
async def landing():
    return _page("landing.html")


@router.get("/app")
async def studio():
    return _page("design.html")


@router.get("/design")
async def design_alias():
    return RedirectResponse("/app", status_code=308)


@router.get("/pricing")
async def pricing():
    return RedirectResponse("/#pricing", status_code=308)


for _path in ("/login", "/signup", "/forgot", "/reset", "/verify"):
    router.add_api_route(_path, lambda: _page("auth.html"), methods=["GET"])


@router.get("/account")
async def account():
    return _page("account.html")


@router.get("/admin")
async def admin():
    return _page("admin.html")


@router.get("/terms")
async def terms():
    return _page("terms.html")


@router.get("/privacy")
async def privacy():
    return _page("privacy.html")


@router.get("/favicon.ico")
@router.get("/favicon.svg")
async def favicon():
    return FileResponse(
        WEB / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /api/\nDisallow: /account\nDisallow: /admin\nDisallow: /app\n")


@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.get("/readyz")
async def readyz():
    from . import api_design

    try:
        with api_design.get_store()._conn() as c:
            c.execute("SELECT 1").fetchone()
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "database": str(e)}, status_code=503)
    return {"ok": True, "database": "ok", "freecad": api_design.get_client() is not None}


_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; font-src 'self' data:; "
    "frame-ancestors 'self'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


class SecurityHeadersMiddleware:
    """Baseline browser hardening on every response (CSP keeps scripts first-party; inline is needed for the
    three.js import map)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        https = scope.get("scheme") == "https" or os.environ.get("DUOSIM_COOKIE_SECURE") == "1"

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers") or [])
                names = {k.lower() for k, _ in h}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", _CSP.encode()),
                ]
                if https:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                h += [(k, v) for k, v in extra if k not in names]
                msg = {**msg, "headers": h}
            await send(msg)

        await self.app(scope, receive, send_with_headers)
