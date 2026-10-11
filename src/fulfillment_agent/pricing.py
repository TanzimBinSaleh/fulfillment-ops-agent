"""Token accounting and cost.

Prices are USD per million tokens, copied from
https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-11.
Check that page before quoting costs: prices change. Unknown models report
cost as None rather than guessing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# model: (base input, 5-minute cache write, cache read, output)
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    # Haiku 5.5 prices for prompts up to 100K tokens; this agent stays far below that.
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
}


@dataclass
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0

    def add_response_usage(self, u) -> None:
        get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
        self.input_tokens += get("input_tokens") or 0
        self.cache_creation_input_tokens += get("cache_creation_input_tokens") or 0
        self.cache_read_input_tokens += get("cache_read_input_tokens") or 0
        self.output_tokens += get("output_tokens") or 0
        self.api_calls += 1

    def __iadd__(self, other: "Usage") -> "Usage":
        for k, v in asdict(other).items():
            setattr(self, k, getattr(self, k) + v)
        return self

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def cost_usd(self, model: str) -> float | None:
        prices = PRICES_PER_MTOK.get(model)
        if prices is None:
            return None
        base, write, read, out = prices
        return (self.input_tokens * base + self.cache_creation_input_tokens * write
                + self.cache_read_input_tokens * read + self.output_tokens * out) / 1_000_000

    def to_dict(self, model: str | None = None) -> dict:
        d = asdict(self)
        d["total_input_tokens"] = self.total_input_tokens
        if model:
            d["cost_usd"] = self.cost_usd(model)
        return d
