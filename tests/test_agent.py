from conftest import FakeClient, FakeResponse, text, tool_use

from fulfillment_agent.agent import FulfillmentAgent


def _agent(settings, script, **kw):
    client = FakeClient(script)
    return FulfillmentAgent(settings, client=client, model="claude-sonnet-5-5", schema_text="TABLE t", **kw), client


def _count_breakpoints(req):
    n = sum("cache_control" in t for t in req["tools"])
    n += sum("cache_control" in b for b in req["system"])
    n += sum("cache_control" in b for m in req["messages"] for b in m["content"])
    return n


def test_tool_loop_runs_sql_then_answers(settings):
    agent, client = _agent(settings, [
        FakeResponse([text("Checking."), tool_use("t1", "run_sql", query="SELECT COUNT(*) FROM inventory WHERE on_hand = 0")]),
        FakeResponse([tool_use("t2", "final_answer", answer="62 SKUs are out of stock.",
                               sql="SELECT COUNT(*) FROM inventory WHERE on_hand = 0")]),
    ])
    r = agent.run("How many SKUs are out of stock?")
    assert r.stop == "final_answer" and r.answer.startswith("62")
    assert r.turns == 2 and r.usage.api_calls == 2
    # The SQL result went back to the model as a tool_result for t1.
    tr = client.requests[1]["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1" and "62" in tr["content"]


def test_sql_error_is_returned_so_model_can_fix_it(settings):
    agent, client = _agent(settings, [
        FakeResponse([tool_use("t1", "run_sql", query="SELECT nope FROM inventory")]),
        FakeResponse([tool_use("t2", "run_sql", query="SELECT COUNT(*) FROM inventory")]),
        FakeResponse([tool_use("t3", "final_answer", answer="565", sql=None)]),
    ])
    r = agent.run("count inventory")
    err = client.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "nope" in err["content"]
    assert [c["is_error"] for c in r.tool_calls] == [True, False]


def test_cache_breakpoints_placed_and_limited(settings):
    agent, client = _agent(settings, [
        FakeResponse([tool_use("t1", "run_sql", query="SELECT 1")]),
        FakeResponse([tool_use("t2", "run_sql", query="SELECT 2")]),
        FakeResponse([tool_use("t3", "final_answer", answer="done", sql=None)]),
    ])
    agent.run("q")
    for req in client.requests:
        assert _count_breakpoints(req) == 3            # tools, system, newest message
        assert "cache_control" in req["tools"][-1]
        assert "cache_control" in req["messages"][-1]["content"][-1]
    # History itself is never mutated with cache markers.
    assert not any("cache_control" in b for m in agent.history for b in m["content"])


def test_cache_can_be_disabled(settings):
    agent, client = _agent(settings, [FakeResponse([tool_use("t1", "final_answer", answer="a", sql=None)])],
                           use_cache=False)
    agent.run("q")
    assert _count_breakpoints(client.requests[0]) == 0


def test_followup_turn_carries_final_answer_tool_result(settings):
    agent, client = _agent(settings, [
        FakeResponse([tool_use("t1", "final_answer", answer="first", sql=None)]),
        FakeResponse([tool_use("t2", "final_answer", answer="second", sql=None)]),
    ])
    agent.run("one")
    agent.run("two")
    last_user = client.requests[1]["messages"][-1]["content"]
    assert last_user[0]["type"] == "tool_result" and last_user[0]["tool_use_id"] == "t1"
    assert last_user[-1] == {"type": "text", "text": "two", "cache_control": {"type": "ephemeral"}}


def test_plain_text_ending_is_accepted(settings):
    agent, _ = _agent(settings, [FakeResponse([text("Just text.")], stop_reason="end_turn")])
    r = agent.run("hi")
    assert r.stop == "end_turn" and r.answer == "Just text."


def test_turn_budget_stops_runaway_loops(settings):
    from dataclasses import replace
    script = [FakeResponse([tool_use(f"t{i}", "run_sql", query="SELECT 1")]) for i in range(3)]
    client = FakeClient(script)
    agent = FulfillmentAgent(replace(settings, max_turns=3), client=client, schema_text="TABLE t")
    r = agent.run("loop forever")
    assert r.stop == "max_turns" and r.turns == 3 and len(client.requests) == 3


def test_substitution_flow_with_approval(settings):
    from fulfillment_agent.evaluation import substitution_cases
    case = next(c for c in substitution_cases(settings) if c["expected"] == "propose")
    approvals = []
    agent, _ = _agent(settings, [
        FakeResponse([tool_use("t1", "find_substitute_candidates", order_item_id=case["order_item_id"])]),
        FakeResponse([tool_use("t2", "propose_substitution", order_item_id=case["order_item_id"],
                               proposed_sku=case["preferred"], reason="closest match",
                               customer_message="Hi! ... - Your pickup team")]),
        FakeResponse([tool_use("t3", "final_answer", answer="Proposed.", sql=None)]),
    ], approve=lambda p: approvals.append(p) or False)
    r = agent.run("handle it")
    assert len(approvals) == 1
    assert r.proposals[0]["status"] == "rejected_by_manager"


def test_system_prompt_is_static_and_long_enough_to_cache(settings):
    a1, _ = _agent(settings, [])
    a2, _ = _agent(settings, [])
    assert a1.system_text == a2.system_text
    assert len(a1.system_text) > 4000          # well above the 512-token cache minimum
    assert "2026-10-12" in a1.system_text and "Substitution policy" in a1.system_text
