"""
Tests for the coordinator's routing and screening decisions.

The specialist agents are stubbed throughout. What's under test is the
coordinator's own judgement -- which subagents to invoke, which candidates
survive a scan -- not whether Claude analyses a stock well.
"""

import pytest

import coordinator as c


def verdict(ticker, value, score):
    return {"ticker": ticker, "verdict": value, "valuation_score": score}


@pytest.fixture(autouse=True)
def stub_agents(monkeypatch):
    """Replace every specialist with something instant and deterministic."""
    monkeypatch.setattr(c, "run_fundamentals_agent",
                        lambda t: verdict(t, "fairly_valued", 50))
    monkeypatch.setattr(c, "run_technical_agent",
                        lambda t: {"ticker": t, "trend_verdict": "uptrend"})
    monkeypatch.setattr(c, "run_risk_critic",
                        lambda f=None, tv=None, ticker=None: {
                            "ticker": ticker or f["ticker"], "verdict": "approved",
                            "hard_veto_triggered": False})
    monkeypatch.setattr(c, "run_portfolio_agent",
                        lambda results: {"positions": [], "cash_pct": 100.0})


def route(query_type, tickers=(), sector=None):
    return {"query_type": query_type, "tickers": list(tickers), "sector": sector}


class TestResolveTickers:
    def test_named_tickers_win(self):
        assert c.resolve_tickers(route("single_ticker", ["AMD"])) == ["AMD"]

    def test_sector_maps_to_its_watchlist(self):
        assert c.resolve_tickers(route("sector_scan", sector="semiconductor")) == \
            c.SECTOR_WATCHLISTS["semiconductor"]

    def test_sector_matches_on_substring(self):
        """The router says 'banking'; the watchlist key is 'bank'."""
        assert c.resolve_tickers(route("sector_scan", sector="banking")) == \
            c.SECTOR_WATCHLISTS["bank"]

    def test_unknown_sector_falls_back_to_the_default_list(self):
        assert c.resolve_tickers(route("sector_scan", sector="shipping")) == c.DEFAULT_WATCHLIST


class TestShortlistForScan:
    """The gate used to be absolute -- verdict == "undervalued" or you were
    dropped -- and it returned nothing for 11 of 11 tickers across two
    sectors. A comparative question needs a comparative gate."""

    def test_produces_a_shortlist_when_nothing_is_undervalued(self):
        screened = {
            "NVDA": verdict("NVDA", "overvalued", 18), "AMD": verdict("AMD", "overvalued", 25),
            "AVGO": verdict("AVGO", "fairly_valued", 52), "TSM": verdict("TSM", "fairly_valued", 61),
            "QCOM": verdict("QCOM", "fairly_valued", 58), "INTC": verdict("INTC", "overvalued", 30),
        }
        shortlist, rest = c.shortlist_for_scan(screened)
        assert [t for t, _ in shortlist] == ["TSM", "QCOM", "AVGO"]
        assert len(rest) == 3

    def test_verdict_tier_outranks_score(self):
        screened = {
            "A": verdict("A", "fairly_valued", 90), "B": verdict("B", "undervalued", 55),
            "C": verdict("C", "undervalued", 70), "D": verdict("D", "overvalued", 99),
        }
        shortlist, _ = c.shortlist_for_scan(screened)
        assert [t for t, _ in shortlist] == ["C", "B", "A"]

    @pytest.mark.parametrize("score", [None, "high", {}, float("nan")])
    def test_unusable_scores_do_not_crash(self, score):
        screened = {"A": verdict("A", "fairly_valued", score), "B": verdict("B", "fairly_valued", 40)}
        shortlist, _ = c.shortlist_for_scan(screened)
        assert shortlist[0][0] == "B"

    def test_missing_score_key_is_tolerated(self):
        screened = {"A": {"ticker": "A", "verdict": "fairly_valued"}, "B": verdict("B", "fairly_valued", 40)}
        assert c.shortlist_for_scan(screened)[0][0][0] == "B"

    def test_fewer_candidates_than_the_limit(self):
        shortlist, rest = c.shortlist_for_scan({"A": verdict("A", "overvalued", 10)})
        assert len(shortlist) == 1 and rest == []


