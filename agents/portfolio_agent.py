"""
Portfolio agent: turns a pile of per-ticker verdicts into an actual
allocation, with a thesis a human can argue with.

This is the only agent that sees more than one ticker at a time, which
makes it the only place two portfolio-level questions can be asked:

  - Sizing. "Undervalued" is not a position size. Conviction has to come
    from how well fundamentals, technical and risk agree with each other,
    and that comparison doesn't exist until all three have reported.
  - Concentration. Five names that each cleared review individually can
    still be one bet wearing five hats. get_sector_profile is here and
    nowhere else for exactly that reason.

Division of labour with the code around it: the model proposes an
allocation and argues for it; enforce_allocation_rules disposes. The
risk critic's rejections are applied as a hard filter in code before this
agent is ever invoked -- a veto that a downstream model can talk itself
out of is not a veto. Same reasoning as the note in risk_critic_agent.py,
one layer up: advisory judgement stays with the model, anything that must
hold regardless of how persuasive the reasoning sounds goes in Python.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent_loop import run_agent
from tools.market_data import get_sector_profile

MAX_POSITION_PCT = 35.0
MAX_SECTOR_PCT = 60.0

TOOL_FUNCTIONS = {
    "get_sector_profile": get_sector_profile,
}

TOOLS = [
    {
        "name": "get_sector_profile",
        "description": "Returns sector, industry, market cap and beta for a ticker. Use this on every candidate before sizing them, so you can see whether names that each look fine on their own are actually the same bet -- concentration is invisible in the individual verdicts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "submit_portfolio",
        "description": "Submit the final portfolio. Call this once you've sized every candidate you intend to hold and checked the mix for concentration -- not before.",
        "input_schema": {
            "type": "object",
            "properties": {
                "positions": {
                    "type": "array",
                    "description": "The names you intend to hold. Every entry is an object with ticker, allocation_pct, conviction, thesis and key_risks -- never a bare ticker string. Empty if you are holding all cash.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "allocation_pct": {
                                "type": "number",
                                "description": "Percent of total portfolio, 0-100.",
                            },
                            "conviction": {"type": "string", "enum": ["high", "medium", "low"]},
                            "thesis": {
                                "type": "string",
                                "description": "One or two sentences on why this name earns its size. Cite the specific metrics that drove it.",
                            },
                            "key_risks": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["ticker", "allocation_pct", "conviction", "thesis", "key_risks"],
                    },
                },
                "cash_pct": {
                    "type": "number",
                    "description": "Percent held in cash. Positions plus cash should total 100.",
                },
                "excluded": {
                    "type": "array",
                    "description": "Candidates you considered and chose not to hold, with the reason.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["ticker", "reason"],
                    },
                },
                "concentration_notes": {
                    "type": "array",
                    "description": "Portfolio-level exposures worth flagging, e.g. sector or beta concentration.",
                    "items": {"type": "string"},
                },
                "summary": {
                    "type": "string",
                    "description": "A short plain-English brief for the human reviewing this.",
                },
            },
            "required": ["positions", "cash_pct", "excluded", "concentration_notes", "summary"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a portfolio manager assembling a long-term position book from "
    "candidates that have already been analysed. You receive each "
    "candidate's fundamentals, technical and risk verdicts -- their "
    "conclusions, not their reasoning. Candidates already rejected by the "
    "risk critic have been removed before you see them; do not try to "
    "reinstate them.\n\n"
    "Size by conviction, and let conviction come from agreement between "
    "the three verdicts. A name all three like earns a full position. A "
    "name where fundamentals are strong but the technical read says "
    "'extended' earns a smaller one, or cash while you wait. A name "
    "approved only with caution earns a starter position at most.\n\n"
    "Call get_sector_profile on every candidate before sizing. Names that "
    "cleared review individually can still be one concentrated bet, and "
    "you are the only stage that can see this. Flag it when you find it.\n\n"
    "A candidate carrying a `screen` block with basis 'relative' reached "
    "you by being among the best of the names scanned, not by being cheap "
    "on its own terms -- a sector can be uniformly expensive and still "
    "have a top three. Size those more conservatively than a candidate "
    "that cleared on an absolute basis, and say plainly in your summary "
    "that the shortlist was comparative.\n\n"
    f"Hard constraints, enforced in code after you submit: no single "
    f"position above {MAX_POSITION_PCT:.0f}%, no sector above "
    f"{MAX_SECTOR_PCT:.0f}%, positions plus cash equal 100. Stay inside "
    "them yourself rather than relying on the correction.\n\n"
    "Holding cash is a legitimate answer. If nothing clears the bar, say "
    "so and size accordingly -- an empty book beats a forced one. When "
    "you're done, call submit_portfolio."
)


def partition_candidates(results: dict) -> tuple:
    """Split analysed tickers into what the portfolio agent may consider
    and what has already been ruled out before it runs.

    Two things get filtered here rather than left to the model: candidates
    that never cleared the fundamentals screen (no full analysis exists to
    size against) and candidates the risk critic rejected or hard-vetoed.
    """
    eligible, blocked = {}, {}

    for ticker, result in results.items():
        risk = result.get("risk")

        if risk is None:
            blocked[ticker] = result.get("skipped", "no completed risk assessment")
            continue

        if risk.get("hard_veto_triggered") or risk.get("verdict") == "rejected":
            reasons = risk.get("veto_reasons") or risk.get("risk_flags") or []
            detail = "; ".join(reasons[:3]) if reasons else "no reason given"
            blocked[ticker] = f"risk critic rejected: {detail}"
            continue

        eligible[ticker] = result

    return eligible, blocked


def normalise_positions(raw) -> tuple:
    """Coerce the model's positions into the shape the rest of the code expects.

    The tool schema asks for objects with a ticker and a size, but a schema
    is a strong hint, not a guarantee -- a smaller model will now and then
    hand back a bare list of tickers, or a size as a string. Coercing beats
    dying halfway through a run, and every coercion is reported rather than
    applied quietly, because a portfolio the model didn't really size is a
    thing the reader needs to know about.

    Returns (positions, notes).
    """
    positions, notes = [], []

    for entry in raw or []:
        if isinstance(entry, str):
            positions.append({
                "ticker": entry,
                "allocation_pct": 0.0,
                "conviction": "low",
                "thesis": "",
                "key_risks": [],
            })
            notes.append(f"{entry}: returned as a bare ticker with no sizing -- defaulted to 0%")
            continue

        if not isinstance(entry, dict) or not entry.get("ticker"):
            notes.append(f"discarded malformed position entry: {entry!r}")
            continue

        position = dict(entry)
        position.setdefault("conviction", "low")
        position.setdefault("thesis", "")
        position.setdefault("key_risks", [])
        try:
            position["allocation_pct"] = max(float(position.get("allocation_pct") or 0.0), 0.0)
        except (TypeError, ValueError):
            notes.append(
                f"{position['ticker']}: unparseable allocation "
                f"{position.get('allocation_pct')!r} -- defaulted to 0%"
            )
            position["allocation_pct"] = 0.0
        positions.append(position)

    return positions, notes


def ensure_envelope(portfolio: dict) -> dict:
    """Guarantee every key the caller is entitled to, whatever the model sent.

    "required" in a tool schema is a strong instruction, not an enforced
    contract -- this agent has already been observed returning positions as
    bare strings and omitting concentration_notes outright. Anything reading
    a portfolio should be able to index these keys without defending itself,
    so the defending happens once, here.
    """
    portfolio.setdefault("positions", [])
    portfolio.setdefault("excluded", [])
    portfolio.setdefault("adjustments", [])
    portfolio.setdefault("summary", "")
    portfolio.setdefault("cash_pct", 0.0)

    notes = portfolio.get("concentration_notes")
    if isinstance(notes, str):
        notes = [notes]
    elif not isinstance(notes, list):
        notes = []
    portfolio["concentration_notes"] = [str(n) for n in notes]

    return portfolio


def enforce_allocation_rules(portfolio: dict, sector_by_ticker: dict = None) -> dict:
    """Apply the constraints that must hold regardless of the model's argument.

    Order matters: normalise to 100 first, then cap. Capping first and
    renormalising afterwards scales the capped position straight back
    through its own limit.

    Cap overflow goes to cash rather than being spread across the other
    holdings -- trimming an oversized position is not a reason to buy more
    of everything else, and a book that quietly grows the names the model
    sized deliberately is worse than one that holds the difference.

    Every change is recorded in `adjustments`; silently rewriting the
    model's numbers would hide exactly the disagreement worth reading.
    """
    positions, adjustments = normalise_positions(portfolio.get("positions"))

    if not positions:
        portfolio["positions"] = []
        portfolio["cash_pct"] = 100.0
        portfolio["adjustments"] = adjustments
        return portfolio

    # 1. normalise the proposed book to 100
    cash = max(float(portfolio.get("cash_pct") or 0.0), 0.0)
    proposed = sum(p["allocation_pct"] for p in positions) + cash

    if abs(proposed - 100.0) > 0.5:
        if proposed <= 0:
            cash = 100.0
        else:
            scale = 100.0 / proposed
            for position in positions:
                position["allocation_pct"] = round(position["allocation_pct"] * scale, 2)
            cash = round(cash * scale, 2)
        adjustments.append(f"allocations summed to {proposed:.1f}% -- renormalised to 100%")

    # 2. single-position cap, overflow to cash
    for position in positions:
        if position["allocation_pct"] > MAX_POSITION_PCT:
            adjustments.append(
                f"{position['ticker']}: {position['allocation_pct']:.1f}% trimmed to "
                f"{MAX_POSITION_PCT:.1f}% (single-position limit)"
            )
            position["allocation_pct"] = MAX_POSITION_PCT

    # 3. sector cap, overflow to cash
    if sector_by_ticker:
        by_sector = {}
        for position in positions:
            sector = sector_by_ticker.get(position["ticker"]) or "unknown"
            by_sector.setdefault(sector, []).append(position)

        for sector, group in by_sector.items():
            exposure = sum(p["allocation_pct"] for p in group)
            if sector != "unknown" and exposure > MAX_SECTOR_PCT:
                scale = MAX_SECTOR_PCT / exposure
                for position in group:
                    position["allocation_pct"] = round(position["allocation_pct"] * scale, 2)
                adjustments.append(
                    f"{sector}: {exposure:.1f}% trimmed to {MAX_SECTOR_PCT:.1f}% (sector limit)"
                )

    # 4. cash absorbs every trim plus any rounding drift
    invested = round(sum(p["allocation_pct"] for p in positions), 2)
    portfolio["positions"] = sorted(positions, key=lambda p: -p["allocation_pct"])
    portfolio["cash_pct"] = round(100.0 - invested, 2)
    portfolio["adjustments"] = adjustments
    return portfolio


SECTOR_MANDATE_NOTE = (
    "This shortlist came from a scan of the {sector} sector, which is what "
    "the user explicitly asked for. Single-sector exposure is the shape of "
    "the request, not a defect in the book -- note it plainly, but do not "
    "treat it as a reason to hold cash. What you are deciding is which of "
    "these names is worth holding and at what size relative to each other. "
    "Let cash reflect your conviction in the candidates, not the fact that "
    "they share a sector.\n\n"
)


def run_portfolio_agent(results: dict, mandate: dict = None) -> dict:
    """Build a portfolio from the per-ticker results the coordinator collected.

    `mandate` carries what the coordinator knows about the request and this
    agent otherwise cannot see. A sector scan returns one sector by
    construction, so without being told, this agent reads its own shortlist
    as 100% concentrated and declines the entire book -- correct
    diversification logic applied to a question that was never about
    diversification. Isolated context is the right default; this is context
    that has to be handed over deliberately.
    """
    sector_scoped = bool(mandate and mandate.get("query_type") == "sector_scan")

    eligible, blocked = partition_candidates(results)

    if not eligible:
        return {
            "positions": [],
            "cash_pct": 100.0,
            "excluded": [{"ticker": t, "reason": r} for t, r in blocked.items()],
            "concentration_notes": [],
            "adjustments": [],
            "summary": (
                "No candidate survived analysis, so there is nothing to hold. "
                f"{len(blocked)} ticker(s) were ruled out before sizing."
            ),
        }

    prompt = ""
    if sector_scoped:
        prompt += SECTOR_MANDATE_NOTE.format(sector=mandate.get("sector") or "requested")

    prompt += (
        f"Assemble a long-term portfolio from these {len(eligible)} candidate(s).\n\n"
        f"{json.dumps(eligible, indent=2)}\n\n"
    )
    if blocked:
        prompt += (
            "Already excluded before you were called, for your summary's "
            f"context only -- do not reinstate these:\n{json.dumps(blocked, indent=2)}\n\n"
        )
    prompt += (
        "Check each candidate's sector profile, then size the book and "
        "submit it."
    )

    portfolio = run_agent(
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_functions=TOOL_FUNCTIONS,
        terminal_tool="submit_portfolio",
        user_message=prompt,
    )

    portfolio = ensure_envelope(portfolio)

    positions, coercion_notes = normalise_positions(portfolio.get("positions"))
    portfolio["positions"] = positions

    sector_by_ticker = {}
    for position in positions:
        try:
            sector_by_ticker[position["ticker"]] = get_sector_profile(position["ticker"]).get("sector")
        except Exception as exc:
            print(f"  !! sector lookup failed for {position['ticker']}: {exc}")
            sector_by_ticker[position["ticker"]] = None

    # the sector cap exists to stop a diversified book drifting into one
    # bet; on a scan the user asked for one sector, so it would only fight
    # the request and force cash the conviction doesn't call for
    portfolio = enforce_allocation_rules(
        portfolio, None if sector_scoped else sector_by_ticker
    )
    portfolio["adjustments"] = coercion_notes + portfolio["adjustments"]

    # the code-side filter is the authority on exclusions, not the model's list
    model_exclusions = {}
    for entry in portfolio.get("excluded") or []:
        if isinstance(entry, dict) and entry.get("ticker"):
            model_exclusions[entry["ticker"]] = entry.get("reason", "no reason given")
        elif isinstance(entry, str):
            model_exclusions[entry] = "no reason given"
    model_exclusions.update(blocked)
    portfolio["excluded"] = [
        {"ticker": t, "reason": r} for t, r in model_exclusions.items()
    ]

    # the model wrote its narrative before enforcement ran, so a trimmed
    # position leaves prose quoting a size the book no longer holds -- say so
    # rather than leaving the reader to reconcile the two
    if portfolio["adjustments"]:
        portfolio["summary"] = (
            f"{portfolio['summary']}\n\n[Adjusted after submission: "
            f"{len(portfolio['adjustments'])} correction(s) applied by the allocation rules -- "
            "see `adjustments`. Position sizes quoted above may predate them.]"
        )

    held = {p["ticker"] for p in portfolio["positions"]}
    reinstated = held & set(blocked)
    if reinstated:
        portfolio["positions"] = [p for p in portfolio["positions"] if p["ticker"] not in reinstated]
        portfolio["adjustments"].append(
            f"removed rejected ticker(s) the model tried to hold: {', '.join(sorted(reinstated))}"
        )
        portfolio = enforce_allocation_rules(
            portfolio, None if sector_scoped else sector_by_ticker
        )

    return portfolio


if __name__ == "__main__":
    from agents.fundamentals_agent import run_fundamentals_agent
    from agents.technical_agent import run_technical_agent
    from agents.risk_critic_agent import run_risk_critic

    tickers = sys.argv[1:] or ["NVDA", "AMD"]

    results = {}
    for ticker in tickers:
        print(f"--- {ticker}: full deep dive ---")
        f_verdict = run_fundamentals_agent(ticker)
        t_verdict = run_technical_agent(ticker)
        results[ticker] = {
            "fundamentals": f_verdict,
            "technical": t_verdict,
            "risk": run_risk_critic(f_verdict, t_verdict),
        }

    print(json.dumps(run_portfolio_agent(results), indent=2))
