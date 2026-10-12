from fulfillment_agent.db import run_readonly_query
from fulfillment_agent.evaluation import (load_sql_questions, render_markdown, results_match, substitution_cases,
                                          summarise)


def test_exact_match_ignores_row_and_column_order():
    assert results_match([[1, "a"], [2, "b"]], [["b", 2], ["a", 1]]) == {"strict": True, "lenient": True}


def test_numbers_compare_after_rounding():
    assert results_match([[87.3]], [[87.30000001]])["strict"]
    assert results_match([[59]], [[59.0]])["strict"]


def test_extra_columns_pass_lenient_only():
    m = results_match([["milk"]], [["milk", 11]])
    assert m == {"strict": False, "lenient": True}


def test_wrong_answer_fails_both():
    assert results_match([[5]], [[6]]) == {"strict": False, "lenient": False}
    assert results_match([["a"], ["b"]], [["a"]]) == {"strict": False, "lenient": False}


def test_every_gold_query_runs(settings):
    qs = load_sql_questions()
    assert len(qs) == 40 and len({q["id"] for q in qs}) == 40
    for q in qs:
        run_readonly_query(settings, q["gold_sql"], row_limit=5000)


def test_gold_query_matches_itself(settings):
    for q in load_sql_questions():
        rows = run_readonly_query(settings, q["gold_sql"], row_limit=5000).rows
        assert results_match(rows, rows)["strict"], q["id"]


def test_substitution_cases_are_consistent(settings):
    cases = substitution_cases(settings)
    assert len(cases) > 30
    assert {c["expected"] for c in cases} == {"propose", "no_proposal"}
    for c in cases:
        if c["expected"] == "propose":
            assert c["preferred"] in c["compliant_options"]


def test_summary_and_report_render():
    sql_rows = [{"id": "q1", "difficulty": "easy", "strict": True, "lenient": True, "sql_errors": 1,
                 "latency_s": 2.0, "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 900, "output_tokens": 50, "api_calls": 2}}]
    s = summarise(sql_rows, [], "claude-sonnet-5-5")
    assert s["sql"]["execution_accuracy_pct"] == 100.0 and s["sql"]["recovered_after_sql_error"] == 1
    assert s["sql"]["usage"]["cache_hit_share_of_input"] == 90.0
    md = render_markdown([{"model": "claude-sonnet-5-5", "use_cache": True, "summary": s}])
    assert "execution accuracy **100.0%**" in md


def test_preferred_substitute_keeps_the_same_product_when_possible(settings):
    # e.g. out-of-stock Red Oak Gala Apples should go to another brand's Gala Apples,
    # not to Red Oak oranges.
    from fulfillment_agent import policy
    hits = 0
    for c in substitution_cases(settings):
        if c["preferred"]:
            ctx = policy.load_context(settings, c["order_item_id"])
            lines = {policy.load_product(settings, s)[0]["product_line"] for s in c["compliant_options"]}
            if ctx.original["product_line"] in lines:
                hits += 1
                assert policy.load_product(settings, c["preferred"])[0]["product_line"] == ctx.original["product_line"]
    assert hits > 10
