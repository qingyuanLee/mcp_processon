"""ProcessOn MCP Server — expose ProcessOn AI diagram generation via MCP.

Lets an AI host turn natural-language ideas into editable online diagrams
(flowcharts, architecture diagrams, mindmaps, UML, timelines ...) and turn
Markdown into mindmaps, using the user's own ProcessOn permanent account.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Optional

from mcp.server import MCPServer

from processon_mcp.cache import get_cache_backend
from processon_mcp.cache.base import CacheBackend
from processon_mcp.processon_client import ProcessOnClient
from processon_mcp.processon_config import (
    ALLOWED_STRUCTURES,
    ProcessOnAuthError,
    ProcessOnError,
)

# ---------------------------------------------------------------------------
# Initialise MCP server
# ---------------------------------------------------------------------------

mcp = MCPServer("processon-mcp")

# ---------------------------------------------------------------------------
# Lazy-initialised shared objects
# ---------------------------------------------------------------------------

_cache: Optional[CacheBackend] = None
_client: Optional[ProcessOnClient] = None


def _get_cache() -> CacheBackend:
    global _cache
    if _cache is None:
        _cache = get_cache_backend()
    return _cache


def _get_client() -> ProcessOnClient:
    global _client
    if _client is None:
        _client = ProcessOnClient(cache=_get_cache())
    return _client


def _auto_title() -> str:
    return "po-mcp-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Helpers: normalise the rich result the hosted service returns
# ---------------------------------------------------------------------------


def _format_chart_result(result: dict) -> str:
    """Render a generate_chart / md_to_mindmap result into a readable text block."""
    parts: list[str] = []

    def pick(*keys: str) -> str:
        for k in keys:
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    img = pick("imgUrl", "previewUrl", "imageUrl", "rawImgUrl")
    visit = pick("visitUrl", "editUrl", "url", "rawVisitUrl")
    message = pick("message", "msg", "content")

    if message:
        parts.append(message)

    if img:
        parts.append(f"\n预览图（可直接打开）:\n{img}")
    if visit:
        parts.append(
            f"\n在线编辑（可复制粘贴二次编辑）:\n{visit}\n"
            "把生成的 DSL/内容粘到这个链接即可继续编辑。"
        )

    # Fall back: dump raw JSON so nothing is silently lost
    if not parts:
        return json.dumps(result, ensure_ascii=False, indent=2)

    # Attach raw links verbatim when present but not yet surfaced
    extra = {k: v for k, v in result.items() if isinstance(v, str) and v.startswith("http") and v not in (img, visit)}
    if extra:
        parts.append("\n其他链接:\n" + json.dumps(extra, ensure_ascii=False, indent=2))
    return "\n".join(parts)


# ===================================================================
# TOOLS
# ===================================================================


@mcp.tool()
def processon_whoami() -> str:
    """Show current ProcessOn authentication status.

    Reads PROCESSON_API_KEY (or cached OAuth token). Does not make a network call.
    """
    client = _get_client()
    status = client.auth_status()
    backend = type(_get_cache()).__name__
    if status["authenticated"]:
        return f"Authenticated with ProcessOn (token: {status['token_masked']}), cache_backend={backend}"
    return (
        "Not authenticated. Set PROCESSON_API_KEY in .env or the environment. "
        "Create a token at https://smart.processon.com/user"
    )


@mcp.tool()
def processon_generate_chart(
    prompt: str,
    chart_type: str = "",
) -> str:
    """Generate an editable online diagram from a natural-language description.

    Turn an idea, process, or structure into a professional ProcessOn diagram.
    Supports flowcharts / swimlane diagrams, sequence diagrams, software &
    cloud architecture diagrams, ER diagrams, org charts, timelines,
    infographics and more. Returns a preview image and an editable link.

    Use this whenever the user wants to "画个图 / 流程图 / 架构图 / 思维导图 /
    visualization / create a diagram". If the chart type is ambiguous, ask first.

    Args:
        prompt: A clear description of the diagram. Include the goal, the key
                nodes/steps/entities, decision points, and any required labels.
                Write in the user's language; professional layout will be applied.
        chart_type: Optional hint, e.g. "flowchart", "sequence", "architecture",
                    "er", "org", "timeline", "infographic". Helps the model pick
                    the right style. Leave empty to let the model decide.
    """
    client = _get_client()
    full_prompt = prompt.strip()
    if chart_type:
        full_prompt = f"[{chart_type.strip()}] {full_prompt}"
    try:
        result = client.generate_chart(full_prompt)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请在 https://smart.processon.com/user 创建 API Token 并填入 PROCESSON_API_KEY。"
    except ProcessOnError as e:
        return f"生成图表失败：{e.msg}"
    return _format_chart_result(result)


@mcp.tool()
def processon_design_diagram(
    prompt: str,
    diagram_type: str = "",
) -> str:
    """Turn an idea into a **Mermaid diagram definition (DSL)** — the planning /
    first-draft step of LLM-led diagramming.

    Use this FIRST when you want to design a diagram from scratch: it returns
    editable Mermaid source code (not a rendered image). You — the LLM — then
    read it, refine it, add/remove nodes and edges, and finally hand the edited
    Mermaid to `processon_render_mermaid` to get a real editable ProcessOn chart.

    Supported diagrams map to Mermaid types: flowchart/architecture/network
    deployment (graph TD/LR), mind map (mindmap), sequence (sequenceDiagram),
    ER model (erDiagram), class diagram (classDiagram), timeline, C4, etc.

    This is the recommended workflow for "from 0 to 1 to 100":
      1. design_diagram  -> get a Mermaid skeleton
      2. edit the Mermaid yourself (iterate nodes/edges/labels)
      3. render_mermaid   -> get preview image + editable link
      4. repeat 2-3 until the diagram is right.

    Args:
        prompt: What the diagram should describe (goal, entities, steps,
                decisions, relationships). Write in the user's language.
        diagram_type: Hint for the target shape, e.g. "flowchart",
                      "mindmap", "sequence", "er", "architecture",
                      "network-deployment", "timeline". Leave empty to infer.
    """
    client = _get_client()
    full_prompt = prompt.strip()
    if diagram_type:
        full_prompt = f"[{diagram_type.strip()}] {full_prompt}"
    try:
        dsl = client.generate_diagram_dsl(full_prompt)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请在 https://smart.processon.com/user 创建 API Token 并填入 PROCESSON_API_KEY。"
    except ProcessOnError as e:
        return f"生成图结构草稿失败：{e.msg}"
    return (
        "以下是 ProcessOn 给出的 Mermaid 草稿。你（LLM）可以直接修改它：增删节点、改连线、补标签，"
        "然后调用 processon_render_mermaid 渲染成可编辑图。\n\n"
        + dsl
    )


@mcp.tool()
def processon_render_mermaid(
    mermaid_code: str,
    title: str = "",
    diagram_type: str = "",
) -> str:
    """Render a Mermaid definition YOU wrote into an editable ProcessOn diagram.

    This is the "edit → render" step of LLM-led diagramming. Give it the full
    Mermaid source code (your own, or refined from processon_design_diagram).
    ProcessOn renders it verbatim into a professional, editable online diagram
    and returns a preview image + an editable link.

    The LLM fully owns the diagram content here — iterate the Mermaid text and
    call this again to get the next version. Supports the same shapes as Mermaid:
    flowcharts, architecture / network deployment, mindmaps, sequence, ER,
    class, timeline, C4, etc.

    Args:
        mermaid_code: The complete Mermaid source (e.g. starts with
                      `graph TD`, `mindmap`, `sequenceDiagram`, `erDiagram`).
        title: Optional diagram title.
        diagram_type: Optional shape hint, e.g. "flowchart", "mindmap",
                      "sequence", "er", "architecture", "network-deployment".
    """
    client = _get_client()
    try:
        result = client.render_mermaid(
            mermaid_code=mermaid_code, title=title, diagram_type=diagram_type
        )
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请配置 PROCESSON_API_KEY。"
    except ProcessOnError as e:
        return f"Mermaid 渲染失败：{e.msg}。请检查 Mermaid 语法是否正确。"
    return _format_chart_result(result)


@mcp.tool()
def processon_md_to_mindmap(
    markdown: str,
    title: str = "",
    structure: str = "mind_free",
) -> str:
    """Convert Markdown text into an editable ProcessOn mindmap.

    Parses headings and bullet lists in the Markdown and renders them as an
    editable online mindmap. Great for turning meeting notes, an outline, or a
    document summary into a visual mindmap.

    Args:
        markdown: The Markdown content (headings + bullet lists).
        title: Mindmap title. Auto-generated as "po-mcp-<timestamp>" when empty.
        structure: Layout style. One of:
                   mind_free (自由), mind_right (向右), mind_org (组织),
                   mind_ishikawa_left (鱼骨), mind_timeline_h (时间轴),
                   mind_tree_free (树), mind_treeTable_left_title (树表).
    """
    client = _get_client()
    title = title.strip() or _auto_title()
    if structure not in ALLOWED_STRUCTURES:
        structure = "mind_free"
    try:
        result = client.md_to_mindmap(title=title, markdown=markdown, structure=structure)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请配置 PROCESSON_API_KEY。"
    except ProcessOnError as e:
        return f"Markdown 转思维导图失败：{e.msg}"
    data = result.get("data", result) if isinstance(result, dict) else {}
    return _format_chart_result(data)


@mcp.tool()
def processon_create_folder(
    title: str,
    parent_folder_id: str = "root",
) -> str:
    """Create a folder inside the user's ProcessOn "我的文件" (My Files).

    Use this to organize generated diagrams into project directories, e.g.
    create "mcp_processon" once, then put all related charts inside it.

    Args:
        title: Folder name, e.g. "mcp_processon".
        parent_folder_id: Parent folder id; "root" (default) puts it at top level.
    """
    client = _get_client()
    try:
        folder = client.create_folder(title, parent_id=parent_folder_id)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请在 .env 配置 PROCESSON_ACCOUNT / PROCESSON_PASSWORD。"
    except ProcessOnError as e:
        return f"创建文件夹失败：{e.msg}"
    return (f"已创建文件夹：{folder.get('title')}\n"
            f"folderId={folder.get('folderId')}\n"
            f"parentId={folder.get('parentId')}")


@mcp.tool()
def processon_list_files(folder_id: str = "root") -> str:
    """List charts and sub-folders inside a ProcessOn "我的文件" folder.

    Args:
        folder_id: Folder id to list. Use "root" for top level (default).
    """
    client = _get_client()
    try:
        data = client.list_files(folder_id)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请配置 PROCESSON_ACCOUNT / PROCESSON_PASSWORD。"
    except ProcessOnError as e:
        return f"列文件失败：{e.msg}"
    lines = [f"文件夹 {folder_id} 内容："]
    for f in data.get("folders", []):
        lines.append(f"  [文件夹] {f.get('title')}  (folderId={f.get('folderId')})")
    for c in data.get("charts", []):
        lines.append(f"  [图表]   {c.get('title')}  (chartId={c.get('chartId')})")
    if not data.get("folders") and not data.get("charts"):
        lines.append("  (空)")
    return "\n".join(lines)


@mcp.tool()
def processon_create_chart(
    title: str,
    folder_id: str = "root",
    category: str = "flowbase",
) -> str:
    """Create a blank editable chart directly in "我的文件", already titled.

    This writes into the user's own file tree (not a temporary third-party
    share). Use it to archive/keep a chart in a folder. The chart is blank —
    for AI-authored content, use processon_render_mermaid to generate it,
    then open the editable link to move it in.

    Args:
        title: Chart title, e.g. "po_mcp-v1".
        folder_id: Folder to put it in (default "root").
        category: Diagram type. "flowbase" (flowchart, default), "mind",
                  "umltable", "network" etc. Leave as default for a blank flowchart.
    """
    client = _get_client()
    try:
        chart = client.create_chart(title, folder_id=folder_id, category=category)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请配置 PROCESSON_ACCOUNT / PROCESSON_PASSWORD。"
    except ProcessOnError as e:
        return f"创建图表失败：{e.msg}"
    chart_id = chart.get("chartId")
    return (f"已在我的文件创建图表：{chart.get('title')}\n"
            f"chartId={chart_id}\n"
            f"pageId={chart.get('definitionId')}\n"
            f"打开：{client.WEB_BASE}/diagraming/{chart_id}")


@mcp.tool()
def processon_draw_flowchart(
    chart_id: str,
    page_id: str,
    nodes: list,
    edges: list,
    theme: str = "",
) -> str:
    """Draw a flowchart DIRECTLY into an existing ProcessOn chart in "我的文件".

    This is the LLM-authoring step: you describe the nodes and edges and they
    are rendered as native, editable ProcessOn shapes (not a flat image).
    First create the chart with processon_create_chart (it returns chartId and
    pageId=definitionId), then call this to populate it.

    Args:
        chart_id: Target chart id (from processon_create_chart).
        page_id:  Target page id = the chart's definitionId (from create_chart).
        nodes: List of nodes, each {"id": str, "label": str,
               "shape": "rectangle"|"decision"|"terminator",
               "x"?: float, "y"?: float}. Omit x/y for automatic vertical layout.
        edges: List of edges, each {"from": <node id>, "to": <node id>,
               "label"?: str}. Draws a downward arrow between them.
        theme: Visual style, applied client-side (does NOT consume ProcessOn's
               paid AI style tokens). One of: "techblue" (the ProcessOn AI
               default), "cleanemerald", "warmorange", "slatepurple".
               Empty = plain default colours.

    Example:
        nodes=[{"id":"a","label":"开始","shape":"terminator"},
               {"id":"b","label":"校验登录","shape":"decision"},
               {"id":"c","label":"查询数据库","shape":"rectangle"}]
        edges=[{"from":"a","to":"b"},{"from":"b","to":"c","label":"通过"}]
        theme="techblue"
    """
    client = _get_client()
    try:
        data = client.draw_flowchart(chart_id, page_id, nodes, edges, theme=theme)
    except ProcessOnAuthError as e:
        return f"认证失败：{e.msg}。请配置 PROCESSON_ACCOUNT / PROCESSON_PASSWORD。"
    except ProcessOnError as e:
        return f"画图失败：{e.msg}"
    themed = f"，应用主题 {theme}" if theme else ""
    return (f"已向图表写入 {len(nodes)} 个节点、{len(edges)} 条连线{themed}。\n"
            f"打开查看：{client.WEB_BASE}/diagraming/{chart_id}")


@mcp.tool()
def processon_cache_info() -> str:
    """Show cache backend info and whether a token is stored."""
    cache = _get_cache()
    backend = type(cache).__name__
    has_token = cache.exists("auth:token")
    return f"Cache backend: {backend}\nToken cached: {'yes' if has_token else 'no'}"


@mcp.tool()
def processon_cache_clear(prefix: str = "") -> str:
    """Clear cached data. Optionally clear only keys with a given prefix.

    Args:
        prefix: Key prefix to clear (empty = clear everything).
    """
    cache = _get_cache()
    count = cache.clear(prefix)
    return f"Cleared {count} cache entries" + (f" with prefix '{prefix}'" if prefix else "") + "."


# ===================================================================
# RESOURCES
# ===================================================================


@mcp.resource("processon://status")
def resource_status() -> str:
    """Current ProcessOn authentication and cache status."""
    client = _get_client()
    status = client.auth_status()
    if status["authenticated"]:
        return f"ProcessOn authenticated (token {status['token_masked']})"
    return "Not authenticated. Set PROCESSON_API_KEY or run processon_generate_chart after configuring it."


# ===================================================================
# PROMPTS
# ===================================================================


@mcp.prompt()
def processon_setup_guide() -> str:
    """Step-by-step guide to configure the ProcessOn MCP server."""
    return """## ProcessOn MCP Setup Guide

### 1. Get a personal API token
Open https://smart.processon.com/user, create an access token (looks like
`sk-po-...`) and copy it.

### 2. Put the credential in .env (or environment)
```
PROCESSON_API_KEY=sk-po-your-token
```

### 3. Run the server
```
processon-mcp                 # stdio (default, for MCP hosts like Doubao/Claude)
processon-mcp --transport http --port 3100
```

### 4. Use the tools
Ask the AI to:
- "画一个用户登录流程图"
- "把这段 Markdown 生成思维导图"
- "画一个微服务架构图：网关、用户服务、订单服务、MySQL、Redis"
"""
