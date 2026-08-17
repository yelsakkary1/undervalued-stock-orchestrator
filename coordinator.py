"""
Coordinator: classifies an incoming research query, decides which
subagents actually need to run, executes them in the right order, and
returns the combined result.

This is the piece that distinguishes an orchestrator from a fixed
pipeline -- not every query needs every agent. A pure risk question
skips fundamentals/technical entirely; a broad sector scan funnels
through fundamentals first so technical only runs on survivors.

Analysis then feeds a synthesis stage: the specialist agents each answer
"what about this one ticker", and the portfolio agent answers the
question none of them can see -- what to actually hold, in what size,
and whether the names that each passed review are secretly one bet.
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv

from agents.fundamentals_agent import run_fundamentals_agent
from agents.technical_agent import run_technical_agent
from agents.risk_critic_agent import run_risk_critic
from agents.portfolio_agent import run_portfolio_agent

load_dotenv()
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

DEFAULT_WATCHLIST = ["NVDA", "AMD", "GOOGL", "AAPL", "MSFT"]

# How many names a sector scan carries through to technical + risk. The
# funnel exists to control cost, so the shortlist stays small -- but it is
# a shortlist, not a bar, and it is never empty.
SECTOR_SCAN_SHORTLIST = 3

VERDICT_RANK = {"undervalued": 0, "fairly_valued": 1, "overvalued": 2}

SECTOR_WATCHLISTS = {
    "semiconductor": ["NVDA", "AMD", "AVGO", "TSM", "QCOM", "INTC"],
    "bank": ["JPM", "BAC", "WFC", "C", "USB"],
    "software": ["MSFT", "CRM", "ADBE", "NOW", "PANW"],
}

ROUTING_TOOLS = [
    {
        "name": "submit_routing_plan",
        "description": "Submit how this query should be routed. Call this once you've classified the query -- before any agent runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["single_ticker", "sector_scan", "risk_only", "vague"],
                    "description": "single_ticker: one or a few named tickers, full deep dive. sector_scan: a sector/theme named, no specific tickers. risk_only: the query is ONLY about risk/red flags, not a buy thesis -- do not use this just because risk is part of a normal buy question. vague: no ticker or sector given at all.",
                },
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols explicitly named in the query, if any. Empty if none were named.",
                },
                "sector": {
                    "type": ["string", "null"],
                    "description": "Sector or theme named in the query (e.g. 'semiconductor'), if this is a sector_scan. Null otherwise.",
                },
            },
            "required": ["query_type", "tickers", "sector"],
        },
    }
]

ROUTING_SYSTEM_PROMPT = (
    "You are the coordinator for a stock research system. Classify the "
    "user's query so the right subagents can be invoked -- don't run "
    "unnecessary analysis. single_ticker means one or a few named stocks "
    "get a full deep dive. sector_scan means a sector or theme was named "
    "without specific tickers. risk_only means the query is purely about "
    "risk or red flags, not a buy thesis -- a normal 'is this a good buy' "
    "question is single_ticker, not risk_only, even though risk is part "
    "of a good answer. vague means no ticker or sector was given at all. "
    "Call submit_routing_plan with your classification."
)


def classify_query(query: str) -> dict:
    print("Classifying query...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=ROUTING_SYSTEM_PROMPT,
        tools=ROUTING_TOOLS,
        tool_choice={"type": "tool", "name": "submit_routing_plan"},
        messages=[{"role": "user", "content": query}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_routing_plan":
            return block.input
    raise RuntimeError("Coordinator failed to produce a routing plan.")


def resolve_tickers(plan: dict) -> list:
    if plan["tickers"]:
        return plan["tickers"]
    if plan["query_type"] == "sector_scan" and plan.get("sector"):
        sector_key = plan["sector"].lower()
        for key, tickers in SECTOR_WATCHLISTS.items():
            if key in sector_key or sector_key in key:
                return tickers
        print(f"Note: no watchlist for sector '{plan['sector']}' -- using default watchlist.")
    return DEFAULT_WATCHLIST


def clarification_response(query: str) -> dict:
    """What a vague query gets instead of a scan.

    Falling through to the default watchlist would answer a question the
    user didn't ask -- five tickers times three agents, all discarded once
    they say which stock they actually meant. Asking costs one routing call.
    """
    return {
        "status": "needs_clarification",
        "query": query,
        "query_type": "vague",
        "message": (
            "No ticker or sector was named, so there's nothing specific to "
            "research. Name a ticker for a deep dive, or a sector to scan."
        ),
        "known_sectors": sorted(SECTOR_WATCHLISTS),
        "example_tickers": DEFAULT_WATCHLIST,
        "candidates": {},
        "portfolio": None,
    }


def shortlist_for_scan(screened: dict, limit: int = SECTOR_SCAN_SHORTLIST) -> tuple:
    """Rank screened tickers and split them into a shortlist and the rest.

    The gate here used to be absolute -- verdict == "undervalued" or you
    were dropped -- and it returned nothing for 11 of 11 tickers across two
    sectors, because almost nothing is cheap in absolute terms right now.
    That is the coordinator decomposing the task too narrowly: "find
    undervalued semiconductors" is a comparative question, and answering it
    with a bar set in isolation gives the broad question no coverage at all.

    Ranking answers the question that was actually asked and always
    produces something to look at. Each survivor is tagged with the basis
    it cleared on, so "cheapest in a rich sector" never gets mistaken
    downstream for "cheap".
    """
    def rank_key(item):
        ticker, verdict = item
        tier = VERDICT_RANK.get(verdict.get("verdict"), len(VERDICT_RANK))
        score = verdict.get("valuation_score")
        # NaN passes an isinstance float check and then compares false against
        # everything, which silently scrambles the sort rather than failing --
        # an unscored candidate must rank last, not wherever it happens to land.
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            score = 0.0
        return (tier, -float(score), ticker)

    ranked = sorted(screened.items(), key=rank_key)
    return ranked[:limit], ranked[limit:]


def analyse_candidates(plan: dict, tickers: list) -> dict:
    """Run the specialist agents over the tickers in scope.

    Sector scans funnel: fundamentals screens everything, and only the
    top-ranked names pay for technical and risk.
    """
    results = {}

    if plan["query_type"] == "risk_only":
        for ticker in tickers:
            print(f"--- {ticker}: risk-only check ---")
            results[ticker] = {"risk": run_risk_critic(ticker=ticker)}
        return results

    if plan["query_type"] == "sector_scan":
        screened = {}
        for ticker in tickers:
            print(f"--- {ticker}: fundamentals screen ---")
            screened[ticker] = run_fundamentals_agent(ticker)

        shortlist, rest = shortlist_for_scan(screened)
        total = len(screened)

        for rank, (ticker, f_verdict) in enumerate(shortlist, start=1):
            basis = "absolute" if f_verdict.get("verdict") == "undervalued" else "relative"
            print(f"--- {ticker}: technical + risk (rank {rank}/{total}, {basis}) ---")
            t_verdict = run_technical_agent(ticker)
            risk = run_risk_critic(f_verdict, t_verdict)
            results[ticker] = {
                "fundamentals": f_verdict,
                "technical": t_verdict,
                "risk": risk,
                "screen": {"basis": basis, "sector_rank": rank, "of": total},
            }

        for offset, (ticker, f_verdict) in enumerate(rest):
            rank = len(shortlist) + offset + 1
            results[ticker] = {
                "fundamentals": f_verdict,
                "skipped": f"ranked {rank} of {total} on fundamentals -- outside the top {len(shortlist)}",
                "screen": {"basis": "relative", "sector_rank": rank, "of": total},
            }
        return results

    for ticker in tickers:
        print(f"--- {ticker}: full deep dive ---")
        f_verdict = run_fundamentals_agent(ticker)
        t_verdict = run_technical_agent(ticker)
        risk = run_risk_critic(f_verdict, t_verdict)
        results[ticker] = {"fundamentals": f_verdict, "technical": t_verdict, "risk": risk}

    return results


def run_orchestrator(query: str, synthesise: bool = True) -> dict:
    plan = classify_query(query)
    print(f"Routing plan: {plan}")

    if plan["query_type"] == "vague":
        print("Query is too vague to route -- asking for clarification.")
        return clarification_response(query)

    tickers = resolve_tickers(plan)
    print(f"Tickers in scope: {tickers}")

    results = analyse_candidates(plan, tickers)

    # A risk-only query asked what could go wrong, not what to buy --
    # answering it with an allocation would be answering a different question.
    portfolio = None
    if synthesise and plan["query_type"] != "risk_only":
        print("--- portfolio synthesis ---")
        portfolio = run_portfolio_agent(results)

    return {
        "query": query,
        "query_type": plan["query_type"],
        "tickers_analysed": tickers,
        "candidates": results,
        "portfolio": portfolio,
    }


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--no-synthesis"]
    synthesise = "--no-synthesis" not in sys.argv

    query = args[0] if args else "Is NVDA a good long-term buy?"
    output = run_orchestrator(query, synthesise=synthesise)
    print(json.dumps(output, indent=2))