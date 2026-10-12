"""Browser end-to-end of the Studio (main page `/`) against a real FreeCAD worker.

Self-contained: starts a FreeCAD worker and two CemSim servers (API-key mode and dev-open mode),
drives the page in headless Chromium and checks what a user sees.  No LLM credits are spent: the
spec is pasted in and built through "Build edited spec" (the model path is covered by
tests/integration/test_06_design_build.py).

Usage: CEMSIM_FREECAD=/opt/freecad-1.1.4 python tests/validation/e2e_design.py [screenshot_dir]
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cemsim.freecad_worker import FreeCADWorker, free_port
from cemsim.platform.store import Store
from tests.integration.pngstat import decode

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
RESULTS: list[bool] = []

SPEC = {
    "name": "<script>window.__xss = 1</script>",  # must stay inert text
    "parts": [
        {
            "id": "slab",
            "kind": "box",
            "size": [8, 5, 0.4],
            "at": [-4, -2.5, -0.4],
            "color": "#8d9399",
            "group": "civil",
        },
        {"id": "tank", "kind": "cylinder", "radius": 1.5, "height": 4, "at": [-1, 0, 0], "color": "#6f8fb5"},
        {
            "id": "roof",
            "kind": "cone",
            "radius": 1.5,
            "radius2": 0.3,
            "height": 0.9,
            "at": [-1, 0, 4],
            "color": "#6f8fb5",
        },
        {
            "id": "pump",
            "kind": "cylinder",
            "radius": 0.4,
            "height": 1.2,
            "axis": "x",
            "at": [1.5, 0, 0.6],
            "color": "#2f9e44",
        },
        {"id": "line", "kind": "pipe", "radius": 0.12, "path": [[0.5, 0, 0.8], [1.5, 0, 0.8]], "color": "#d4ac0d"},
    ],
}


def changed_fraction(a: bytes, b: bytes, tol: int = 14) -> float:
    """Share of pixels that differ between two same-size PNGs (grid/background cancel out)."""
    wa, ha, ch, ra = decode(a)
    wb, hb, _, rb = decode(b)
    assert (wa, ha) == (wb, hb), "pane size changed between captures"
    n = 0
    for x, y in zip(ra, rb, strict=True):
        for i in range(0, wa * ch, ch):
            if abs(x[i] - y[i]) + abs(x[i + 1] - y[i + 1]) + abs(x[i + 2] - y[i + 2]) > tol:
                n += 1
    return n / (wa * ha)


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for {url}")


def start_server(port, fc_port, token, data_dir, dev_open, public=False):
    env = {
        **os.environ,
        "FREECAD_MCP_TOKEN": token,
        "CEMSIM_DATA_DIR": str(data_dir),
        "CEMSIM_DEV_OPEN": "1" if dev_open else "",
        "CEMSIM_PUBLIC_DEMO": "1" if public else "",
        "GEMINI_API_KEY": "",  # no provider: the page must say so, and building must still work
        "ANTHROPIC_API_KEY": "",
        "CEMSIM_ENV_FILE": os.devnull,
    }
    p = subprocess.Popen(
        [sys.executable, "-m", "cemsim.server", "--port", str(port), "--freecad", f"127.0.0.1:{fc_port}"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    wait_http(f"http://127.0.0.1:{port}/api/snapshots")
    return p


async def drive(base, key=None, expect_key_prompt=False):
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = await b.new_context(viewport={"width": 1500, "height": 850}, accept_downloads=True)
        pg = await ctx.new_page()
        errors, dialogs = [], []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on(
            "console",
            # the 401 (before a key is entered) and the 422 (the invalid edit below) are expected
            lambda m: (
                errors.append(m.text) if m.type == "error" and "401" not in m.text and "422" not in m.text else None
            ),
        )
        pg.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
        await pg.goto(f"{base}/")

        if expect_key_prompt:
            await pg.wait_for_function(
                "document.querySelector('#chip-freecad').textContent.includes('your key')", timeout=15000
            )
            check("without a key the page asks for one", True)
            await pg.fill("#key", key)
            await pg.dispatch_event("#key", "change")
        await pg.wait_for_function(
            "document.querySelector('#chip-freecad').textContent.startsWith('FreeCAD: ok')", timeout=30000
        )
        chip = await pg.text_content("#chip-freecad")
        check("FreeCAD chip reports a healthy worker", "ok" in chip and "0.1.26" in chip, chip)
        st = await pg.text_content("#status")
        check("no LLM key configured -> the page says so", "No LLM provider" in st, st)

        empty_pane = await pg.locator("#view").screenshot()  # grid only: the negative control
        await pg.fill("#spec", json.dumps(SPEC))
        check("rebuild enabled once a spec is present", not await pg.is_disabled("#rebuild"))
        await pg.click("#rebuild")
        await pg.wait_for_function("document.querySelector('#verdict').textContent.includes('verified')", timeout=90000)
        verdict = await pg.text_content("#verdict")
        check("verdict: all parts verified against the spec", "5 parts match" in verdict, verdict)
        rows = await pg.locator("#checks tbody tr").count()
        check("one verification row per part", rows == 5, str(rows))
        fails = await pg.locator("#checks td.fail").count()
        check("no failed checks", fails == 0)

        await pg.wait_for_function("window.DESIGN.meshObjects > 0", timeout=15000)
        n = await pg.evaluate("window.DESIGN.meshObjects")
        check("3-D view holds the FreeCAD-built meshes", n == 5, str(n))
        await pg.wait_for_function("document.querySelector('#shot').naturalWidth > 0", timeout=15000)
        check("FreeCAD render is shown", await pg.is_visible("#shot"))
        # what the user sees: the pane must differ from the empty pane (the grid and background cancel out)
        await pg.wait_for_timeout(300)
        diff = changed_fraction(empty_pane, await pg.locator("#view").screenshot())
        check("3-D pane shows the model (differs from the empty pane)", diff > 0.05, f"{diff:.1%} of pixels changed")
        check("generate button is disabled without a provider", await pg.is_disabled("#go"))
        if OUT:
            OUT.mkdir(parents=True, exist_ok=True)
            await pg.wait_for_timeout(800)
            await pg.screenshot(path=str(OUT / ("design_studio.png")))

        xss = await pg.evaluate("window.__xss")
        txt = await pg.input_value("#spec")
        check("hostile text in the spec stays inert", xss is None and "<script>" in txt and not dialogs)

        # downloads carry real CAD data (fetched with the API key, saved by the page)
        async with pg.expect_download() as dl:
            await pg.get_by_role("button", name="STEP", exact=True).click()
        path = await (await dl.value).path()
        head = Path(path).read_bytes()[:13]
        check("STEP download is a STEP file", head == b"ISO-10303-21;", repr(head))

        # an invalid edit is explained, not swallowed
        bad = json.loads(json.dumps(SPEC))
        bad["parts"][1]["radius"] = -2
        await pg.fill("#spec", json.dumps(bad))
        await pg.click("#rebuild")
        await pg.wait_for_function("document.querySelector('#status').className.includes('err')", timeout=15000)
        msgs = await pg.locator("#warns li").all_text_contents()
        check("validation errors are listed", any("tank.radius" in m for m in msgs), "; ".join(msgs)[:120])
        check("no page errors", not errors, "; ".join(errors[:3]))
        await b.close()


async def drive_studio(base, shot_dir=None):
    """Public-demo instance: no key needed; key hygiene; simulator gallery + live console; HMI round trip."""
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        pg = await (await b.new_context(viewport={"width": 1500, "height": 850})).new_page()
        sent_keys, errors = [], []
        pg.on("request", lambda r: sent_keys.append(r.headers.get("x-api-key")) if r.headers.get("x-api-key") else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(f"{base}/")
        await pg.wait_for_function(
            "document.querySelector('#chip-usage').textContent.includes('public demo')", timeout=30000
        )
        use = await pg.text_content("#chip-usage")
        check("no key needed: the page runs as the public demo", "public demo" in use, use)
        check("main page is the Studio", "Studio" in await pg.title())

        # a model key typed by mistake is refused locally: never stored, never sent
        gem = "AQ.Ab8NotARealKeyJustTheShapeOfOne_0123456789abcdefghij"
        await pg.fill("#key", gem)
        await pg.dispatch_event("#key", "change")
        await pg.wait_for_function(
            "document.querySelector('#status').textContent.includes('not a platform key')", timeout=5000
        )
        stored = await pg.evaluate("sessionStorage.getItem('cemsim_key')")
        check("model key refused with an explanation", stored is None and await pg.input_value("#key") == "")
        await pg.reload()
        await pg.wait_for_function(
            "document.querySelector('#chip-usage').textContent.includes('public demo')", timeout=30000
        )
        check(
            "a model key is never sent to the server",
            not any(k.startswith("AQ.") for k in sent_keys),
            str(sent_keys[:2]),
        )

        # simulators tab: the cement kiln is the flagship demo, any plug-in gets a live console
        await pg.click('.tab[data-view="sims"]')
        await pg.wait_for_selector("#cards .card", timeout=15000)
        names = await pg.locator("#cards .card h4").all_text_contents()
        check(
            "gallery lists the plug-ins, cement first",
            names and "Cement kiln" in names[0] and any("Water tank" in n for n in names),
            "; ".join(names),
        )
        check("flagship card links to the operator HMI", await pg.locator('#cards a[href="/hmi"]').count() == 1)
        tank = pg.locator("#cards .card", has_text="Water tank")
        await tank.get_by_role("button", name="Live console").click()
        await pg.wait_for_selector('#con-tags tr[data-tag="level_m"]', timeout=20000)
        t0 = float((await pg.text_content("#con-t")).split("=")[1].split()[0])
        await pg.wait_for_function(
            f"parseFloat(document.querySelector('#con-t').textContent.split('=')[1]) > {t0 + 8}", timeout=20000
        )
        lvl = float(await pg.locator('#con-tags tr[data-tag="level_m"] td').nth(1).text_content())
        check("live console steps the simulator", 2.5 < lvl < 3.6, f"level {lvl} m")
        form = pg.locator("#con-cmds form", has_text="set_level_sp")
        await form.locator("input").fill("4.5")
        await form.get_by_role("button", name="send").click()
        await pg.wait_for_function(
            "document.querySelector('#con-msg').textContent.includes('set_level_sp: ok')", timeout=10000
        )
        sp = await pg.wait_for_function(
            "parseFloat(document.querySelector('#con-tags tr[data-tag=\"level_sp_m\"] td:nth-child(2)').textContent) === 4.5",
            timeout=10000,
        )
        check("commands reach the simulator", bool(sp))
        await form.locator("input").fill("99")
        await form.get_by_role("button", name="send").click()
        await pg.wait_for_function(
            "document.querySelector('#con-msg').textContent.includes('outside') || document.querySelector('#con-msg').textContent.includes('within')",
            timeout=10000,
        )
        check("a rejected command is explained", True)
        if shot_dir:
            await pg.screenshot(path=str(Path(shot_dir) / "studio_simulators.png"))
        await pg.click("#con-stop")
        check("console can be stopped", await pg.is_hidden("#console"))

        # the operator demo is one click away and leads back to the Studio
        await pg.goto(f"{base}/hmi")
        await pg.wait_for_selector("#navStudio", timeout=15000)
        await pg.click("#navStudio")
        await pg.wait_for_selector("#chip-freecad", timeout=15000)
        check("HMI -> Studio navigation", pg.url.rstrip("/") == base.rstrip("/"), pg.url)
        check("no page errors", not errors, "; ".join(errors[:3]))
        await b.close()


async def main():
    home = Path(tempfile.mkdtemp(prefix="cemsim-e2e-"))
    data = home / "data"
    fc_port, srv_key, srv_open = free_port(), free_port(), free_port()
    procs = []
    with FreeCADWorker(port=fc_port, home=home / "fc") as w:
        try:
            store = Store(data / "platform.sqlite3")
            key = store.create_api_key(store.workspace("e2e"), "browser")[1]
            procs.append(start_server(srv_key, fc_port, w.token, data, dev_open=False))
            print(f"== API-key mode (http://127.0.0.1:{srv_key})")
            await drive(f"http://127.0.0.1:{srv_key}", key=key, expect_key_prompt=True)

            procs.append(start_server(srv_open, fc_port, w.token, home / "data2", dev_open=True))
            print(f"== dev-open mode (http://127.0.0.1:{srv_open})")
            await drive(f"http://127.0.0.1:{srv_open}")

            srv_pub = free_port()
            procs.append(start_server(srv_pub, fc_port, w.token, home / "data3", dev_open=False, public=True))
            print(f"== public-demo mode (http://127.0.0.1:{srv_pub})")
            await drive_studio(f"http://127.0.0.1:{srv_pub}", OUT)
        finally:
            for p in procs:
                p.terminate()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


sys.exit(asyncio.run(main()))
