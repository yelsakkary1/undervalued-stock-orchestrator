"""
Shared tool-use loop for the specialist agents.

Every specialist follows the same contract: call data tools until it has
enough information, then terminate by calling its own submit_* tool with a
structured verdict. Only the tool set, the system prompt, and the opening
message differ -- so the loop lives here instead of being copy-pasted into
each agent.

Three failure modes this handles that a plain `while True` doesn't:

  1. The model sometimes finishes gathering data and then writes a prose
     summary instead of calling its submit tool (stop_reason=end_turn).
     Treating that as fatal turns a recoverable hiccup into a dead run --
     and a sector scan fans out to ~18 agent runs, so anything that fails
     even occasionally per run will fail somewhere almost every scan. We
     nudge the model back toward its submit tool instead of raising.

  2. An unbounded loop lets a model that keeps calling data tools burn
     tokens with no ceiling. MAX_ITERATIONS caps the round trips.

  3. A single tool raising (delisted ticker, network blip) would otherwise
     kill the whole agent. The error goes back to the model as a tool
     result, so it can reason around one missing input.
"""

import json
import math
import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

MAX_ITERATIONS = 10
MAX_NUDGES = 2

NUDGE_TEMPLATE = (
    "You have not submitted your result yet. Call the {terminal_tool} tool "
    "now with your conclusion, based on the data you already have. Do not "
    "reply with prose."
)


class AgentError(RuntimeError):
    """Raised when an agent cannot be brought to a structured verdict."""


def _jsonable(value):
    """Replace NaN/Infinity with None so tool results are valid JSON.

    pandas yields NaN for periods a company has no filing for, and
    json.dumps happily emits a bare `NaN` literal -- which no JSON parser
    accepts, and which reads to the model as noise rather than as "missing".
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _call_tool(tool_functions, block):
    """Execute one tool call, returning an error payload rather than raising."""
    func = tool_functions.get(block.name)
    if func is None:
        return {"error": f"No such tool: {block.name}"}
    try:
        result = func(**block.input)
    except Exception as exc:
        print(f"  !! {block.name} failed: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    print(f"  <- got result from {block.name}")
    return result


def run_agent(
    system_prompt,
    tools,
    tool_functions,
    terminal_tool,
    user_message,
    model=None,
    max_iterations=MAX_ITERATIONS,
):
    """Run one agent to completion and return its terminal tool's input.

    `tools` is the full schema list including the terminal submit tool;
    `tool_functions` maps only the *data* tool names to callables, since
    the terminal tool is handled here rather than executed.
    """
    messages = [{"role": "user", "content": user_message}]
    nudges = 0

    for _ in range(max_iterations):
        print("Calling Claude...")
        response = client.messages.create(
            model=model or MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        print(f"Claude responded (stop_reason={response.stop_reason})")

        if response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            for block in tool_use_blocks:
                if block.name == terminal_tool:
                    print(f"Received final result via {terminal_tool}.")
                    return block.input

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in tool_use_blocks:
                print(f"  -> calling {block.name}({block.input})")
                result = _call_tool(tool_functions, block)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(_jsonable(result), default=str),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "end_turn":
            if nudges >= MAX_NUDGES:
                raise AgentError(
                    f"Agent did not call {terminal_tool} after {nudges} nudges."
                )
            nudges += 1
            print(f"No {terminal_tool} call -- nudging ({nudges}/{MAX_NUDGES}).")
            if response.content:
                messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": NUDGE_TEMPLATE.format(terminal_tool=terminal_tool),
            })
            continue

        raise AgentError(f"Unexpected stop_reason: {response.stop_reason}")

    raise AgentError(
        f"Agent hit the {max_iterations}-iteration cap without calling {terminal_tool}."
    )
