"""CLI entry shared by the MCP servers: `python -m opscopilot.mcp_servers.crm --http`."""

from __future__ import annotations

import argparse
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings


def run_server(server: MCPServer, *, default_port: int) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP, not stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument(
        "--allowed-hosts",
        default=os.environ.get("MCP_ALLOWED_HOSTS", ""),
        help="comma-separated Host headers to accept (DNS-rebinding protection), e.g. crm-mcp:*",
    )
    args = parser.parse_args()
    if not args.http:
        server.run("stdio")
        return
    security = None
    if args.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in args.allowed_hosts.split(",") if h.strip()],
        )
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        transport_security=security,
    )
