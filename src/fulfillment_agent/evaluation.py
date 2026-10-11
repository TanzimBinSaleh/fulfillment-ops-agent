"""Eval harness.

Two suites:

* sql (evals/sql_questions.jsonl): natural-language questions with a gold
  query each. Scored by execution accuracy, the standard text-to-SQL metric:
  run Claude's final query and the gold query and compare result sets.
  "strict" requires the same rows and columns (column order ignored);
  "lenient" also accepts extra columns as long as the gold columns are there.

* substitution: every order item that needs attention in today's open orders.
  Claude handles each one in dry-run mode (nothing is written). Scored by the
  same policy code the guardrail uses: was Claude's first proposal compliant,
  was the final action right (propose vs. leave for removal), did it pick the
  policy's preferred option, and is the customer text within the style rules.

Every run records tokens, cost and latency, so caching on/off and different
models can be compared on identical cases.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime

from . import policy
from .agent import FulfillmentAgent
from .config import OPS_NOW, PROJECT_ROOT, Settings
from .db import connect, describe_schema, run_readonly_query
from .pricing import Usage

EVAL_DIR = PROJECT_ROOT / "evals"
RESULTS_DIR = EVAL_DIR / "results"


# ------------------------------------------------------------- result compare

def _norm(v):
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, (int, float)):
        return repr(round(float(v), 2))
    return str(v).strip()


def _row_key(row) -> tuple:
    return tuple(sorted(_norm(v) for v in row))


def results_match(gold_rows: list[list], pred_rows: list[list]) -> dict:
    strict = Counter(map(_row_key, gold_rows)) == Counter(map(_row_key, pred_rows))
    lenient = strict
    if not strict and len(gold_rows) == len(pred_rows) and gold_rows and pred_rows:
        gcols = list(zip(*gold_rows))
        pcols = list(zip(*pred_rows))
        if len(pcols) > len(gcols):
            used: set[int] = set()
            mapping = []
            for g in gcols:
                g_ms = Counter(map(_norm, g))
                j = next((j for j, p in enumerate(pcols) if j not in used and Counter(map(_norm, p)) == g_ms), None)
                if j is None:
                    break
                used.add(j)
                mapping.append(j)
            if len(mapping) == len(gcols):
                projected = [[row[j] for j in mapping] for row in pred_rows]
                lenient = Counter(map(_row_key, gold_rows)) == Counter(map(_row_key, projected))
    return {"strict": strict, "lenient": lenient}


# ------------------------------------------------------------------ sql suite

def load_sql_questions() -> list[dict]:
    with open(EVAL_DIR / "sql_questions.jsonl") as f:
        return [json.loads(line) for line in f if line.strip()]


def run_sql_suite(agent: FulfillmentAgent, settings: Settings, limit: int | None = None, log=print) -> list[dict]:
    rows = []
    for q in load_sql_questions()[:limit]:
        agent.reset()
        sql_log_start = len(agent.ctx.sql_log)
        try:
            res = agent.run(q["question"])
        except Exception as e:  # API failure etc. Count it as a miss, keep going.
            rows.append({"id": q["id"], "difficulty": q["difficulty"], "error": repr(e),
                         "strict": False, "lenient": False})
            log(f"  {q['id']} ERROR {e!r}")
            continue
        run_sql_log = agent.ctx.sql_log[sql_log_start:]
        pred_sql = res.sql
        if not pred_sql:
            ok = [s for s in run_sql_log if s["ok"]]
            pred_sql = ok[-1]["query"] if ok else None

        gold = run_readonly_query(settings, q["gold_sql"], row_limit=5000).rows
        match = {"strict": False, "lenient": False}
        pred_error = None
        if pred_sql:
            try:
                pred = run_readonly_query(settings, pred_sql, row_limit=5000).rows
                match = results_match(gold, pred)
            except Exception as e:
                pred_error = str(e).splitlines()[0]

        rows.append({
            "id": q["id"], "difficulty": q["difficulty"], "question": q["question"],
            "pred_sql": pred_sql, "pred_error": pred_error, **match,
            "answer": res.answer, "stop": res.stop, "turns": res.turns,
            "sql_calls": len(run_sql_log), "sql_errors": sum(not s["ok"] for s in run_sql_log),
            "latency_s": round(res.latency_s, 2), "usage": res.usage.to_dict(agent.model),
        })
        log(f"  {q['id']} {'PASS' if match['lenient'] else 'FAIL'}"
            f"{'' if match['strict'] == match['lenient'] else ' (lenient only)'}  "
            f"{res.turns} turns, {res.latency_s:.1f}s")
    return rows


# --------------------------------------------------------- substitution suite

def substitution_cases(settings: Settings, limit: int | None = None) -> list[dict]:
    today = OPS_NOW.date().isoformat()
    with connect(settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT oi.order_item_id, oi.order_id
            FROM orders o JOIN order_items oi USING (order_id) JOIN inventory i USING (sku)
            WHERE o.status IN ('received','picking') AND o.pickup_slot_start::date = %s
              AND oi.pick_status = 'pending' AND i.on_hand < oi.quantity
            ORDER BY oi.order_item_id
        """, (today,))
        rows = cur.fetchall()
    cases = []
    for item_id, order_id in rows[:limit]:
        ctx = policy.load_context(settings, item_id)
        options = policy.compliant_options(settings, ctx)
        cases.append({
            "order_item_id": item_id, "order_id": order_id,
            "expected": "propose" if (ctx.allow_substitution and options) else "no_proposal",
            "compliant_options": options,
            "preferred": _preferred_option(settings, ctx, options),
        })
    return cases