class TestRouting:
    def test_vague_query_asks_instead_of_scanning(self, monkeypatch):
        """Falling through to the default watchlist would run five tickers
        times three agents to answer a question nobody asked."""
        monkeypatch.setattr(c, "classify_query", lambda q: route("vague"))
        called = []
        monkeypatch.setattr(c, "run_fundamentals_agent", lambda t: called.append(t))

        out = c.run_orchestrator("what should I invest in?")
        assert out["status"] == "needs_clarification"
        assert called == []
        assert out["portfolio"] is None

    def test_risk_only_skips_fundamentals_and_technical(self, monkeypatch):
        monkeypatch.setattr(c, "classify_query", lambda q: route("risk_only", ["INTC"]))
        called = []
        monkeypatch.setattr(c, "run_fundamentals_agent", lambda t: called.append(("f", t)))
        monkeypatch.setattr(c, "run_technical_agent", lambda t: called.append(("t", t)))

        out = c.run_orchestrator("what are the risks with INTC?")
        assert called == []
        assert "risk" in out["candidates"]["INTC"]

    def test_risk_only_does_not_build_a_portfolio(self, monkeypatch):
        """That query asked what could go wrong, not what to buy."""
        monkeypatch.setattr(c, "classify_query", lambda q: route("risk_only", ["INTC"]))
        assert c.run_orchestrator("risks with INTC?")["portfolio"] is None

    def test_sector_scan_funnels_to_the_shortlist(self, monkeypatch):
        monkeypatch.setattr(c, "classify_query", lambda q: route("sector_scan", sector="semiconductor"))
        scores = {"NVDA": 90, "AMD": 80, "AVGO": 70, "TSM": 10, "QCOM": 20, "INTC": 30}
        monkeypatch.setattr(c, "run_fundamentals_agent",
                            lambda t: verdict(t, "fairly_valued", scores[t]))
        technical = []
        monkeypatch.setattr(c, "run_technical_agent",
                            lambda t: technical.append(t) or {"ticker": t})

        out = c.run_orchestrator("find undervalued semiconductor stocks")
        assert technical == ["NVDA", "AMD", "AVGO"], "only the shortlist should pay for technical"
        assert out["candidates"]["TSM"]["skipped"]
        assert "risk" not in out["candidates"]["TSM"]

    def test_scan_tags_the_basis_each_candidate_cleared_on(self, monkeypatch):
        """"cheapest in a rich sector" must never read downstream as "cheap"."""
        monkeypatch.setattr(c, "classify_query", lambda q: route("sector_scan", sector="bank"))
        monkeypatch.setattr(c, "run_fundamentals_agent",
                            lambda t: verdict(t, "undervalued" if t == "JPM" else "overvalued", 50))

        out = c.run_orchestrator("find undervalued bank stocks")
        assert out["candidates"]["JPM"]["screen"]["basis"] == "absolute"
        others = [r["screen"]["basis"] for t, r in out["candidates"].items() if t != "JPM"]
        assert set(others) == {"relative"}

    def test_single_ticker_runs_the_full_deep_dive(self, monkeypatch):
        monkeypatch.setattr(c, "classify_query", lambda q: route("single_ticker", ["AMD"]))
        out = c.run_orchestrator("is AMD a good buy?")
        assert set(out["candidates"]["AMD"]) >= {"fundamentals", "technical", "risk"}

    def test_synthesis_can_be_skipped(self, monkeypatch):
        monkeypatch.setattr(c, "classify_query", lambda q: route("single_ticker", ["AMD"]))
        assert c.run_orchestrator("is AMD a good buy?", synthesise=False)["portfolio"] is None


class TestEnvelope:
    """Every route returns the same top-level shape, so a caller never has
    to know which branch produced the answer."""

    KEYS = {"query", "query_type", "candidates", "portfolio"}

    @pytest.mark.parametrize("plan", [
        route("vague"),
        route("risk_only", ["INTC"]),
        route("single_ticker", ["AMD"]),
        route("sector_scan", sector="bank"),
    ])
    def test_shape_is_consistent(self, monkeypatch, plan):
        monkeypatch.setattr(c, "classify_query", lambda q: plan)
        assert self.KEYS <= set(c.run_orchestrator("q"))
