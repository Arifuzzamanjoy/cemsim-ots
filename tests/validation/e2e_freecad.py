"""E2E test of the FreeCAD 3D buttons (build / live on / off) against the mock RPC server.

Usage: python tests/validation/e2e_freecad.py <cemsim host:port> <mock port> [<cemsim with dead freecad host:port>]
"""

import asyncio
import sys
import time
import xmlrpc.client

from playwright.async_api import async_playwright

BASE = sys.argv[1]
MOCK = xmlrpc.client.ServerProxy(f"http://127.0.0.1:{sys.argv[2]}")
DEAD = sys.argv[3] if len(sys.argv) > 3 else None
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


async def fc_state(pg):
    cls = await pg.get_attribute("#fc-dot", "class")
    msg = await pg.text_content("#fc-msg")
    return (cls or "").replace("fc-dot", "").strip(), msg or ""


async def wait_state(pg, want, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, msg = await fc_state(pg)
        if st == f"fc-{want}":
            return True, msg, time.time() - t0
        await pg.wait_for_timeout(250)
    st, msg = await fc_state(pg)
    return False, f"{st}: {msg}", time.time() - t0


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1500, "height": 850})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(f"http://{BASE}/")
        await pg.wait_for_timeout(2000)
        check("FreeCAD controls rendered", await pg.is_visible("#fc-build") and await pg.is_visible("#fc-dot"))

        # 1. build -> checking/building -> live, button disabled while building, pushes arrive
        MOCK.mock_mode("ok")
        await pg.click("#fc-build")
        await pg.wait_for_timeout(150)
        st, msg = await fc_state(pg)
        dis = await pg.is_disabled("#fc-build")
        check(
            "build gives immediate feedback", st in ("fc-checking", "fc-building") and dis, f"{st} {msg} disabled={dis}"
        )
        ok, msg, dt = await wait_state(pg, "live", 20)
        check("build completes -> live", ok, f"{msg} after {dt:.1f}s")
        u0 = MOCK.mock_stats()["updates"]
        await pg.wait_for_timeout(4000)
        u1 = MOCK.mock_stats()["updates"]
        check("live updates stream (~1 Hz)", u1 - u0 >= 3, f"{u1 - u0} updates in 4 s")

        # 2. stalled FreeCAD GUI -> error with actionable hint; HMI stays responsive
        MOCK.mock_mode("stalled")
        ok, msg, dt = await wait_state(pg, "error", 20)
        check("stalled GUI reported as error", ok, f"after {dt:.1f}s")
        check("error message tells the operator what to do", "click once inside the FreeCAD window" in msg, msg[:110])
        toast = await pg.locator(".toast").last.text_content()
        check("error toast shown", toast and "FreeCAD" in toast, toast[:80] if toast else "")
        await pg.select_option("#speed", "2")
        await pg.wait_for_timeout(700)
        spd = await pg.eval_on_selector("#speed", "e => e.value")
        check("other controls still respond during FreeCAD stall", spd == "2", f"speed={spd}")
        await pg.click("#fc-build")  # build while stalled: must not freeze, must report
        started = False  # busy = checking or building
        for _ in range(12):
            st, _ = await fc_state(pg)
            if st in ("fc-checking", "fc-building"):
                started = True
                break
            await pg.wait_for_timeout(100)
        ok, msg, dt = await wait_state(pg, "error", 20)
        check(
            "build during stall: starts, then ends in clear error (no hang)",
            started and ok and "GUI thread" in msg and dt > 1.0,
            f"started={started} {dt:.1f}s {msg[:50]}",
        )

        # 3. recovery: FreeCAD comes back -> live without user action (15 s back-off)
        MOCK.mock_mode("ok")
        await pg.click("#fc-on")
        ok, msg, dt = await wait_state(pg, "live", 25)
        check("live on recovers after FreeCAD is fixed", ok, f"after {dt:.1f}s")

        # 4. FreeCAD restarted (model lost) -> automatic rebuild, stays live
        builds0 = MOCK.mock_stats()["builds"]
        MOCK.mock_mode("restarted")
        await pg.wait_for_timeout(4000)
        s = MOCK.mock_stats()
        st, msg = await fc_state(pg)
        check(
            "model lost in FreeCAD -> rebuilt automatically",
            s["builds"] == builds0 + 1 and st == "fc-live",
            f"builds {builds0}->{s['builds']} state {st}",
        )

        # 5. live on without prior build (fresh FreeCAD) -> builds automatically
        await pg.click("#fc-off")
        ok, msg, dt = await wait_state(pg, "off", 5)
        check("off stops live mode", ok)
        u0 = MOCK.mock_stats()["updates"]
        await pg.wait_for_timeout(2500)
        check("no pushes while off", MOCK.mock_stats()["updates"] == u0)
        MOCK.mock_mode("restarted")
        builds0 = MOCK.mock_stats()["builds"]
        await pg.click("#fc-on")
        ok, msg, dt = await wait_state(pg, "live", 15)
        check(
            "live on alone builds the model when missing",
            ok and MOCK.mock_stats()["builds"] == builds0 + 1,
            f"{dt:.1f}s builds +{MOCK.mock_stats()['builds'] - builds0}",
        )

        check("no page errors", not errors, "; ".join(errors[:3]))
        await b.close()

        # 6. FreeCAD not running at all (second CemSim pointed at a dead port)
        if DEAD:
            b = await p.chromium.launch()
            pg = await b.new_page()
            await pg.goto(f"http://{DEAD}/")
            await pg.wait_for_timeout(1500)
            await pg.click("#fc-build")
            ok, msg, dt = await wait_state(pg, "error", 15)
            check(
                "FreeCAD not running -> fast, clear error",
                ok and dt < 8 and "not reachable" in msg,
                f"{dt:.1f}s {msg[:90]}",
            )
            await b.close()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


sys.exit(asyncio.run(main()))
