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

import hashlib
import json
import os
import time
import uuid
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
                    if key in ("PROCESSON_API_KEY", "PROCESSON_TOKEN", "PO_API_BASE_URL",
                               "PROCESSON_ACCOUNT", "PROCESSON_PASSWORD",
                               "PO_ACCOUNT", "PO_PASSWORD") and not os.getenv(key):
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

    def generate_diagram_dsl(self, prompt: str) -> str:
        """Ask ProcessOn to turn an intent into Mermaid DSL (a draft).

        The returned Mermaid text is what the LLM can then edit/iterate itself.
        """
        result = self._call_remote_tool("generate_diagram_dsl", {"prompt": prompt})
        for key in ("mermaid", "dsl", "content", "code", "definition"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(result, ensure_ascii=False, indent=2)

    def render_mermaid(
        self,
        mermaid_code: str,
        title: str = "",
        diagram_type: str = "",
    ) -> Dict[str, Any]:
        """Render a self-authored Mermaid definition into an editable ProcessOn diagram.

        The LLM authors/iterates the Mermaid; ProcessOn only renders it verbatim.
        Returns imgUrl (preview) + visitUrl (editable link).
        """
        prompt = (
            "Render the following Mermaid diagram DEFINITION VERBATIM into an "
            "editable ProcessOn diagram. Do NOT redesign, rename, add, or remove "
            "any nodes or edges — render exactly this Mermaid code as-is. "
        )
        if diagram_type:
            prompt += f"Diagram type: {diagram_type}. "
        if title:
            prompt += f"Title: {title}. "
        prompt += "\n\nMermaid code:\n```mermaid\n" + mermaid_code.strip() + "\n```"
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
        # The /transform/md endpoint works anonymously but REQUIRES a stable
        # partnerFlag (official clients send skill_mind_doc_<uuid>). Persist it
        # in the cache so the same install keeps one flag.
        partner_flag = self._get_partner_flag()
        payload: Dict[str, Any] = {
            "title": title,
            "markdown": markdown,
            "structure": structure,
            "source": "skill_mind_documentsummary",
            "partnerFlag": partner_flag,
        }
        if theme:
            payload["theme"] = theme

        url = API_BASE + path
        # This endpoint does not require a Bearer token; do not send one.
        headers = dict(DEFAULT_HEADERS)
        resp = self._session.request(
            method,
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        try:
            data = resp.json()
        except Exception:
            raise ProcessOnError(
                f"Unparseable response (HTTP {resp.status_code}): {resp.text[:300]!r}",
                status_code=resp.status_code,
            )
        # Success shape: {"code":"200","ok":true,"data":{imgUrl,visitUrl}}
        if not isinstance(data, dict) or data.get("ok") is False or str(data.get("code")) not in ("200", "0"):
            raise ProcessOnError(
                f"ProcessOn error: {data.get('message') or data}", body=data
            )
        return data

    def _get_partner_flag(self) -> str:
        """Return a stable partnerFlag, creating and persisting one on first use."""
        if self._cache:
            existing = self._cache.get("partner:flag")
            if existing and isinstance(existing, dict) and existing.get("flag"):
                return existing["flag"]
        flag = "skill_mind_doc_" + uuid.uuid4().hex
        if self._cache:
            self._cache.set("partner:flag", {"flag": flag})
        return flag

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

    # ------------------------------------------------------------------
    # Main-site file management (www.processon.com "我的文件")
    # ------------------------------------------------------------------
    # Separate auth from the AI MCP: account + password (password sent
    # MD5-hashed) -> JWT, sent as the `token` request header. Env:
    # PROCESSON_ACCOUNT, PROCESSON_PASSWORD. Creates folders/charts inside
    # the user's own "我的文件".

    WEB_BASE = "https://www.processon.com"

    def _web_session(self) -> requests.Session:
        if not hasattr(self, "_sess"):
            self._sess = requests.Session()
            self._sess.headers.update({
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
                "Referer": self.WEB_BASE + "/diagrams",
                "Origin": self.WEB_BASE,
            })
        return self._sess

    def web_login(self) -> str:
        account = os.getenv("PROCESSON_ACCOUNT") or os.getenv("PO_ACCOUNT")
        password = os.getenv("PROCESSON_PASSWORD") or os.getenv("PO_PASSWORD")
        if not account or not password:
            raise ProcessOnAuthError(
                "File management needs PROCESSON_ACCOUNT and PROCESSON_PASSWORD "
                "(your Processon web login) in .env or the environment."
            )
        sess = self._web_session()
        md5_pwd = hashlib.md5(password.encode("utf-8")).hexdigest()
        payload = {
            "account": account, "password": md5_pwd,
            "userSource": "register", "businessType": "login",
            "registerType": "phone", "terminalType": "web", "channelType": "po",
        }
        sess.get(self.WEB_BASE + "/", timeout=20)
        resp = sess.post(self.WEB_BASE + "/api/personal/login/v2/account",
                         data=json.dumps(payload),
                         headers={"Content-Type": "application/json"}, timeout=20)
        try:
            data = resp.json()
        except Exception:
            raise ProcessOnError(f"Login unparseable (HTTP {resp.status_code}): {resp.text[:200]!r}",
                                 status_code=resp.status_code)
        if str(data.get("code")) != "200":
            raise ProcessOnAuthError(f"Main-site login failed: {data.get('msg') or data}",
                                     status_code=resp.status_code)
        token = (data.get("data") or {}).get("token")
        if not token:
            raise ProcessOnAuthError("Login succeeded but no token returned.")
        sess.headers.update({"token": token})
        return token

    def _web_call(self, method: str, path: str, *,
                  data: Optional[Dict[str, Any]] = None,
                  params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sess = self._web_session()
        if "token" not in sess.headers:
            self.web_login()
        url = self.WEB_BASE + path
        form = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        resp = sess.request(method, url, data=data, params=params, headers=form, timeout=30)
        try:
            out = resp.json()
        except Exception:
            raise ProcessOnError(f"API {path} unparseable (HTTP {resp.status_code}): {resp.text[:200]!r}",
                                 status_code=resp.status_code)
        if str(out.get("code")) in ("408", "403") and "鉴权" in str(out.get("msg", "")):
            self.web_login()
            resp = sess.request(method, url, data=data, params=params, headers=form, timeout=30)
            out = resp.json()
        if str(out.get("code")) != "200":
            raise ProcessOnError(f"API {path} error: {out.get('msg') or out}", body=out)
        return out.get("data") or {}

    def create_folder(self, title: str, parent_id: str = "root") -> Dict[str, Any]:
        data = self._web_call("POST", "/api/personal/folder/new",
                              data={"title": title, "folderId": parent_id})
        return data.get("folder") or data

    def list_files(self, folder_id: str = "root") -> Dict[str, Any]:
        return self._web_call("GET", "/api/personal/folder/load_files",
                              params={"folderId": folder_id, "sidx": "lastModify",
                                      "sort": "desc", "pageSize": 50, "page": 1})

    def create_chart(self, title: str, folder_id: str = "root",
                     category: str = "flowbase") -> Dict[str, Any]:
        data = self._web_call("POST", "/api/personal/diagraming/create",
                              data={"folderId": folder_id, "category": category})
        chart = data.get("chart") or data
        chart_id = chart.get("chartId")
        if title and chart_id:
            self.rename_chart(chart_id, title)
            chart["title"] = title
        return chart

    def rename_chart(self, chart_id: str, title: str) -> Dict[str, Any]:
        msg = json.dumps([{"action": "changeTitle", "title": title}], ensure_ascii=False)
        return self._web_call(
            "POST",
            f"/api/personal/diagraming/canvas/v2/msg?mlfffid={chart_id}&mlffcid={chart_id}",
            data={"msgStr": msg, "canvasId": chart_id, "chartId": chart_id,
                  "ignore": "msgStr", "msgversion": ""},
        )
