"""The agent: a hand-written tool-use loop over the Claude Messages API.

Written by hand rather than with a framework so every piece is visible:
tool dispatch, error feedback, human approval, prompt caching, a turn budget
and per-request token accounting.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import describe_schema
from .pricing import Usage
from .prompts import build_system_prompt
from .tools import FINAL_ANSWER_SPEC, TOOL_SPECS, ApprovalFn, ToolContext, ToolError, call_tool, deny_all

CACHE = {"type": "ephemeral"}


@dataclass
class AgentResult:
    answer: str
    sql: str | None
    stop: str                      # final_answer | end_turn | max_turns | max_tokens | refusal
    turns: int
    tool_calls: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0


def _block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    return block.model_dump(exclude_none=True)


def _get(block: Any, key: str):
    return block[key] if isinstance(block, dict) else getattr(block, key)


class FulfillmentAgent:
    def __init__(
        self,
        settings: Settings,
        client: Any = None,
        model: str | None = None,
        use_cache: bool = True,
        approve: ApprovalFn = deny_all,
        dry_run: bool = False,
        schema_text: str | None = None,
    ):
        if client is None:
            import anthropic  # imported lazily so tests run without an API key
            client = anthropic.Anthropic(max_retries=4)
        self.client = client
        self.settings = settings
        self.model = model or settings.model
        self.use_cache = use_cache
        self.ctx = ToolContext(settings=settings, approve=approve, dry_run=dry_run)
        self.system_text = build_system_prompt(schema_text or describe_schema(settings))
        self.history: list[dict] = []
        self._pending_results: list[dict] = []  # tool_results owed to the API before the next user turn

    # ------------------------------------------------------------ request build
    def _tools(self) -> list[dict]:
        tools = copy.deepcopy(TOOL_SPECS + [FINAL_ANSWER_SPEC])
        if self.use_cache:
            tools[-1]["cache_control"] = CACHE   # caches every tool definition
        return tools

    def _system(self) -> list[dict]:
        block = {"type": "text", "text": self.system_text}
        if self.use_cache:
            block["cache_control"] = CACHE       # caches tools + system prompt
        return [block]

    def _messages(self) -> list[dict]:
        msgs = copy.deepcopy(self.history)
        if self.use_cache and msgs:
            # Moving breakpoint on the newest block: each loop iteration reads the
            # whole conversation so far from cache and only pays for the new part.
            msgs[-1]["content"][-1]["cache_control"] = CACHE
        return msgs

    def build_request(self) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.settings.max_tokens,
            "system": self._system(),
            "tools": self._tools(),
            "messages": self._messages(),
        }

    # ---------------------------------------------------------------- the loop
    def run(self, user_text: str) -> AgentResult:
        started = time.perf_counter()
        usage = Usage()
        tool_calls: list[dict] = []
        n_proposals_before = len(self.ctx.proposal_log)

        self.history.append({"role": "user", "content": self._pending_results + [{"type": "text", "text": user_text}]})
        self._pending_results = []

        answer, sql, stop, turns = "", None, "max_turns", 0
        for turns in range(1, self.settings.max_turns + 1):
            response = self.client.messages.create(**self.build_request())
            usage.add_response_usage(response.usage)
            content = [_block_to_dict(b) for b in response.content]
            self.history.append({"role": "assistant", "content": content})

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                # Model ended without the final_answer tool: accept its text.
                answer = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                stop = response.stop_reason if response.stop_reason in ("max_tokens", "refusal") else "end_turn"
                break

            results, final = [], None
            for tu in tool_uses:
                name, args = tu["name"], tu.get("input") or {}
                if name == "final_answer":
                    final = args
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": "Delivered to user."})
                    continue
                try:
                    payload = call_tool(self.ctx, name, args)
                    results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                    "content": json.dumps(payload, default=str)})
                    tool_calls.append({"name": name, "input": args, "is_error": False})
                except ToolError as e:
                    results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                    "content": str(e), "is_error": True})
                    tool_calls.append({"name": name, "input": args, "is_error": True, "error": str(e)})

            if final is not None:
                answer, sql, stop = final.get("answer", ""), final.get("sql"), "final_answer"
                self._pending_results = results   # sent with the next user message
                break
            self.history.append({"role": "user", "content": results})
        else:
            # Turn budget exhausted; the last user turn holds tool results already.
            self._pending_results = self.history.pop()["content"]
            answer = "Stopped: reached the tool-call budget before finishing."

        return AgentResult(
            answer=answer, sql=sql, stop=stop, turns=turns, tool_calls=tool_calls,
            proposals=self.ctx.proposal_log[n_proposals_before:], usage=usage,
            latency_s=time.perf_counter() - started,
        )

    def reset(self) -> None:
        self.history.clear()
        self._pending_results = []
