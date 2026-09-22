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

import base64
import hashlib
import json
import math
import os
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

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
        # Decode JWT to keep userId/fullName (needed by outline mindmap writes).
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            self._web_user_id = claims.get("userId", "")
            self._web_full_name = claims.get("fullName", "")
        except Exception:
            self._web_user_id, self._web_full_name = "", ""
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

    def ensure_folder_path(self, path: str, root_id: str = "root") -> str:
        """Resolve a /-separated folder path, creating each missing segment.

        Idempotent: reuses an existing same-named child folder instead of
        creating duplicates (ProcessOn allows duplicate names, so we must
        check list_files ourselves). Returns the deepest folderId.
        """
        current = root_id
        for seg in [s for s in path.split("/") if s]:
            listing = self.list_files(current)
            existing = ""
            for f in (listing.get("folders") or listing.get("children")
                      or listing.get("data", {}).get("folders") or []):
                if f.get("title") == seg or f.get("name") == seg:
                    existing = f.get("folderId") or f.get("id") or ""
                    break
            if existing:
                current = existing
            else:
                created = self.create_folder(seg, parent_id=current)
                current = created.get("folderId") or created.get("id")
        return current


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

    def find_chart_by_name(self, title: str, folder_id: str = "root") -> Optional[Dict[str, Any]]:
        """Return an existing same-titled chart in folder_id, or None."""
        listing = self.list_files(folder_id)
        for ch in listing.get("charts") or []:
            if ch.get("title") == title:
                return ch
        return None

    def ensure_chart(self, title: str, folder_id: str = "root",
                    category: str = "flowbase") -> Dict[str, Any]:
        """Upsert a chart: if a same-titled chart exists in folder_id, delete it
        to trash first, then create a fresh one (true overwrite). Returns the new chart."""
        existing = self.find_chart_by_name(title, folder_id)
        if existing:
            self.delete_chart(existing.get("chartId") or existing.get("id"))
        return self.create_chart(title, folder_id=folder_id, category=category)

    def delete_chart(self, chart_id: str, resource: str = "diagrams") -> Dict[str, Any]:
        """Move a chart to trash (recoverable, not permanent)."""
        return self._web_call("POST", "/api/personal/folder/to_trash",
                              data={"fileType": "chart", "fileId": chart_id,
                                    "resource": resource})

    def move_file(self, file_id: str, target_folder_id: str,
                  file_type: str = "chart") -> Dict[str, Any]:
        """Move a chart (file_type=chart) or folder (file_type=folder) under
        target_folderId."""
        return self._web_call("POST", "/api/personal/folder/move",
                              data={"fileType": file_type, "fileId": file_id,
                                    "targetFolderId": target_folder_id})

    # ------------------------------------------------------------------
    # Sharing (generate a public view link) — hand-rolled, no SDK
    # ------------------------------------------------------------------
    # Verified against the live site (2026-09-18):
    #   * The share URL is  https://www.processon.com/view/link/{viewLinkId}
    #   * viewLinkId is a server-minted 24-hex id, NOT derivable from chartId
    #     (a private chart has viewLinkId=null; it is minted when sharing is
    #     opened). So pure URL construction from chartId cannot work.
    #   * Flow:
    #       1. GET  /api/personal/chart/share/open?chartId=<id>
    #              -> mints/returns the viewLinkId   (POST returns 405!)
    #       2. POST /api/personal/view/update_link/<id>  expire=""
    #              -> makes the link permanent (expireTime=null)
    #       3. compose  {WEB_BASE}/view/link/{viewLinkId}

    def share_chart(self, chart_id: str, permanent: bool = True) -> Dict[str, Any]:
        """Open public sharing for a chart and return the share link.

        Returns {"chartId", "viewLinkId", "shareUrl", "permanent", "expireTime"}.
        """
        data = self._web_call("GET", "/api/personal/chart/share/open",
                              params={"chartId": chart_id})
        view_link_id = data.get("viewLinkId")
        expire_time = None
        if permanent and view_link_id:
            d = self._web_call(
                "POST", f"/api/personal/view/update_link/{chart_id}",
                data={"chartId": chart_id, "expire": ""})
            view_link_id = d.get("viewLinkId") or view_link_id
            expire_time = d.get("expireTime")
        if not view_link_id:
            raise ProcessOnError(f"Failed to mint a share link for chart {chart_id}")
        return {
            "chartId": chart_id,
            "viewLinkId": view_link_id,
            "shareUrl": f"{self.WEB_BASE}/view/link/{view_link_id}",
            "permanent": permanent,
            "expireTime": expire_time,
        }

    # ------------------------------------------------------------------
    # Chart definition read-back (diagraming/get/chart/def)
    # ------------------------------------------------------------------

    def get_chart_def(self, chart_id: str) -> Dict[str, Any]:
        """Read a chart back as its full editable definition.

        This is ProcessOn's server-side read-back: resolve the defId
        (canvas/get/chartdefids) then fetch the raw elements JSON
        (diagraming/get/chart/def). Returns:
          {"chartId","defId","meta","elements": {id: shape-or-link}}.

        Note: ProcessOn's "export to .vsdx" runs purely in the browser
        (visio.sdk.umd.js converts the definition client-side; there is no
        server download endpoint). To get a real .vsdx file, open the chart
        in a browser and use its Export -> Visio menu; this method gives
        you the same underlying definition programmatically.
        """
        cd = self._web_call("GET", "/api/personal/canvas/get/chartdefids",
                            params={"chartId": chart_id})
        canvas = cd.get("canvas", {})
        def_id = canvas.get("mainCanvasId") or (
            (canvas.get("chartDefIds") or [{}])[0].get("definitionId"))
        if not def_id:
            raise ProcessOnError(f"No defId found for chart {chart_id}: {cd}")
        raw = self._web_call("GET", "/api/personal/diagraming/get/chart/def",
                             params={"chartId": chart_id, "defId": def_id})
        def_json = raw.get("def")
        elements = {}
        if isinstance(def_json, str):
            try:
                parsed = json.loads(def_json)
                elements = parsed.get("elements", parsed)
            except Exception:
                elements = {"_raw": def_json}
        elif isinstance(def_json, dict):
            elements = def_json.get("elements", def_json)
        return {
            "chartId": chart_id,
            "defId": def_id,
            "meta": cd.get("chart", {}),
            "elements": elements,
        }

    def export_chart_to_vsdx(self, chart_id: str, out_path: str) -> str:
        """Read a chart back and render it to a local .vsdx (Visio) file.

        Combines get_chart_def with vsdx_exporter: no Visio required, pure
        Python. Returns the out_path. Only covers shapes our flowchart tool
        produces (rectangle/diamond/terminator nodes, linker edges, containers).
        """
        from .vsdx_exporter import export_def_to_vsdx
        info = self.get_chart_def(chart_id)
        title = (info.get("meta") or {}).get("title") or "ProcessOn Diagram"
        return export_def_to_vsdx(info["elements"], out_path, page_name=title)

    def export_chart_to_jpg(self, chart_id: str, out_path: str,
                            export_type: str = "jpghd") -> str:
        """Export a chart to a high-res JPG via ProcessOn's server pipeline.

        Two-step (reverse-engineered from the web app):
          1. GET /api/personal/chart/export/get/user/power?chartId=..&exportType=jpghd
             -> returns a task id (data field).
          2. Poll for the KS3 CDN download URL, then stream the bytes.

        export_type: "jpghd" (high-res JPG, VIP) or "jpg" (normal).
        Returns the out_path.
        """
        import time
        import urllib.request

        # step 1: trigger export, get task id
        power = self._web_call(
            "GET", "/api/personal/chart/export/get/user/power",
            params={"chartId": chart_id, "exportType": export_type})
        task_id = power if isinstance(power, str) else (
            power.get("taskId") or power.get("data") or "")
        if not task_id:
            raise ProcessOnError(
                f"export power did not return a task id: {power!r}")

        # step 2: poll for the CDN download URL. ProcessOn's frontend polls a
        # result endpoint; the exact path varies, so we try the known candidates
        # until one returns a usable http(s) URL.
        download_url = ""
        poll_paths = [
            "/api/personal/chart/export/get/result",
            "/api/personal/chart/export/get/poll",
            "/api/personal/chart/export/get/status",
            "/api/personal/chart/export/get/info",
        ]
        deadline = time.time() + 60
        while time.time() < deadline:
            for pp in poll_paths:
                try:
                    d = self._web_call("GET", pp, params={
                        "chartId": chart_id, "exportType": export_type,
                        "taskId": task_id})
                    url = d.get("url") or d.get("fileUrl") or d.get("downloadUrl") or ""
                    if isinstance(url, str) and url.startswith("http"):
                        download_url = url
                        break
                except Exception:
                    continue
            if download_url:
                break
            time.sleep(2)

        if not download_url:
            raise ProcessOnError(
                f"could not resolve download URL for task {task_id} "
                f"(polling endpoint not found); open the chart in a browser "
                f"and use Export -> JPG")

        # step 3: stream bytes to out_path (CDN, no auth needed)
        req = urllib.request.Request(
            download_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        with open(out_path, "wb") as f:
            f.write(data)
        return out_path

    # ------------------------------------------------------------------
    # Canvas drawing (write shapes/links into an editable chart)
    # ------------------------------------------------------------------
    # We build ProcessOn's native "create" messages. A chart's pageId is its
    # definitionId (returned by create_chart). Shape templates are captured from
    # the real web app: rectangle (basic), decision (flow diamond), terminator
    # (flow start/end pill). Links use name="linker" and reference shape ids.

    # Named color themes (RGB triplets as "r,g,b") — replace ProcessOn's paid
    # "AI style optimize" with our own coloring + a setTheme message.
    THEMES = {
        "techblue": {  # the ProcessOn AI default captured from the web app
            "fill": "0,91,153", "font": "183,233,255", "line": "0,67,112",
            "page": "245,245,245", "name": "aiTheme0",
            "container_fill": "224,234,244",
        },
        "cleanemerald": {
            "fill": "46,125,86", "font": "232,245,233", "line": "27,94,32",
            "page": "250,250,250", "name": "emerald",
            "container_fill": "232,242,234",
        },
        "warmorange": {
            "fill": "230,81,0", "font": "255,243,224", "line": "191,54,12",
            "page": "255,248,240", "name": "orange",
            "container_fill": "250,238,224",
        },
        "slatepurple": {
            "fill": "94,53,177", "font": "237,231,246", "line": "74,20,140",
            "page": "245,245,250", "name": "purple",
            "container_fill": "232,228,244",
        },
    }

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------
    # Text sizing & auto layout (layered, crossing-minimised)
    # ------------------------------------------------------------------
    # draw_flowchart used to place nodes on a fixed vertical ladder with fixed
    # box sizes, so long labels overflowed the box and the diagram was just a
    # list. We now (1) size each box from its label's estimated pixel width and
    # (2) run a Sugiyama-style layered layout: layer by longest path (handles
    # cycles), then barycenter ordering to cut edge crossings, then coordinates.
    # Lines that still cross after layout get distinct colors + dash styles so
    # the reader can tell them apart.

    FONT_SIZE = 14            # default node font size (px), used for width math
    LINE_HEIGHT = 1.4         # font_size multiplier per text line
    LINK_PALETTE = [          # "r,g,b" triplets, as ProcessOn expects
        "232,73,59", "46,125,209", "46,158,91", "178,91,184",
        "224,138,46", "139,92,246", "13,148,136", "190,75,120",
    ]
    LAYOUT_GAP_X = 80.0          # horizontal gap between boxes in a layer
    LAYOUT_GAP_Y = 110.0         # vertical gap between layers
    MIN_NODE_GAP = 40.0          # hard floor: no two boxes closer than this
    CONT_PAD = 48.0              # container inner padding around its nodes

    @staticmethod
    def _text_width(text: str, font_size: int = FONT_SIZE) -> float:
        """Rough pixel width of text at the given font size.

        CJK glyphs count 1em, ASCII ~0.55em, punctuation ~0.35em, space 0.3em.
        Good enough for sizing boxes so labels fit; verified visually.
        """
        w = 0.0
        for ch in text:
            o = ord(ch)
            if o > 0x2E7F:                       # CJK / wide glyphs
                w += 1.0
            elif ch == " ":
                w += 0.30
            elif ch in ".,;:!?()[]{}\"'`|/\\-–—_":
                w += 0.35
            else:
                w += 0.55
        return w * font_size

    @staticmethod
    def _node_size(shape: str, label: str,
                   font_size: int = FONT_SIZE) -> Tuple[float, float]:
        """Auto-size a box so `label` fits inside it (multi-line aware).

        Padding differs per shape: rectangle has plain margins, the terminator
        pill loses space to its round caps, the decision diamond's inscribed
        text area is narrower/taller relative to the box.
        """
        if shape == "decision":
            min_w, min_h, w_pad, h_pad = 96.0, 76.0, 34.0, 34.0
        elif shape == "terminator":
            min_w, min_h, w_pad, h_pad = 120.0, 52.0, 48.0, 18.0
        else:
            min_w, min_h, w_pad, h_pad = 120.0, 60.0, 26.0, 18.0
        txt_w = ProcessOnClient._text_width(label, font_size)
        usable_w = max(min_w - w_pad, 40.0)
        lines = max(1, math.ceil(txt_w / usable_w))
        w = max(min_w, txt_w + w_pad)
        h = max(min_h, lines * font_size * ProcessOnClient.LINE_HEIGHT + h_pad)
        return math.ceil(w), math.ceil(h)

    @classmethod
    def _layered_layout(cls, nodes: List[Dict[str, Any]],
                        edges: List[Dict[str, Any]],
                        ) -> Dict[str, Tuple[float, float]]:
        """Assign (x, y) to every node id.

        - Layers: longest-path from the sources (roots). Cycles cannot hang the
          loop because layer[v] is only ever raised to layer[u]+1.
        - Ordering: barycenter sweeps (down then up) reduce edge crossings.
        - Coordinates: each layer is a horizontal band; nodes are placed
          left-to-right with fixed gaps and vertically centred in the band.
        """
        ids = [n["id"] for n in nodes]
        id_set = set(ids)
        succ: Dict[str, List[str]] = {i: [] for i in ids}
        pred: Dict[str, List[str]] = {i: [] for i in ids}
        for e in edges:
            u, v = e.get("from"), e.get("to")
            if u in id_set and v in id_set and u != v and v not in succ[u]:
                succ[u].append(v)
                pred[v].append(u)
        layer = {i: 0 for i in ids}
        indeg = {i: len(pred[i]) for i in ids}
        q = deque(i for i in ids if indeg[i] == 0)
        seen = set()
        while q:
            u = q.popleft()
            seen.add(u)
            for v in succ[u]:
                layer[v] = max(layer[v], layer[u] + 1)
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        # Cycle fallback: any node never reached still gets max(pred)+1.
        for v in ids:
            if v not in seen:
                layer[v] = max([0] + [layer[u] + 1 for u in pred[v] if u in id_set])

        depth = max(layer.values()) + 1
        layers: List[List[str]] = [[] for _ in range(depth)]
        for i in ids:
            layers[layer[i]].append(i)

        # Barycenter ordering — a few down+up sweeps (crossing reduction).
        for _ in range(4):
            for li in range(1, depth):
                ref = {u: idx for idx, u in enumerate(layers[li - 1])}
                def bary_down(u: str) -> float:
                    ps = [ref[p] for p in pred[u] if p in ref]
                    return sum(ps) / len(ps) if ps else float("inf")
                layers[li].sort(key=bary_down)
            for li in range(depth - 2, -1, -1):
                ref = {u: idx for idx, u in enumerate(layers[li + 1])}
                def bary_up(u: str) -> float:
                    ns = [ref[s] for s in succ[u] if s in ref]
                    return sum(ns) / len(ns) if ns else float("inf")
                layers[li].sort(key=bary_up)

        sizes = {n["id"]: cls._node_size(n.get("shape", "rectangle"),
                                         n.get("label", n.get("id", "")))
                 for n in nodes}

        # Pass 1: measure every band width (node w + gap). Each band is then
        # centred on the widest band — this is what gives the chart a vertical
        # spine and a symmetric look instead of a ragged left-aligned stack.
        band_ws: List[float] = []
        for band in layers:
            bw = 0.0
            for i in band:
                bw += sizes[i][0] + cls.LAYOUT_GAP_X
            band_ws.append(max(0.0, bw - cls.LAYOUT_GAP_X))
        page_w = max(band_ws) if band_ws else 0.0

        pos: Dict[str, Tuple[float, float]] = {}
        y = 60.0
        for li, band in enumerate(layers):
            band_h = max(sizes[i][1] for i in band) if band else 0.0
            x = 60.0 + (page_w - band_ws[li]) / 2.0   # centre this band
            for i in band:
                w, h = sizes[i]
                pos[i] = (x, y + (band_h - h) / 2)
                x += w + cls.LAYOUT_GAP_X
            y += band_h + cls.LAYOUT_GAP_Y
        return pos

    @staticmethod
    def _segments_intersect(p1: Tuple[float, float], p2: Tuple[float, float],
                            p3: Tuple[float, float], p4: Tuple[float, float]) -> bool:
        """True when segments p1-p2 and p3-p4 properly cross."""
        def ccw(a, b, c):
            return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
        d1, d2 = ccw(p3, p4, p1), ccw(p3, p4, p2)
        d3, d4 = ccw(p1, p2, p3), ccw(p1, p2, p4)
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

    @staticmethod
    def _count_crossings(placed: Dict[str, Dict[str, Any]],
                         edges: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
        """Return [(i, j), ...] edge-index pairs whose connectors cross.

        Connectors are the straight lines from src bottom-centre to dst
        top-centre — the geometry actually sent to ProcessOn for broken links
        with two points (and a good proxy for normal ones).
        """
        pts = []
        for e in edges:
            a, b = placed.get(e.get("from")), placed.get(e.get("to"))
            if a and b:
                pa, pb = a["props"], b["props"]
                pts.append(((pa["x"] + pa["w"] / 2, pa["y"] + pa["h"]),
                            (pb["x"] + pb["w"] / 2, pb["y"])))
            else:
                pts.append(None)
        pairs = []
        for i in range(len(pts)):
            if pts[i] is None:
                continue
            for j in range(i + 1, len(pts)):
                if pts[j] is None:
                    continue
                if ProcessOnClient._segments_intersect(
                        pts[i][0], pts[i][1], pts[j][0], pts[j][1]):
                    pairs.append((i, j))
        return pairs

    @staticmethod
    def _rects_gap_ok(a: Dict[str, float], b: Dict[str, float],
                      min_gap: float = MIN_NODE_GAP) -> bool:
        """True when the two boxes are at least `min_gap` apart on some axis."""
        gx = max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"])
        gy = max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"])
        return gx >= min_gap or gy >= min_gap

    @classmethod
    def _separate_rects(cls, rects: List[Dict[str, float]],
                        min_gap: float = MIN_NODE_GAP,
                        skip_pairs=None, max_iters: int = 80) -> int:
        """Greedily push overlapping / too-close rects apart (in place).

        rects: [{"id", "x", "y", "w", "h"}, ...]. skip_pairs: set of
        (id1, id2) pairs never separated (e.g. a node and its own container).
        Returns the number of remaining violations (0 = clean).
        """
        skip = skip_pairs or set()
        for _ in range(max_iters):
            bad = 0
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    a, b = rects[i], rects[j]
                    if (a["id"], b["id"]) in skip or (b["id"], a["id"]) in skip:
                        continue
                    if cls._rects_gap_ok(a, b, min_gap):
                        continue
                    bad += 1
                    # horizontal/vertical separation (negative = overlap)
                    gx = max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"])
                    gy = max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"])
                    need_x = min_gap - gx
                    need_y = min_gap - gy
                    if need_x <= need_y:
                        # push along x: move whichever is to the right, rightward
                        if a["x"] <= b["x"]:
                            b["x"] += need_x
                        else:
                            a["x"] += need_x
                    else:
                        if a["y"] <= b["y"]:
                            b["y"] += need_y
                        else:
                            a["y"] += need_y
            if bad == 0:
                return 0
        return bad

    @staticmethod
    def _to_rgb(color: str) -> str:
        """Normalise a user color to ProcessOn's "r,g,b" triplet form."""
        c = color.strip()
        if c.startswith("#"):
            c = c.lstrip("#")
            if len(c) == 3:
                c = "".join(ch * 2 for ch in c)
            return f"{int(c[0:2], 16)},{int(c[2:4], 16)},{int(c[4:6], 16)}"
        return c  # already "r,g,b"

    def _shape(self, name: str, category: str, title: str,
               x: float, y: float, w: float, h: float,
               path: list, zindex: int = 1,
               colors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        fill = {"color": colors["fill"]} if colors else {}
        line = {"lineWidth": 1.5}
        if colors:
            line["lineColor"] = colors["line"]
        font = {"color": colors["font"]} if colors else {}
        return {
            "id": self._new_id(), "name": name, "title": title, "category": category,
            "group": "", "groupName": None, "locked": False, "link": "",
            "children": [], "parent": "",
            "resizeDir": ["tl", "tr", "br", "bl", "l", "t", "r", "b"],
            "attribute": {"container": False, "visible": True, "rotatable": True,
                          "linkable": True, "collapsable": False, "collapsed": False,
                          "fixedLink": False, "markerOffset": 5},
            "dataAttributes": [],
            "props": {"x": x, "y": y, "w": w, "h": h, "zindex": zindex, "angle": 0},
            "shapeStyle": {"alpha": 1}, "lineStyle": line,
            "fillStyle": fill, "theme": {}, "path": path, "fontStyle": font,
            "textBlock": [{"position": {"x": 10, "y": 0, "w": "w-20", "h": "h"},
                           "text": title}],
            "anchors": [{"x": "w/2", "y": "0"}, {"x": "w/2", "y": "h"},
                        {"x": "0", "y": "h/2"}, {"x": "w", "y": "h/2"}],
        }

    def make_node(self, shape: str, title: str, x: float, y: float,
                  zindex: int = 1,
                  colors: Optional[Dict[str, str]] = None,
                  w: Optional[float] = None, h: Optional[float] = None) -> Dict[str, Any]:
        """Build one flowchart node. shape in {rectangle, decision, terminator}.

        Defaults keep the historical fixed sizes; pass w/h for text-aware sizing.
        """
        if shape == "decision":
            return self._shape("decision", "flow", title, x, y, w or 90, h or 70,
                [{"actions": [
                    {"action": "move", "x": "0", "y": "h/2"},
                    {"action": "line", "x": "w/2", "y": "0"},
                    {"action": "line", "x": "w", "y": "h/2"},
                    {"action": "line", "x": "w/2", "y": "h"},
                    {"action": "line", "x": "0", "y": "h/2"},
                    {"action": "close", "y": "0"}]}], zindex, colors)
        if shape == "terminator":
            return self._shape("terminator", "flow", title, x, y, w or 120, h or 52,
                [{"actions": [
                    {"action": "move", "x": "Math.min(w,h)/3", "y": "0"},
                    {"action": "line", "x": "w-Math.min(w,h)/3", "y": "0"},
                    {"action": "curve", "x": "w-Math.min(w,h)/3", "y": "h",
                     "x1": "w+Math.min(w,h)/3/3", "x2": "w+Math.min(w,h)/3/3",
                     "y1": "0", "y2": "h"},
                    {"action": "line", "x": "Math.min(w,h)/3", "y": "h"},
                    {"action": "curve", "x": "Math.min(w,h)/3", "y": "0",
                     "x1": "-Math.min(w,h)/3/3", "x2": "-Math.min(w,h)/3/3",
                     "y1": "h", "y2": "0"},
                    {"action": "close"}]}], zindex, colors)
        # default rectangle
        return self._shape("rectangle", "basic", title, x, y, w or 120, h or 60,
            [{"actions": [
                {"action": "move", "x": "0", "y": "0"},
                {"action": "line", "x": "w", "y": "0"},
                {"action": "line", "x": "w", "y": "h"},
                {"action": "line", "x": "0", "y": "h"},
                {"action": "close", "y": "0"}]}], zindex, colors)

    def make_container(self, title: str, x: float, y: float, w: float, h: float,
                      zindex: int = 0, colors: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A dashed transparent group frame (architecture "大虚线框").

        Built by reusing _shape("rectangle", "basic", ...) so every required
        field (path, anchors, resizeDir, attribute...) is present — exactly the
        captured node shape. Only the stroke is dashed and the fill is emptied.
        Missing "path" is what previously crashed the frontend (white loop).
        """
        shape = self._shape("rectangle", "basic", title, x, y, w, h,
            [{"actions": [
                {"action": "move", "x": "0", "y": "0"},
                {"action": "line", "x": "w", "y": "0"},
                {"action": "line", "x": "w", "y": "h"},
                {"action": "line", "x": "0", "y": "h"},
                {"action": "close", "y": "0"}]}],
            zindex=zindex, colors=None)
        shape["lineStyle"] = {"lineWidth": 1.5, "lineStyle": "dashed",
                              "drawType": "none"}
        # Light tinted background so the container reads as a "layer" behind
        # the dark filled nodes — clear chromatic separation.
        if colors and "container_fill" in colors:
            shape["fillStyle"] = {"color": colors["container_fill"]}
        else:
            shape["fillStyle"] = {}
        return shape

    def make_link(self, src: Dict[str, Any], dst: Dict[str, Any],
                  label: str = "", zindex: int = 4,
                  colors: Optional[Dict[str, str]] = None,
                  link_type: str = "broken",
                  line_style: str = "") -> Dict[str, Any]:
        """Build a linker between src's bottom-center and dst's top-center.

        link_type: "broken" (elbow/折线, default) or "normal" (straight/直连).
        line_style: "" (solid, default), "dashed", "dot" or "dashdot" —
                    maps to lineStyle.lineStyle + drawType:"none", matching
                    the captured web-app payloads.
        """
        # Anchor on the node *edge*, not the centre, so the connector touches
        # the frame instead of crossing through it. Angle is the direction from
        # the node centre to this anchor, in ProcessOn's math coords (y up):
        #   bottom = -pi/2 = 3pi/2, top = +pi/2, right = 0, left = pi.
        # From a captured payload: src bottom-anchor angle was exactly 3pi/2.
        sx_c = src["props"]["x"] + src["props"]["w"] / 2
        sy_c = src["props"]["y"] + src["props"]["h"] / 2
        dx_c = dst["props"]["x"] + dst["props"]["w"] / 2
        dy_c = dst["props"]["y"] + dst["props"]["h"] / 2
        # Decide sides by relative centre position (layered layout is vertical).
        if dy_c > sy_c + 4:                       # dst below src
            sx, sy = sx_c, src["props"]["y"] + src["props"]["h"]
            from_angle = 3 * math.pi / 2
            dx, dy = dx_c, dst["props"]["y"]
            to_angle = math.pi / 2
        elif dy_c < sy_c - 4:                     # dst above src
            sx, sy = sx_c, src["props"]["y"]
            from_angle = math.pi / 2
            dx, dy = dx_c, dst["props"]["y"] + dst["props"]["h"]
            to_angle = 3 * math.pi / 2
        else:                                     # same row: left / right
            if dx_c >= sx_c:
                sx, sy = src["props"]["x"] + src["props"]["w"], sy_c
                from_angle = 0.0
                dx, dy = dst["props"]["x"], dy_c
                to_angle = math.pi
            else:
                sx, sy = src["props"]["x"], sy_c
                from_angle = math.pi
                dx, dy = dst["props"]["x"] + dst["props"]["w"], dy_c
                to_angle = 0.0
        line = {"lineWidth": 1.5}
        if colors:
            line["lineColor"] = colors["line"]
        if line_style in ("dashed", "dot", "dashdot"):
            line["lineStyle"] = line_style
            line["drawType"] = "none"
        linker = {
            "id": self._new_id(), "name": "linker", "text": label, "group": "",
            "linkerType": link_type if link_type in ("normal", "broken") else "broken",
            "points": [], "locked": False, "dataAttributes": [],
            "props": {"zindex": zindex},
            "lineStyle": line,
            "from": {"x": sx, "y": sy, "id": src["id"], "angle": from_angle},
            "to": {"id": dst["id"], "x": dx, "y": dy, "angle": to_angle},
            "textBlock": [],
        }
        if link_type == "broken":
            # Elbow path: drop from src's edge to a lane in the gap just below
            # src (not the midpoint, which lands on a middle layer's boxes),
            # run horizontally to dst's x, then drop to dst's edge.
            span = dy - sy
            mid_y = sy + min(30.0, span * 0.25) if span > 0 else sy
            linker["points"] = [{"x": sx, "y": mid_y},
                                {"x": dx, "y": mid_y}]
        return linker

    def _avoid_boxes_on_link(self, link: Dict[str, Any],
                             boxes: List[Dict[str, Any]]) -> None:
        """Slide a broken-elbow's horizontal segment to the first y that does NOT
        cross any box other than the connector's own endpoints. Boxes = nodes +
        container frames. Works on the two middle points of the elbow."""
        if link.get("linkerType") != "broken" or len(link.get("points", [])) < 2:
            return
        from_id = link.get("from", {}).get("id")
        to_id = link.get("to", {}).get("id")
        p0, p1 = link["points"][0], link["points"][1]
        x_lo = min(p0["x"], p1["x"])
        x_hi = max(p0["x"], p1["x"])

        def crosses(y: float) -> bool:
            for b in boxes:
                bid = b.get("id")
                if bid == from_id or bid == to_id:
                    continue
                bp = b.get("props", b)
                bx, by, bw, bh = bp["x"], bp["y"], bp["w"], bp["h"]
                # horizontal line at y overlaps this box vertically AND horizontally
                if by <= y <= by + bh and bx <= x_hi and bx + bw >= x_lo:
                    return True
            return False

        y = p0["y"]
        step = 20.0
        # scan down first (elbows usually route downward between layers)
        for _ in range(40):
            if not crosses(y):
                break
            y += step
        else:
            y = p0["y"]
            for _ in range(40):
                if not crosses(y):
                    break
                y -= step
        p0["y"] = y
        p1["y"] = y

    def _set_theme_message(self, colors: Dict[str, str], page_id: str) -> Dict[str, Any]:
        """Build a setTheme message matching the captured ProcessOn AI theme."""
        shape = {"fontStyle": {"color": colors["font"]},
                 "fillStyle": {"color": colors["fill"]},
                 "lineStyle": {"lineColor": colors["line"]}}
        linker = {"fontStyle": {"color": colors["line"]},
                  "lineStyle": {"lineColor": colors["line"]}}
        page = {"backgroundColor": colors["page"]}
        update = {"shape": shape, "linker": linker, "page": page,
                  "name": colors.get("name", "customTheme"),
                  "colors": [{"shape": shape, "linker": linker, "page": page}]}
        return {"action": "setTheme",
                "content": {"theme": {}, "update": update},
                "pageId": page_id}

    def _update_page_message(self, colors: Dict[str, str], page_id: str,
                             width: int = 1200, height: int = 800) -> Dict[str, Any]:
        """Set page background + hide grid (captured from the web app)."""
        attrs = {"padding": 20, "backgroundColor": colors["page"],
                 "orientation": "portrait", "gridSize": 15, "width": width,
                 "showGrid": False, "lineJumps": False, "height": height}
        return {"action": "updatePage",
                "content": {"page": {**attrs, "backgroundColor": "transparent",
                                     "showGrid": True},
                            "update": attrs},
                "pageId": page_id}

    def draw_flowchart(self, chart_id: str, page_id: str,
                       nodes: list, edges: list,
                       theme: str = "",
                       auto_layout: bool = True) -> Dict[str, Any]:
        """Draw a flowchart into an existing chart with text-aware sizing and
        an automatic layered layout.

        nodes: [{"id": str, "label": str, "shape": "rectangle|decision|terminator",
                 "x"?: float, "y"?: float,
                 "group"?: str, "container"?: bool}]
               x/y are optional: omit them (or pass auto_layout=True) to get a
               layered layout that minimizes edge crossings. Boxes auto-size so
               long labels stay inside the shape.
               Grouping for architecture diagrams: give nodes a "group" (e.g.
               "接入层") and the tool draws a dashed transparent container frame
               around each group; declare a frame explicitly with
               {"id","label","container":True} to set its title. Lines still
               anchor on node edges. No two boxes are ever closer than
               MIN_NODE_GAP — overlapping / crowded inputs are pushed apart.
        edges: [{"from": node_id, "to": node_id, "label"?: str,
                 "style"?: "solid"|"dashed"|"dot"|"dashdot",
                 "type"?: "broken"|"normal",
                 "color"?: "#RRGGBB" | "r,g,b"}]
               style: line dash pattern (default solid); type: elbow broken
               (default) vs straight normal; color: explicit line color.
               Edges that still cross after layout are automatically recolored
               (LINK_PALETTE) and re-dashed so the reader can tell them apart.
        theme: optional preset name — techblue / cleanemerald / warmorange /
               slatepurple. Colours shapes, hides the grid, sets a page background
               (replaces the paid ProcessOn "AI style optimize"). Empty = default.
        auto_layout: False keeps the caller's x/y as-is (no layering, no
                     auto-sizing of positions; boxes still auto-size to labels).
        """
        colors = self.THEMES.get(theme) if theme else None
        plain = [n for n in nodes if not n.get("container")]
        cont_titles = {n["id"]: n.get("label", n["id"])
                       for n in nodes if n.get("container")}
        explicit = all(n.get("x") is not None and n.get("y") is not None
                       for n in plain)
        use_auto = auto_layout and not explicit

        if use_auto:
            pos = self._layered_layout(plain, edges)

        placed: Dict[str, Dict[str, Any]] = {}
        for step, n in enumerate(plain):
            shp = n.get("shape", "rectangle")
            label = n.get("label", n.get("id", ""))
            w, h = self._node_size(shp, label)
            if use_auto:
                x, y = pos[n["id"]]
            else:
                x = n.get("x", 200)
                y = n.get("y", 80 + step * 150)
            node = self.make_node(shp, label, x, y, zindex=step + 1,
                                  colors=colors, w=w, h=h)
            placed[n["id"]] = node

        # --- group nodes into dashed container frames ---
        groups: Dict[str, List[str]] = {}
        for n in plain:
            g = n.get("group")
            if g:
                groups.setdefault(str(g), []).append(n["id"])

        node_rects = [{"id": n["id"], **placed[n["id"]]["props"]} for n in plain]
        cont_rects: List[Dict[str, float]] = []
        for g, ids in groups.items():
            if not ids:
                continue
            xs = [placed[i]["props"] for i in ids]
            x0 = min(v["x"] for v in xs) - self.CONT_PAD
            y0 = min(v["y"] for v in xs) - self.CONT_PAD
            x1 = max(v["x"] + v["w"] for v in xs) + self.CONT_PAD
            y1 = max(v["y"] + v["h"] for v in xs) + self.CONT_PAD
            tw = self._text_width(cont_titles.get(g, str(g)))
            cont_rects.append({"id": "cont:" + str(g), "x": x0, "y": y0,
                               "w": max(x1 - x0, tw + 60.0), "h": y1 - y0})

        # Two-stage separation:
        # 1) containers apart from each other — member nodes move along with
        #    their frame, so the inner padding never breaks;
        # 2) plain nodes apart from each other (crowded/overlapping inputs).
        if cont_rects:
            orig = {c["id"]: (c["x"], c["y"]) for c in cont_rects}
            self._separate_rects(cont_rects, min_gap=self.MIN_NODE_GAP)
            for c in cont_rects:
                dx = c["x"] - orig[c["id"]][0]
                dy = c["y"] - orig[c["id"]][1]
                if dx or dy:
                    for i in groups.get(c["id"][5:], []):
                        p_ = placed[i]["props"]
                        p_["x"] += dx
                        p_["y"] += dy
                        for r in node_rects:
                            if r["id"] == i:
                                r["x"] += dx
                                r["y"] += dy
        self._separate_rects(node_rects, min_gap=self.MIN_NODE_GAP)
        for r in node_rects:
            placed[r["id"]]["props"]["x"] = r["x"]
            placed[r["id"]]["props"]["y"] = r["y"]

        containers = [self.make_container(
            cont_titles.get(cr["id"][5:], cr["id"][5:]),
            cr["x"], cr["y"], cr["w"], cr["h"], colors=colors)
            for cr in cont_rects]

        shapes = containers + [v for v in placed.values()]
        links = []
        for e in edges:
            a = placed.get(e.get("from")); b = placed.get(e.get("to"))
            if a and b:
                links.append(self.make_link(
                    a, b, e.get("label", ""), colors=colors,
                    link_type=e.get("type", "broken"),
                    line_style=e.get("style", "")))
        # Route elbow horizontal segments around unrelated node/container boxes,
        # so a connector never slices through a box it has nothing to do with.
        for link in links:
            self._avoid_boxes_on_link(link, list(placed.values()) + containers)
        # Edges crossing after layout → distinct color + dash per edge, so a
        # reader can still follow each one (user's explicit style/color wins).
        crossing = self._count_crossings(placed, edges)
        involved: List[int] = []
        for i, j in crossing:
            for k in (i, j):
                if k not in involved:
                    involved.append(k)
        involved.sort()
        for slot, ei in enumerate(involved):
            e = edges[ei]
            if e.get("style") or e.get("color"):
                continue
            link = links[ei]
            link["lineStyle"]["lineColor"] = self.LINK_PALETTE[
                slot % len(self.LINK_PALETTE)]
            dash = ["dashed", "dot", "dashdot"][
                (slot // len(self.LINK_PALETTE)) % 3]
            link["lineStyle"]["lineStyle"] = dash
            link["lineStyle"]["drawType"] = "none"
        # Explicit per-edge color overrides (r,g,b or #hex).
        for ei, e in enumerate(edges):
            c = e.get("color")
            if c:
                links[ei]["lineStyle"]["lineColor"] = self._to_rgb(c)

        content = shapes + links
        messages = [{"action": "create", "content": content, "pageId": page_id}]
        if colors:
            bb_w = max((v["props"]["x"] + v["props"]["w"] for v in placed.values()),
                       default=0)
            bb_h = max((v["props"]["y"] + v["props"]["h"] for v in placed.values()),
                       default=0)
            messages.append(self._update_page_message(
                colors, page_id,
                width=max(1200, int(bb_w) + 80),
                height=max(800, int(bb_h) + 80)))
            messages.append(self._set_theme_message(colors, page_id))
        msg = [{"action": "command", "messages": messages,
                "name": "", "pageId": page_id}]
        return self._web_call(
            "POST",
            f"/api/personal/diagraming/canvas/v2/msg?mlfffid={chart_id}&mlffcid={chart_id}",
            data={"msgStr": json.dumps(msg, ensure_ascii=False), "canvasId": chart_id,
                  "chartId": chart_id, "ignore": "msgStr", "msgversion": ""},
        )

    # ------------------------------------------------------------------
    # Outline / mindmap (tree editor, NOT the shape canvas)
    # ------------------------------------------------------------------
    # Separate editor: POST /api/personal/outline/canvas/msg. A chart created
    # with category=outline has a fixed root node id="root". Nodes are added
    # with action="add" (parent = parent node id), titled with action="update"
    # (key="title"). Parent is set directly at add time — no indent needed.

    def create_outline_chart(self, title: str, folder_id: str = "root") -> Dict[str, Any]:
        """Create an empty mindmap (outline) chart in My Files."""
        return self.create_chart(title, folder_id=folder_id, category="outline")

    def write_mindmap(self, chart_id: str, page_id: str,
                      root_title: str, nodes: list,
                      theme: str = "bg_caihong",
                      structure: str = "mind_right") -> Dict[str, Any]:
        """Write a tree into an outline chart.

        nodes: [{"text": str, "children": [ ... ]}] — the root's direct children.
        Recursively adds each node (parent = its parent node id) and titles it.
        """
        uid = getattr(self, "_web_user_id", "") or ""
        full = getattr(self, "_web_full_name", "") or ""
        root_node = {"freeChildren": [], "root": True, "theme": theme, "id": "root",
                     "title": root_title, "version": 0, "structure": structure,
                     "leftChildren": [], "todoList": {}}
        actions = [{"action": "update",
                    "content": {"key": "title", "nodes": [dict(root_node)],
                                "oldNodes": [dict(root_node, title="")],
                                "userId": uid, "fullName": full},
                    "pageId": page_id}]

        def walk(children: list, parent_id: str) -> None:
            for i, ch in enumerate(children):
                nid = self._new_id()
                actions.append({"action": "add", "add-data": {},
                                "content": [{"id": nid, "title": "",
                                             "children": [], "parent": parent_id}],
                                "indexs": {nid: i}, "parts": {}, "updates": {},
                                "original": {}, "pageId": page_id})
                actions.append({"action": "update",
                                "content": {"key": "title",
                                            "nodes": [{"id": nid, "title": ch.get("text", ""),
                                                       "parent": parent_id}],
                                            "oldNodes": [{"id": nid, "title": "",
                                                          "parent": parent_id}],
                                            "userId": uid, "fullName": full},
                                "pageId": page_id})
                walk(ch.get("children", []), nid)

        walk(nodes, "root")
        return self._web_call(
            "POST",
            f"/api/personal/outline/canvas/msg?mlfffid={chart_id}&mlffcid={chart_id}",
            data={"msgStr": json.dumps(actions, ensure_ascii=False), "canvasId": chart_id,
                  "chartId": chart_id, "ignore": "msgStr", "msgversion": ""},
        )

    # ------------------------------------------------------------------
    # Mindmap (mind_free editor) — the real mind map, NOT the outline editor
    # ------------------------------------------------------------------
    # Separate editor: POST /api/personal/mindmap/canvas/msg. A chart created
    # with category=mind_free already has a root node. Nodes are made with
    # action="create" (content.content[], index, newPart, right-content-index);
    # the title is set directly at create time (no separate update needed).

    MIND_COLORS = ["#729B8D", "#EED484", "#E19873", "#DFE8D7"]

    def create_mindmap_chart(self, title: str, folder_id: str = "root") -> Dict[str, Any]:
        """Create an empty mindmap (mind_free) chart in My Files."""
        return self.create_chart(title, folder_id=folder_id, category="mind_free")

    def write_mindmap_tree(self, chart_id: str, page_id: str,
                           root_title: str, nodes: list) -> Dict[str, Any]:
        """Write a tree into a mind_free chart.

        nodes: [{"text": str, "children": [...],
                 "summary": "optional summary over this node's children",
                 "boundary": "optional boundary label around this node",
                 "links": [{"to": "another node's text", "label": "..."}]}]
        """
        actions = [{"action": "changeTitle", "title": root_title, "editorVersion": "V2"}]
        text_to_id: Dict[str, str] = {}
        deferred_links: list = []

        def walk(children: list, parent_id: str, depth: int,
                 sibling_counter: dict) -> None:
            for i, ch in enumerate(children):
                nid = str(uuid.uuid4())
                item = {"children": [], "id": nid, "title": ch.get("text", ""),
                        "parent": parent_id}
                if depth == 0:
                    item["lineStyle"] = {"randomLineColor": self.MIND_COLORS[
                        sibling_counter.get(parent_id, 0) % len(self.MIND_COLORS)]}
                actions.append({"action": "create", "content": {
                    "content": [item], "index": {nid: i}, "updates": {}, "original": {},
                    "newPart": {nid: "right"},
                    "add-data": {"updateTopicList": []},
                    "right-content-index": i + 1},
                    "pageId": page_id})
                sibling_counter[parent_id] = sibling_counter.get(parent_id, 0) + 1
                text_to_id[ch.get("text", "")] = nid

                grandchildren = ch.get("children", [])
                walk(grandchildren, nid, depth + 1, sibling_counter)

                # summary over this node's children
                if ch.get("summary"):
                    sid = str(uuid.uuid4())
                    actions.append({"action": "addSummary", "content": {
                        "content": [{"parent": nid, "range": f"0,{len(grandchildren)}",
                                     "children": [], "id": sid,
                                     "title": ch["summary"], "summary": True}]},
                        "pageId": page_id})
                # boundary around this node (covers just this sibling)
                if ch.get("boundary"):
                    bid = str(uuid.uuid4())
                    actions.append({"action": "addBoundary", "content": {
                        "content": [{"parent": parent_id, "range": f"{i},1",
                                     "children": [], "id": bid,
                                     "boundary": True}]},
                        "pageId": page_id})
                # cross-node links (resolved after the tree exists)
                for lk in ch.get("links", []):
                    deferred_links.append((nid, lk))

        walk(nodes, "root", 0, {})

        # resolve cross-node connections by target text; use full template
        # (start/end anchors + angles, real coords zeroed — client re-lays out).
        for from_id, lk in deferred_links:
            to_id = text_to_id.get(lk.get("to", ""))
            if not to_id:
                continue
            cid = str(uuid.uuid4())
            conn = {
                "from": from_id, "to": to_id,
                "end": {"x": "0.7", "y": "0.0", "index": 2},
                "startAngle": 135.8793622579657, "endAngle": 315.8793622579657,
                "points": [], "styles": {}, "label": lk.get("label", ""), "pts": [],
                "id": cid,
                "start": {"x": "0.2", "y": "1.0", "index": 4},
                "realEnd": {"x": 0, "y": 0}, "realStart": {"x": 0, "y": 0},
            }
            actions.append({"action": "addConnection",
                            "content": {"content": [conn]},
                            "pageId": page_id, "editorVersion": "V2"})
        result = self._web_call(
            "POST",
            f"/api/personal/mindmap/canvas/msg?mlfffid={chart_id}&mlffcid={chart_id}",
            data={"msgStr": json.dumps(actions, ensure_ascii=False), "canvasId": chart_id,
                  "chartId": chart_id, "ignore": "msgStr", "msgversion": "v6"},
        )
        result["node_ids"] = text_to_id
        return result
