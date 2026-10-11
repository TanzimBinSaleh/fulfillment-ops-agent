import psycopg
import pytest

from fulfillment_agent.data.generate import generate
from fulfillment_agent.db import QueryRejected, check_sql, connect, describe_schema, run_readonly_query


@pytest.mark.parametrize("q", [
    "DELETE FROM orders",
    "UPDATE inventory SET on_hand = 0",
    "SELECT 1; DROP TABLE orders",
    "DROP TABLE orders",
    "",
])
def test_check_sql_rejects_non_select(q):
    with pytest.raises(QueryRejected):
        check_sql(q)


def test_check_sql_allows_select_with_comment_and_cte():
    assert check_sql("-- note\nWITH x AS (SELECT 1) SELECT * FROM x;")


def test_writable_cte_is_stopped_by_the_database(settings):
    # Passes the cheap text check; the read-only role must stop it.
    with pytest.raises(psycopg.Error):
        run_readonly_query(settings, "WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d")
    with connect(settings.ro_url) as conn:
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] > 0


def test_statement_timeout(settings):
    with pytest.raises(psycopg.errors.QueryCanceled):
        run_readonly_query(settings, "SELECT pg_sleep(6)")


def test_row_limit_truncates(settings):
    r = run_readonly_query(settings, "SELECT sku FROM products", row_limit=10)
    assert len(r.rows) == 10 and r.truncated


def test_schema_description_has_comments_and_foreign_keys(settings):
    s = describe_schema(settings)
    assert "TABLE order_items" in s
    assert "order_id integer -> orders.order_id" in s
    assert "Substitutes must share it" in s


def test_generator_is_deterministic():
    a, b = generate(7), generate(7)
    assert a["orders"] == b["orders"] and a["order_items"] == b["order_items"]
    assert generate(8)["orders"] != a["orders"]


def test_synthetic_customers_use_fictional_numbers():
    assert all("-555-01" in c["phone"] for c in generate(7)["customers"])


def test_customers_only_order_items_matching_their_preference():
    data = generate(7)
    prefs = {c["customer_id"]: c["dietary_preference"] for c in data["customers"]}
    products = {p["sku"]: p for p in data["products"]}
    cust_of = {o["order_id"]: o["customer_id"] for o in data["orders"]}
    for item in data["order_items"]:
        pref = prefs[cust_of[item["order_id"]]]
        p = products[item["sku"]]
        if pref and p["category"] != "household":
            assert p[f"is_{pref}"], (pref, p["name"])