def _preferred_option(settings: Settings, ctx: policy.SubContext, options: list[str]) -> str | None:
    """The policy's tie-break order: same brand, closest size, lowest price, store brand."""
    if not options:
        return None
    o = ctx.original
    ranked = []
    for sku in options:
        c, _ = policy.load_product(settings, sku)
        ranked.append(((c["brand"] != o["brand"], abs(math.log(c["size_value"] / o["size_value"])),
                        c["price"], not c["is_store_brand"]), sku))
    return min(ranked)[1]


SUB_PROMPT = ("Order item {order_item_id} in order {order_id} can't be filled from stock. Handle it according to "
              "the substitution policy: propose the best compliant substitute with a customer text, or explain why "
              "none should be proposed.")


def message_ok(msg: str | None) -> bool:
    return bool(msg) and len(msg) <= 320 and "pickup team" in msg.lower() and "$" in msg


def run_substitution_suite(agent: FulfillmentAgent, settings: Settings, limit: int | None = None,
                           log=print) -> list[dict]:
    rows = []
    for case in substitution_cases(settings, limit):
        agent.reset()
        try:
            res = agent.run(SUB_PROMPT.format(**case))
        except Exception as e:
            rows.append({**case, "error": repr(e), "action_correct": False, "first_compliant": None})
            log(f"  item {case['order_item_id']} ERROR {e!r}")
            continue
        attempts = res.proposals
        accepted = [p for p in attempts if p["status"] == "dry_run_ok"]
        action = "propose" if accepted else "no_proposal"
        first = attempts[0] if attempts else None
        final_sku = accepted[-1]["proposed_sku"] if accepted else None
        rows.append({
            **case,
            "action": action,
            "action_correct": action == case["expected"],
            "attempts": len(attempts),
            "first_compliant": (not first["violations"]) if first else None,
            "first_violations": first["violations"] if first else [],
            "guardrail_blocks": sum(p["status"] == "blocked_by_policy" for p in attempts),
            "final_sku": final_sku,
            "chose_preferred": (final_sku == case["preferred"]) if final_sku else None,
            "message_ok": message_ok(accepted[-1]["customer_message"]) if accepted else None,
            "customer_message": accepted[-1]["customer_message"] if accepted else None,
            "answer": res.answer, "stop": res.stop, "turns": res.turns,
            "latency_s": round(res.latency_s, 2), "usage": res.usage.to_dict(agent.model),
        })
        log(f"  item {case['order_item_id']} expected={case['expected']} got={action} "
            f"{'PASS' if action == case['expected'] else 'FAIL'}")
    return rows


