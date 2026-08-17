"""
Risk critic: reviews the fundamentals and technical agents' structured
verdicts and decides whether the candidate clears a risk check.

Deliberately receives only the other two agents' CONCLUSIONS, not their
raw reasoning -- context isolation by design. Seeing their full reasoning
trail would make this agent more likely to rationalize their analysis
instead of critiquing it with fresh eyes.

Also supports a risk-only mode (both verdicts omitted, ticker passed
directly) -- this is what lets the coordinator skip fundamentals/
technical entirely for a pure "what are the risks with X" query, instead
of running unnecessary analysis just to answer a risk question.

Note: this veto is model-judged, not a hard programmatic gate. That's the
right call here because the output is advisory -- a human reviews it
before anything happens with real money. If this system ever executed
trades directly instead of just recommending them, that's exactly the
point where a hard hook/gate (Project 2's territory) would be required
instead of an agent's judgment call.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent_loop import run_agent
from tools.market_data import get_risk_flags

TOOL_FUNCTIONS = {
    "get_risk_flags": get_risk_flags,
}

TOOLS = [
    {
        "name": "get_risk_flags",
        "description": "Returns risk-specific data for a stock ticker, currently short interest as a percent of float. Use this to check for market skepticism that might contradict a bullish fundamentals or technical read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "submit_risk_assessment",
        "description": "Submit the risk critic's final verdict on a candidate, after reviewing available context against risk-specific red flags.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "verdict": {"type": "string", "enum": ["approved", "approved_with_caution", "rejected"]},
                "hard_veto_triggered": {"type": "boolean"},
                "veto_reasons": {"type": "array", "items": {"type": "string"}},
                "risk_flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ticker", "verdict", "hard_veto_triggered", "veto_reasons", "risk_flags"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a risk critic reviewing stock candidates for long-term "
    "investment. When available, you receive structured verdicts from a "
    "fundamentals agent and a technical agent -- their conclusions only, "
    "not their reasoning -- so you can evaluate the case with fresh eyes "
    "instead of rationalizing their analysis. Use get_risk_flags to check "
    "for risk-specific red flags such as elevated short interest.\n\n"
    "Your remit is risk, not price. The fundamentals agent has already "
    "ruled on whether the valuation is attractive, and its verdict is in "
    "front of you; re-deciding that question here counts the same concern "
    "twice and rejects everything in an expensive market. A rich multiple "
    "is a risk flag, never a rejection.\n\n"
    "Reserve 'rejected' for risk-specific grounds: going-concern language, "
    "financial restatements, debt covenant breach risk, or litigation and "
    "regulatory action that threatens the business. Set "
    "hard_veto_triggered only for those. Elevated short interest, "
    "concentration, high beta, a full multiple and a stretched entry point "
    "are soft flags -- surface them, and reach for "
    "'approved_with_caution' when they stack up, so the portfolio stage "
    "can size the position accordingly rather than never seeing it.\n\n"
    "Do not rely on a single confidence score -- back your verdict with "
    "concrete, falsifiable reasons. When ready, call "
    "submit_risk_assessment."
)


def run_risk_critic(fundamentals_verdict: dict = None, technical_verdict: dict = None, ticker: str = None) -> dict:
    if ticker is None:
        if fundamentals_verdict:
            ticker = fundamentals_verdict["ticker"]
        elif technical_verdict:
            ticker = technical_verdict["ticker"]
        else:
            raise ValueError("run_risk_critic needs either a ticker or at least one prior verdict.")

    if fundamentals_verdict and technical_verdict:
        prompt = (
            f"Review this candidate for risk before approval.\n\n"
            f"Fundamentals verdict:\n{json.dumps(fundamentals_verdict, indent=2)}\n\n"
            f"Technical verdict:\n{json.dumps(technical_verdict, indent=2)}\n\n"
            f"Check for additional risk flags before submitting your assessment."
        )
    else:
        prompt = (
            f"No fundamentals or technical analysis was requested for this "
            f"query -- focus purely on risk flags for {ticker}. Check for "
            f"red flags before submitting your assessment."
        )

    return run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_functions=TOOL_FUNCTIONS,
        terminal_tool="submit_risk_assessment",
        user_message=prompt,
    )


if __name__ == "__main__":
    from agents.fundamentals_agent import run_fundamentals_agent
    from agents.technical_agent import run_technical_agent

    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"

    print(f"Running fundamentals agent for {ticker}...")
    fundamentals_verdict = run_fundamentals_agent(ticker)

    print(f"Running technical agent for {ticker}...")
    technical_verdict = run_technical_agent(ticker)

    assessment = run_risk_critic(fundamentals_verdict, technical_verdict)
    print(json.dumps(assessment, indent=2))