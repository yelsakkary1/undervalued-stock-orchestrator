"""
Fundamentals agent: evaluates a stock's valuation and financial health,
then reports a structured verdict via the submit_verdict tool.

This agent only ever sees its own four data tools plus submit_verdict --
it has no knowledge of the technical agent or the risk critic. The
coordinator is what merges outputs from all three.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent_loop import run_agent
from tools.market_data import (
    get_valuation_snapshot,
    get_financial_health,
    get_growth_trend,
    get_ownership_signals,
)

TOOL_FUNCTIONS = {
    "get_valuation_snapshot": get_valuation_snapshot,
    "get_financial_health": get_financial_health,
    "get_growth_trend": get_growth_trend,
    "get_ownership_signals": get_ownership_signals,
}

TOOLS = [
    {
        "name": "get_valuation_snapshot",
        "description": "Returns core valuation ratios for a stock ticker: trailing/forward P/E, P/B, EV/EBITDA, PEG, and free cash flow yield. Use this first when assessing whether a stock is cheap relative to earnings, book value, or cash generation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_financial_health",
        "description": "Returns debt and efficiency metrics for a stock ticker: debt-to-equity, current ratio, and return on equity. Use this to check whether a cheap-looking stock is cheap because it's a bargain, or because the business is deteriorating.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_growth_trend",
        "description": "Returns multi-year revenue trend for a stock ticker, most recent year first. Use this to see whether revenue is growing, flat, or eroding over time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"},
                "years": {"type": "integer", "description": "How many years of history to return (default 5)"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ownership_signals",
        "description": "Returns recent insider transaction activity for a stock ticker. Use this as a conviction signal alongside valuation and health metrics -- heavy insider buying strengthens an undervalued thesis, heavy selling weakens it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "submit_verdict",
        "description": "Submit the fundamentals agent's final assessment for a ticker. Call this once you've evaluated valuation, financial health, growth trend, and ownership signals -- not before.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "verdict": {"type": "string", "enum": ["undervalued", "fairly_valued", "overvalued"]},
                "valuation_score": {
                    "type": "number",
                    "description": "0-100, how attractive this name looks on valuation and quality grounds. 50 is typical for a large-cap today; 80+ is genuinely cheap for what you get; below 20 is expensive. This exists so candidates can be ranked against each other -- it does not replace your verdict, and a high score on a name you called overvalued is a contradiction, not a nuance.",
                },
                "key_metrics": {
                    "type": "object",
                    "properties": {
                        "pe_ratio": {"type": ["number", "null"]},
                        "fcf_yield": {"type": ["number", "null"]},
                        "debt_to_equity": {"type": ["number", "null"]},
                    },
                },
                "flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "verdict", "valuation_score", "key_metrics", "flags"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a fundamentals analyst evaluating stocks for long-term value "
    "investing. Use the available tools to check valuation, financial "
    "health, growth trend, and ownership signals before forming a verdict. "
    "A low P/E alone does not mean undervalued -- check quality metrics "
    "too, so you don't mistake a value trap for a bargain.\n\n"
    "Judge this ticker on its own merits; you are not comparing it to "
    "anything, and you cannot see what else is being screened. Alongside "
    "the verdict, give a valuation_score so the coordinator can rank "
    "candidates it screened separately -- the score carries the "
    "shades of grey the three-way verdict flattens, which is what makes "
    "'the least expensive of a rich sector' expressible at all.\n\n"
    "When you have enough information, call submit_verdict."
)


def run_fundamentals_agent(ticker: str) -> dict:
    return run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_functions=TOOL_FUNCTIONS,
        terminal_tool="submit_verdict",
        user_message=f"Evaluate {ticker} for a long-term undervalued-stock thesis.",
    )


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    verdict = run_fundamentals_agent(ticker)
    print(json.dumps(verdict, indent=2))