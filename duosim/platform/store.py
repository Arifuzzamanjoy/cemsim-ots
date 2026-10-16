"""Tenant store for the SaaS layer: workspaces, API keys, saved designs, usage quotas (SQLite).

Deliberately small: one file, no ORM, standard library only.  API keys are random 256-bit
tokens; only their SHA-256 is stored, so a database leak does not leak usable keys.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEY_PREFIX = "ds_"
LEGACY_KEY_PREFIXES = ("cs_",)  # keys issued before the DuoSim rename stay valid
ID_RE = re.compile(r"^[0-9a-f]{16}$")  # design ids are server-generated hex
DESIGN_ARTIFACTS = {
    "mesh": "mesh.json",
    "step": "design.step",
    "fcstd": "design.FCStd",
    "png": "preview.png",
    "spec": "spec.json",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    key_hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS designs (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    prompt TEXT NOT NULL, provider TEXT, model TEXT, name TEXT, spec TEXT NOT NULL,
    verified INTEGER, summary TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS designs_ws ON designs(workspace_id, created DESC);
CREATE TABLE IF NOT EXISTS usage (
    workspace_id TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, day, kind));
"""


class QuotaExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    scope: str = ""  # sub-tenant inside a shared workspace: anonymous public-demo visitors are scoped by IP


def data_dir() -> Path:
    return Path(os.environ.get("DUOSIM_DATA_DIR") or Path.cwd() / ".duosim")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path | None = None):
        self.root = Path(path).parent if path else data_dir()
        self.path = Path(path) if path else self.root / "platform.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        try:
            with c:
                yield c
        finally:
            c.close()

    # -- workspaces & keys ------------------------------------------------
    def workspace(self, name: str, create: bool = True) -> Workspace | None:
        with self._conn() as c:
            row = c.execute("SELECT id, name FROM workspaces WHERE name=?", (name,)).fetchone()
            if row is None and create:
                wid = uuid.uuid4().hex[:12]
                c.execute("INSERT INTO workspaces(id, name, created) VALUES (?,?,?)", (wid, name, time.time()))
                return Workspace(wid, name)
            return Workspace(row["id"], row["name"]) if row else None

    def create_api_key(self, workspace: Workspace, label: str = "") -> tuple[str, str]:
        """Returns (key_id, plaintext key).  The plaintext is shown once and never stored."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        kid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_keys(id, workspace_id, key_hash, label, created) VALUES (?,?,?,?,?)",
                (kid, workspace.id, hash_key(key), label[:80], time.time()),
            )
        return kid, key

    def revoke_key(self, key_id: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (key_id,)).rowcount > 0

    def authenticate(self, key: str | None) -> Workspace | None:
        if not key or not key.startswith((KEY_PREFIX, *LEGACY_KEY_PREFIXES)) or len(key) > 200:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT w.id, w.name FROM api_keys k JOIN workspaces w ON w.id = k.workspace_id "
                "WHERE k.key_hash=? AND k.revoked=0",
                (hash_key(key),),
            ).fetchone()
        return Workspace(row["id"], row["name"]) if row else None

    # -- usage ------------------------------------------------------------
    def take(self, ws: Workspace, kind: str, limit: int) -> int:
        """Count one use for today; raises QuotaExceeded when it would exceed `limit`."""
        return self.take_scoped(ws.id, time.strftime("%Y-%m-%d", time.gmtime()), kind, limit, "daily")

    def take_scoped(self, scope_id: str, window: str, kind: str, limit: int, label: str = "") -> int:
        """Count one use of `kind` by `scope_id` in `window` (e.g. a day or an hour string)."""
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO usage(workspace_id, day, kind, n) VALUES (?,?,?,0)", (scope_id, window, kind)
            )
            n = c.execute(
                "SELECT n FROM usage WHERE workspace_id=? AND day=? AND kind=?", (scope_id, window, kind)
            ).fetchone()["n"]
            if n >= limit:
                raise QuotaExceeded(f"{label + ' ' if label else ''}limit of {limit} {kind} reached")
            c.execute("UPDATE usage SET n=n+1 WHERE workspace_id=? AND day=? AND kind=?", (scope_id, window, kind))
            return n + 1

    def refund_scoped(self, scope_id: str, window: str, kind: str) -> None:
        """Give back one use (the provider failed through no fault of the caller)."""
        with self._conn() as c:
            c.execute(
                "UPDATE usage SET n = MAX(n - 1, 0) WHERE workspace_id=? AND day=? AND kind=?", (scope_id, window, kind)
            )

    def used_today(self, ws: Workspace, kind: str) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._conn() as c:
            r = c.execute(
                "SELECT n FROM usage WHERE workspace_id=? AND day=? AND kind=?", (ws.id, day, kind)
            ).fetchone()
        return r["n"] if r else 0

    # -- designs ----------------------------------------------------------
    def design_dir(self, ws: Workspace, design_id: str) -> Path:
        return self.root / "designs" / ws.id / design_id

    def save_design(
        self,
        ws: Workspace,
        *,
        prompt: str,
        provider: str | None,
        model: str | None,
        spec: dict[str, Any],
        verified: bool | None,
        summary: dict[str, Any] | None,
        artifacts: dict[str, bytes] | None = None,
        design_id: str | None = None,
    ) -> str:
        did = design_id or uuid.uuid4().hex[:16]
        d = self.design_dir(ws, did)
        d.mkdir(parents=True, exist_ok=True)
        (d / DESIGN_ARTIFACTS["spec"]).write_text(json.dumps(spec, indent=1))
        for k, blob in (artifacts or {}).items():
            if k in DESIGN_ARTIFACTS and k != "spec":
                (d / DESIGN_ARTIFACTS[k]).write_bytes(blob)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO designs(id, workspace_id, prompt, provider, model, name, spec, verified, "
                "summary, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    ws.id,
                    prompt[:2000],
                    provider,
                    model,
                    str(spec.get("name", ""))[:80],
                    json.dumps(spec),
                    None if verified is None else int(verified),
                    json.dumps(summary or {}),
                    time.time(),
                ),
            )
        return did

    def get_design(self, ws: Workspace, design_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM designs WHERE id=? AND workspace_id=?", (design_id, ws.id)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["spec"] = json.loads(d["spec"])
        d["summary"] = json.loads(d["summary"] or "{}")
        d["verified"] = None if d["verified"] is None else bool(d["verified"])
        d["artifacts"] = [k for k, f in DESIGN_ARTIFACTS.items() if (self.design_dir(ws, design_id) / f).exists()]
        return d

    def list_designs(self, ws: Workspace, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, provider, model, verified, created, prompt FROM designs "
                "WHERE workspace_id=? ORDER BY created DESC LIMIT ?",
                (ws.id, limit),
            ).fetchall()
        return [{**dict(r), "verified": None if r["verified"] is None else bool(r["verified"])} for r in rows]

    def artifact_path(self, ws: Workspace, design_id: str, kind: str) -> Path | None:
        name = DESIGN_ARTIFACTS.get(kind)
        if name is None or not ID_RE.match(design_id):  # server-generated hex only: no path tricks
            return None
        p = self.design_dir(ws, design_id) / name
        return p if p.is_file() else None
