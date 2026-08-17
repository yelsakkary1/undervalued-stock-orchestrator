"""
Tests for the portfolio agent's code-side guardrails.

The model proposes an allocation and argues for it; this layer decides
what actually holds. Everything here is the "disposes" half, so none of it
needs the model -- which is the point. A constraint you can only verify by
asking Claude nicely is not a constraint.
"""

import pytest

from agents import portfolio_agent as pa
from agents.portfolio_agent import (
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    ensure_envelope,
    enforce_allocation_rules,
    normalise_positions,
    partition_candidates,
    run_portfolio_agent,
)


def book(positions, cash=0.0):
    return {"positions": positions, "cash_pct": cash}


def pos(ticker, pct):
    return {"ticker": ticker, "allocation_pct": pct}


def total(portfolio):
    return round(sum(p["allocation_pct"] for p in portfolio["positions"]) + portfolio["cash_pct"], 2)


def candidate(ticker, risk):
    return {"fundamentals": {"ticker": ticker}, "technical": {"ticker": ticker}, "risk": risk}


class TestPartitionCandidates:
    """The risk critic's veto is applied here, in code, before the
    portfolio model is invoked. A veto a downstream model can talk itself
    out of is not a veto."""

    def test_approved_candidates_are_eligible(self, approved):
        eligible, blocked = partition_candidates({"AAA": candidate("AAA", approved("AAA"))})
        assert set(eligible) == {"AAA"}
        assert blocked == {}

    def test_rejected_verdict_is_blocked(self):
        results = {"BBB": candidate("BBB", {
            "ticker": "BBB", "verdict": "rejected", "hard_veto_triggered": False,
            "veto_reasons": ["extreme P/E on contracting revenue"],
        })}
        eligible, blocked = partition_candidates(results)
        assert eligible == {}
        assert "extreme P/E" in blocked["BBB"]

    def test_hard_veto_blocks_even_an_approved_verdict(self):
        """hard_veto_triggered and verdict can disagree; the veto wins."""
        results = {"CCC": candidate("CCC", {
            "ticker": "CCC", "verdict": "approved", "hard_veto_triggered": True,
            "veto_reasons": ["going-concern language in filing"],
        })}
        eligible, blocked = partition_candidates(results)
        assert eligible == {}
        assert "going-concern" in blocked["CCC"]

    def test_candidate_without_full_analysis_is_blocked(self):
        """Sector-scan names outside the shortlist never got a risk pass,
        so there is nothing to size them against."""
        results = {"DDD": {"fundamentals": {}, "skipped": "ranked 5 of 6 on fundamentals"}}
        eligible, blocked = partition_candidates(results)
        assert eligible == {}
        assert "ranked 5 of 6" in blocked["DDD"]


class TestNormalisePositions:
    """"required" in a tool schema is a strong instruction, not an enforced
    contract. Every shape below was returned by the live model."""

    def test_bare_ticker_strings_are_coerced(self):
        positions, notes = normalise_positions(["NVDA", "JPM"])
        assert [p["ticker"] for p in positions] == ["NVDA", "JPM"]
        assert all(p["allocation_pct"] == 0.0 for p in positions)
        assert len(notes) == 2

    def test_unparseable_allocation_defaults_to_zero(self):
        positions, notes = normalise_positions([{"ticker": "X", "allocation_pct": "40%"}])
        assert positions[0]["allocation_pct"] == 0.0
        assert "unparseable" in notes[0]

    def test_entries_without_a_ticker_are_discarded(self):
        positions, notes = normalise_positions([{"nope": 1}, 42, None])
        assert positions == []
        assert len(notes) == 3

    def test_missing_optional_fields_are_filled(self):
        positions, _ = normalise_positions([{"ticker": "X", "allocation_pct": 10}])
        assert positions[0]["conviction"] == "low"
        assert positions[0]["key_risks"] == []

    def test_negative_allocations_are_floored_at_zero(self):
        positions, _ = normalise_positions([{"ticker": "X", "allocation_pct": -5}])
        assert positions[0]["allocation_pct"] == 0.0


class TestEnsureEnvelope:
    """The live model omitted concentration_notes despite it being
    required. Callers should not have to defend every access."""

    KEYS = {"positions", "excluded", "adjustments", "summary", "cash_pct", "concentration_notes"}

    def test_every_key_exists_even_from_nothing(self):
        assert self.KEYS <= set(ensure_envelope({}))

    def test_notes_as_a_bare_string_become_a_list(self):
        assert ensure_envelope({"concentration_notes": "too much tech"})["concentration_notes"] == ["too much tech"]

    @pytest.mark.parametrize("junk", [42, None, {"a": 1}])
    def test_unusable_notes_become_empty(self, junk):
        assert ensure_envelope({"concentration_notes": junk})["concentration_notes"] == []

    def test_existing_values_are_preserved(self):
        out = ensure_envelope({"summary": "kept", "cash_pct": 12.0})
        assert out["summary"] == "kept" and out["cash_pct"] == 12.0


