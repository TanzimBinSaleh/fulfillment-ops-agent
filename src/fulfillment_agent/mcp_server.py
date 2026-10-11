"""MCP server exposing the same tools to any MCP client (Claude Desktop, Claude Code, ...).

Run:  fulfillment-agent mcp      (stdio transport)

The client's own tool-permission prompt is the human approval step here, so
propose_substitution writes a pending_approval row once the user allows the call.
"""

from __future__ import annotations

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
except ImportError:  # 1.x, where the same class was called FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError as MCPToolError

from . import tools
from .config import get_settings
from .db import describe_schema
from .policy import POLICY_TEXT

mcp = MCPServer(
    "fulfillment-ops",
    instructions=(
        "Tools for a grocery store's online pickup department. Read the fulfillment://schema and "
        "fulfillment://policy resources first. run_sql is read-only. Check substitutes against the policy "
        "before calling propose_substitution."
    ),
)

_ctx = tools.ToolContext(settings=get_settings(), approve=lambda _: True)


def _wrap(fn, **kwargs) -> dict:
    try:
        return fn(_ctx, **kwargs)
    except tools.ToolError as e:
        # Raised as the SDK's ToolError so the message reaches the client
        # (and the model) as an is_error result it can act on.
        raise MCPToolError(str(e)) from e


@mcp.tool()
def run_sql(query: str) -> dict:
    """Run one read-only PostgreSQL SELECT against the store database. Max 200 rows returned."""
    return _wrap(tools.run_sql, query=query)


@mcp.tool()
def find_substitute_candidates(order_item_id: int) -> dict:
    """Ordered product, customer dietary preference and in-stock same-subcategory candidates
    (not filtered by policy) for one order item."""
    return _wrap(tools.find_substitute_candidates, order_item_id=order_item_id)


@mcp.tool()
def propose_substitution(order_item_id: int, proposed_sku: str, reason: str, customer_message: str) -> dict:
    """Save a policy-checked substitution proposal (status pending_approval) with a draft customer SMS."""
    return _wrap(tools.propose_substitution, order_item_id=order_item_id, proposed_sku=proposed_sku,
                 reason=reason, customer_message=customer_message)


@mcp.resource("fulfillment://schema")
def schema() -> str:
    """Database schema with column notes."""
    return describe_schema(_ctx.settings)


@mcp.resource("fulfillment://policy")
def substitution_policy() -> str:
    """The store's substitution policy."""
    return POLICY_TEXT


@mcp.prompt()
def substitution_sweep(hours_ahead: int = 2) -> str:
    """Work through every out-of-stock item in orders due soon."""
    return (f"Find every order item that needs attention in orders whose pickup slot starts in the next "
            f"{hours_ahead} hours. For each, find candidates, apply the substitution policy, and propose the best "
            f"compliant substitute with a customer text. Summarise what you proposed and what will be removed.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
