"""Command line entry point: fulfillment-agent <command>."""

from __future__ import annotations

import argparse
import sys
import textwrap

from .config import get_settings


def _terminal_approval(p: dict) -> bool:
    o, s = p["original"], p["substitute"]
    print("\n" + "-" * 72)
    print(f"APPROVAL NEEDED  order item {p['order_item_id']}")
    print(f"  original:   {o['name']}  ${o['price']:.2f}  ({o['sku']})")
    print(f"  substitute: {s['name']}  ${s['price']:.2f}  ({s['sku']})")
    print(f"  reason:     {p['reason']}")
    print("  customer text:")
    print(textwrap.indent(textwrap.fill(p["customer_message"], 66), "    "))
    while True:
        ans = input("Approve? [y/n] ").strip().lower()
        if ans in ("y", "yes", "n", "no"):
            print("-" * 72)
            return ans.startswith("y")


def _print_result(r, model: str, verbose: bool) -> None:
    if verbose:
        for c in r.tool_calls:
            flag = "ERROR " if c["is_error"] else ""
            arg = c["input"].get("query") or c["input"]
            print(f"  [{flag}{c['name']}] {str(arg)[:300]}")
    print("\n" + r.answer)
    if r.sql and verbose:
        print("\nSQL:\n" + r.sql)
    u = r.usage
    cost = u.cost_usd(model)
    print(f"\n({r.turns} turns, {u.api_calls} API calls, {u.total_input_tokens} in "
          f"[{u.cache_read_input_tokens} cache read], {u.output_tokens} out, "
          f"{'$%.4f' % cost if cost is not None else 'cost n/a'}, {r.latency_s:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fulfillment-agent", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="create schema and roles, load synthetic data")
    p.add_argument("--seed", type=int, default=7)

    for name in ("ask", "chat"):
        p = sub.add_parser(name, help="one question" if name == "ask" else "interactive session")
        if name == "ask":
            p.add_argument("question")
        p.add_argument("--model")
        p.add_argument("--no-cache", action="store_true")
        p.add_argument("-v", "--verbose", action="store_true", help="show tool calls and SQL")

    sub.add_parser("mcp", help="run the MCP server over stdio")
    sub.add_parser("reset-proposals", help="delete proposals the agent wrote")

    p = sub.add_parser("eval", help="run the eval suite")
    p.add_argument("--model")
    p.add_argument("--suite", choices=["sql", "substitution", "all"], default="all")
    p.add_argument("--limit", type=int, help="only the first N cases of each suite")
    p.add_argument("--compare-cache", action="store_true", help="run twice: caching on and off")
    p.add_argument("--no-cache", action="store_true")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.cmd == "setup":
        from .db import setup_database
        counts = setup_database(settings, seed=args.seed)
        print("Loaded synthetic data: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return 0

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0

    if args.cmd == "reset-proposals":
        from .db import reset_agent_proposals
        print(f"Deleted {reset_agent_proposals(settings)} agent proposals.")
        return 0

    if args.cmd in ("ask", "chat"):
        from .agent import FulfillmentAgent
        agent = FulfillmentAgent(settings, model=args.model, use_cache=not args.no_cache,
                                 approve=_terminal_approval)
        if args.cmd == "ask":
            _print_result(agent.run(args.question), agent.model, args.verbose)
            return 0
        print("Fulfillment Ops Agent. Type a question, 'reset' to start over, or 'quit'.")
        while True:
            try:
                q = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if q in ("quit", "exit"):
                return 0
            if q == "reset":
                agent.reset()
                continue
            if q:
                _print_result(agent.run(q), agent.model, args.verbose)

    if args.cmd == "eval":
        from .evaluation import main as eval_main
        return eval_main(settings, args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
