"""Browser end-to-end of the commercial product: landing -> sign-up -> Studio (CAD import) -> history ->
account (mock billing upgrade, API key, model provider, team) -> sign out / in -> admin console.

Self-contained: starts its own server (mock billing, temporary data directory, no model keys, no FreeCAD)
and drives headless Chromium.  Usage: python tests/validation/e2e_saas.py [port] [screenshot_dir]
"""

import asyncio
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else None
BASE = f"http://127.0.0.1:{PORT}"
RESULTS: list[bool] = []
PW = "e2e password 123"


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def stl() -> bytes:
    """A box and a cylinder (mm), binary STL."""
    tris = []
    x0, y0, dx, dy, dz = 0, 0, 4000, 2000, 1000
    v = [(x0 + i * dx, y0 + j * dy, k * dz) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    for a, b, c in [
        (0, 2, 3),
        (0, 3, 1),
        (4, 5, 7),
        (4, 7, 6),
        (0, 1, 5),
        (0, 5, 4),
        (2, 6, 7),
        (2, 7, 3),
        (0, 4, 6),
        (0, 6, 2),
        (1, 3, 7),
        (1, 7, 5),
    ]:
        tris.append((v[a], v[b], v[c]))
    n, r, h, cx = 40, 800, 3000, 7000
    ring = [(cx + r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)) for k in range(n)]
    for k in range(n):
        (ax, ay), (bx, by) = ring[k], ring[(k + 1) % n]
        tris += [((ax, ay, 0), (bx, by, 0), (bx, by, h)), ((ax, ay, 0), (bx, by, h), (ax, ay, h)),
                 ((cx, 0, 0), (bx, by, 0), (ax, ay, 0)), ((cx, 0, h), (ax, ay, h), (bx, by, h))]  # fmt: skip
    out = bytearray(b"\0" * 80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<12fH", 0, 0, 0, *[c for p in t for c in p], 0)
    return bytes(out)


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"timeout waiting for {url}")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="duosim-saas-e2e-"))
    env = {**os.environ, "DUOSIM_DATA_DIR": str(tmp / "data"), "DUOSIM_BILLING": "mock", "DUOSIM_ADMIN_EMAILS": "admin@e2e.test",
           "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "DUOSIM_ENV_FILE": os.devnull, "DUOSIM_DEV_OPEN": "",
           "DUOSIM_PUBLIC_DEMO": "", "DUOSIM_SECRET_KEY": "e2e-" + "s" * 40}  # fmt: skip
    srv = subprocess.Popen(
        [sys.executable, "-m", "duosim.server", "--port", str(PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    model = tmp / "plant.stl"
    model.write_bytes(stl())
    try:
        wait_http(f"{BASE}/healthz")
        async with async_playwright() as p:
            exe = os.environ.get("E2E_CHROMIUM") or None  # a preinstalled browser when Playwright's own is absent
            b = await p.chromium.launch(
                executable_path=exe, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            )
            ctx = await b.new_context(bypass_csp=True, viewport={"width": 1440, "height": 900})
            pg = await ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status in (404, 500) else None)
            pg.on(
                "console",
                lambda m: (
                    errors.append(m.text) if m.type == "error" and "401" not in m.text and "402" not in m.text else None
                ),
            )

            # ---------------------------------------------------------------- landing
            await pg.goto(BASE + "/")
            await pg.wait_for_selector("#plans .price", timeout=15000)
            n = await pg.locator("#plans .price").count()
            check("landing: pricing cards come from the billing catalogue", n == 4, str(n))
            costs = await pg.locator("#costs tr").count()
            check("landing: credit price list rendered", costs >= 6, str(costs))
            check("landing: title and hero", "DuoSim" in await pg.title() and await pg.is_visible("h1"))
            if OUT:
                OUT.mkdir(parents=True, exist_ok=True)
                await pg.screenshot(path=str(OUT / "landing.png"))
                await pg.screenshot(path=str(OUT / "landing_full.png"), full_page=True)
            mob = await (
                await b.new_context(bypass_csp=True, viewport={"width": 390, "height": 844}, is_mobile=True)
            ).new_page()
            await mob.goto(BASE + "/")
            await mob.wait_for_selector("#plans .price", timeout=15000)
            sw = await mob.evaluate("document.documentElement.scrollWidth")
            check("landing: no horizontal scroll at phone width", sw <= 391, f"scrollWidth {sw}")
            if OUT:
                await mob.screenshot(path=str(OUT / "landing_mobile.png"))
            await mob.context.close()

            # ---------------------------------------------------------------- sign-up
            await pg.click("text=Start free, 50 credits")
            await pg.wait_for_url("**/signup*")
            await pg.fill("#name", "Erin Engineer")
            await pg.fill("#email", "erin@e2e.test")
            await pg.fill("#password", PW)
            await pg.check("#terms")
            await pg.click("#submit")
            await pg.wait_for_url("**/app?welcome=1", timeout=15000)
            await pg.wait_for_function("!document.querySelector('#chip-credits').hidden", timeout=15000)
            chip = await pg.text_content("#chip-credits")
            check("sign-up lands in the Studio with the free credits", "50 credits" in chip, chip)
            check("signed-in Studio hides the API-key box", await pg.is_hidden("#keybox"))
            st = await pg.text_content("#status")
            check(
                "no model provider: the Studio says so and CAD import still works", "CAD import still works" in st, st
            )

            # ---------------------------------------------------------------- CAD import
            await pg.click('.itab[data-in="cad"]')
            await pg.set_input_files("#cad-file", str(model))
            await pg.uncheck("#build")
            await pg.wait_for_function("!document.querySelector('#go').disabled", timeout=5000)
            cost = await pg.text_content("#cost")
            check("the button shows the price before running", "2 credits" in cost, cost)
            await pg.click("#go")
            await pg.wait_for_function("document.querySelector('#spec').value.includes('cylinder')", timeout=20000)
            rep = await pg.text_content("#import-report")
            check("CAD import: report shows the fitted primitives", "1 box" in rep and "1 cylinder" in rep, rep)
            await pg.wait_for_function(
                "document.querySelector('#chip-credits').textContent.includes('48')", timeout=10000
            )
            check("CAD import: 2 credits charged", True)
            n3d = await pg.evaluate("window.DESIGN.meshObjects")
            check("without FreeCAD the spec is previewed in 3-D", n3d == 2, str(n3d))
            if OUT:
                await pg.screenshot(path=str(OUT / "studio_import.png"))
            await pg.click('.tab[data-view="history"]')
            await pg.wait_for_selector("#hist tbody tr", timeout=10000)
            rows = await pg.locator("#hist tbody tr").count()
            check("history lists the imported design", rows == 1, str(rows))

            # ---------------------------------------------------------------- account
            await pg.goto(BASE + "/account#billing")
            await pg.wait_for_selector("#bill-plans .price", timeout=15000)
            await pg.click("text=Upgrade to Pro")
            await pg.wait_for_url("**/account?billing=success*", timeout=15000)
            await pg.wait_for_selector("text=Thank you!", timeout=10000)
            await pg.goto(BASE + "/account#overview")
            await pg.wait_for_function("document.querySelector('#ov-plan').textContent === 'Pro'", timeout=10000)
            cr = await pg.text_content("#ov-credits")
            check("mock checkout upgrades to Pro and credits the allowance", cr.replace(",", "") == "1048", cr)
            await pg.goto(BASE + "/account#billing")
            await pg.wait_for_selector("#ledger tr", timeout=10000)
            n = await pg.locator("#ledger tr").count()
            check("ledger lists grant, charge and purchase", n >= 3, str(n))
            if OUT:
                await pg.screenshot(path=str(OUT / "account_billing.png"))

            await pg.goto(BASE + "/account#keys")
            await pg.fill("#key-label", "ci")
            await pg.click("#new-key button")
            await pg.wait_for_selector("#key-new .secret", timeout=10000)
            key = (await pg.text_content("#key-new .secret")).strip()
            check("API key is shown once", key.startswith("ds_"), key[:6])
            req = urllib.request.Request(f"{BASE}/api/design/status", headers={"X-API-Key": key})
            with urllib.request.urlopen(req) as r:
                check("the new key authenticates against the API", r.status == 200)

            await pg.goto(BASE + "/account#providers")
            await pg.fill("#pv-name", "my-openai")
            await pg.select_option("#pv-type", "openai")
            await pg.fill("#pv-key", "sk-e2e-0123456789")
            await pg.click("#new-prov button")
            await pg.wait_for_selector("#providers td >> text=my-openai", timeout=10000)
            hint = await pg.text_content("#providers tr td:nth-child(4)")
            check("BYOK provider stored and masked", hint.strip() == "…6789", hint)

            await pg.goto(BASE + "/account#team")
            await pg.fill("#inv-email", "colleague@e2e.test")
            await pg.click("#invite button")
            await pg.wait_for_selector("#invites >> text=colleague@e2e.test", timeout=10000)
            check("team invitation is pending", True)

            await pg.goto(BASE + "/app")
            await pg.wait_for_function(
                "[...document.querySelectorAll('#provider option')].some(o => o.textContent.includes('my-openai'))",
                timeout=10000,
            )
            dis = await pg.evaluate("document.querySelector('#mode option[value=deep]').disabled")
            check("Pro unlocks the deep agent and BYOK models in the Studio", dis is False)

            # ---------------------------------------------------------------- sign out / in
            await pg.goto(BASE + "/account")
            await pg.click("text=Sign out")
            await pg.wait_for_url(BASE + "/", timeout=10000)
            await pg.goto(BASE + "/login")
            await pg.fill("#email", "erin@e2e.test")
            await pg.fill("#password", "wrong password 1")
            await pg.click("#submit")
            await pg.wait_for_selector(".notice.bad", timeout=10000)
            check("wrong password is refused", "wrong email or password" in await pg.text_content(".notice.bad"))
            await pg.fill("#password", PW)
            await pg.click("#submit")
            await pg.wait_for_url("**/app", timeout=10000)
            check("sign-in returns to the Studio", True)

            # ---------------------------------------------------------------- admin
            adm = await (await b.new_context(bypass_csp=True, viewport={"width": 1440, "height": 900})).new_page()
            await adm.goto(BASE + "/signup")
            await adm.fill("#email", "admin@e2e.test")
            await adm.fill("#password", PW)
            await adm.check("#terms")
            await adm.click("#submit")
            await adm.wait_for_url("**/app?welcome=1", timeout=15000)
            await adm.goto(BASE + "/admin")
            await adm.wait_for_selector("#stats .stat", timeout=10000)
            users = await adm.text_content("#stats .stat .v")
            check("admin console shows platform KPIs", users.strip() == "2", users)
            await adm.goto(BASE + "/admin#workspaces")
            await adm.wait_for_selector("#workspaces tr", timeout=10000)
            check("admin console lists workspaces", await adm.locator("#workspaces tr").count() >= 2)
            if OUT:
                await adm.goto(BASE + "/admin#stats")
                await adm.wait_for_selector("#stats .stat", timeout=10000)
                await adm.screenshot(path=str(OUT / "admin.png"))
            await pg.goto(BASE + "/admin")
            await pg.wait_for_selector("#denied .notice", timeout=10000)
            check("non-admins are refused by the admin console", True)

            check("no page errors", not errors, "; ".join(errors[:3]))
            await b.close()
    finally:
        srv.terminate()
        try:
            _, err = srv.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
            _, err = srv.communicate()
        if b"Traceback" in (err or b""):
            check("server log has no tracebacks", False, err.decode(errors="replace")[-400:])
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
