"""processon-mcp CLI entrypoint.

Usage:
    processon-mcp                              # stdio transport (default)
    processon-mcp --transport streamable-http   # Streamable HTTP
    processon-mcp --transport http             # alias for streamable-http
    processon-mcp --transport sse               # SSE
"""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="processon-mcp",
        description="ProcessOn MCP Server — generate editable online diagrams via Model Context Protocol.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse", "streamable-http"],
        default="stdio",
        help="Transport to use (default: stdio). 'http' is an alias for 'streamable-http'.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP/SSE transport.")
    parser.add_argument("--port", type=int, default=3100, help="Port for HTTP/SSE transport (default: 3100).")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    from processon_mcp.server import mcp  # noqa: E402

    transport = "streamable-http" if args.transport == "http" else args.transport
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    elif transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
