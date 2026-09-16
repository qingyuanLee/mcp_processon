"""Cache backend abstraction (pluggable, SQLite built-in)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class CacheBackend(ABC):
    """Abstract key-value cache used for tokens and small metadata."""

    @abstractmethod
    def get(self, key: str) -> Optional[dict]:
        """Return the cached dict for *key*, or None."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store *value* under *key*, optionally with a TTL in seconds."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete *key*. Return True if it existed."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether *key* is present and not expired."""

    @abstractmethod
    def clear(self, prefix: str = "") -> int:
        """Delete keys, optionally filtered by *prefix*. Return count removed."""

    # ------------------------------------------------------------------
    # Convenience helpers shared by all backends
    # ------------------------------------------------------------------

    def load_token(self) -> Optional[dict]:
        return self.get("auth:token")

    def save_token(self, token: str, expires_at: float = 0.0, **extra) -> None:
        data = {"token": token, "expires_at": expires_at, **extra}
        self.set("auth:token", data, ttl=(expires_at - _now()) if expires_at else None)


def _now() -> float:
    import time
    return time.time()
