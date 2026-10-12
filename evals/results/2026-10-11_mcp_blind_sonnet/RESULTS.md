# Blind eval run: Claude Sonnet 5.5 through the MCP server (2026-10-11)

## Headline

| Metric | Result |
|---|---|
| Text-to-SQL execution accuracy (40 questions) | **80.0%** (32/40); strict 52.5% |
| by difficulty | easy 83.3%, medium 87.5%, hard 66.7% |
| Text-to-SQL answer accuracy, graded against the answer key | **100%** (40/40) |
| Substitutions: correct action (56 out-of-stock items) | **100%** (56/56) |
| when a compliant substitute exists / when none is allowed | 100% (49/49) / 100% (7/7) |
| First proposal policy-compliant | **100%** (49/49); guardrail blocked 0 |
| Picked the policy-preferred option | 98.0% (48/49) |
| Customer text within style rules | 100% (49/49) |

Not measured: token cost and prompt-caching savings. Those need the API agent loop (`fulfillment-agent eval --compare-cache`).

## Method

- **Model:** `claude-sonnet-5-5`, run as eight Claude Code helper agents: four batches of 10 SQL questions and four batches of 14 substitution cases. The model ID is confirmed in the agents' transcripts.
- **Same inputs as the API agent:** each helper read `brief.md`, which is the exact system prompt `prompts.py` builds (schema, policy, store notes), and used the same three tools through `fulfillment-agent mcp`. Substitutions ran with `FULFILLMENT_DRY_RUN=1`, which applies the policy check but never writes.
- **Blind:** before the run, the answer key (`evals/`), the tests, `evaluation.py`, `DEMO.md`, the git history and earlier demo files were moved into an encrypted archive. Agent-written proposals from the demo were deleted from the database. The helpers got only the questions.
- **Audited:** every command the helpers ran was checked from their saved transcripts. All 165 shell commands were calls to the logging tool helper, and the only files read were the system prompt, the rules and each helper's own batch file.
- **Scored** by `evals/score_mcp_run.py`, using the same comparison function, case list, policy code and preferred-option ranking as the API harness. Raw call logs are in `logs/`; per-item results are in `scored.json`.

**Difference from the API harness:** each helper handled its batch in one session, so later questions in a batch could benefit from earlier context (for example, the schema it had already explored). The API harness starts every question fresh.

## Why execution accuracy (80%) and answer accuracy (100%) differ

Execution accuracy compares the result set of Claude's final query with the gold query's. All 8 misses gave the correct answer in the reply, but the query's result set was shaped differently:

| Question | What happened |
|---|---|
| q03, q08, q15, q36, q38, q40 | Correct top answer, but the query returned several ranked rows for context (e.g. "Aisle 4 has the most (11); next are Dairy 1 and Produce with 9 each"). The gold query returns only the rows the question asks for. |
| q28 | Excluded cancelled orders, which the gold query does not; the answer (frozen) is the same either way. |
| q30 | Counts per slot are identical to the gold query's, but the slot was converted to local time without a timezone, so the timestamp text differs. |

The 32 passes were also checked: each reply states the same values as the gold result. Answer accuracy was graded by comparing each reply with the gold query's result. There were no SQL errors in any run, so the "recovered after an error" metric doesn't apply.

The scorer was left unchanged after seeing these results, so 80% is the figure under the metric as defined before the run.

## The one non-preferred substitution

Item 2990: two store-brand 1 pt options tied on every policy criterion at $2.25 (chocolate ice cream and non-dairy vanilla frozen dessert). The policy ranking breaks the tie by SKU and prefers chocolate; Claude chose non-dairy vanilla to keep the flavor and said "non-dairy" in the customer text. Both are policy-compliant.
