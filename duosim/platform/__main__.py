"""Operator CLI for the platform layer.

python -m duosim.platform keys create --workspace acme --label "ci"   # prints the key once
python -m duosim.platform keys revoke <key-id>
python -m duosim.platform workspaces
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m duosim.platform")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keys").add_subparsers(dest="sub", required=True)
    c = k.add_parser("create")
    c.add_argument("--workspace", required=True)
    c.add_argument("--label", default="")
    r = k.add_parser("revoke")
    r.add_argument("key_id")
    sub.add_parser("workspaces")
    sub.add_parser("simulators")
    n = sub.add_parser("new")
    n.add_argument("id")
    n.add_argument("--name", default="")
    n.add_argument("--domain", default="")
    n.add_argument("--dir", default="simulators")
    ck = sub.add_parser("check")
    ck.add_argument("id")
    ck.add_argument("--dir", default="")
    a = ap.parse_args(argv)

    if a.cmd in ("simulators", "new", "check"):
        return _sim_commands(a)

    st = Store()
    if a.cmd == "keys" and a.sub == "create":
        ws = st.workspace(a.workspace)
        assert ws is not None
        kid, key = st.create_api_key(ws, a.label)
        print(
            f"workspace: {ws.name} ({ws.id})\nkey id:    {kid}\napi key:   {key}\n(store it now: it cannot be shown again)"
        )
        return 0
    if a.cmd == "keys" and a.sub == "revoke":
        ok = st.revoke_key(a.key_id)
        print("revoked" if ok else "no such key", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if a.cmd == "workspaces":
        with sqlite3.connect(st.path) as c:
            for wid, name, n in c.execute(
                "SELECT w.id, w.name, (SELECT COUNT(*) FROM api_keys k WHERE k.workspace_id=w.id AND k.revoked=0) "
                "FROM workspaces w ORDER BY w.created"
            ):
                print(f"{wid}  {name}  active keys: {n}")
        return 0
    return 2


def _sim_commands(a) -> int:
    from . import scaffold
    from .registry import Registry, conformance, default_registry

    if a.cmd == "new":
        try:
            target = scaffold.write(a.dir, a.id, a.name, a.domain)
        except (ValueError, FileExistsError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"created {target}/  - next: python -m duosim.platform check {a.id} --dir {a.dir}")
        return 0
    reg = default_registry()
    if a.cmd == "check" and a.dir:
        reg = Registry()
        reg.load_directory(a.dir)
    if a.cmd == "simulators":
        for m in reg.manifests():
            print(
                f"{m.id:16s} {m.domain:20s} v{m.version:8s} {len(m.tags)} tags, {len(m.commands)} commands - {m.name}"
            )
        return 0
    try:
        plugin = reg.get(a.id)
    except KeyError as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        return 2
    problems = conformance(plugin)
    for p in problems:
        print(f"FAIL {p}")
    print(f"{a.id}: {'conforms' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
