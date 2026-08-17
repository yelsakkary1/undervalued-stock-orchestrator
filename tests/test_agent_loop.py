"""
Tests for the shared agent loop.

These cover the recovery paths rather than the happy path, because the
happy path is the one you notice breaking. Each of these failures was
observed against the live API before the loop handled it.
"""

import json
import math

import pytest

from agents.agent_loop import AgentError, MAX_ITERATIONS, MAX_NUDGES, _jsonable, run_agent
from conftest import response, text_block, tool_use_block

TOOLS = [{"name": "get_data"}, {"name": "submit_verdict"}]
FUNCS = {"get_data": lambda ticker: {"pe": 12.0}}


def run(**overrides):
    kwargs = {
        "system_prompt": "sys",
        "tools": TOOLS,
        "tool_functions": FUNCS,
        "terminal_tool": "submit_verdict",
        "user_message": "Evaluate AMD",
    }
    kwargs.update(overrides)
    return run_agent(**kwargs)


class TestTerminalTool:
    def test_returns_the_terminal_tools_input(self, stub_client):
        stub_client([
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD", "verdict": "fairly_valued"})]),
        ])
        assert run() == {"ticker": "AMD", "verdict": "fairly_valued"}

    def test_data_tools_run_before_the_verdict(self, stub_client):
        client = stub_client([
            response("tool_use", [tool_use_block("get_data", {"ticker": "AMD"})]),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD"})]),
        ])
        run()
        # the loop must have appended the assistant turn and the tool_result
        assert client.sent[-1]["role"] == "user"
        assert client.sent[-1]["content"][0]["type"] == "tool_result"
        assert json.loads(client.sent[-1]["content"][0]["content"]) == {"pe": 12.0}


class TestEndTurnRecovery:
    """The model finishes gathering data, then writes prose instead of
    calling its submit tool. Observed roughly one run in four against
    Haiku; fatal across a scan that fans out to ~18 agent runs."""

    def test_nudges_back_to_the_terminal_tool(self, stub_client):
        client = stub_client([
            response("tool_use", [tool_use_block("get_data", {"ticker": "AMD"})]),
            response("end_turn", [text_block("AMD looks fairly valued given the P/E of 12.")]),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD", "verdict": "fairly_valued"})]),
        ])
        assert run()["verdict"] == "fairly_valued"
        assert "submit_verdict" in client.sent[-1]["content"]

    def test_keeps_the_models_prose_in_history(self, stub_client):
        """The nudge has to leave the model's own turn in place, or the
        next request has a user message answering nothing."""
        client = stub_client([
            response("end_turn", [text_block("some reasoning")]),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD"})]),
        ])
        run()
        assert [m["role"] for m in client.sent[-2:]] == ["assistant", "user"]

    def test_gives_up_after_max_nudges(self, stub_client):
        client = stub_client([response("end_turn", [text_block("no")]) for _ in range(10)])
        with pytest.raises(AgentError, match="after 2 nudges"):
            run()
        assert client.calls == MAX_NUDGES + 1

    def test_survives_an_empty_content_turn(self, stub_client):
        """An assistant message with empty content is rejected by the API,
        so the nudge must not append one."""
        stub_client([
            response("end_turn", []),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD"})]),
        ])
        assert run() == {"ticker": "AMD"}


class TestBounds:
    def test_iteration_cap_stops_an_endless_tool_caller(self, stub_client):
        client = stub_client([
            response("tool_use", [tool_use_block("get_data", {"ticker": "AMD"})]) for _ in range(50)
        ])
        with pytest.raises(AgentError, match="iteration cap"):
            run()
        assert client.calls == MAX_ITERATIONS

    def test_unexpected_stop_reason_raises(self, stub_client):
        stub_client([response("max_tokens", [text_block("truncated")])])
        with pytest.raises(AgentError, match="Unexpected stop_reason"):
            run()


class TestToolFailures:
    def test_a_raising_tool_is_reported_not_fatal(self, stub_client):
        """One delisted ticker or network blip should not kill the run."""
        def boom(ticker):
            raise ConnectionError("yfinance timed out")

        client = stub_client([
            response("tool_use", [tool_use_block("get_data", {"ticker": "ZZZZ"})]),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "ZZZZ"})]),
        ])
        assert run(tool_functions={"get_data": boom}) == {"ticker": "ZZZZ"}
        sent = json.loads(client.sent[-1]["content"][0]["content"])
        assert "ConnectionError" in sent["error"]

    def test_unknown_tool_is_reported_not_fatal(self, stub_client):
        client = stub_client([
            response("tool_use", [tool_use_block("no_such_tool", {})]),
            response("tool_use", [tool_use_block("submit_verdict", {"ticker": "AMD"})]),
        ])
        run()
        assert "No such tool" in json.loads(client.sent[-1]["content"][0]["content"])["error"]


class TestJsonable:
    """pandas yields NaN for periods a company has no filing for, and
    json.dumps emits a bare `NaN` literal -- invalid JSON, and noise
    rather than "missing" to the model reading it."""

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_become_null(self, value):
        assert _jsonable(value) is None

    def test_recurses_through_containers(self):
        cleaned = _jsonable({"a": [1.0, float("nan")], "b": {"c": float("inf")}})
        assert cleaned == {"a": [1.0, None], "b": {"c": None}}

    def test_output_is_strict_json(self):
        payload = {"revenue_trend": [215938000000.0, float("nan")]}
        assert json.dumps(_jsonable(payload), allow_nan=False)
        with pytest.raises(ValueError):
            json.dumps(payload, allow_nan=False)

    def test_ordinary_values_pass_through(self):
        assert _jsonable({"pe": 12.0, "name": "AMD", "ok": True, "n": None}) == {
            "pe": 12.0, "name": "AMD", "ok": True, "n": None,
        }
