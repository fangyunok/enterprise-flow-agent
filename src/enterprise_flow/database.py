"""SQLite business transactions; distinct from graph checkpoints."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 user_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, department_id TEXT NOT NULL,
 display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cost_centers (
 code TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, department_scope TEXT NOT NULL, label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
 policy_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, department_scope TEXT NOT NULL,
 kind TEXT NOT NULL, city TEXT NOT NULL, version TEXT NOT NULL,
 effective_from TEXT NOT NULL, effective_to TEXT, clause_id TEXT NOT NULL,
 title TEXT NOT NULL, content TEXT NOT NULL, cap_cents INTEGER,
 CHECK(cap_cents IS NULL OR cap_cents >= 0)
);
CREATE TABLE IF NOT EXISTS orders (
 order_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 tenant_id TEXT NOT NULL, kind TEXT NOT NULL, city TEXT NOT NULL,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, amount_cents INTEGER NOT NULL,
 currency TEXT NOT NULL, receipt_valid INTEGER NOT NULL, status TEXT NOT NULL,
 CHECK(amount_cents > 0)
);
CREATE TABLE IF NOT EXISTS drafts (
 draft_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 tenant_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
 input_json TEXT NOT NULL, result_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 request_key TEXT, request_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(owner_id, request_key)
);
CREATE TABLE IF NOT EXISTS confirmations (
 draft_id TEXT NOT NULL REFERENCES drafts(draft_id), version INTEGER NOT NULL,
 owner_id TEXT NOT NULL REFERENCES users(user_id), content_hash TEXT NOT NULL,
 confirmed_at TEXT NOT NULL, PRIMARY KEY(draft_id, version, owner_id)
);
CREATE TABLE IF NOT EXISTS submissions (
 submission_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(user_id),
 approved_version INTEGER NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(tenant_id, draft_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
 owner_id TEXT NOT NULL REFERENCES users(user_id), key TEXT NOT NULL,
 request_hash TEXT NOT NULL, submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
 PRIMARY KEY(owner_id, key)
);
CREATE TABLE IF NOT EXISTS preferences (
 owner_id TEXT NOT NULL REFERENCES users(user_id), key TEXT NOT NULL, value TEXT NOT NULL,
 version INTEGER NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY(owner_id, key)
);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 tenant_id TEXT NOT NULL, mode TEXT NOT NULL, message TEXT NOT NULL,
 request_id TEXT, record_json TEXT NOT NULL, UNIQUE(owner_id, request_id)
);
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
 action TEXT NOT NULL, resource_id TEXT NOT NULL, details_json TEXT NOT NULL, at_utc TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
