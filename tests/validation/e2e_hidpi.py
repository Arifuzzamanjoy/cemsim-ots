"""Regression for the growing-canvas bug on scaled (HiDPI / Windows 125-150 %) displays.

Opens the chart screens at several devicePixelRatios, lets the HMI redraw ~20 times,
and checks every canvas keeps its size and actually contains drawn pixels.
"""

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "localhost:8000"
JS = """() => [...document.querySelectorAll('.screen.active canvas')].map(c => {
  const g = c.getContext('2d'), d = g.getImageData(0, 0, c.width, c.height).data;
  let ink = 0; for (let i = 0; i < d.length; i += 4 * 97) if (d[i + 3] > 0) ink++;
  return {id: c.id, w: c.width, h: c.height, cssH: c.getBoundingClientRect().height, ink};
})"""


async def main():
    bad = 0
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for dpr in (1.0, 1.25, 1.5, 2.0):
            pg = await b.new_page(viewport={"width": 1280, "height": 600}, device_scale_factor=dpr)
            await pg.goto(f"http://{BASE}/")
            await pg.wait_for_timeout(1500)
            for scr in ("profiles", "trends"):
                await pg.click(f'#nav button[data-screen="{scr}"]')
                await pg.wait_for_timeout(1500)
                first = await pg.evaluate(JS)
                await pg.wait_for_timeout(10000)
                last = await pg.evaluate(JS)
                for a, z in zip(first, last):
                    stable = (a["w"], a["h"]) == (z["w"], z["h"])
                    sane = z["h"] <= 800 * dpr and abs(z["cssH"] - z["h"] / dpr) < 2
                    drawn = z["ink"] > 50
                    ok = stable and sane and drawn
                    bad += not ok
                    print(
                        f"{'PASS' if ok else 'FAIL'} dpr={dpr} {scr}/{z['id']}: {a['w']}x{a['h']} -> {z['w']}x{z['h']} "
                        f"css h {z['cssH']:.0f}px ink {z['ink']}"
                    )
            await pg.close()
        await b.close()
    print("hidpi canvas checks:", "all passed" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
