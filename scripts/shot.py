import asyncio
import sys

from playwright.async_api import async_playwright


async def main(url, out, screen, w=1600, h=900):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": w, "height": h})
        msgs = []
        pg.on("console", lambda m: msgs.append(f"{m.type}: {m.text}"))
        pg.on("pageerror", lambda e: msgs.append(f"PAGEERROR: {e}"))
        await pg.goto(url)
        await pg.wait_for_timeout(2500)
        if screen != "plant":
            await pg.click(f'#nav button[data-screen="{screen}"]')
            await pg.wait_for_timeout(2500)
        await pg.screenshot(path=out)
        print("\n".join(msgs[:20]))
        await b.close()


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "plant"))
