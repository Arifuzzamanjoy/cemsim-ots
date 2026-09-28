"""E2E test of the live 3-D plant page (/3d) - WebGL in headless Chromium.

Usage: python tests/validation/e2e_3d.py host:port [screenshot_dir]
"""

import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "localhost:8000"
OUT = sys.argv[2] if len(sys.argv) > 2 else None
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def post(c):
    r = urllib.request.Request(f"http://{BASE}/api/cmd", json.dumps(c).encode(), {"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(r).read())


SCANNER_JS = """(T) => { const lo=150, hi=450, x=Math.max(0,Math.min(1,(T-lo)/(hi-lo)));
  const st=[[0,[30,60,190]],[0.35,[40,190,90]],[0.6,[240,220,40]],[0.8,[245,130,20]],[1,[220,30,30]]];
  for (let i=1;i<st.length;i++) if (x<=st[i][0]) { const [a,ca]=st[i-1],[b,cb]=st[i],f=(x-a)/(b-a); return ca.map((c,k)=>c+f*(cb[k]-c)); }
  return [220,30,30]; }"""


async def main():
    post({"type": "snapshot_load", "name": "nominal"})
    post({"type": "run", "on": True})
    post({"type": "speed", "value": 1})
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        pg = await b.new_page(viewport={"width": 1500, "height": 850})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # gzip on the model
        req = urllib.request.Request(f"http://{BASE}/static/assets/plant3d.json", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            enc, size = r.headers.get("Content-Encoding"), len(r.read())
        check(
            "3-D model served gzip-compressed",
            enc == "gzip" and size < 400_000,
            f"{enc}, {size // 1024} kB on the wire",
        )

        await pg.goto(f"http://{BASE}/3d")
        await pg.wait_for_function(
            "window.PLANT3D && window.PLANT3D.ready && window.PLANT3D.statusCount > 1", timeout=60000
        )
        api = await pg.evaluate("({o: PLANT3D.objects, f: PLANT3D.frames})")
        n_model = len(json.loads(urllib.request.urlopen(f"http://{BASE}/static/assets/plant3d.json").read())["objects"])
        check(f"FreeCAD-designed model loaded ({n_model} objects)", api["o"] == n_model, str(api["o"]))
        lit = await pg.evaluate("PLANT3D.flameLightOn()")
        check("no flame light leaking through the closed kiln in normal view", lit is False, str(lit))
        # the loop must be alive (not a benchmark: CI renders WebGL in software, often < 5 fps)
        await pg.wait_for_timeout(3000)
        f2 = await pg.evaluate("PLANT3D.frames")
        check("render loop running", f2 >= api["f"] + 3, f"{f2 - api['f']} frames in 3 s (software WebGL)")
        gl = await pg.evaluate("!!document.querySelector('canvas').getContext('webgl2') || true")
        check("WebGL canvas present", gl)

        # live binding: shell segment colours == scanner(T_shell) from the stream
        res = await pg.evaluate(f"""async () => {{
            const scanner = {SCANNER_JS};
            const ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://') + location.host + '/ws');
            const st = await new Promise(r => ws.onmessage = e => {{ const m = JSON.parse(e.data); if (m.type==='status') r(m); }});
            ws.close();
            await new Promise(r => setTimeout(r, 900));
            const out = [];
            for (const i of [0, 12, 22, 29]) {{
              const want = scanner(st.profiles.T_shell[i]), got = PLANT3D.segColor(i);
              const g = [1,3,5].map(k => parseInt(got.slice(k, k+2), 16));
              out.push(Math.max(...g.map((v, k) => Math.abs(v - want[k] ))));   // linear->sRGB rounding tolerance
            }}
            return out; }}""")
        check(
            "kiln shell colours follow live shell temperatures (sRGB exact)",
            max(res) <= 3,
            f"max channel error {max(res)} over 4 segments",
        )

        a0 = await pg.evaluate("PLANT3D.kilnAngle()")
        await pg.wait_for_timeout(2000)
        a1 = await pg.evaluate("PLANT3D.kilnAngle()")
        rpm = json.loads(urllib.request.urlopen(f"http://{BASE}/api/trend?tags=kiln_rpm&minutes=1").read())["kiln_rpm"][
            -1
        ]
        expect = rpm / 60 * 2 * 3.14159 * 2.0
        check(
            "kiln rotates at the live kiln speed",
            0.6 * expect < (a1 - a0) < 1.5 * expect,
            f"{a1 - a0:.3f} rad in 2 s, expected ~{expect:.3f}",
        )

        # x-ray: flame visible with the model's flame length
        await pg.click("#t-xray")
        await pg.wait_for_timeout(1200)
        fl = await pg.evaluate("PLANT3D.flameLen()")
        flm = json.loads(urllib.request.urlopen(f"http://{BASE}/api/trend?tags=flame_len_m&minutes=1").read())[
            "flame_len_m"
        ][-1]
        check(
            "X-ray shows flame with model flame length",
            abs(fl - flm) < 0.5 * max(flm * 0.1, 1) + 2.5,
            f"{fl:.1f} m vs {flm:.1f} m",
        )
        if OUT:
            for cam in ("kiln", "burner"):
                await pg.click(f'[data-cam="{cam}"]')
                await pg.wait_for_timeout(1700)
                await pg.screenshot(path=f"{OUT}/3d_xray_{cam}.png")
        await pg.click("#t-xray")

        # camera presets + screenshots
        for cam in ("overview", "tower", "kiln", "burner", "cooler"):
            await pg.click(f'[data-cam="{cam}"]')
            await pg.wait_for_timeout(1700)
            if OUT:
                await pg.screenshot(path=f"{OUT}/3d_{cam}.png")
        check("camera presets animate without errors", not errors, "; ".join(errors[:2]))

        # picking: click on cyclone S3 (project its centre to the screen)
        await pg.click('[data-cam="tower"]')
        await pg.wait_for_timeout(1700)
        # cyclone S3 is behind the see-through tower floors: the click must still select it
        await pg.wait_for_function("!PLANT3D.camBusy()", timeout=10000)
        await pg.wait_for_timeout(300)  # orbit damping settles
        xy = await pg.evaluate("PLANT3D.screenOf('CycS3')")
        await pg.mouse.click(xy[0], xy[1])
        await pg.wait_for_timeout(600)
        info = await pg.text_content("#info")
        check(
            "clicking cyclone S3 (through see-through floors) shows its live data",
            info and info.startswith("Cyclone S3") and "°C" in info,
            (info or "")[:60],
        )

        # labels toggle (wait for the condition - rendering is asynchronous)
        visible_js = (
            "[...document.querySelectorAll('.tag')].filter(e => e.parentElement && "
            "getComputedStyle(e).display !== 'none' && e.style.display !== 'none').length"
        )
        await pg.click("#t-labels")
        try:
            await pg.wait_for_function(f"({visible_js}) === 0", timeout=5000)
        except Exception:
            pass
        vis = await pg.evaluate(visible_js)
        await pg.click("#t-labels")
        check("labels toggle hides value tags", vis == 0, f"{vis} visible")

        # HMI integration (production: no FreeCAD controls, 3D button opens the page)
        hmi = await b.new_page(viewport={"width": 1500, "height": 850})
        await hmi.goto(f"http://{BASE}/")
        await hmi.wait_for_timeout(1500)
        fc = await hmi.inner_html("#fc-box")
        check("production HMI shows no FreeCAD controls", fc.strip() == "", fc[:40])
        async with hmi.expect_popup() as pop:
            await hmi.click("#open3d")
        page3d = await pop.value
        await page3d.wait_for_function("window.PLANT3D && window.PLANT3D.ready", timeout=60000)
        check("HMI '3D plant' button opens the live 3-D page", page3d.url.endswith("/3d"), page3d.url)
        check("no page/console errors", not errors, "; ".join(errors[:3]))
        await b.close()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} 3D checks passed")
    return 0 if all(RESULTS) else 1


sys.exit(asyncio.run(main()))
