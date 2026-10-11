from __future__ import annotations

import pytest

from fulfillment_agent.config import get_settings
from fulfillment_agent.db import connect


@pytest.fixture(scope="session")
def settings():
    s = get_settings()
    try:
        with connect(s.ro_url) as conn:
            conn.execute("SELECT 1 FROM orders LIMIT 1")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"database not available or not set up ({e}); run `fulfillment-agent setup`")
    return s


class FakeResponse:
    def __init__(self, content, stop_reason="tool_use", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or {"input_tokens": 100, "output_tokens": 20,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


class FakeClient:
    """Stands in for anthropic.Anthropic: replays scripted responses and
    records every request so tests can inspect what would have been sent."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        import copy
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self.script.pop(0)


def tool_use(id_, name, **input_):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def text(t):
    return {"type": "text", "text": t}
