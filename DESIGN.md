# Design notes

Why the system is shaped the way it is, and what running it against live data taught me. See [README.md](README.md) for setup and usage.

## 1. The risk critic sees conclusions, not reasoning

The risk critic receives the fundamentals and technical agents' structured verdicts. It does not receive their reasoning trails, and that is deliberate.

Give a critic the full chain of thought behind a conclusion and it tends to follow that chain rather than test it. Give it only the conclusion and it has to form its own view. I wanted the second behavior, so the coordinator passes verdicts and nothing else.

## 2. The model proposes, code disposes

Advisory judgment belongs to the model. Anything that has to hold regardless of how convincing the model's reasoning sounds belongs in Python.

| Enforced in code | Left to the model |
|---|---|
| A rejection requires a named solvency-grade cause | Whether such a cause exists |
| Risk-critic vetoes filter candidates before the portfolio agent runs | Which names to hold |
| Position cap (35%), sector cap (60%) | Position sizing within the caps |
| Allocations always total 100% | How much cash to carry |
| Rejected tickers stripped if reinstated | The thesis for each holding |

This split is not defensive habit. It comes from things I watched the models actually do against the live API:

- the portfolio agent returned `positions` as bare ticker strings instead of objects
- it omitted `concentration_notes` even though the schema marks it required
- it listed a risk-rejected ticker among its holdings
- it serialized `positions` as a JSON *string* instead of an array
- the risk critic rejected a candidate on valuation grounds with `hard_veto_triggered` set

Every correction the code makes gets recorded in `adjustments` instead of being applied quietly, because a rewritten allocation you cannot see is worse than one you can argue with.

### The stringified payload

This one is worth separating from the others, because the damage was not a bad answer. It was a bad action.

`normalize_positions` checked whether each *entry* was a string. It never checked whether the payload itself was one. So when the model handed back its positions array as JSON text, `for entry in raw` walked the string character by character. Every brace, quote, colon and digit became a "position", and each of those was then sent to Yahoo Finance as a ticker lookup. One malformed field produced roughly 150 failed HTTP requests for symbols like `{`, `%` and `;`.

The fix has two halves, because the failure did. A string payload is now parsed back into a list where possible and discarded with a note where not, and any other non-list type is rejected outright. Separately, every symbol is screened against a plausibility pattern before it reaches the network, so no future malformation can turn one bad field into a hundred requests.

The lesson I took from it: validate at the boundary, not after the side effects have already fired.

### The veto that was not a veto

The risk critic's prompt says, in plain language, that a rich multiple is a flag and never a rejection, and that `hard_veto_triggered` is reserved for going-concern language, restatements, covenant breaches and legal action.

It rejected AMD anyway. The stated grounds were a 129x P/E, a low FCF yield and high beta. None of those is a solvency question, and all of them were concerns the fundamentals agent had already folded into an `overvalued` verdict. The same objection got counted twice, the second time at the severity that stops a candidate dead. An hour earlier, on the identical prompt, the same agent had produced three perfectly correct `approved_with_caution` verdicts during a sector scan.

Asking more firmly was never going to fix that. `submit_risk_assessment` now requires a `veto_category` naming which solvency-grade problem applies, and `enforce_veto_contract` downgrades any rejection categorized `none` to `approved_with_caution`. The stated reasons survive as risk flags, and the override is recorded:

```json
"overrides": ["rejection downgraded to approved_with_caution: no solvency-grade cause named (veto_category='none'). Stated reasons kept as risk flags."]
```

The model still decides whether a solvency-grade problem exists. It just cannot spend a veto without naming one. The signal now reaches the portfolio stage to be sized down instead of being destroyed upstream.

I keep coming back to the same conclusion here. A schema is a strong instruction, not an enforced contract.

## 3. A comparative question needs a comparative gate

The sector scan originally let a ticker through only if fundamentals returned `undervalued`. Against live data that gate rejected 11 of 11 tickers across two sectors, and every scan came back with an empty portfolio.

Strictness was not the problem. "Find me undervalued semiconductors" is a comparative question, and I was answering it with a bar set in isolation. The coordinator had decomposed a broad research task so narrowly that the broad task got no coverage at all.

The fix came in two parts. The fundamentals agent now emits a `valuation_score` alongside its verdict, since a three-way enum flattens exactly the gradation that "least expensive of a rich sector" depends on. The coordinator ranks candidates on `(verdict tier, score)` and carries the top 3 forward, which always produces something to look at.

Survivors carry the basis they cleared on, so nothing downstream confuses *best available* with *cheap*:

```json
"screen": { "basis": "relative", "sector_rank": 1, "of": 6 }
```

The portfolio agent reads that tag and sizes relative-basis candidates more conservatively.

The scan gate turned out to be only half the problem. The risk critic was independently killing candidates on valuation grounds, re-deciding a question the fundamentals agent had already settled and whose verdict was sitting in its own prompt. Names that survived the gate died one stage later for the same reason. Narrowing its remit in the prompt was not enough on its own, which is what led to the veto contract above.

## 4. Isolated context still has to be handed over

A sector scan returns one sector by construction, and the portfolio agent cannot see the query that produced its shortlist.

Left uninformed, it read its own candidates as 100% sector concentration and declined the entire book. The diversification logic was sound. It was being applied to a question that was never about diversification, and every scan would have hit it: ask for bank stocks, get bank stocks, then hold none of them for being all banks.

The coordinator now passes a `mandate` describing the request. On a scan the model is told that single-sector exposure is the shape of the ask rather than a defect, and the code-side sector cap is waived, since that cap exists to stop a diversified book drifting into one bet.

Isolated context is still the right default. This was context that had to be handed over deliberately, and I had not done it.

## What I would do next

- **Parallelize the scan.** Six tickers currently run their agents one at a time.
- **Cache yfinance lookups.** A scan refetches `.info` on nearly every tool call.
- **Token and cost telemetry per agent**, which would let me put real numbers on what the routing saves.
- **Port it to the Agent SDK**, and compare the hand-rolled version against real subagent spawning, tool restrictions and forking.