# ------------------------------------------------------------------ summarise

def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def _usage_summary(rows: list[dict], model: str) -> dict:
    total = Usage()
    for r in rows:
        if "usage" in r:
            u = {k: r["usage"][k] for k in ("input_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens", "output_tokens", "api_calls")}
            total += Usage(**u)
    lat = [r["latency_s"] for r in rows if "latency_s" in r]
    cost = total.cost_usd(model)
    n = len(rows) or 1
    return {
        **total.to_dict(model),
        "cost_per_case_usd": round(cost / n, 5) if cost is not None else None,
        "cache_hit_share_of_input": _pct(total.cache_read_input_tokens, total.total_input_tokens),
        "median_latency_s": round(statistics.median(lat), 2) if lat else None,
        "p90_latency_s": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 2) if lat else None,
    }


def summarise(sql_rows: list[dict], sub_rows: list[dict], model: str) -> dict:
    out: dict = {}
    if sql_rows:
        by_diff = {}
        for d in ("easy", "medium", "hard"):
            rs = [r for r in sql_rows if r["difficulty"] == d]
            by_diff[d] = {"n": len(rs), "execution_accuracy_pct": _pct(sum(r["lenient"] for r in rs), len(rs))}
        recovered = [r for r in sql_rows if r.get("sql_errors") and r["lenient"]]
        out["sql"] = {
            "n": len(sql_rows),
            "execution_accuracy_pct": _pct(sum(r["lenient"] for r in sql_rows), len(sql_rows)),
            "strict_accuracy_pct": _pct(sum(r["strict"] for r in sql_rows), len(sql_rows)),
            "by_difficulty": by_diff,
            "runs_with_sql_error": sum(bool(r.get("sql_errors")) for r in sql_rows),
            "recovered_after_sql_error": len(recovered),
            "usage": _usage_summary(sql_rows, model),
        }
    if sub_rows:
        attempted = [r for r in sub_rows if r.get("first_compliant") is not None]
        proposed = [r for r in sub_rows if r.get("final_sku")]
        should_propose = [r for r in sub_rows if r["expected"] == "propose"]
        should_not = [r for r in sub_rows if r["expected"] == "no_proposal"]
        out["substitution"] = {
            "n": len(sub_rows),
            "action_accuracy_pct": _pct(sum(r["action_correct"] for r in sub_rows), len(sub_rows)),
            "correct_when_substitute_exists_pct": _pct(sum(r["action_correct"] for r in should_propose), len(should_propose)),
            "correct_when_none_allowed_pct": _pct(sum(r["action_correct"] for r in should_not), len(should_not)),
            "first_attempt_compliance_pct": _pct(sum(r["first_compliant"] for r in attempted), len(attempted)),
            "guardrail_blocks": sum(r.get("guardrail_blocks", 0) for r in sub_rows),
            "chose_policy_preferred_pct": _pct(sum(bool(r["chose_preferred"]) for r in proposed), len(proposed)),
            "message_style_ok_pct": _pct(sum(bool(r["message_ok"]) for r in proposed), len(proposed)),
            "violation_counts": dict(Counter(v.split(":")[0] for r in sub_rows for v in r.get("first_violations", []))),
            "usage": _usage_summary(sub_rows, model),
        }
    return out


