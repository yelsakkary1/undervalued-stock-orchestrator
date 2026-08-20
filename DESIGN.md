# Design notes

Why the system is shaped the way it is, and what running it against live data taught me. See [README.md](README.md) for setup and usage.

## 1. The risk critic sees conclusions, not reasoning

The risk critic receives the fundamentals and technical agents' structured verdicts. It does not receive their reasoning trails, and that is intentional.

What I noticed during evaluation is that the critic could be biased just like humans. If someone asks my opinion before I have heard anyone else's, my take is my own. If I hear their side first, I end up measuring my verdict against theirs and refining it, even when I don't mean to.

Give a critic the full chain of thought behind a conclusion and it tends to follow that chain rather than test it. Give it only the conclusion and it has to form its own view. I wanted the second behavior, so the coordinator passes verdicts and nothing else.

## 2. The model proposes, code disposes

I started out writing the rules into the prompts. Then I watched the model break them. Now anything that has to hold every single time is checked in code, and the prompt only covers the parts where I actually want the model using its judgment.

| Checked in code | Left to the model |
|---|---|
| A rejection has to name a specific serious problem | Whether such a problem exists |
| Rejected stocks are removed before the portfolio agent runs | Which stocks to hold |
| No position over 35%, no sector over 60% | How big each position is, within those limits |
| Positions and cash always add up to 100% | How much cash to sit on |
| A rejected stock is stripped out if it reappears | The reasoning behind each holding |

None of this is caution for its own sake. Each row is there because I watched the model do something it was told not to:

- it sent back a list of ticker symbols where it was supposed to send full positions with sizes
- it left out a field it had been told was required
- it tried to hold a stock the risk critic had already rejected
- it sent the whole list of positions as one block of text instead of as a list
- it rejected a stock for being expensive, and flagged that as a severe problem

When the code overrides the model, it writes down what it changed. A portfolio that got quietly rewritten is worse than one you can disagree with.

### The one that cost me 150 requests

This one is different from the others. The rest gave me a wrong answer. This one made my code go and do something.

The model sent its list of positions as one long block of text instead of as an actual list. My code was checking whether each item in a list was text, but never checked whether the whole thing was. So it walked through that block one character at a time, treated every bracket, quote and digit as a stock, and looked each one up on Yahoo Finance. About 150 failed requests, for tickers named `{`, `%` and `;`.

Two fixes, because there were two problems. A block of text now gets read back into a list where that works, and thrown out with a note where it does not. And every symbol gets checked against what a ticker can plausibly look like before anything touches the network.

What I took from it: check what comes in before you act on it, not after.

### The veto that was not a veto

The risk critic's instructions say, in plain English, that a stock being expensive is worth flagging but is never on its own a reason to reject it, and that the severe flag is reserved for a company that might not survive, restated accounts, a broken debt agreement, or legal action.

It rejected AMD anyway. The reasons it gave were a 129x price to earnings ratio, weak cash generation and high volatility. None of those is a question about whether the company survives, and all of them were things the fundamentals agent had already accounted for when it called the stock overvalued. The same objection got counted twice, the second time at the level that kills a stock outright. An hour earlier, working from the same instructions, it had handled three other stocks correctly.

Asking more firmly was never going to fix that. The critic now has to name which specific problem justifies a rejection, picked from a fixed list. If it names none and rejects anyway, `enforce_veto_contract` downgrades the rejection to a caution, keeps the reasons it gave as flags, and records the override:

```json
"overrides": ["rejection downgraded to approved_with_caution: no solvency-grade cause named (veto_category='none'). Stated reasons kept as risk flags."]
```

The model still decides whether a serious problem exists. It just cannot spend a rejection without saying which one. The concern still reaches the portfolio stage, where it gets sized down instead of being thrown out a step earlier.

Telling a model what shape its answer has to take is a strong request. It is not a guarantee.

## 3. A comparative question needs a comparative gate

Another issue I came across was that when I asked the system to find undervalued stocks in a sector, it struggled to return any, even though it had carried out a full analysis on every name. The reason was that I was testing each stock against an absolute bar instead of against the others. I replaced that bar with a ranking, so a sector run always comes back with a shortlist I can act on.

It's like asking my friends who has the fastest car and they come back saying all our cars do 0-60 in under 5 seconds. It's a useful insight, but it still doesn't answer my question.

The bar was one line of code: a stock went through only if the fundamentals agent called it undervalued. Run against real prices, that rejected all 11 stocks across two sectors. The bar was not too high. It was the wrong kind of test. I had asked a question about which stocks were best and answered it with a test that only knows good from bad.

The fix came in two parts. The fundamentals agent now gives a score out of 100 as well as its verdict, because three options are not enough to say "the cheapest of an expensive bunch". The coordinator sorts on the verdict first and the score second, and takes the top three.

The three that go through carry a note saying how they got there:

```json
"screen": { "basis": "relative", "sector_rank": 1, "of": 6 }
```

The portfolio agent reads that and gives those stocks smaller positions than it would give something genuinely cheap.

The bar turned out to be only half the problem. The risk critic was separately killing stocks for being expensive, re-deciding something the fundamentals agent had already settled and whose verdict was sitting right there in its prompt. Names that got past the bar died one step later for the same reason. Telling it to stay off valuation was not enough on its own, which is what led to the veto contract above.

## 4. Isolated context still has to be handed over

One thing I learned through investing is that diversification has to be a key habit, to prevent overexposure to one sector or asset class in case that sector dips. So I put a 60% sector cap in the code.

Testing the system, I figured out that the portfolio agent was refusing to hold stocks for the wrong reason. It gave a no-buy verdict on several names just because they all belonged to the same sector and breached that 60% rule, not because they had red flags.

Two things had to be true at once for that to happen. A sector scan returns one sector by construction, and the portfolio agent cannot see the query that produced its shortlist. Left uninformed, it read its own candidates as 100% sector concentration and declined the entire book. The diversification logic was sound. It was being applied to a question that was never about diversification, and every scan would have hit it: ask for bank stocks, get bank stocks, then hold none of them for being all banks.

The coordinator now passes a `mandate` describing the request. On a scan the model is told that single-sector exposure is the shape of the ask rather than a defect, and the code-side sector cap is waived, since that cap exists to stop a diversified book drifting into one bet.

Isolated context is still the right default. This was context that had to be handed over deliberately, and I had not done it.

## What I would do next

- **Parallelize the scan.** Six tickers currently run their agents one at a time.
- **Cache yfinance lookups.** A scan refetches `.info` on nearly every tool call.
- **Token and cost telemetry per agent**, which would let me put real numbers on what the routing saves.
- **Port it to the Agent SDK**, and compare the hand-rolled version against real subagent spawning, tool restrictions and forking.