class TestAllocationRules:
    def test_single_position_is_capped(self):
        out = enforce_allocation_rules(book([pos("AAA", 70.0), pos("BBB", 30.0)]))
        assert max(p["allocation_pct"] for p in out["positions"]) <= MAX_POSITION_PCT
        assert total(out) == 100.0

    def test_cap_survives_renormalisation(self):
        """Regression: capping before renormalising scaled the capped
        position straight back through its own limit (70 -> 35 -> 53.85)."""
        out = enforce_allocation_rules(book([pos("AAA", 70.0), pos("BBB", 30.0)]))
        assert out["positions"][0]["allocation_pct"] == MAX_POSITION_PCT

    def test_trim_overflow_goes_to_cash_not_other_positions(self):
        """Trimming one name is not a reason to buy more of another."""
        out = enforce_allocation_rules(book([pos("AAA", 70.0), pos("BBB", 30.0)]))
        held = {p["ticker"]: p["allocation_pct"] for p in out["positions"]}
        assert held["BBB"] == 30.0
        assert out["cash_pct"] == 35.0

    def test_sector_exposure_is_capped(self):
        sectors = {"NVDA": "Technology", "AMD": "Technology", "AVGO": "Technology", "JPM": "Financials"}
        out = enforce_allocation_rules(
            book([pos("NVDA", 30.0), pos("AMD", 30.0), pos("AVGO", 30.0), pos("JPM", 10.0)]),
            sectors,
        )
        tech = sum(p["allocation_pct"] for p in out["positions"] if sectors[p["ticker"]] == "Technology")
        assert tech <= MAX_SECTOR_PCT
        assert total(out) == 100.0

    def test_unknown_sector_is_not_capped(self):
        """Missing sector data must not silently trim a legitimate book.

        These three sum to 90 with no cash, so renormalisation still runs --
        what must not happen is a sector trim on top of it.
        """
        out = enforce_allocation_rules(
            book([pos("AAA", 30.0), pos("BBB", 30.0), pos("CCC", 30.0)]),
            {"AAA": None, "BBB": None, "CCC": None},
        )
        assert not any("sector limit" in a for a in out["adjustments"])
        assert total(out) == 100.0

    @pytest.mark.parametrize("positions,cash", [
        ([pos("AAA", 30.0), pos("BBB", 30.0)], 0.0),
        ([pos("AAA", 35.0), pos("BBB", 35.0)], 70.0),
        ([pos("AAA", 100.0)], 100.0),
        ([], 0.0),
    ])
    def test_book_always_totals_one_hundred(self, positions, cash):
        assert total(enforce_allocation_rules(book(positions, cash))) == 100.0

    def test_empty_book_is_all_cash(self):
        out = enforce_allocation_rules(book([]))
        assert out["positions"] == [] and out["cash_pct"] == 100.0

    def test_positions_are_sorted_by_size(self):
        out = enforce_allocation_rules(book([pos("A", 10.0), pos("B", 30.0), pos("C", 20.0)]))
        assert [p["ticker"] for p in out["positions"]] == ["B", "C", "A"]

    def test_adjustments_record_every_change(self):
        out = enforce_allocation_rules(book([pos("AAA", 90.0), pos("BBB", 30.0)]))
        assert out["adjustments"], "silent rewrites hide the disagreement worth reading"


class TestRunPortfolioAgent:
    @pytest.fixture(autouse=True)
    def _no_network_or_model(self, monkeypatch):
        monkeypatch.setattr(pa, "get_sector_profile", lambda ticker: {"sector": "Technology"})

    def test_model_cannot_reinstate_a_rejected_ticker(self, monkeypatch, approved):
        """The last line of defence: even if the model lists a vetoed name
        in its positions, it does not reach the book."""
        results = {
            "AAA": candidate("AAA", approved("AAA")),
            "BBB": candidate("BBB", {"ticker": "BBB", "verdict": "rejected",
                                     "hard_veto_triggered": False, "veto_reasons": ["covenant risk"]}),
        }
        monkeypatch.setattr(pa, "run_agent", lambda **kw: {
            "positions": [pos("AAA", 50.0), pos("BBB", 50.0)],
            "cash_pct": 0.0, "excluded": [], "concentration_notes": [], "summary": "s",
        })
        out = run_portfolio_agent(results)
        assert [p["ticker"] for p in out["positions"]] == ["AAA"]
        assert "BBB" in {e["ticker"] for e in out["excluded"]}
        assert total(out) == 100.0

    def test_no_eligible_candidates_short_circuits_to_cash(self):
        """Must not spend a model call to conclude an empty list is empty."""
        results = {"DDD": {"fundamentals": {}, "skipped": "did not clear the screen"}}
        out = run_portfolio_agent(results)
        assert out["positions"] == [] and out["cash_pct"] == 100.0
        assert {e["ticker"] for e in out["excluded"]} == {"DDD"}

    def test_malformed_model_output_still_yields_a_valid_book(self, monkeypatch, approved):
        """The exact live failure: positions as bare strings, no notes."""
        monkeypatch.setattr(pa, "run_agent", lambda **kw: {"positions": ["AAA"], "cash_pct": 0.0})
        out = run_portfolio_agent({"AAA": candidate("AAA", approved("AAA"))})
        assert total(out) == 100.0
        assert out["concentration_notes"] == []
        assert any("bare ticker" in a for a in out["adjustments"])


