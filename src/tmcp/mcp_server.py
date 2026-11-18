"""The MCP server: tools, resources, prompt, and the filter that shows each agent only its own tools (MCP-03)."""

from typing import Any

from mcp.server import CacheHint
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.types import ListResourcesResult, ListToolsResult
from pydantic import AnyHttpUrl

from tmcp import __version__
from tmcp.config import Settings
from tmcp.policy.engine import Permissions
from tmcp.tools import read_tools, resources, write_tools
from tmcp.tools.runtime import current_permissions

INSTRUCTIONS = """\
This server gives governed access to the Titanic passenger database (891 passengers, 1912).
Start with the resource titanic://me (your permissions) and the tool list_tables.
Use the table 'passengers' for most questions. Use aggregate_passengers for statistics.
The dataset 'reference' never changes. The dataset 'working' changes only through change requests
that a second agent approves. Each error has a stable 'code' and a 'hint' that tells you what to do."""


def _resource_visible(perms: Permissions, uri: str) -> bool:
    need = resources.RESOURCE_PERMISSIONS.get(uri)
    return need is None or perms.can(need)


async def visibility_middleware(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    """MCP-03: tools/list and resources/list show only what the agent may use. The chain returns the wire dict."""
    result = await call_next(ctx)
    if ctx.method not in ("tools/list", "resources/list") or result is None:
        return result
    perms = current_permissions()
    if isinstance(result, ListToolsResult):
        result.tools = [t for t in result.tools if perms.can(t.name)]
    elif isinstance(result, ListResourcesResult):
        result.resources = [r for r in result.resources if _resource_visible(perms, str(r.uri))]
    elif isinstance(result, dict) and ctx.method == "tools/list":
        result["tools"] = [t for t in result.get("tools", []) if perms.can(t["name"])]
    elif isinstance(result, dict):
        result["resources"] = [r for r in result.get("resources", []) if _resource_visible(perms, str(r["uri"]))]
    return result


def build_mcp_server(settings: Settings, verifier: TokenVerifier) -> MCPServer:
    private = CacheHint(ttl_ms=30_000, scope="private")  # lists differ for each agent
    server = MCPServer(
        name="titanic-mcp",
        title="Titanic MCP Server",
        version=__version__,
        instructions=INSTRUCTIONS,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.jwt_issuer or settings.resource_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            validate_token_resource=False,  # JwtTokenVerifier validates 'aud' itself (AUTH-02)
        ),
        cache_hints={"tools/list": private, "resources/list": private},
        middleware=[visibility_middleware],
        warn_on_duplicate_tools=True,
    )
    read_tools.register(server)
    write_tools.register(server)
    resources.register(server)
    return server
