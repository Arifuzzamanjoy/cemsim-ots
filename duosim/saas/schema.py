"""SQLite schema of the SaaS layer and an idempotent migration of the tenant store's `workspaces` table."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL DEFAULT '',
    password_hash TEXT, email_verified INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0,
    suspended INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS memberships (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')), created REAL NOT NULL,
    PRIMARY KEY (user_id, workspace_id));
CREATE INDEX IF NOT EXISTS memberships_ws ON memberships(workspace_id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    created REAL NOT NULL, expires REAL NOT NULL, ip TEXT, user_agent TEXT);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    email TEXT, workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE, role TEXT,
    created REAL NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL, balance INTEGER NOT NULL, kind TEXT NOT NULL, op TEXT NOT NULL DEFAULT '',
    ref TEXT, user_id TEXT, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_ws ON credit_ledger(workspace_id, id DESC);
CREATE TABLE IF NOT EXISTS billing_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, workspace_id TEXT, user_id TEXT,
    action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', ip TEXT);
CREATE INDEX IF NOT EXISTS audit_ws ON audit_log(workspace_id, id DESC);
CREATE TABLE IF NOT EXISTS provider_keys (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL, type TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
    key_enc TEXT NOT NULL, key_hint TEXT NOT NULL DEFAULT '', vision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
    UNIQUE (workspace_id, name));
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, last_status TEXT, last_delivery REAL);
"""

# columns added to the tenant store's `workspaces` table
WORKSPACE_COLUMNS = {
    "display_name": "TEXT",
    "plan": "TEXT NOT NULL DEFAULT 'free'",
    "credits": "INTEGER NOT NULL DEFAULT 0",
    "credits_period": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "stripe_customer_id": "TEXT",
    "stripe_subscription_id": "TEXT",
}


def migrate(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(workspaces)")}
    for col, decl in WORKSPACE_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE workspaces ADD COLUMN {col} {decl}")
    c.execute("CREATE INDEX IF NOT EXISTS workspaces_customer ON workspaces(stripe_customer_id)")
