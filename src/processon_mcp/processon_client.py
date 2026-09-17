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
        },
        "cleanemerald": {
            "fill": "46,125,86", "font": "232,245,233", "line": "27,94,32",
            "page": "250,250,250", "name": "emerald",
        },
        "warmorange": {
            "fill": "230,81,0", "font": "255,243,224", "line": "191,54,12",
            "page": "255,248,240", "name": "orange",
        },
        "slatepurple": {
            "fill": "94,53,177", "font": "237,231,246", "line": "74,20,140",
            "page": "245,245,250", "name": "purple",
        },
    }

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:16]

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
                  colors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Build one flowchart node. shape in {rectangle, decision, terminator}."""
        if shape == "decision":
            return self._shape("decision", "flow", title, x, y, 90, 70,
                [{"actions": [
                    {"action": "move", "x": "0", "y": "h/2"},
                    {"action": "line", "x": "w/2", "y": "0"},
                    {"action": "line", "x": "w", "y": "h/2"},
                    {"action": "line", "x": "w/2", "y": "h"},
                    {"action": "line", "x": "0", "y": "h/2"},
                    {"action": "close", "y": "0"}]}], zindex, colors)
        if shape == "terminator":
            return self._shape("terminator", "flow", title, x, y, 120, 52,
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
        return self._shape("rectangle", "basic", title, x, y, 120, 60,
            [{"actions": [
                {"action": "move", "x": "0", "y": "0"},
                {"action": "line", "x": "w", "y": "0"},
                {"action": "line", "x": "w", "y": "h"},
                {"action": "line", "x": "0", "y": "h"},
                {"action": "close", "y": "0"}]}], zindex, colors)

    def make_link(self, src: Dict[str, Any], dst: Dict[str, Any],
                  label: str = "", zindex: int = 4,
                  colors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Build a broken arrow from src node's bottom-center to dst's top-center."""
        sx = src["props"]["x"] + src["props"]["w"] / 2
        sy = src["props"]["y"] + src["props"]["h"]
        dx = dst["props"]["x"] + dst["props"]["w"] / 2
        dy = dst["props"]["y"]
        line = {"lineWidth": 1.5}
        if colors:
            line["lineColor"] = colors["line"]
        return {
            "id": self._new_id(), "name": "linker", "text": label, "group": "",
            "linkerType": "broken", "points": [{"x": sx, "y": sy}, {"x": dx, "y": dy}],
            "locked": False, "dataAttributes": [], "props": {"zindex": zindex},
            "lineStyle": line,
            "from": {"x": sx, "y": sy, "id": src["id"], "angle": 0},
            "to": {"id": dst["id"], "x": dx, "y": dy, "angle": 3.1415926535897936},
            "textBlock": [],
        }

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
                       theme: str = "") -> Dict[str, Any]:
        """Draw a simple flowchart into an existing chart.

        nodes: [{"id": str, "label": str, "shape": "rectangle|decision|terminator",
                 "x"?: float, "y"?: float}]
        edges: [{"from": node_id, "to": node_id, "label"?: str}]
        theme: optional preset name — techblue / cleanemerald / warmorange /
               slatepurple. Colours shapes, hides the grid, sets a page background
               (replaces the paid ProcessOn "AI style optimize"). Empty = default.
        """
        colors = self.THEMES.get(theme) if theme else None
        placed: Dict[str, Dict[str, Any]] = {}
        step = 0
        for n in nodes:
            shp = n.get("shape", "rectangle")
            h = 70 if shp == "decision" else 52 if shp == "terminator" else 60
            w = 90 if shp == "decision" else 120
            x = n.get("x", 200)
            y = n.get("y", 80 + step * 150)
            node = self.make_node(shp, n.get("label", n.get("id", "")),
                                  x, y, zindex=step + 1, colors=colors)
            placed[n["id"]] = node
            step += 1
        shapes = [v for v in placed.values()]
        links = []
        for e in edges:
            a = placed.get(e["from"]); b = placed.get(e["to"])
            if a and b:
                links.append(self.make_link(a, b, e.get("label", ""), colors=colors))
        content = shapes + links
        messages = [{"action": "create", "content": content, "pageId": page_id}]
        if colors:
            messages.append(self._update_page_message(colors, page_id))
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
