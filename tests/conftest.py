"""
Shared fixtures.

Every test here runs offline. Nothing calls the Anthropic API and nothing
calls yfinance -- the agent loop is driven by a scripted stub client, and
the data tools are stubbed wherever a test needs them. That is deliberate:
a suite that needs a network and a funded API key to tell you whether the
allocation maths is right is a suite you will stop running.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# The agent modules build an Anthropic client at import time, so the key has
# to exist before collection. No request is ever made with it.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-never-used")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def text_block(body):
    """A text content block, as the API would return it."""
    return SimpleNamespace(type="text", text=body)


def tool_use_block(name, tool_input, block_id="tu_1"):
    """A tool_use content block, as the API would return it."""
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(stop_reason, content):
    """One messages.create return value."""
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class StubClient:
    """Replays a scripted list of responses in place of the real client.

    Records the messages it was last sent so a test can assert on what the
    loop actually put in the conversation -- which is the thing worth
    checking about a loop whose whole job is maintaining that history.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.sent = None
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        self.sent = kwargs["messages"]
        if not self.script:
            raise AssertionError("stub client ran out of scripted responses")
        return self.script.pop(0)


@pytest.fixture
def stub_client(monkeypatch):
    """Install a scripted client into agent_loop; returns the installed stub."""
    from agents import agent_loop

    def install(script):
        client = StubClient(script)
        monkeypatch.setattr(agent_loop, "client", client)
        return client

    return install


@pytest.fixture
def approved():
    """A risk verdict that clears every code-side gate."""
    def build(ticker):
        return {
            "ticker": ticker,
            "verdict": "approved",
            "hard_veto_triggered": False,
            "veto_reasons": [],
            "risk_flags": [],
        }

    return build
