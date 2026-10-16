"""Operator CLI for the platform layer.

python -m duosim.platform keys create --workspace acme --label "ci"   # prints the key once
python -m duosim.platform keys revoke <key-id>
python -m duosim.platform workspaces
python -m duosim.platform users list
python -m duosim.platform users admin <email> [--revoke]
python -m duosim.platform credits <workspace> <delta> [--note ...]
python -m duosim.platform plan <workspace> <free|pro|team|enterprise>
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
    us = sub.add_parser("users").add_subparsers(dest="sub", required=True)
    us.add_parser("list")
    ua = us.add_parser("admin")
    ua.add_argument("email")
    ua.add_argument("--revoke", action="store_true")
    cr = sub.add_parser("credits")
    cr.add_argument("workspace")
    cr.add_argument("delta", type=int)
    cr.add_argument("--note", default="operator adjustment")
    pl = sub.add_parser("plan")
    pl.add_argument("workspace")
    pl.add_argument("plan")
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
    if a.cmd in ("users", "credits", "plan"):
        return _saas_commands(a, st)
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


def _saas_commands(a, st: Store) -> int:
    from ..saas import saas_for
    from ..saas.security import InputError

    s = saas_for(st)
    if a.cmd == "users" and a.sub == "list":
        for u in s.list_users(limit=500):
            flags = ",".join(k for k in ("is_admin", "suspended") if u[k]) or "-"
            print(f"{u['id']}  {u['email']:40s} {flags}")
        return 0
    if a.cmd == "users":
        u = s.user_by_email(a.email)
        if u is None:
            print("no such user", file=sys.stderr)
            return 1
        s.set_user_flags(u["id"], is_admin=not a.revoke)
        print(f"{u['email']}: platform admin {'revoked' if a.revoke else 'granted'}")
        return 0
    ws = st.workspace(a.workspace, create=False)
    if ws is None:
        print("no such workspace (use its name, see `workspaces`)", file=sys.stderr)
        return 1
    try:
        if a.cmd == "credits":
            s.grant(ws.id, a.delta, "adjust", "operator", note=a.note)
            print(f"{ws.name}: balance {s.balance(ws.id)}")
        else:
            s.set_plan(ws.id, a.plan, grant=True)
            print(f"{ws.name}: plan {a.plan}, balance {s.balance(ws.id)}")
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


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
