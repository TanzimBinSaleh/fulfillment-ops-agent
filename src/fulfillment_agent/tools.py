"""Tool implementations shared by the Claude agent loop and the MCP server.

Each tool returns a JSON-serialisable dict. Failures raise ToolError, which the
agent loop sends back to Claude as a tool_result with is_error=true so the
model can read the message and correct itself (e.g. fix a SQL typo).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import psycopg

from . import policy
from .config import OPS_NOW, Settings
from .db import QueryRejected, connect, run_readonly_query


class ToolError(Exception):
    pass


# A human decides whether an agent-written change goes through.
ApprovalFn = Callable[[dict], bool]


def deny_all(_: dict) -> bool:
    return False


@dataclass
class ToolContext:
    settings: Settings
    approve: ApprovalFn = deny_all
    dry_run: bool = False                 # evals: validate and log, never write
    proposal_log: list[dict] = field(default_factory=list)
    sql_log: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- tool bodies

def run_sql(ctx: ToolContext, query: str) -> dict:
    try:
        result = run_readonly_query(ctx.settings, query)
    except QueryRejected as e:
        ctx.sql_log.append({"query": query, "ok": False, "error": str(e)})
        raise ToolError(str(e)) from e
    except psycopg.Error as e:
        msg = (e.diag.message_primary if getattr(e, "diag", None) and e.diag.message_primary else str(e)).strip()
        hint = getattr(getattr(e, "diag", None), "message_hint", None)
        ctx.sql_log.append({"query": query, "ok": False, "error": msg})
        raise ToolError(f"PostgreSQL error: {msg}" + (f" (hint: {hint})" if hint else "")) from e
    ctx.sql_log.append({"query": query, "ok": True, "row_count": len(result.rows)})
    return result.to_payload()


def find_substitute_candidates(ctx: ToolContext, order_item_id: int, limit: int = 12) -> dict:
    sub = policy.load_context(ctx.settings, order_item_id)
    if sub is None:
        raise ToolError(f"order_item_id {order_item_id} not found")
    # Deliberately NOT filtered by the dietary or price rules: applying the
    # written policy is Claude's job, and the eval measures how well it does.
    with connect(ctx.settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT {policy.PRODUCT_COLS}, i.on_hand, i.aisle
            FROM products JOIN inventory i USING (sku)
            WHERE subcategory = %s AND sku <> %s AND i.on_hand > 0
            ORDER BY (brand = %s) DESC, abs(size_value - %s), price
            LIMIT %s
        """, (sub.original["subcategory"], sub.original["sku"], sub.original["brand"],
              sub.original["size_value"], limit))
        rows = cur.fetchall()
    candidates = []
    for r in rows:
        c = policy._product_row(r[:-2])
        c["on_hand"], c["aisle"] = r[-2], r[-1]
        candidates.append(c)
    return {
        "order_item": {
            "order_item_id": sub.order_item_id,
            "quantity": sub.quantity,
            "allow_substitution": sub.allow_substitution,
            "pick_status": sub.pick_status,
            "original_on_hand": sub.original_on_hand,
            "needs_substitution": policy.needs_substitution(sub),
        },
        "customer_dietary_preference": sub.dietary_preference,
        "original_product": sub.original,
        "in_stock_candidates_same_subcategory": candidates,
    }


