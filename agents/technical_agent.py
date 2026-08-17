"""
Technical agent: checks whether a stock's trend supports a long-term entry,
then reports a structured verdict via the submit_verdict tool.

This is deliberately narrow -- it's not hunting for day-trading signals
(MACD crossovers, breakout patterns). Its job is to confirm the trend
isn't structurally broken and flag whether now looks like a reasonable
entry or an extended one. It's a check on the fundamentals thesis, not a
separate trading signal.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent_loop import run_agent
from tools.market_data import (
    get_trend_signals,
    get_momentum_indicators,
    get_relative_strength,
)

TOOL_FUNCTIONS = {
    "get_trend_signals": get_trend_signals,
    "get_momentum_indicators": get_momentum_indicators,
    "get_relative_strength": get_relative_strength,
}

TOOLS = [
    {
        "name": "get_trend_signals",
        "description": "Returns trend positioning for a stock ticker: 50/200-day moving average relationship and position within the 52-week range. Use this to check whether the current trend supports a long-term entry, independent of valuation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_momentum_indicators",
        "description": "Returns momentum and risk context for a stock ticker: 14-day RSI, beta, and recent average volume. Use this as a caution check -- flags a short-term overbought or unusually volatile stock -- not as a standalone buy/sell trigger.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_relative_strength",
        "description": "Returns a stock's 6-month return compared to a benchmark (default S&P 500). Use this to see whether the stock is outperforming or underperforming the broader market, not just its own price history in isolation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"},
                "benchmark": {"type": "string", "description": "Benchmark ticker to compare against, defaults to SPY if omitted"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "submit_verdict",
        "description": "Submit the technical agent's final assessment for a ticker. Call this once you've evaluated trend positioning, momentum/risk context, and relative strength -- not before.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "trend_verdict": {"type": "string", "enum": ["uptrend", "consolidating", "downtrend"]},
                "entry_quality": {"type": "string", "enum": ["good_pullback", "extended", "neutral"]},
                "key_indicators": {
                    "type": "object",
                    "properties": {
                        "sma_50": {"type": ["number", "null"]},
                        "sma_200": {"type": ["number", "null"]},
                        "rsi_14": {"type": ["number", "null"]},
                    },
                },
                "flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "trend_verdict", "entry_quality", "key_indicators", "flags"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a technical analyst supporting long-term value investing "
    "decisions. Your job is not to find short-term trading signals -- it's "
    "to confirm the stock's trend isn't structurally broken and to assess "
    "whether now looks like a reasonable long-term entry point or an "
    "extended one. Use the available tools to check trend positioning, "
    "momentum/risk context, and relative strength before forming a "
    "verdict. When you have enough information, call submit_verdict with "
    "your conclusion."
)


def run_technical_agent(ticker: str) -> dict:
    return run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_functions=TOOL_FUNCTIONS,
        terminal_tool="submit_verdict",
        user_message=f"Assess {ticker}'s trend for a long-term entry.",
    )


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    verdict = run_technical_agent(ticker)
    print(json.dumps(verdict, indent=2))