"""Bad-input probe of the HTTP and WebSocket command channels."""

import asyncio
import json
import sys
import urllib.request

import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "localhost:8000"
BAD = [
    {"type": "drive", "tag": "NOPE", "cmd": "sp", "value": 5},
    {"type": "drive", "tag": "ID_FAN", "cmd": "sp", "value": None},
    {"type": "drive", "tag": "ID_FAN", "cmd": "sp", "value": "abc"},
    {"type": "drive", "tag": "ID_FAN", "cmd": "explode"},
    {"type": "pid", "tag": "TIC_CAL", "field": "sp", "value": "NaN"},
    {"type": "pid", "tag": "TIC_CAL", "field": "sp", "value": 5000},
    {"type": "pid", "tag": "TIC_CAL", "field": "mode", "value": "TURBO"},
    {"type": "dist", "id": "nope", "value": 1},
    {"type": "dist", "id": "coal_lhv_kiln", "value": -5},
    {"type": "dist", "id": "coal_lhv_kiln"},
    {"type": "snapshot_load", "name": "does_not_exist"},
    {"type": "snapshot_load", "name": "../../README"},
    {"type": "snapshot_load"},
    {"type": "group", "tag": "G_X", "cmd": "start"},
    {"type": "group", "tag": "G_FEED", "cmd": "dance"},
    {"type": "speed", "value": "fast"},
    {"type": "session", "cmd": "bogus"},
    {},
    {"type": "freecad", "cmd": "nope", "on": False},
]


def post(c):
    req = urllib.request.Request(f"http://{BASE}/api/cmd", json.dumps(c).encode(), {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120]


async def ws_probe():
    async with websockets.connect(f"ws://{BASE}/ws", max_size=None) as ws:
        await ws.recv()  # meta
        fails = 0
        for c in [*BAD, "not json at all", "[1,2,3]"]:
            await ws.send(c if isinstance(c, str) else json.dumps(c))
            while True:
                m = json.loads(await ws.recv())
                if m["type"] == "ack":
                    break
            if m.get("ok") is not False:
                fails += 1
                print("  WS accepted bad input:", c, m)
        await ws.send(json.dumps({"type": "speed", "value": 1}))
        while (m := json.loads(await ws.recv()))["type"] != "ack":
            pass
        print("WS still alive after bad inputs, good command ok =", m.get("ok"), "| bad accepted:", fails)


bad_ok = 0
for c in BAD:
    st, body = post(c)
    ok = isinstance(body, dict) and body.get("ok")
    if st != 200 or ok:
        bad_ok += 1
    print(
        f"{st} {'ACCEPTED!' if ok else 'rejected'}  {json.dumps(c)[:70]:70s} -> {body.get('msg') if isinstance(body, dict) else body}"
    )
print("HTTP: bad inputs accepted or 5xx:", bad_ok)
st, body = post({"type": "speed", "value": 1e308})
print("speed 1e308 is clamped, ok =", body.get("ok"))
asyncio.run(ws_probe())
with urllib.request.urlopen(f"http://{BASE}/api/trend?tags=T_calciner&minutes=1") as r:
    print("simulator alive, trend ok:", r.status)