def render_markdown(runs: list[dict]) -> str:
    lines = [f"# Eval results ({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]
    for run in runs:
        s = run["summary"]
        lines += [f"## {run['model']} | prompt caching {'on' if run['use_cache'] else 'off'}", ""]
        if "sql" in s:
            q = s["sql"]
            u = q["usage"]
            lines += [
                f"**Text-to-SQL** ({q['n']} questions): execution accuracy **{q['execution_accuracy_pct']}%** "
                f"(strict {q['strict_accuracy_pct']}%). Easy {q['by_difficulty']['easy']['execution_accuracy_pct']}%, "
                f"medium {q['by_difficulty']['medium']['execution_accuracy_pct']}%, "
                f"hard {q['by_difficulty']['hard']['execution_accuracy_pct']}%. "
                f"Recovered from a SQL error in {q['recovered_after_sql_error']} of {q['runs_with_sql_error']} runs that hit one.",
                f"Cost ${u['cost_usd']:.4f} total, ${u['cost_per_case_usd']:.5f} per question; "
                f"{u['cache_hit_share_of_input']}% of input tokens served from cache; "
                f"median latency {u['median_latency_s']}s.", "",
            ] if u["cost_usd"] is not None else [f"Text-to-SQL accuracy {q['execution_accuracy_pct']}%", ""]
        if "substitution" in s:
            t = s["substitution"]
            u = t["usage"]
            lines += [
                f"**Substitutions** ({t['n']} out-of-stock items): correct action **{t['action_accuracy_pct']}%** "
                f"(substitute exists {t['correct_when_substitute_exists_pct']}%, none allowed "
                f"{t['correct_when_none_allowed_pct']}%). First proposal policy-compliant "
                f"{t['first_attempt_compliance_pct']}%; guardrail blocked {t['guardrail_blocks']} proposals. "
                f"Picked the policy-preferred option {t['chose_policy_preferred_pct']}%; "
                f"customer text within style rules {t['message_style_ok_pct']}%.",
                f"Cost ${u['cost_usd']:.4f} total, ${u['cost_per_case_usd']:.5f} per item; "
                f"median latency {u['median_latency_s']}s.", "",
            ] if u["cost_usd"] is not None else [f"Substitution action accuracy {t['action_accuracy_pct']}%", ""]
    if len(runs) == 2 and runs[0]["use_cache"] != runs[1]["use_cache"]:
        on = next(r for r in runs if r["use_cache"])["summary"]
        off = next(r for r in runs if not r["use_cache"])["summary"]
        lines += ["## Prompt caching impact", ""]
        for suite in ("sql", "substitution"):
            if suite in on and suite in off:
                c_on, c_off = on[suite]["usage"]["cost_usd"], off[suite]["usage"]["cost_usd"]
                l_on, l_off = on[suite]["usage"]["median_latency_s"], off[suite]["usage"]["median_latency_s"]
                if c_on is not None and c_off:
                    lines.append(f"- {suite}: cost {100 * (1 - c_on / c_off):.1f}% lower with caching "
                                 f"(${c_off:.4f} -> ${c_on:.4f}); median latency {l_off}s -> {l_on}s.")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------- main

def run_once(settings: Settings, model: str, use_cache: bool, suite: str, limit: int | None, schema_text: str) -> dict:
    agent = FulfillmentAgent(settings, model=model, use_cache=use_cache, dry_run=True, schema_text=schema_text)
    print(f"\n== {agent.model} | caching {'on' if use_cache else 'off'} ==")
    started = time.perf_counter()
    sql_rows = run_sql_suite(agent, settings, limit) if suite in ("sql", "all") else []
    sub_rows = run_substitution_suite(agent, settings, limit) if suite in ("substitution", "all") else []
    return {
        "model": agent.model, "use_cache": use_cache, "suite": suite, "limit": limit,
        "wall_time_s": round(time.perf_counter() - started, 1),
        "summary": summarise(sql_rows, sub_rows, agent.model),
        "sql_rows": sql_rows, "substitution_rows": sub_rows,
    }


def main(settings: Settings, args) -> int:
    model = args.model or settings.model
    schema_text = describe_schema(settings)
    modes = [True, False] if args.compare_cache else [not args.no_cache]
    runs = [run_once(settings, model, c, args.suite, args.limit, schema_text) for c in modes]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = RESULTS_DIR / f"{stamp}_{model}.json"
    md_path = RESULTS_DIR / f"{stamp}_{model}.md"
    json_path.write_text(json.dumps({"runs": runs}, indent=2, default=str))
    md = render_markdown(runs)
    md_path.write_text(md)
    print("\n" + md)
    print(f"Saved {json_path.relative_to(PROJECT_ROOT)} and {md_path.relative_to(PROJECT_ROOT)}")
    return 0


__all__ = ["results_match", "summarise", "render_markdown", "substitution_cases", "main"]