def propose_substitution(ctx: ToolContext, order_item_id: int, proposed_sku: str, reason: str,
                         customer_message: str) -> dict:
    sub = policy.load_context(ctx.settings, order_item_id)
    if sub is None:
        raise ToolError(f"order_item_id {order_item_id} not found")
    found = policy.load_product(ctx.settings, proposed_sku)
    if found is None:
        raise ToolError(f"SKU {proposed_sku} not found")
    candidate, on_hand = found
    broken = policy.violations(sub, candidate, on_hand)

    entry = {
        "order_item_id": order_item_id, "proposed_sku": proposed_sku, "reason": reason,
        "customer_message": customer_message, "violations": broken, "status": None,
    }
    ctx.proposal_log.append(entry)

    if broken:
        entry["status"] = "blocked_by_policy"
        raise ToolError("Proposal refused by policy check: " + "; ".join(broken))
    if ctx.dry_run:
        entry["status"] = "dry_run_ok"
        return {"status": "accepted (dry run, nothing written)"}
    if not ctx.approve({**entry, "original": sub.original, "substitute": candidate}):
        entry["status"] = "rejected_by_manager"
        return {"status": "rejected_by_manager",
                "note": "A manager rejected this proposal. Do not retry the same SKU; ask what they prefer."}

    with connect(ctx.settings.rw_url) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO substitution_proposals
                (order_item_id, proposed_sku, reason, customer_message, status, created_by, created_at)
            VALUES (%s, %s, %s, %s, 'pending_approval', 'agent', %s)
            RETURNING proposal_id
        """, (order_item_id, proposed_sku, reason, customer_message, datetime.now(OPS_NOW.tzinfo)))
        proposal_id = cur.fetchone()[0]
        conn.commit()
    entry["status"] = "saved"
    entry["proposal_id"] = proposal_id
    return {"status": "saved", "proposal_id": proposal_id,
            "note": "Saved as pending_approval; the customer text is queued for the pickup lead to send."}


# ------------------------------------------------------- schemas for Claude

TOOL_SPECS: list[dict] = [
    {
        "name": "run_sql",
        "description": (
            "Run ONE read-only PostgreSQL SELECT (or WITH ... SELECT) against the store database and get rows back "
            "as JSON. Use it to answer questions about orders, inventory, customers, pick status and substitutions. "
            "Results are capped at 200 rows (truncated=true when cut off), so aggregate or LIMIT in SQL rather than "
            "pulling raw rows. Write statements are rejected. If you get an error, read it, fix the query, and retry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A single PostgreSQL SELECT statement."}},
            "required": ["query"],
        },
    },
    {
        "name": "find_substitute_candidates",
        "description": (
            "For one order_item_id, return the ordered product, the customer's dietary preference, whether the item "
            "allows and needs substitution, and in-stock products from the same subcategory. The candidates are NOT "
            "filtered by the substitution policy: you must check each against every policy rule yourself before "
            "proposing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_item_id": {"type": "integer"}},
            "required": ["order_item_id"],
        },
    },
    {
        "name": "propose_substitution",
        "description": (
            "Propose a substitute SKU for an order item and draft the text message to the customer. A policy check "
            "runs first and refuses non-compliant proposals with the reasons. A human manager then approves or "
            "rejects it; nothing is sent to the customer automatically. Call this at most once per order item unless "
            "it was refused. Do NOT call it for items with allow_substitution = false."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_item_id": {"type": "integer"},
                "proposed_sku": {"type": "string"},
                "reason": {"type": "string", "description": "One sentence: why this substitute, for the manager."},
                "customer_message": {
                    "type": "string",
                    "description": "SMS to the customer, under 320 characters, friendly, names the original and the "
                                   "substitute with sizes and price, and says they can decline it at pickup.",
                },
            },
            "required": ["order_item_id", "proposed_sku", "reason", "customer_message"],
        },
    },
]

FINAL_ANSWER_SPEC: dict = {
    "name": "final_answer",
    "description": (
        "Finish the task. Call this exactly once, as your last action, with your answer for the store manager. "
        "If a single SQL query's result is what answers the question, put that query in `sql`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "Concise answer in plain English. Lead with the result."},
            "sql": {"type": ["string", "null"], "description": "The SELECT whose result answers the question, or null."},
        },
        "required": ["answer", "sql"],
    },
}

DISPATCH = {
    "run_sql": run_sql,
    "find_substitute_candidates": find_substitute_candidates,
    "propose_substitution": propose_substitution,
}


def call_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if fn is None:
        raise ToolError(f"Unknown tool: {name}")
    try:
        inspect.signature(fn).bind(ctx, **args)
    except TypeError as e:  # wrong/missing arguments from the model
        raise ToolError(f"Bad arguments for {name}: {e}") from e
    return fn(ctx, **args)
