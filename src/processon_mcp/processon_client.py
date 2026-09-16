"""ProcessOn MCP — API client.

Talks to ProcessOn's hosted AI diagram service (smart.processon.com).

Two calling paths:
  1. Remote MCP (Streamable HTTP, JSON-RPC 2.0) — `generate_chart`, the
     flagship "natural language -> editable diagram" tool. We POST a single
     `tools/call` envelope (stateless mode), exactly like the official
     skill's Node-spawn fallback.
  2. Direct REST — `/v1/api/transform/md` turns Markdown into a mindmap.

Authentication: a personal API token (sk-po-..., created at
https://smart.processon.com/user) is sent as `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

import requests

from processon_mcp.cache.base import CacheBackend
from processon_mcp.processon_config import (
    ALLOWED_STRUCTURES,
    API_BASE,
    DEFAULT_HEADERS,
    ENDPOINTS,
    MCP_TOOL_TIMEOUT,
    MCP_URL,
    REQUEST_TIMEOUT,
    ProcessOnAuthError,
    ProcessOnError,
    logger,
)


class ProcessOnClient:
    """ProcessOn API client backed by a pluggable cache."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        token: Optional[str] = None,
        cache: Optional[CacheBackend] = None,
    ) -> None:
        self._load_env_file()

        # Credential resolution: explicit arg > env var > cache
        self.api_key = api_key or os.getenv("PROCESSON_API_KEY") or os.getenv("PO_API_KEY")
        raw_token = token or os.getenv("PROCESSON_TOKEN") or os.getenv("PO_TOKEN")

        # Normalize Authorization header value
        self.authorization: Optional[str] = None
        self._cache = cache
        self._session = requests.Session()

        if raw_token:
            self.authorization = raw_token if raw_token.lower().startswith("bearer ") else f"Bearer {raw_token}"
        elif self.api_key:
            self.authorization = f"Bearer {self.api_key}"

        # Restore cached token (OAuth flow) if nothing else is configured
        if not self.authorization and self._cache:
            data = self._cache.load_token()
            if data:
                self.authorization = data.get("token")

    # ------------------------------------------------------------------
    # Env file loading (project .env, then ~/.processon-mcp/.env)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_env_file() -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(here))
        candidates = [
            os.path.join(project_root, ".env"),
            os.path.expanduser("~/.processon-mcp/.env"),
        ]
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                for line in open(path, encoding="utf-8").read().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip("\"'")
                    if key in ("PROCESSON_API_KEY", "PROCESSON_TOKEN", "PO_API_BASE_URL") and not os.getenv(key):
                        os.environ[key] = value
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        if self.authorization:
            headers["Authorization"] = self.authorization
        return headers

    def ensure_authenticated(self) -> None:
        if not self.authorization:
            raise ProcessOnAuthError(
                "No ProcessOn credentials. Set PROCESSON_API_KEY (create one at "
                "https://smart.processon.com/user) in .env or the environment."
            )

    # ------------------------------------------------------------------
    # Low-level JSON-RPC call to the remote MCP service
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_rpc_response(resp: requests.Response) -> Dict[str, Any]:
        body = resp.text or ""
        # Try plain JSON first
        try:
            return json.loads(body)
        except Exception:
            pass
        # Fall back to SSE: concatenate `data:` lines and parse as JSON
        if "text/event-stream" in resp.headers.get("Content-Type", "") or body.lstrip().startswith("event:"):
            chunks = []
            for line in body.splitlines():
                if line.startswith("data:"):
                    chunks.append(line[5:].strip())
            if chunks:
                try:
                    return json.loads("\n".join(chunks))
                except Exception:
                    pass
        raise ProcessOnError(
            f"Unparseable MCP response (HTTP {resp.status_code}): {body[:300]!r}",
            status_code=resp.status_code,
        )

    def _call_remote_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """POST a JSON-RPC tools/call to the hosted MCP endpoint (stateless)."""
        self.ensure_authenticated()
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0x7FFFFFFF,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        resp = self._session.post(
            MCP_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            timeout=MCP_TOOL_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise ProcessOnAuthError(
                f"ProcessOn rejected the credential (HTTP {resp.status_code}). "
                "Token may be invalid or expired.",
                status_code=resp.status_code,
                body=resp.text,
            )
        rpc = self._parse_rpc_response(resp)
        if rpc.get("error"):
            err = rpc["error"]
            raise ProcessOnError(
                f"ProcessOn MCP error: {err.get('message', err)}",
                body=err,
            )
        result = rpc.get("result", {})
        return self._extract_tool_content(result)

    @staticmethod
    def _extract_tool_content(result: Dict[str, Any]) -> Dict[str, Any]:
        """The hosted MCP wraps its real payload in result.content[].text (JSON)."""
        content = result.get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                try:
                    parsed = json.loads(joined)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"content": joined}
                except Exception:
                    return {"content": joined}
        return result

    # ------------------------------------------------------------------
    # Public API — diagram generation
    # ------------------------------------------------------------------

    def generate_chart(self, prompt: str) -> Dict[str, Any]:
        """Generate an editable online diagram from a natural-language prompt.

        Returns a dict with keys like imgUrl/previewUrl (image),
        visitUrl/editUrl (editable link), message/ok.
        """
        return self._call_remote_tool("generate_chart", {"prompt": prompt})

    def md_to_mindmap(
        self,
        title: str,
        markdown: str,
        structure: str = "mind_free",
        theme: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Convert Markdown into an editable ProcessOn mindmap (direct REST)."""
        method, path = ENDPOINTS["md_to_mindmap"]
        structure = structure if structure in ALLOWED_STRUCTURES else "mind_free"
        payload: Dict[str, Any] = {
            "title": title,
            "markdown": markdown,
            "structure": structure,
            "source": "mcp_processon",
        }
        if theme:
            payload["theme"] = theme

        url = API_BASE + path
        resp = self._session.request(
            method,
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise ProcessOnAuthError(
                f"ProcessOn rejected the credential (HTTP {resp.status_code}).",
                status_code=resp.status_code,
                body=resp.text,
            )
        try:
            data = resp.json()
        except Exception:
            raise ProcessOnError(
                f"Unparseable response (HTTP {resp.status_code}): {resp.text[:300]!r}",
                status_code=resp.status_code,
            )
        if isinstance(data, dict) and data.get("success") is False:
            raise ProcessOnError(
                f"ProcessOn error: {data.get('error', data)}", body=data
            )
        return data

    # ------------------------------------------------------------------
    # Auth status
    # ------------------------------------------------------------------

    def auth_status(self) -> Dict[str, Any]:
        """Return a best-effort auth status without making a network call."""
        if self.authorization:
            token = self.authorization.replace("Bearer ", "")
            masked = (token[:8] + "..." + token[-4:]) if len(token) > 12 else "configured"
            return {"authenticated": True, "token_masked": masked}
        return {"authenticated": False, "token_masked": None}