class TestSectorMandate:
    """A sector scan returns one sector by construction. Without being told
    that was the request, this agent reads its own shortlist as 100%
    concentrated and declines the whole book -- correct diversification
    logic applied to a question that was never about diversification."""

    @pytest.fixture(autouse=True)
    def _stub_sector(self, monkeypatch):
        monkeypatch.setattr(pa, "get_sector_profile", lambda ticker: {"sector": "Financial Services"})

    def _run(self, monkeypatch, mandate, approved):
        monkeypatch.setattr(pa, "run_agent", lambda **kw: {
            "positions": [pos("BAC", 30.0), pos("JPM", 30.0), pos("WFC", 30.0)],
            "cash_pct": 10.0, "excluded": [], "concentration_notes": [], "summary": "s",
        })
        results = {t: candidate(t, approved(t)) for t in ("BAC", "JPM", "WFC")}
        return run_portfolio_agent(results, mandate=mandate)

    def test_sector_cap_is_waived_for_a_sector_scan(self, monkeypatch, approved):
        out = self._run(monkeypatch, {"query_type": "sector_scan", "sector": "bank"}, approved)
        assert not any("sector limit" in a for a in out["adjustments"])
        assert sum(p["allocation_pct"] for p in out["positions"]) == 90.0

    def test_sector_cap_still_applies_without_a_sector_mandate(self, monkeypatch, approved):
        out = self._run(monkeypatch, None, approved)
        assert any("sector limit" in a for a in out["adjustments"])

    def test_single_ticker_mandate_keeps_the_cap(self, monkeypatch, approved):
        out = self._run(monkeypatch, {"query_type": "single_ticker", "sector": None}, approved)
        assert any("sector limit" in a for a in out["adjustments"])

    def test_the_mandate_reaches_the_model(self, monkeypatch, approved):
        seen = {}
        monkeypatch.setattr(pa, "run_agent", lambda **kw: seen.update(kw) or {
            "positions": [], "cash_pct": 100.0, "excluded": [],
            "concentration_notes": [], "summary": "s",
        })
        run_portfolio_agent({"BAC": candidate("BAC", approved("BAC"))},
                            mandate={"query_type": "sector_scan", "sector": "banking"})
        assert "banking" in seen["user_message"]
        assert "not a defect" in seen["user_message"]

    def test_no_mandate_note_without_a_scan(self, monkeypatch, approved):
        seen = {}
        monkeypatch.setattr(pa, "run_agent", lambda **kw: seen.update(kw) or {
            "positions": [], "cash_pct": 100.0, "excluded": [],
            "concentration_notes": [], "summary": "s",
        })
        run_portfolio_agent({"BAC": candidate("BAC", approved("BAC"))}, mandate=None)
        assert "not a defect" not in seen["user_message"]


class TestSummaryReconciliation:
    """The model writes its narrative before enforcement runs, so a trimmed
    position leaves prose quoting a size the book no longer holds."""

    @pytest.fixture(autouse=True)
    def _stub_sector(self, monkeypatch):
        monkeypatch.setattr(pa, "get_sector_profile", lambda ticker: {"sector": "Financials"})

    def test_summary_flags_that_sizes_were_adjusted(self, monkeypatch, approved):
        monkeypatch.setattr(pa, "run_agent", lambda **kw: {
            "positions": [pos("JPM", 50.0), pos("BAC", 25.0)], "cash_pct": 25.0,
            "excluded": [], "concentration_notes": [], "summary": "JPM earns a 50% core position.",
        })
        out = run_portfolio_agent({t: candidate(t, approved(t)) for t in ("JPM", "BAC")})
        assert out["positions"][0]["allocation_pct"] == MAX_POSITION_PCT
        assert "Adjusted after submission" in out["summary"]

    def test_untouched_book_keeps_a_clean_summary(self, monkeypatch, approved):
        monkeypatch.setattr(pa, "run_agent", lambda **kw: {
            "positions": [pos("JPM", 30.0), pos("BAC", 25.0)], "cash_pct": 45.0,
            "excluded": [], "concentration_notes": [], "summary": "A measured book.",
        })
        out = run_portfolio_agent({t: candidate(t, approved(t)) for t in ("JPM", "BAC")})
        assert out["adjustments"] == []
        assert out["summary"] == "A measured book."
