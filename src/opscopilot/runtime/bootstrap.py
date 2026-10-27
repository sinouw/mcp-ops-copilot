"""Wire settings into a gateway: remote MCP servers over HTTP, or local stdio subprocesses."""

from __future__ import annotations

from opscopilot.config import Settings
from opscopilot.runtime.gateway import ServerTarget, ToolGateway, stdio_target


def server_targets(settings: Settings) -> dict[str, ServerTarget]:
    env = {
        "DATABASE_URL": settings.database_url,
        "REFUND_APPROVAL_THRESHOLD_CENTS": str(settings.refund_approval_threshold_cents),
        "REFUND_HARD_CAP_CENTS": str(settings.refund_hard_cap_cents),
    }
    return {
        "crm": settings.mcp_crm_url or stdio_target("opscopilot.mcp_servers.crm", env),
        "ticketing": settings.mcp_ticketing_url
        or stdio_target("opscopilot.mcp_servers.ticketing", env),
    }


def build_gateway(settings: Settings) -> ToolGateway:
    return ToolGateway(server_targets(settings))
