"""Cache factory — resolves the configured backend (SQLite built-in)."""

from __future__ import annotations

from .base import CacheBackend
from .sqlite_cache import SQLiteCache


def get_cache_backend() -> CacheBackend:
    """Return the cache backend (SQLite, zero-config, at ~/.processon-mcp/cache.db)."""
    return SQLiteCache()


__all__ = ["CacheBackend", "SQLiteCache", "get_cache_backend"]
