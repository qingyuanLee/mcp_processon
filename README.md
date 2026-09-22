<div align="center">

# processon-mcp

**Turn your own ProcessOn account into an LLM-driven diagramming engine.**

Draw flowcharts, mindmaps and outlines natively — **no paid AI credits, no bitmaps, every shape is editable.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-stdio%2Fhttp-orange.svg)](https://modelcontextprotocol.io/)

</div>

## Why this exists

Most ProcessOn integrations just call the paid "AI generate" endpoint: you burn
credits, get a flat image, and can't tweak it. This project goes deeper — it
drives ProcessOn's **native canvas editor** the same way a human would, so the
LLM itself assembles editable shapes, connectors, mindmap branches and annotations,
and ProcessOn spends **zero AI quota**.

The output is always a **live, editable online diagram** — not a screenshot.

## ✨ Features

- **Zero-quota native drawing** — LLM authors shapes/edges directly onto the
  canvas; no AI consumption, no bitmap.
- **Three editor protocols, reverse-engineered and working:**
  - `flowbase` — flowcharts & architecture: rectangles, decision diamonds,
    terminators, arrows with **4 line styles** (solid/dashed/dot/dashdot) and
    straight (`normal`) or elbow (`broken`) routing, **auto-sized boxes** (long
    labels wrap inside the shape), **layered auto-layout** (Sugiyama-style:
    hierarchy layers + barycenter ordering to cut crossings; every layer is
    centred on the widest band so the spine reads symmetrically), **no-crowding
    guarantee** (boxes are never closer than a hard floor — crowded or
    overlapping inputs are pushed apart), **dashed group containers** (give
    nodes a `group` and the tool wraps them in a dashed transparent frame for
    architecture layers/subsystems, with a light tinted background that
    contrasts the dark node fills), **edges anchor on the frame edge** (correct
    ProcessOn anchor angles — no lines cutting through boxes), elbow routes bend
    in the inter-layer gap, elbow horizontal segments are routed around unrelated
    node/container boxes (cross-layer lines never slice through a box), remaining
    crossing lines get distinct colors and dash styles automatically, **4 built-in themes**, colored fills, hidden
    grid.
  - `outline` — outliner / thinking notes, root-level tree writing.
  - `mind_free` — real mindmaps with auto-colored branches.
- **Mindmap annotations** — `summary` (概要), `boundary` (外框), and
  cross-node `links` (跨节点连线), all verified end-to-end.
- **Full file management** — create folders (idempotent path), list files,
  create/rename charts, **move** charts/folders, **delete to trash**. Building
  blocks are idempotent: `ensure_folder_path` reuses same-named folders,
  `ensure_chart` overwrites a same-named chart (delete + recreate), so rerunning
  never spawns duplicates. Via account login (JWT, auto re-auth on 401/408).
- **Public share links** — `share_chart` opens sharing and mints the
  `https://www.processon.com/view/link/{viewLinkId}` URL (the link id is
  server-assigned, permanent by default, idempotent on re-share). Hand-rolled
  against the private web API — no official SDK.
- **Chart read-back + .vsdx export**
- **Server-side high-res JPG export** — `processon_export_jpg` triggers
  ProcessOn's own export pipeline (`/chart/export/get/user/power`,
  exportType=jpghd), then polls for the KS3 CDN URL and streams the bytes.
  Gives a real 135KB+ PNG-quality JPG without a browser. — `processon_get_chart_def` reads a chart's
  full element JSON back from ProcessOn (`chartdefids` → `chart/def`);
  `processon_export_vsdx` renders it to a standalone **.vsdx (Visio)** file in
  pure Python (no Visio install): rectangles / diamonds / terminators, linker
  edges, group frames, px→inch conversion, y-flip, centered text. Designed for
  Visio / drawio; containers render bottom-most so covered nodes aren't dropped.
- **Dual auth** — `sk-po-...` token for the AI surface, account+password for
  the personal file surface.
- **Standard MCP** — tools over stdio (default) or Streamable HTTP, pluggable
  SQLite cache.

## 🚀 Quick Start

```bash
uv venv && uv pip install -e .      # or: pip install -e .
```

### Credentials

Copy `.env.example` → `.env` and fill in:

```ini
# Drawing (AI surface, Bearer token from https://smart.processon.com/user)
PROCESSON_API_KEY=sk-po-...

# Files / native canvas (your personal account)
PROCESSON_ACCOUNT=your_phone
PROCESSON_PASSWORD=your_password     # MD5-hashed in transit
```

### Run

```bash
processon-mcp                          # stdio — for Doubao / Claude / Cursor
processon-mcp --transport http --port 3100
```

## 🔧 MCP Tools

| Tool | What it does |
|------|--------------|
| `processon_whoami` | Show account + auth status |
| `processon_create_folder` / `processon_list_files` | Organize "My Files" |
| `processon_create_chart` | Create an empty editable chart (`flowbase` / `outline` / `mind_free` / `markdown`) |
| `processon_rename_chart` | Rename a chart |
| `processon_move_file` / `processon_delete_chart` | Move charts/folders, delete chart to trash |
| `processon_draw_flowchart` | **LLM draws native shapes + edges** into a chart — auto-sized boxes, layered auto-layout, crossing auto-recoloring, per-edge color, line styles, straight/elbow links, theme |
| `processon_draw_mindmap` | Outline-style mind notes |
| `processon_make_mindmap` | **Real mindmap** with colored branches + summary / boundary / links |
| `processon_share_chart` | Open public sharing → return `https://www.processon.com/view/link/{viewLinkId}` (permanent by default) |
| `processon_design_diagram` / `processon_render_mermaid` | Mermaid draft → editable render |
| `processon_generate_chart` | One-shot natural-language chart (AI surface) |
| `processon_md_to_mindmap` | Markdown → editable mindmap |
| `processon_get_chart_def` | Read a chart's full element JSON back (chartdefids + chart/def) |
| `processon_export_vsdx` | Read chart def → render to a local `.vsdx` (Visio) file |
| `processon_export_jpg` | Trigger server-side export → stream high-res `.jpg` to local path |

### LLM-led flowchart, end to end

```python
# create_chart → draw_flowchart(nodes, edges, theme="techblue")
nodes=[{"id":"start","label":"开始","shape":"terminator"},
       {"id":"auth","label":"登录","shape":"rectangle"},
       {"id":"ok","label":"校验通过?","shape":"decision"}]
edges=[{"from":"start","to":"auth"},{"from":"auth","to":"ok"}]
# Boxes auto-size to labels; nodes auto-layout into hierarchy layers with
# reduced crossings (omit x/y, or pass auto_layout=True).
# optional per edge: "style": "solid"|"dashed"|"dot"|"dashdot",
#                    "type": "broken"|"normal"  (elbow vs straight),
#                    "color": "#RRGGBB"|"r,g,b"
# Lines that still cross get distinct colors + dash styles automatically.
```

Every shape is a native ProcessOn object — click it in the browser and edit text,
color, geometry, exactly as if you had drawn it by hand.

### Mindmap with annotations

```python
nodes=[
  {"text":"认证体系","summary":"两套认证","boundary":"基础",
   "children":[{"text":"sk-po token"},{"text":"账号密码"}]},
  {"text":"画图能力","children":[
     {"text":"流程图"},
     {"text":"思维导图","links":[{"to":"sk-po token","label":"共用"}]}]},
]
# summary = 概要条, boundary = 外框, links = 跨节点连线
```

## 🧩 Known boundaries (tested)

- Native writing targets **flowbase / outline / mind_free**. `markdown`-type
  files use a collaborative document format with no public write API.
- Mindmap links use a fixed anchor template; extreme layouts may need manual nudging.
- Rendering is async on ProcessOn's side (seconds), timeout 180s.
- `.vsdx` export targets **Visio / drawio** (offline editing/archive). Round-tripping
  the .vsdx *back into* ProcessOn may lose styling, since ProcessOn's own importer
  simplifies shapes and folds groups — that is its importer's behaviour, not an export bug.

## 🏗️ Structure

```
src/processon_mcp/
├── server.py             # MCP tools/resources/prompts
├── processon_client.py   # dual-auth HTTP client + all 3 editor protocols
└── cache/sqlite_cache.py
```

## ⚠️ Disclaimer

Unofficial integration against ProcessOn's private web + AI APIs. Not affiliated
with ProcessOn. Your token and password are secrets — never commit `.env`.

## 📄 License

MIT
