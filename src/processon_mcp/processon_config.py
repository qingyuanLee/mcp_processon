"""ProcessOn MCP — configuration, constants, and error types."""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("processon_mcp")
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# API base URL (domain-allowlisted)
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://smart.processon.com"
ALLOWED_BASE_HOSTS = ("smart.processon.com", "processon.com")


def _resolve_api_base() -> str:
    env_url = os.getenv("PO_API_BASE_URL") or os.getenv("PROCESSON_API_BASE_URL")
    if not env_url:
        return DEFAULT_API_BASE
    try:
        host = urlparse(env_url).hostname or ""
    except Exception:
        host = ""
    if host in ALLOWED_BASE_HOSTS:
        return env_url.rstrip("/")
    logger.warning(
        "PO_API_BASE_URL host '%s' not in allowlist — using default %s",
        host, DEFAULT_API_BASE,
    )
    return DEFAULT_API_BASE


API_BASE: str = _resolve_api_base()

# Remote MCP HTTP endpoint (Streamable HTTP, JSON-RPC 2.0)
MCP_URL: str = API_BASE + "/mcp"

# ---------------------------------------------------------------------------
# HTTP defaults
# ---------------------------------------------------------------------------

DEFAULT_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/event-stream",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

REQUEST_TIMEOUT = 120  # diagram generation can take a while
MCP_TOOL_TIMEOUT = 180

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

ENDPOINTS: Dict[str, Tuple[str, str]] = {
    # Markdown -> editable mindmap (direct REST, no MCP handshake)
    "md_to_mindmap": ("POST", "/v1/api/transform/md"),
    # OAuth temporary token query (used by the browser-authorization flow)
    "token_query": ("GET", "/v1/token/temporary/query"),
}

# ---------------------------------------------------------------------------
# Diagram structures allowed for md_to_mindmap (mirrors official skill)
# ---------------------------------------------------------------------------

ALLOWED_STRUCTURES = [
    "mind_free",
    "mind_right",
    "mind_org",
    "mind_ishikawa_left",
    "mind_timeline_h",
    "mind_tree_free",
    "mind_treeTable_left_title",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProcessOnError(Exception):
    """Base error for ProcessOn API operations."""

    def __init__(self, msg: str, status_code: int | None = None, body: object = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.status_code = status_code
        self.body = str(body)[:500] if body else None


class ProcessOnAuthError(ProcessOnError):
    """Raised when authentication is missing or rejected (401/403)."""
