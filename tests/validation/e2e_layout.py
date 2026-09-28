"""Mimic layout validation: pipe connections, topology and overlaps (Playwright).

Reads window.MIMIC_GEOM (ports, vessels, pipes registered by the HMI) and checks:
  1. every pipe endpoint resolves; a free 'to' end must be a junction ON another pipe
  2. all pipe segments are orthogonal (P&ID style)
  3. no pipe passes through a vessel (cyclone body/cone/vortex finder, calciner, ...)
  4. gas chain topology: CAL -> S5 -> S4 -> S3 -> S2 -> S1 -> ID fan -> stack,
     each duct leaving a cyclone roof outlet and entering a side inlet
  5. each stage's meal discharges into the riser feeding the NEXT stage down
  6. no pipe runs through a value box
Usage: python tests/validation/e2e_layout.py host:port [screenshot.png]
"""

import asyncio
import itertools
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "localhost:8000"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def on_segment(p, a, b, tol=0.5):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    if abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) > tol * max(abs(x2 - x1) + abs(y2 - y1), 1):
        return False
    return min(x1, x2) - tol <= x <= max(x1, x2) + tol and min(y1, y2) - tol <= y <= max(y1, y2) + tol


def inside_poly(pt, poly, inset=1.5):
    """Strictly inside a convex polygon by more than `inset` px."""
    x, y = pt
    n = len(poly)
    sign = 0
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        L = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 or 1
        d = cross / L
        if abs(d) < inset:
            return False
        s = 1 if d > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def samples(pts, skip_ends=4.0, step=1.0):
    """Points along a polyline, excluding `skip_ends` px at both ends (port contact)."""
    segs = list(itertools.pairwise(pts))
    total = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in segs)
    d = 0.0
    for a, b in segs:
        L = abs(b[0] - a[0]) + abs(b[1] - a[1])
        n = max(int(L / step), 1)
        for i in range(n + 1):
            t = i / n
            s = d + t * L
            if skip_ends <= s <= total - skip_ends:
                yield (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        d += L


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 900})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        await pg.goto(f"http://{BASE}/")
        await pg.wait_for_timeout(1500)
        geom = await pg.evaluate("() => window.MIMIC_GEOM")
        boxes = await pg.evaluate("""() => [...document.querySelectorAll('#mimic g.val')].map(g => {
            const svg = document.getElementById('mimic'), r = g.querySelector('rect');
            const t = svg.getScreenCTM().inverse().multiply(g.getScreenCTM());   // group -> svg user space
            return {tag: g.querySelector('text').textContent, x: t.e, y: t.f, w: +r.getAttribute('width'), h: +r.getAttribute('height')}; })""")
        if len(sys.argv) > 2:
            await pg.screenshot(path=sys.argv[2])
        await b.close()

    ports, vessels, pipes = geom["ports"], geom["vessels"], geom["pipes"]
    check("no console errors while building the mimic", not errs, "; ".join(errs[:3]))

    # 1. endpoints
    bad = [pp["label"] for pp in pipes if any(q is None for q in pp["pts"])]
    check("all pipe endpoints resolve to ports", not bad, str(bad))
    for pp in pipes:
        if isinstance(pp["to"], list):
            end = tuple(pp["pts"][-1])
            hosts = [
                q for q in pipes if q is not pp and any(on_segment(end, a, c) for a, c in zip(q["pts"], q["pts"][1:]))
            ]
            pp["joins"] = hosts[0]["label"] if hosts else None
            check(f"junction lands on a pipe: {pp['label']}", hosts, f"end {end} -> {pp['joins']}")

    # 2. orthogonal
    diag = [pp["label"] for pp in pipes for a, c in zip(pp["pts"], pp["pts"][1:]) if a[0] != c[0] and a[1] != c[1]]
    check("all pipe runs orthogonal", not diag, str(diag))

    # 3. pipes through vessels
    hits = []
    for pp in pipes:
        for pt in samples(pp["pts"]):
            for v in vessels:
                if any(inside_poly(pt, poly) for poly in v["polys"]):
                    hits.append(f"{pp['label']} through {v['name']} at ({pt[0]:.0f},{pt[1]:.0f})")
                    break
    uniq = sorted({h.split(" at ")[0] for h in hits})
    check("no pipe passes through a vessel", not hits, "; ".join(uniq[:6]))

    # 4. gas chain topology
    chain = [
        ("CAL.out", "S5.in"),
        ("S5.out", "S4.in"),
        ("S4.out", "S3.in"),
        ("S3.out", "S2.in"),
        ("S2.out", "S1.in"),
        ("S1.out", "IDF.in"),
        ("IDF.out", "STACK.in"),
    ]
    gas = {(pp["from"], pp["to"]) for pp in pipes if pp["cls"] == "pipe-gas"}
    missing = [c for c in chain if c not in gas]
    check("gas chain CAL->S5->S4->S3->S2->S1->ID fan->stack", not missing, str(missing))
    for n in ("S1", "S2", "S3", "S4", "S5"):
        ins = [pp for pp in pipes if pp["cls"] == "pipe-gas" and pp["to"] == f"{n}.in"]
        outs = [pp for pp in pipes if pp["cls"] == "pipe-gas" and pp["from"] == f"{n}.out"]
        x_in, y_in = ports[f"{n}.in"]
        x_out, y_out = ports[f"{n}.out"]
        check(
            f"{n}: one gas inlet at the side, one outlet at the roof",
            len(ins) == 1 and len(outs) == 1 and y_in > y_out and x_in != x_out,
            f"in {ports[n + '.in']} out {ports[n + '.out']}",
        )

    # 5. meal routing
    want = {
        "kiln feed -> riser S2->S1": "S2 -> S1",
        "S1 meal -> riser S3->S2": "S3 -> S2",
        "S2 meal -> riser S4->S3": "S4 -> S3",
        "S3 meal -> riser S5->S4": "S5 -> S4",
    }
    for lab, riser in want.items():
        pp = next((q for q in pipes if q["label"] == lab), None)
        check(f"meal '{lab}' joins riser '{riser}'", pp and pp.get("joins") == riser, pp and pp.get("joins"))
    m4 = next(q for q in pipes if q["label"] == "S4 meal -> calciner")
    check("S4 meal ends at the calciner", m4["to"] == "CAL.meal_in")

    # 6. value boxes not crossed by pipes
    crossed = set()
    for bx in boxes:
        rect = [
            [bx["x"], bx["y"]],
            [bx["x"] + bx["w"], bx["y"]],
            [bx["x"] + bx["w"], bx["y"] + bx["h"]],
            [bx["x"], bx["y"] + bx["h"]],
        ]
        for pp in pipes:
            if any(inside_poly(pt, rect, inset=0.5) for pt in samples(pp["pts"], 0.0)):
                crossed.add(f"{pp['label']} x box@({bx['x']:.0f},{bx['y']:.0f})")
    check("no pipe runs through a value box", not crossed, "; ".join(sorted(crossed)[:6]))

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} layout checks passed")
    return 0 if all(RESULTS) else 1


sys.exit(asyncio.run(main()))
