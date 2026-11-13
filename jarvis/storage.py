"""A small SQLite database for everything Jarvis remembers: alarms, timers, reminders,
calendar events, lists, personal memories, vocabulary, conversation history, security events.

File: ~/.local/share/jarvis-bot/jarvis.db
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY, label TEXT, hour INTEGER, minute INTEGER, days TEXT DEFAULT '',
    next_fire REAL, snooze_until REAL, enabled INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY, name TEXT, duration REAL, ends REAL, created REAL);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, text TEXT, next_fire REAL, repeat TEXT DEFAULT 'none',
    needs_confirmation INTEGER DEFAULT 0, awaiting INTEGER DEFAULT 0, nag_count INTEGER DEFAULT 0,
    next_nag REAL, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, title TEXT, start REAL, end REAL, location TEXT DEFAULT '',
    notes TEXT DEFAULT '', reminded INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS list_items (
    id INTEGER PRIMARY KEY, list TEXT, item TEXT, created REAL);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY, text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS vocab (
    word TEXT PRIMARY KEY, definition TEXT, source TEXT, added REAL, next_review REAL,
    interval_days REAL DEFAULT 1, ease REAL DEFAULT 2.3, reps INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0, last_result TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS user_words (
    word TEXT PRIMARY KEY, count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY, time REAL, source TEXT, user TEXT, reply TEXT);
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY, time REAL, kind TEXT, duration REAL, path TEXT, transcript TEXT DEFAULT '',
    peak REAL, notified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Run a change; returns the new row id (for INSERT) or the number of changed rows."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    # small key/value store for state like "away mode"
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    @staticmethod
    def now() -> float:
        return time.time()
