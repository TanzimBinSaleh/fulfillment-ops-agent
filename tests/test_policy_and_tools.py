import pytest

from fulfillment_agent import policy
from fulfillment_agent.db import connect
from fulfillment_agent.evaluation import substitution_cases
from fulfillment_agent.tools import ToolContext, ToolError, call_tool


def _ctx(**over):
    original = dict(sku="A", name="A", brand="X", category="dairy", subcategory="milk", size_value=1.0,
                    size_unit="gal", price=4.00, is_store_brand=False, is_organic=False,
                    is_vegan=False, is_gluten_free=True, is_dairy_free=False)
    base = dict(order_item_id=1, quantity=2, allow_substitution=True, pick_status="pending",
                original=original, original_on_hand=0, dietary_preference=None)
    base.update(over)
    return policy.SubContext(**base)


def _cand(**over):
    c = dict(sku="B", name="B", brand="Y", category="dairy", subcategory="milk", size_value=1.0, size_unit="gal",
             price=4.50, is_store_brand=True, is_organic=False, is_vegan=False, is_gluten_free=True,
             is_dairy_free=False)
    c.update(over)
    return c


def test_compliant_substitute_has_no_violations():
    assert policy.violations(_ctx(), _cand(), 10) == []


@pytest.mark.parametrize("ctx_over, cand_over, on_hand, rule", [
    ({"allow_substitution": False}, {}, 10, "rule 1"),
    ({"original_on_hand": 5}, {}, 10, "rule 2"),
    ({}, {"subcategory": "plant_milk"}, 10, "rule 3"),
    ({}, {}, 1, "rule 4"),
    ({}, {"size_value": 3.0}, 10, "rule 5"),
    ({}, {"price": 5.01}, 10, "rule 6"),
    ({}, {"is_gluten_free": False}, 10, "rule 7"),
    ({"dietary_preference": "vegan"}, {}, 10, "rule 7"),
])
def test_each_rule_is_enforced(ctx_over, cand_over, on_hand, rule):
    v = policy.violations(_ctx(**ctx_over), _cand(**cand_over), on_hand)
    assert any(x.startswith(rule) for x in v), v


def test_household_items_skip_dietary_rule():
    ctx = _ctx(dietary_preference="vegan")
    ctx.original.update(category="household", is_gluten_free=False)
    assert policy.violations(ctx, _cand(category="household", is_gluten_free=False), 10) == []


def _first_case(settings, expected):
    return next(c for c in substitution_cases(settings) if c["expected"] == expected)


def test_candidates_are_unfiltered_but_in_stock(settings):
    case = _first_case(settings, "propose")
    out = call_tool(ToolContext(settings), "find_substitute_candidates", {"order_item_id": case["order_item_id"]})
    assert out["order_item"]["needs_substitution"] is True
    cands = out["in_stock_candidates_same_subcategory"]
    assert cands and all(c["on_hand"] > 0 for c in cands)
    assert all(c["subcategory"] == out["original_product"]["subcategory"] for c in cands)


def test_guardrail_blocks_noncompliant_proposal(settings):
    case = next(c for c in substitution_cases(settings) if c["expected"] == "no_proposal")
    ctx = ToolContext(settings, dry_run=True)
    sub = policy.load_context(settings, case["order_item_id"])
    with connect(settings.ro_url) as conn:
        any_sku = conn.execute("SELECT sku FROM products WHERE subcategory = %s AND sku <> %s LIMIT 1",
                               (sub.original["subcategory"], sub.original["sku"])).fetchone()[0]
    with pytest.raises(ToolError, match="refused by policy"):
        call_tool(ctx, "propose_substitution", {"order_item_id": case["order_item_id"], "proposed_sku": any_sku,
                                                "reason": "r", "customer_message": "m"})
    assert ctx.proposal_log[-1]["status"] == "blocked_by_policy"


def test_dry_run_accepts_compliant_and_writes_nothing(settings):
    case = _first_case(settings, "propose")
    ctx = ToolContext(settings, dry_run=True)
    out = call_tool(ctx, "propose_substitution", {"order_item_id": case["order_item_id"],
                                                  "proposed_sku": case["preferred"], "reason": "r",
                                                  "customer_message": "m"})
    assert "dry run" in out["status"]
    with connect(settings.ro_url) as conn:
        assert conn.execute("SELECT count(*) FROM substitution_proposals WHERE created_by='agent'").fetchone()[0] == 0


def test_manager_rejection_and_approval(settings):
    case = _first_case(settings, "propose")
    args = {"order_item_id": case["order_item_id"], "proposed_sku": case["preferred"],
            "reason": "r", "customer_message": "m"}
    seen = []
    rejected = call_tool(ToolContext(settings, approve=lambda p: seen.append(p) or False), "propose_substitution", args)
    assert rejected["status"] == "rejected_by_manager" and seen[0]["substitute"]["sku"] == case["preferred"]

    saved = call_tool(ToolContext(settings, approve=lambda p: True), "propose_substitution", args)
    try:
        assert saved["status"] == "saved"
        with connect(settings.ro_url) as conn:
            row = conn.execute("SELECT status, created_by FROM substitution_proposals WHERE proposal_id = %s",
                               (saved["proposal_id"],)).fetchone()
        assert row == ("pending_approval", "agent")
    finally:
        with connect(settings.admin_url, autocommit=True) as conn:
            conn.execute("DELETE FROM substitution_proposals WHERE created_by = 'agent'")


def test_writer_role_cannot_touch_orders(settings):
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect(settings.rw_url) as conn:
            conn.execute("UPDATE orders SET status = 'cancelled'")


def test_bad_tool_arguments_become_tool_errors(settings):
    with pytest.raises(ToolError, match="Bad arguments"):
        call_tool(ToolContext(settings), "run_sql", {"sql": "SELECT 1"})
    with pytest.raises(ToolError, match="Unknown tool"):
        call_tool(ToolContext(settings), "drop_everything", {})
