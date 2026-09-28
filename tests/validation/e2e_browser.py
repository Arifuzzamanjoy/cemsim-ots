"""Browser end-to-end test of the HMI (Playwright/Chromium).

Drives the UI exactly like an operator/trainer and checks the server-side
effect of every action through the REST API.  Usage:
    python tests/validation/e2e_browser.py [host:port]
"""

import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "localhost:8000"
RESULTS = []


def api(path):
    with urllib.request.urlopen(f"http://{BASE}{path}") as r:
        return json.loads(r.read())


def post(c):
    req = urllib.request.Request(f"http://{BASE}/api/cmd", json.dumps(c).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def last_event(tag_prefix):
    ev = [e for e in api("/api/events?n=400") if str(e["tag"]).startswith(tag_prefix)]
    return ev[-1] if ev else None


async def main():
    post({"type": "snapshot_load", "name": "nominal"})
    post({"type": "run", "on": True})
    post({"type": "speed", "value": 5})
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(f"http://{BASE}/")
        await pg.wait_for_timeout(2500)

        # --- live values rendered --------------------------------------------
        txt = await pg.text_content("#footer-kpis")
        check("footer KPIs populated", txt and "--" not in txt, txt)
        n_vals = await pg.eval_on_selector_all(
            ".val text", "els => els.filter(e => !e.textContent.startsWith('--')).length"
        )
        n_all = await pg.eval_on_selector_all(".val text", "els => els.length")
        check("mimic value boxes populated", n_vals >= n_all - 3, f"{n_vals}/{n_all}")

        # --- every screen opens ----------------------------------------------
        for scr in (
            "burner",
            "cooler",
            "profiles",
            "trends",
            "alarms",
            "events",
            "groups",
            "trainer",
            "scoring",
            "balance",
            "plant",
        ):
            await pg.click(f'#nav button[data-screen="{scr}"]')
            await pg.wait_for_timeout(700)
            vis = await pg.is_visible(f"#scr-{scr}")
            check(f"screen '{scr}' visible", vis)

        # --- drive faceplate: ID fan setpoint via mimic click -----------------
        await pg.click('#nav button[data-screen="plant"]')
        await pg.locator('#mimic g[data-drive="ID_FAN"]').click()
        await pg.wait_for_timeout(300)
        head = await pg.text_content("#faceplate .fp-head")
        check("fan click opens drive faceplate", head and " - " in head, head)
        tag = head.split(" - ")[0].strip()
        check("ID fan symbol opens ID fan faceplate", tag == "ID_FAN", tag)
        await pg.fill("#fp-in", "150")
        await pg.click("#fp-set")
        await pg.wait_for_timeout(800)
        msg = await pg.text_content("#fp-msg")
        check("out-of-range drive SP rejected", msg and "outside" in msg, msg)
        await pg.fill("#fp-in", "72.5")
        await pg.click("#fp-set")
        await pg.wait_for_timeout(1200)
        ev = last_event(tag + ".SP")
        check("drive SP entry reaches simulator & event log", ev and abs(float(ev["value"]) - 72.5) < 1e-6, str(ev))
        await pg.fill("#fp-in", "")
        await pg.click("#fp-set")
        await pg.wait_for_timeout(800)
        msg = await pg.text_content("#fp-msg")
        check("empty SP rejected with message", msg and "number" in msg, msg)
        toasts = await pg.locator(".toast").count()
        check("error toast shown", toasts >= 1)
        await pg.click("#fp-x")

        # --- controller faceplate ---------------------------------------------
        auto = await pg.locator('#mimic .sbtn[data-pid="TIC_CAL"]').first.get_attribute("class")
        check("C button shows AUTO state", "auto" in (auto or ""), auto)
        await pg.locator('#mimic .sbtn[data-pid="TIC_CAL"]').first.click()
        await pg.wait_for_timeout(300)
        await pg.fill("#p-sp", "880")
        await pg.click("#p-setsp")
        await pg.wait_for_timeout(1000)
        ev = last_event("TIC_CAL.SP")
        check("PID setpoint change applied", ev and float(ev["value"]) == 880.0, str(ev))
        await pg.fill("#p-sp", "5000")
        await pg.click("#p-setsp")
        await pg.wait_for_timeout(800)
        msg = await pg.text_content("#fp-msg")
        check("out-of-range PID SP rejected", msg and "outside" in msg, msg)
        await pg.click("#p-man")
        await pg.wait_for_timeout(800)
        ev = last_event("TIC_CAL.MODE")
        check("PID MAN switch", ev and ev["value"] == "MAN", str(ev))
        await pg.click("#p-auto")
        await pg.wait_for_timeout(600)
        await pg.click("#fp-x")

        # --- group faceplate: feed stop / start --------------------------------
        await pg.locator("#mimic g.btnbox").filter(has_text="Kiln feed").click()
        await pg.wait_for_timeout(300)
        await pg.click("#g-stop")
        await pg.wait_for_timeout(2500)
        state = await pg.text_content("#fp-state")
        check("group STOP stops kiln feed", "STOPPED" in state or "STOPPING" in state, state)
        await pg.click("#g-start")
        await pg.wait_for_timeout(4000)
        ev = last_event("G_FEED")
        check("group START logged", ev and ev["value"] == "START", str(ev))
        await pg.click("#g-close")

        # --- alarms ack -----------------------------------------------------------
        await pg.click('#nav button[data-screen="alarms"]')
        await pg.wait_for_timeout(800)
        await pg.click("#ack-all")
        await pg.wait_for_timeout(1200)
        rows = await pg.locator("#alarm-table tbody tr.unack").count()
        check("acknowledge all clears unacknowledged", rows == 0, f"{rows} unack rows")

        # --- trends preset ----------------------------------------------------------
        await pg.click('#nav button[data-screen="trends"]')
        await pg.select_option("#trend-preset", "Draught")
        await pg.wait_for_timeout(1500)
        leg = await pg.text_content("#trend-legend")
        check("trend preset draws 4 pens", leg and leg.count("[") == 4, leg[:120] if leg else "")

        # --- trainer: disturbance, bad value, fileset save/load -------------------
        await pg.click('#nav button[data-screen="trainer"]')
        await pg.fill("#dv-coal_lhv_kiln", "24000")
        await pg.click('#dist-box button[data-d="coal_lhv_kiln"]')
        await pg.wait_for_timeout(1000)
        ev = last_event("DIST.coal_lhv_kiln")
        check("disturbance applied", ev and float(ev["value"]) == 24000.0, str(ev))
        await pg.fill("#dv-coal_lhv_kiln", "999")
        await pg.click('#dist-box button[data-d="coal_lhv_kiln"]')
        await pg.wait_for_timeout(1000)
        last_toast = await pg.locator(".toast").last.text_content()
        check("out-of-range disturbance rejected (toast)", last_toast and "outside" in last_toast, last_toast)
        await pg.fill("#snap-name", "e2e_test")
        await pg.fill("#snap-comment", "created by e2e test")
        await pg.click("#snap-save")
        await pg.wait_for_timeout(1500)
        names = [s["name"] for s in api("/api/snapshots")]
        check("fileset saved", "e2e_test" in names, str(names))
        rows = await pg.locator("#snap-table tbody tr").count()
        check("fileset list refreshed in UI", rows == len(names), f"{rows} rows")
        await pg.locator('#snap-table button[data-s="nominal"]').click()
        await pg.wait_for_timeout(1500)
        ev = last_event("SNAPSHOT")
        check("fileset load logged", ev and ev["value"] == "LOAD", str(ev))

        # --- scoring --------------------------------------------------------------
        await pg.click('#nav button[data-screen="scoring"]')
        await pg.click("#sess-start")
        await pg.wait_for_timeout(4000)
        await pg.click("#sess-stop")
        await pg.wait_for_timeout(1000)
        r = api("/api/session")
        check(
            "scoring session produces a total score", r["total"] is not None and 0 <= r["total"] <= 100, str(r["total"])
        )

        # --- heat balance ------------------------------------------------------------
        await pg.click('#nav button[data-screen="balance"]')
        await pg.wait_for_timeout(1500)
        hb = await pg.text_content("#hb-box")
        check("heat balance table rendered", hb and "theoretical" in hb)

        # --- run/stop + speed -----------------------------------------------------
        await pg.click("#btn-run")  # stop
        await pg.wait_for_timeout(1500)
        t1 = api("/api/trend?tags=T_bz&minutes=1")["t"][-1]
        await pg.wait_for_timeout(2500)
        t2 = api("/api/trend?tags=T_bz&minutes=1")["t"][-1]
        check("freeze stops simulation time", t2 == t1, f"{t1} -> {t2}")
        await pg.click("#btn-run")  # start again
        await pg.select_option("#speed", "20")
        await pg.wait_for_timeout(3000)
        t3 = api("/api/trend?tags=T_bz&minutes=5")["t"][-1]
        check("speed 20x advances ~20 s per s", t3 - t2 >= 30, f"advanced {t3 - t2:.0f} s in ~3 s")

        check("no browser console/page errors", not errors, "; ".join(errors[:5]))
        await pg.screenshot(path=sys.argv[2] if len(sys.argv) > 2 else "/tmp/e2e_last.png")
        await b.close()
    post({"type": "speed", "value": 1})
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
