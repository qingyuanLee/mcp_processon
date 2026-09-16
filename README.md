# processon-mcp

**ProcessOn MCP Server** — turn your own ProcessOn permanent account into a
Model Context Protocol tool that generates editable online diagrams for AI hosts
(Doubao, Claude, Cursor, ...).

It wraps [ProcessOn AI](https://smart.processon.com) so an AI can draw flowcharts,
architecture diagrams, mindmaps, UML, timelines and more on demand, and hand back
both a preview image and an **editable online link**.

> Architecture follows the same pattern as [mcp-mubu](https://github.com/liuboacean/mubu-integration):
> a thin MCP Python SDK layer on top of an HTTP API client, with a pluggable
> SQLite cache.

## ✨ Features

- **Natural language → diagram**: describe an idea, get a professional editable chart
- **Markdown → mindmap**: turn notes/outlines into an editable mindmap
- **30+ diagram types**: flowcharts, swimlanes, sequence, architecture, ER, org, timeline, infographic …
- **Editable output, not dead images**: every chart returns a ProcessOn editor link
- **Your own account**: uses your ProcessOn permanent account via a personal API token
- **MCP standard**: tools + resources + prompts over stdio (default) or HTTP
- **Zero-config cache**: SQLite at `~/.processon-mcp/cache.db`

## 🚀 Quick Start

### Install

```bash
# With uv (recommended)
uv venv
uv pip install -e .

# Or plain pip
pip install -e .
```

### Get a token

1. Open https://smart.processon.com/user
2. Create an access token (looks like `sk-po-...`) and copy it.

### Set credentials

Copy `.env.example` to `.env` in the project root and fill it in (loaded
automatically), or export the variable:

```
PROCESSON_API_KEY=sk-po-your-token
```

### Run

```bash
processon-mcp                              # stdio (default — for Doubao/Claude)
processon-mcp --transport http --port 3100   # Streamable HTTP
processon-mcp -v                           # debug logging
```

## 🔧 MCP Tools

| Tool | Description |
|------|-------------|
| `processon_whoami` | Show current ProcessOn auth status |
| `processon_design_diagram` | Turn an idea into a Mermaid diagram definition (first draft / planning) |
| `processon_render_mermaid` | Render a Mermaid you authored into an editable ProcessOn diagram |
| `processon_generate_chart` | One-shot: natural-language prompt -> editable diagram |
| `processon_md_to_mindmap` | Convert Markdown into an editable mindmap |
| `processon_cache_info` | Show cache backend info |
| `processon_cache_clear` | Clear cached data |

### LLM-led diagramming workflow (0 -> 1 -> 100)

ProcessOn has no low-level "add node / add edge" editing API, so the LLM owns
the diagram through **Mermaid source** and ProcessOn only renders it:

1. `processon_design_diagram` — get a Mermaid skeleton for an idea.
2. The LLM edits the Mermaid directly (add/remove nodes, edges, labels).
3. `processon_render_mermaid` — render the edited Mermaid into a professional
   editable online chart; get preview image + editable link.
4. Repeat 2–3 to iterate toward the final diagram.

One Mermaid grammar covers flowcharts, architecture / network-deployment,
mindmaps, sequence, ER, class, timeline and C4 diagrams.

### MCP Resources

| Resource | Description |
|----------|-------------|
| `processon://status` | Auth and cache status |

### MCP Prompts

| Prompt | Description |
|--------|-------------|
| `processon_setup_guide` | Step-by-step configuration guide |

## 🧩 Doubao (豆包) connector

See [`processon-mcp-连接器配置指南.md`](./processon-mcp-连接器配置指南.md)
for how to register this server as a Doubao custom connector.

## 🏗️ Project Structure

```
processon-mcp/
├── pyproject.toml
├── README.md
└── src/
    └── processon_mcp/
        ├── __init__.py
        ├── __main__.py            # CLI entrypoint
        ├── server.py              # MCP server (tools, resources, prompts)
        ├── processon_client.py    # ProcessOn HTTP / JSON-RPC client
        ├── processon_config.py    # Config, constants, error types
        └── cache/
            ├── __init__.py        # Cache factory
            ├── base.py            # CacheBackend ABC
            └── sqlite_cache.py    # Built-in SQLite backend
```

## ⚠️ Notes

- This is an unofficial integration built on ProcessOn's hosted AI API.
- Diagram generation is async on ProcessOn's side; the server waits for the
  result (default timeout 180s).
- The personal API token is equivalent to a password — keep it secret, never
  commit `.env`.

## 📄 License

MIT
