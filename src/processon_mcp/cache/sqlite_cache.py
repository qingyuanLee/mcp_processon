"""Built-in SQLite cache backend — zero configuration."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

from .base import CacheBackend

DEFAULT_CACHE_DIR = os.path.expanduser("~/.processon-mcp")
DEFAULT_CACHE_DB = os.path.join(DEFAULT_CACHE_DIR, "cache.db")


class SQLiteCache(CacheBackend):
    """Tiny key-value store on top of SQLite with optional per-key TTL."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.getenv("PO_CACHE_DB") or DEFAULT_CACHE_DB
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " expires_at REAL NOT NULL DEFAULT 0"
            ")"
        )
        self._conn.commit()

    def _purge_expired(self) -> None:
        self._conn.execute("DELETE FROM kv WHERE expires_at > 0 AND expires_at < ?", (time.time(),))
        self._conn.commit()

    def get(self, key: str) -> Optional[dict]:
        self._purge_expired()
        row = self._conn.execute(
            "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expires_at = (time.time() + ttl) if ttl else 0.0
        self._conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
            (key, json.dumps(value, ensure_ascii=False), expires_at),
        )
        self._conn.commit()

    def delete(self, key: str) -> bool:
        cur = self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

    def exists(self, key: str) -> bool:
        self._purge_expired()
        row = self._conn.execute("SELECT 1 FROM kv WHERE key = ?", (key,)).fetchone()
        return row is not None

    def clear(self, prefix: str = "") -> int:
        if prefix:
            cur = self._conn.execute("DELETE FROM kv WHERE key LIKE ?", (prefix + "%",))
        else:
            cur = self._conn.execute("DELETE FROM kv")
        self._conn.commit()
        return cur.rowcount
