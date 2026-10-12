"""Score an eval run done through the MCP server instead of the API agent loop.

Each log line is one call made by an agent through a logging tool helper:
  {"kind": "begin", "args": {"id": "q01"}}
  {"kind": "tool", "name": "run_sql" | "find_substitute_candidates" | "propose_substitution", "args": ..., "is_error": bool, "output": "..."}
  {"kind": "final_answer", "args": {"id": "q01", "answer": "...", "sql": "..." | null}}

Scoring uses the same functions as the API harness (results_match, the policy
code, the same case list and preferred-option ranking), so the numbers are
comparable. Token cost and prompt-caching savings are not measurable this way.

Usage: python evals/score_mcp_run.py <log_dir> [--out evals/results/<name>]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fulfillment_agent.config import get_settings  # noqa: E402
from fulfillment_agent.db import run_readonly_query  # noqa: E402
from fulfillment_agent.evaluation import (load_sql_questions, message_ok, results_match,  # noqa: E402
                                          substitution_cases)


def read_logs(log_dir: Path) -> dict[str, list[dict]]:
    return {p.stem: [json.loads(line) for line in p.open()] for p in sorted(log_dir.glob("*.jsonl"))}


def segments(records: list[dict]) -> dict[str, list[dict]]:
    """Group records by the item id announced in the preceding 'begin'."""
    out: dict[str, list[dict]] = defaultdict(list)
    current = None
    for r in records:
        if r["kind"] == "begin":
            current = r["args"]["id"]
        out[current].append(r)
    return out


def score_sql(settings, logs) -> list[dict]:
    segs = {}
    for records in logs.values():
        segs.update({k: v for k, v in segments(records).items() if k and k.startswith("q")})
    rows = []
    for q in load_sql_questions():
        seg = segs.get(q["id"], [])
        finals = [r for r in seg if r["kind"] == "final_answer"]
        sql_calls = [r for r in seg if r["kind"] == "tool" and r["name"] == "run_sql"]
        pred_sql = finals[-1]["args"].get("sql") if finals else None
        if not pred_sql:  # same fallback as the API harness: last successful query
            ok = [r for r in sql_calls if not r["is_error"]]
            pred_sql = ok[-1]["args"]["query"] if ok else None
        gold = run_readonly_query(settings, q["gold_sql"], row_limit=5000).rows
        match, err = {"strict": False, "lenient": False}, None
        if pred_sql:
            try:
                match = results_match(gold, run_readonly_query(settings, pred_sql, row_limit=5000).rows)
            except Exception as e:
                err = str(e).splitlines()[0]
        rows.append({"id": q["id"], "difficulty": q["difficulty"], "question": q["question"],
                     "answered": bool(finals), "pred_sql": pred_sql, "pred_error": err, **match,
                     "sql_calls": len(sql_calls), "sql_errors": sum(r["is_error"] for r in sql_calls),
                     "answer": finals[-1]["args"].get("answer") if finals else None})
    return rows


def score_substitutions(settings, logs) -> list[dict]:
    attempts: dict[int, list[dict]] = defaultdict(list)
    for records in logs.values():
        for r in records:
            if r["kind"] == "tool" and r["name"] == "propose_substitution":
                attempts[int(r["args"]["order_item_id"])].append(r)
    answered = {r["args"]["id"] for recs in logs.values() for r in recs if r["kind"] == "final_answer"}
    rows = []
    for case in substitution_cases(settings):
        tries = attempts.get(case["order_item_id"], [])
        accepted = [t for t in tries if not t["is_error"] and "accepted" in t["output"]]
        first = tries[0] if tries else None
        action = "propose" if accepted else "no_proposal"
        final_sku = accepted[-1]["args"]["proposed_sku"] if accepted else None
        rows.append({
            **case, "answered": f"s{case['order_item_id']}" in answered,
            "action": action, "action_correct": action == case["expected"],
            "attempts": len(tries),
            "first_compliant": (not first["is_error"]) if first else None,
            "first_error": first["output"] if first and first["is_error"] else None,
            "guardrail_blocks": sum(t["is_error"] and "refused by policy" in t["output"] for t in tries),
            "final_sku": final_sku,
            "chose_preferred": (final_sku == case["preferred"]) if final_sku else None,
            "message_ok": message_ok(accepted[-1]["args"].get("customer_message")) if accepted else None,
            "customer_message": accepted[-1]["args"].get("customer_message") if accepted else None,
        })
    return rows


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def summarise(sql_rows, sub_rows) -> dict:
    by_diff = {d: pct(sum(r["lenient"] for r in sql_rows if r["difficulty"] == d),
                      sum(r["difficulty"] == d for r in sql_rows)) for d in ("easy", "medium", "hard")}
    erred = [r for r in sql_rows if r["sql_errors"]]
    proposed = [r for r in sub_rows if r["final_sku"]]
    attempted = [r for r in sub_rows if r["first_compliant"] is not None]
    yes = [r for r in sub_rows if r["expected"] == "propose"]
    no = [r for r in sub_rows if r["expected"] == "no_proposal"]
    return {
        "sql": {
            "n": len(sql_rows), "answered": sum(r["answered"] for r in sql_rows),
            "execution_accuracy_pct": pct(sum(r["lenient"] for r in sql_rows), len(sql_rows)),
            "strict_accuracy_pct": pct(sum(r["strict"] for r in sql_rows), len(sql_rows)),
            "by_difficulty": by_diff,
            "runs_with_sql_error": len(erred), "recovered_after_sql_error": sum(r["lenient"] for r in erred),
            "failed_ids": [r["id"] for r in sql_rows if not r["lenient"]],
        },
        "substitution": {
            "n": len(sub_rows), "answered": sum(r["answered"] for r in sub_rows),
            "action_accuracy_pct": pct(sum(r["action_correct"] for r in sub_rows), len(sub_rows)),
            "correct_when_substitute_exists_pct": pct(sum(r["action_correct"] for r in yes), len(yes)),
            "correct_when_none_allowed_pct": pct(sum(r["action_correct"] for r in no), len(no)),
            "first_attempt_compliance_pct": pct(sum(r["first_compliant"] for r in attempted), len(attempted)),
            "proposals_attempted": len(attempted),
            "guardrail_blocks": sum(r["guardrail_blocks"] for r in sub_rows),
            "chose_policy_preferred_pct": pct(sum(bool(r["chose_preferred"]) for r in proposed), len(proposed)),
            "message_style_ok_pct": pct(sum(bool(r["message_ok"]) for r in proposed), len(proposed)),
            "wrong_action_items": [r["order_item_id"] for r in sub_rows if not r["action_correct"]],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    settings = get_settings()
    logs = read_logs(a.log_dir)
    sql_rows, sub_rows = score_sql(settings, logs), score_substitutions(settings, logs)
    summary = summarise(sql_rows, sub_rows)
    print(json.dumps(summary, indent=2))
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "scored.json").write_text(json.dumps(
            {"summary": summary, "sql_rows": sql_rows, "substitution_rows": sub_rows}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
