"""
Tests for the risk critic's veto contract.

The prompt tells this agent that a rich multiple is a flag and never a
rejection, and that hard_veto_triggered is reserved for solvency-grade
problems. On a live run it ignored both. These tests cover the code that
makes the instruction hold regardless.
"""

import pytest

from agents.risk_critic_agent import HARD_VETO_CATEGORIES, enforce_veto_contract


def assessment(**overrides):
    base = {
        "ticker": "AMD",
        "verdict": "approved",
        "hard_veto_triggered": False,
        "veto_category": "none",
        "veto_reasons": [],
        "risk_flags": [],
    }
    base.update(overrides)
    return base


# The verbatim shape returned for AMD on a live run: hard veto set, every
# stated reason a valuation or volatility concern, on a name the
# fundamentals agent had already marked overvalued.
LIVE_AMD_REJECTION = assessment(
    verdict="rejected",
    hard_veto_triggered=True,
    veto_category="none",
    veto_reasons=[
        "Extreme valuation (P/E 129x) with modest ROE (10.2%) creates execution cliff risk",
        "Multiple officer insider selling at current levels signals deteriorating management confidence",
        "Very low FCF yield (0.82%) leaves no margin for disappointment in unproven revenue recovery",
        "Combined with high beta (2.49x) and stretched entry, downside amplification is unacceptable",
    ],
    risk_flags=["Short interest moderate at 2.32% (no contrarian signal)"],
)


class TestUnjustifiedRejections:
    def test_the_live_amd_rejection_is_downgraded(self):
        out = enforce_veto_contract(dict(LIVE_AMD_REJECTION))
        assert out["verdict"] == "approved_with_caution"
        assert out["hard_veto_triggered"] is False

    def test_stated_reasons_survive_as_risk_flags(self):
        """Downgrading the severity must not discard the analysis."""
        out = enforce_veto_contract(dict(LIVE_AMD_REJECTION))
        assert out["veto_reasons"] == []
        assert len(out["risk_flags"]) == 5
        assert any("P/E 129x" in flag for flag in out["risk_flags"])

    def test_the_downgrade_is_recorded(self):
        """A rejection that quietly became a caution would hide the
        disagreement worth reading."""
        out = enforce_veto_contract(dict(LIVE_AMD_REJECTION))
        assert out["overrides"]
        assert "no solvency-grade cause" in out["overrides"][0]

    def test_hard_veto_without_a_rejection_verdict_is_still_caught(self):
        out = enforce_veto_contract(assessment(verdict="approved", hard_veto_triggered=True))
        assert out["hard_veto_triggered"] is False
        assert out["verdict"] == "approved_with_caution"

    @pytest.mark.parametrize("category", [None, "valuation", "", "high_beta"])
    def test_categories_outside_the_enum_do_not_justify_a_veto(self, category):
        out = enforce_veto_contract(assessment(verdict="rejected", veto_category=category))
        assert out["verdict"] == "approved_with_caution"


class TestJustifiedRejections:
    @pytest.mark.parametrize("category", sorted(HARD_VETO_CATEGORIES))
    def test_a_named_solvency_cause_stands(self, category):
        out = enforce_veto_contract(assessment(
            verdict="rejected", hard_veto_triggered=True, veto_category=category,
            veto_reasons=["going-concern language in the latest filing"],
        ))
        assert out["verdict"] == "rejected"
        assert out["hard_veto_triggered"] is True
        assert out["veto_reasons"] == ["going-concern language in the latest filing"]
        assert "overrides" not in out


class TestUntouchedAssessments:
    @pytest.mark.parametrize("verdict", ["approved", "approved_with_caution"])
    def test_non_rejections_pass_through_unchanged(self, verdict):
        original = assessment(verdict=verdict, risk_flags=["elevated short interest"])
        assert enforce_veto_contract(dict(original)) == original

    def test_a_caution_keeps_its_flags(self):
        out = enforce_veto_contract(assessment(
            verdict="approved_with_caution", risk_flags=["high beta", "stretched entry"],
        ))
        assert out["risk_flags"] == ["high beta", "stretched entry"]


class TestDownstreamEffect:
    """The point of the downgrade: the signal reaches the portfolio stage
    to be sized down, instead of being destroyed upstream."""

    def test_downgraded_candidate_becomes_eligible(self):
        from agents.portfolio_agent import partition_candidates

        corrected = enforce_veto_contract(dict(LIVE_AMD_REJECTION))
        results = {"AMD": {"fundamentals": {}, "technical": {}, "risk": corrected}}
        eligible, blocked = partition_candidates(results)

        assert set(eligible) == {"AMD"}
        assert blocked == {}

    def test_genuine_veto_still_blocks(self):
        from agents.portfolio_agent import partition_candidates

        vetoed = enforce_veto_contract(assessment(
            verdict="rejected", hard_veto_triggered=True, veto_category="going_concern",
            veto_reasons=["going-concern language"],
        ))
        eligible, blocked = partition_candidates({"ZZZ": {"fundamentals": {}, "technical": {}, "risk": vetoed}})

        assert eligible == {}
        assert "going-concern" in blocked["ZZZ"]
