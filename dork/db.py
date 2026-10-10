"""SQLite storage. Everything Dork knows lives here, on the origin machine only."""

import sqlite3
import threading
import time

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT,
    token_hash TEXT UNIQUE,
    ip TEXT,
    created_at REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    summary TEXT DEFAULT '',
    summarized_upto INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT,
    content TEXT,
    device TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    category TEXT,
    importance INTEGER,
    embedding TEXT,
    source TEXT,
    hits INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
"""

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")
_conn.executescript(SCHEMA)

# Migrations for databases created by earlier builds.
for _sql in (
    "ALTER TABLE conversations ADD COLUMN memory_upto INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN meta TEXT",  # e.g. the web sources behind an answer
    "ALTER TABLE memories ADD COLUMN path TEXT",  # place in the memory tree, e.g. People/Family/Sister (Priya)
):
    try:
        _conn.execute(_sql)
        _conn.commit()
    except sqlite3.OperationalError:
        pass  # already applied


def now() -> float:
    return time.time()


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params=()) -> int:
    """Execute a write; returns lastrowid."""
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def get_meta(key: str) -> str | None:
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    x("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
