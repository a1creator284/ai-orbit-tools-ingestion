"""Regression tests for the 100-point scoring rubric, scorer and filter.

These tests lock the *production* contract of :mod:`src.scoring` as it is
actually implemented — they are deliberately not a redesign:

* the eight rubric weights are exactly ``25/20/15/15/10/5/5/5 = 100`` and no
  component can pay out more than its own weight;
* a score is a deterministic, reproducible function of the record plus an
  injectable ``today`` — no clock, no randomness, no float drift;
* the total is exactly the sum of the eight component awards, always in
  ``[0, 100]``;
* missing or unverifiable evidence earns **nothing**, is never replaced by a
  plausible default, and is recorded (``missing_evidence`` /
  ``unevidenced_components``);
* "current usage" really means current: a dead or unverifiable product cannot
  buy its way past a threshold with historical fame;
* currency adjustments are transparent — a *note* must not silently discount,
  a *discount* must move points and be visible, and neither may be applied
  twice;
* the §5 thresholds are applied verbatim (``<60`` reject, ``60-69`` skip,
  ``70-79`` selective on evidence, ``>=80`` include) and every rejection or
  skip keeps its reason.

Everything here is offline and date-pinned: no network, no wall clock.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

import pytest

from src.core.config import ScoringConfig, Settings
from src.core.errors import ConfigError
from src.models.base import SourceRef
from src.models.enums import (
    AICapability,
    IOFormat,
    OpenSourceStatus,
    Platform,
    PricingModel,
    QualityBand,
    RejectionReason,
    SignupRequirement,
    ToolStatus,
    VerificationStatus,
)
from src.models.tool import ScoreBreakdown, Tool
from src.scoring.filter import (
    MIN_SELECTIVE_EVIDENCE_CONFIDENCE,
    SELECTIVE_REQUIREMENTS,
    Outcome,
    QualityFilter,
    rejection_rows,
    selective_evidence,
)
from src.scoring.rubric import (
    BANDS,
    COMPONENTS,
    CRITERIA,
    INCLUDE_AT_OR_ABOVE,
    MAX_TOTAL,
    REJECT_BELOW,
    SKIP_BELOW,
    WEIGHTS,
    quantize,
    share_points,
    validate_rubric,
    weights_match_rubric,
)
from src.scoring.scorer import (
    DEAD_STATUSES,
    FRESH_VERIFICATION_DAYS,
    LIVE_STATUSES,
    STALE_VERIFICATION_DAYS,
    QualityScorer,
    _ComponentBuilder,
    assess_currency,
)

#: Every date-sensitive assertion is pinned to this day, so the suite cannot
#: start failing simply because time passed.
TODAY = date(2026, 9, 15)

#: The rubric, written out again by hand. If a weight is ever edited, this
#: literal has to be edited too — which is the point.
EXPECTED_WEIGHTS = {
    "product_quality_capability": 25,
    "real_user_value": 20,
    "current_usage_adoption": 15,
    "activity_maintenance": 15,
    "product_maturity_reliability": 10,
    "recency_momentum": 5,
    "differentiation": 5,
    "information_quality_verifiability": 5,
}


# ================================================================ fixtures
def _source(name: str = "There's An AI For That", url: str | None = None) -> SourceRef:
    return SourceRef(name=name, url=url or "https://theresanaiforthat.com/ai/example")


def make_tool(**overrides: object) -> Tool:
    """A thin but syntactically valid record: almost nothing is evidenced."""
    payload: dict[str, object] = {
        "id": "00000000-0000-5000-8000-000000000001",
        "name": "Example Tool",
    }
    payload.update(overrides)
    return Tool(**payload)


def make_strong_tool(**overrides: object) -> Tool:
    """A fully evidenced, currently verified record (the happy path)."""
    payload: dict[str, object] = {
        "id": "00000000-0000-5000-8000-000000000002",
        "name": "Jasper",
        "company": "Jasper AI, Inc.",
        "website": "https://www.jasper.ai/",
        "logo_url": "https://www.jasper.ai/logo.png",
        "country": "US",
        "version": "4.2",
        "launch_date": date(2026, 5, 1),
        "launch_date_precision": "day",
        "status": ToolStatus.ACTIVE,
        "short_description": "AI copywriting platform for marketing teams.",
        "detailed_overview": "A long, substantive overview of the product. " * 12,
        "primary_task": "ai writing",
        "tags": ["writing", "marketing"],
        "key_features": ["brand voice", "templates", "campaigns", "seo", "chat", "api"],
        "use_cases": ["blog posts", "ad copy", "emails", "landing pages"],
        "ai_capabilities": [
            AICapability.TEXT_GENERATION,
            AICapability.TEXT_SUMMARIZATION,
            AICapability.TRANSLATION,
        ],
        "inputs": [IOFormat.TEXT, IOFormat.PROMPT],
        "outputs": [IOFormat.TEXT, IOFormat.MARKDOWN],
        "platforms": [Platform.WEB, Platform.API, Platform.CHROME_EXTENSION],
        "integrations": ["Slack", "Zapier", "Notion", "Chrome", "Surfer"],
        "has_api": True,
        "api_docs_url": "https://developers.jasper.ai/",
        "open_source_status": OpenSourceStatus.PROPRIETARY,
        "signup_requirement": SignupRequirement.REQUIRED,
        "pros": ["strong templates", "good integrations", "team features"],
        "cons": ["expensive"],
        "limitations": ["no on-prem deployment"],
        "aiorbit_summary": "Mature copywriting suite for marketing teams.",
        "last_verified_date": date(2026, 9, 1),
        "discovery_sources": [
            _source(),
            _source("Futurepedia", "https://www.futurepedia.io/tool/jasper"),
        ],
        "pricing": {
            "model": PricingModel.SUBSCRIPTION,
            "has_free_trial": True,
            "pricing_verified_at": date(2026, 9, 1),
        },
        "adoption": {
            "monthly_visits": 6_000_000,
            "github_stars": 21_000,
            "product_hunt_upvotes": 2_500,
            "directory_saves": 12_000,
            "review_count": 600,
            "review_rating": 4.6,
            "review_platform": "G2",
            "notable_customers": ["Acme", "Globex", "Initech", "Umbrella", "Stark"],
            "funding_raised_usd": 125_000_000.0,
        },
        "verification": {
            "status": VerificationStatus.VERIFIED,
            "is_accessible": True,
            "official_url_checked": "https://www.jasper.ai/",
            "verification_sources": [_source("Official website", "https://www.jasper.ai/")],
            "checked_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        },
    }
    payload.update(overrides)
    return Tool(**payload)


@pytest.fixture
def scorer() -> QualityScorer:
    """Scorer pinned to :data:`TODAY` and to the canonical rubric."""
    return QualityScorer(ScoringConfig(), today=TODAY)


@pytest.fixture
def fltr() -> QualityFilter:
    return QualityFilter(ScoringConfig())


def breakdown_with(total: float, **overrides: object) -> ScoreBreakdown:
    """A :class:`ScoreBreakdown` whose components sum to exactly ``total``.

    Points are poured into the components in rubric order, respecting each
    component's weight, so the result is a schema-valid breakdown — the filter
    is then exercised on a real score object rather than a mock.
    """
    if not 0 <= total <= MAX_TOTAL:
        raise ValueError("total must be within the rubric range")
    remaining = float(total)
    values: dict[str, float] = {}
    for name in COMPONENTS:
        take = min(float(WEIGHTS[name]), remaining)
        values[name] = quantize(take)
        remaining = quantize(remaining - take)
    return ScoreBreakdown(**values, **overrides)  # type: ignore[arg-type]


def scored_tool(total: float, **overrides: object) -> Tool:
    """A record that clears every *selective* evidence gate, scored ``total``."""
    payload: dict[str, object] = {
        "short_description": "A clear, usable description of what this tool does.",
        "discovery_sources": [_source()],
        "verification": {"status": VerificationStatus.VERIFIED},
    }
    payload.update(overrides)
    tool = make_tool(**payload)
    tool.quality = breakdown_with(
        total, currency_state="current", evidence_confidence=0.85
    )
    return tool


# ========================================================== rubric weights
class TestRubricWeights:
    """The eight weights are the contract: 25/20/15/15/10/5/5/5 = 100."""

    def test_exact_eight_component_weights(self) -> None:
        assert dict(WEIGHTS) == EXPECTED_WEIGHTS
        assert len(WEIGHTS) == 8

    def test_weights_sum_to_exactly_one_hundred(self) -> None:
        assert sum(WEIGHTS.values()) == 100
        assert quantize(sum(float(v) for v in WEIGHTS.values())) == MAX_TOTAL

    def test_weight_values_in_descending_rubric_order(self) -> None:
        assert [WEIGHTS[name] for name in COMPONENTS] == [25, 20, 15, 15, 10, 5, 5, 5]

    def test_component_tuple_matches_the_weight_table(self) -> None:
        assert set(COMPONENTS) == set(WEIGHTS)
        assert len(COMPONENTS) == len(set(COMPONENTS)) == 8

    def test_every_component_declares_criteria_that_cannot_overflow_it(self) -> None:
        assert set(CRITERIA) == set(COMPONENTS)
        for component, criteria in CRITERIA.items():
            assert criteria, f"{component} declares no sub-criteria"
            assert quantize(sum(c.share for c in criteria), 4) <= 1.0

    def test_criterion_keys_are_unique_within_a_component(self) -> None:
        for component, criteria in CRITERIA.items():
            keys = [c.key for c in criteria]
            assert len(keys) == len(set(keys)), f"{component} has duplicate criteria"

    def test_bands_and_thresholds_are_the_guideline_values(self) -> None:
        assert dict(BANDS) == {
            "exceptional": 90,
            "excellent": 80,
            "good": 70,
            "average": 60,
        }
        assert (REJECT_BELOW, SKIP_BELOW, INCLUDE_AT_OR_ABOVE) == (60, 70, 80)

    def test_schema_field_bounds_equal_the_rubric_weights(self) -> None:
        """``ScoreBreakdown`` bounds must track the rubric, not drift from it."""
        fields = ScoreBreakdown.model_fields
        for name, weight in WEIGHTS.items():
            bounds = [m for m in fields[name].metadata if hasattr(m, "le")]
            assert bounds, f"{name} has no upper bound in the schema"
            assert float(bounds[0].le) == float(weight)

    def test_shipped_config_matches_the_rubric(self) -> None:
        assert weights_match_rubric(ScoringConfig().weights)
        assert weights_match_rubric(Settings().scoring.weights)

    def test_default_thresholds_are_not_relaxed_by_the_config(self) -> None:
        config = ScoringConfig()
        assert config.reject_below == REJECT_BELOW
        assert config.skip_below == SKIP_BELOW
        assert config.include_at_or_above == INCLUDE_AT_OR_ABOVE

    def test_validate_rubric_accepts_the_canonical_table(self) -> None:
        validate_rubric()
        validate_rubric(dict(WEIGHTS))

    @pytest.mark.parametrize(
        "tampered",
        [
            pytest.param({**EXPECTED_WEIGHTS, "recency_momentum": 6}, id="sums-to-101"),
            pytest.param({**EXPECTED_WEIGHTS, "differentiation": 4}, id="sums-to-99"),
            pytest.param(
                {k: v for k, v in EXPECTED_WEIGHTS.items() if k != "differentiation"},
                id="missing-component",
            ),
            pytest.param({**EXPECTED_WEIGHTS, "vibes": 0}, id="unknown-component"),
        ],
    )
    def test_validate_rubric_rejects_a_tampered_table(self, tampered: dict) -> None:
        with pytest.raises(ConfigError):
            validate_rubric(tampered)

    def test_share_points_converts_a_share_into_points(self) -> None:
        assert share_points("product_quality_capability", 1.0) == 25.0
        assert share_points("product_quality_capability", 0.30) == 7.5
        assert share_points("recency_momentum", 1.0) == 5.0

    def test_quantize_is_half_up_and_stable(self) -> None:
        assert quantize(0.125) == 0.13
        assert quantize(2.675) == 2.68
        assert quantize(quantize(1 / 3)) == quantize(1 / 3)


# ====================================================== component bounding
class TestComponentBounds:
    """No component may ever exceed its weight, and no total may exceed 100."""

    @pytest.mark.parametrize(
        "tool_factory",
        [
            pytest.param(make_tool, id="thin-record"),
            pytest.param(make_strong_tool, id="strong-record"),
        ],
    )
    def test_each_component_is_within_zero_and_its_maximum(
        self, scorer: QualityScorer, tool_factory
    ) -> None:
        result = scorer.score(tool_factory())
        for name, points in result.component_points().items():
            assert 0.0 <= points <= float(WEIGHTS[name]), name
            assert result.max_points[name] == float(WEIGHTS[name])

    def test_an_over_stuffed_record_still_cannot_exceed_any_weight(
        self, scorer: QualityScorer
    ) -> None:
        """Piling on evidence saturates components; it cannot overflow them."""
        tool = make_strong_tool(
            key_features=[f"feature {i}" for i in range(40)],
            use_cases=[f"use case {i}" for i in range(40)],
            integrations=[f"integration {i}" for i in range(40)],
            pros=[f"pro {i}" for i in range(40)],
            platforms=list(Platform),
            inputs=list(IOFormat)[:20],
            outputs=list(IOFormat)[:20],
            ai_capabilities=list(AICapability)[:20],
            adoption={
                "monthly_visits": 10**12,
                "github_stars": 10**9,
                "product_hunt_upvotes": 10**9,
                "directory_saves": 10**9,
                "directory_upvotes": 10**9,
                "review_count": 10**9,
                "review_rating": 5.0,
                "notable_customers": [f"customer {i}" for i in range(20)],
                "funding_raised_usd": 10**11,
                "social_followers": 10**9,
            },
        )
        result = scorer.score(tool)
        for name, points in result.component_points().items():
            assert points <= float(WEIGHTS[name]), f"{name} overflowed its weight"
        assert result.total <= MAX_TOTAL

    def test_each_criterion_award_is_within_its_declared_share(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        for component, awards in result.criteria.items():
            declared = {c.key: c.share for c in CRITERIA[component]}
            for key, points in awards.items():
                assert key in declared, f"{component}.{key} is not in the rubric"
                ceiling = share_points(component, declared[key]) + 0.01
                assert points <= ceiling, f"{component}.{key} exceeded its share"

    def test_component_awards_sum_to_the_component_points(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        for component, awards in result.criteria.items():
            assert quantize(sum(awards.values())) == getattr(result, component)

    def test_builder_clamps_a_component_that_somehow_overshoots(self) -> None:
        from src.scoring.scorer import ComponentScore

        assert ComponentScore("recency_momentum", 99.0, 5.0).clamped() == 5.0
        assert ComponentScore("recency_momentum", -3.0, 5.0).clamped() == 0.0


# ============================================================ determinism
class TestDeterminism:
    """A score is a pure function of the record and the injected date."""

    def test_repeated_scoring_of_the_same_tool_is_identical(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool()
        first = scorer.score(tool)
        second = scorer.score(tool)
        assert first.total == second.total
        assert first.component_points() == second.component_points()
        assert first.criteria == second.criteria
        assert first.missing_evidence == second.missing_evidence
        assert first.adjustments == second.adjustments
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_two_equal_tools_score_identically(self, scorer: QualityScorer) -> None:
        left = scorer.score(make_strong_tool())
        right = scorer.score(make_strong_tool())
        assert left.model_dump(mode="json") == right.model_dump(mode="json")

    def test_two_independent_scorers_agree(self) -> None:
        tool = make_strong_tool()
        a = QualityScorer(ScoringConfig(), today=TODAY).score(tool)
        b = QualityScorer(ScoringConfig(), today=TODAY).score(tool)
        assert a.total == b.total

    def test_the_total_equals_the_component_point_sum(
        self, scorer: QualityScorer
    ) -> None:
        for tool in (make_tool(), make_strong_tool()):
            result = scorer.score(tool)
            assert result.total == quantize(sum(result.component_points().values()))

    @pytest.mark.parametrize(
        "tool_factory",
        [
            pytest.param(make_tool, id="thin"),
            pytest.param(make_strong_tool, id="strong"),
            pytest.param(
                lambda: make_strong_tool(status=ToolStatus.DISCONTINUED), id="dead"
            ),
        ],
    )
    def test_the_total_is_always_within_zero_and_one_hundred(
        self, scorer: QualityScorer, tool_factory
    ) -> None:
        total = scorer.score(tool_factory()).total
        assert 0.0 <= total <= 100.0

    def test_scoring_emits_exactly_the_eight_rubric_components(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        assert list(result.component_points()) == list(COMPONENTS)
        assert set(result.max_points) == set(COMPONENTS)

    def test_score_does_not_mutate_the_record(self, scorer: QualityScorer) -> None:
        tool = make_strong_tool()
        before = tool.model_dump(mode="json", exclude={"last_updated_at"})
        scorer.score(tool)
        after = tool.model_dump(mode="json", exclude={"last_updated_at"})
        assert tool.quality is None
        assert before == after

    def test_apply_attaches_the_same_score_it_returns(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool()
        expected = scorer.score(tool)
        scorer.apply(tool)
        assert tool.quality is not None
        assert tool.quality.total == expected.total
        assert tool.score == expected.total

    def test_band_matches_the_rubric_bands(self, scorer: QualityScorer) -> None:
        assert scorer.band_for(100) is QualityBand.EXCEPTIONAL
        assert scorer.band_for(90) is QualityBand.EXCEPTIONAL
        assert scorer.band_for(89.99) is QualityBand.EXCELLENT
        assert scorer.band_for(80) is QualityBand.EXCELLENT
        assert scorer.band_for(79.99) is QualityBand.GOOD
        assert scorer.band_for(70) is QualityBand.GOOD
        assert scorer.band_for(69.99) is QualityBand.AVERAGE
        assert scorer.band_for(60) is QualityBand.AVERAGE
        assert scorer.band_for(59.99) is QualityBand.REJECT
        assert scorer.band_for(0) is QualityBand.REJECT

    def test_the_scorer_refuses_a_misconfigured_rubric(self) -> None:
        with pytest.raises(ConfigError):
            ScoringConfig(weights={**EXPECTED_WEIGHTS, "recency_momentum": 50})


# ======================================================== missing evidence
class TestMissingEvidence:
    """Absent evidence earns nothing, is never invented, and is recorded."""

    def test_an_empty_record_scores_far_below_the_reject_threshold(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_tool())
        assert result.total < REJECT_BELOW
        assert result.band is QualityBand.REJECT

    def test_no_component_is_fabricated_for_an_empty_record(
        self, scorer: QualityScorer
    ) -> None:
        """Only 'we could not verify it' — itself a finding — may score."""
        result = scorer.score(make_tool())
        for name in COMPONENTS:
            if name == "information_quality_verifiability":
                continue
            assert getattr(result, name) == 0.0, f"{name} invented points"

    def test_missing_evidence_is_recorded_per_criterion(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_tool())
        assert result.missing_evidence
        every_criterion = {
            f"{component}.{c.key}"
            for component, criteria in CRITERIA.items()
            for c in criteria
        }
        assert set(result.missing_evidence) <= every_criterion
        # A thin record must report a miss for every component it could not
        # evidence, not just a token one.
        for expected in (
            "product_quality_capability.key_features",
            "real_user_value.primary_task",
            "current_usage_adoption.monthly_visits",
            "activity_maintenance.site_accessible",
            "product_maturity_reliability.company_identified",
            "recency_momentum.launch_recency",
            "differentiation.editorial_verdict",
            "information_quality_verifiability.verification_status",
        ):
            assert expected in result.missing_evidence

    def test_missing_evidence_is_reported_in_rubric_order(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_tool())
        order = [entry.split(".", 1)[0] for entry in result.missing_evidence]
        ranks = [COMPONENTS.index(name) for name in order]
        assert ranks == sorted(ranks)

    def test_unevidenced_components_are_recorded(self, scorer: QualityScorer) -> None:
        result = scorer.score(make_tool())
        assert set(result.unevidenced_components) == set(COMPONENTS) - {
            "information_quality_verifiability"
        }
        for name in result.unevidenced_components:
            assert getattr(result, name) == 0.0

    def test_a_fully_evidenced_record_has_no_unevidenced_components(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        assert result.unevidenced_components == []

    def test_evidence_confidence_reports_how_much_was_assessable(
        self, scorer: QualityScorer
    ) -> None:
        thin = scorer.score(make_tool())
        strong = scorer.score(make_strong_tool())
        assert 0.0 <= thin.evidence_confidence < 0.2
        assert strong.evidence_confidence == 1.0
        assert thin.evidence_confidence < strong.evidence_confidence

    def test_a_missed_criterion_never_appears_in_the_award_trail(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_tool())
        for entry in result.missing_evidence:
            component, key = entry.split(".", 1)
            assert key not in result.criteria.get(component, {})

    def test_an_unverifiable_launch_date_earns_no_recency_or_history(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(launch_date=None, launch_date_precision=None)
        result = scorer.score(tool)
        assert result.recency_momentum == 0.0
        assert "recency_momentum.launch_recency" in result.missing_evidence
        assert "product_maturity_reliability.operating_history" in result.missing_evidence

    def test_a_future_launch_date_is_bad_data_and_earns_nothing(
        self, scorer: QualityScorer
    ) -> None:
        """A date after ``today`` must not become credit (or a negative age)."""
        tool = make_strong_tool(launch_date=date(2030, 1, 1))
        result = scorer.score(tool)
        assert result.recency_momentum == 0.0
        assert "recency_momentum.launch_recency" in result.missing_evidence
        assert "product_maturity_reliability.operating_history" in result.missing_evidence

    def test_unknown_adoption_scores_zero_rather_than_an_average(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(adoption={})
        result = scorer.score(tool)
        assert result.current_usage_adoption == 0.0
        assert "current_usage_adoption" in result.unevidenced_components
        assert any(
            "scored 0 rather than assumed" in reason
            for reason in result.reasons["current_usage_adoption"]
        )

    def test_every_component_carries_a_human_readable_reason(
        self, scorer: QualityScorer
    ) -> None:
        for tool in (make_tool(), make_strong_tool()):
            result = scorer.score(tool)
            assert set(result.reasons) == set(COMPONENTS)
            for name in COMPONENTS:
                assert result.reasons[name], f"{name} explained nothing"


# ====================================================== currency / gating
class TestCurrencyAssessment:
    """"Current usage" has to be current — absence of proof is not proof."""

    def test_a_verified_accessible_site_is_current(self) -> None:
        tool = make_strong_tool()
        assessment = assess_currency(tool, TODAY)
        assert assessment.state == "current"
        assert assessment.factor == 1.0
        assert assessment.is_current and assessment.has_current_evidence
        assert not assessment.is_dead

    @pytest.mark.parametrize("status", DEAD_STATUSES)
    def test_a_dead_status_is_dead_regardless_of_other_evidence(
        self, status: ToolStatus
    ) -> None:
        tool = make_strong_tool(status=status)
        assessment = assess_currency(tool, TODAY)
        assert assessment.state == "dead"
        assert assessment.factor == 0.0
        assert assessment.is_dead and not assessment.has_current_evidence

    def test_an_inaccessible_site_is_dead(self) -> None:
        tool = make_strong_tool(
            verification={
                "status": VerificationStatus.UNREACHABLE,
                "is_accessible": False,
            }
        )
        assert assess_currency(tool, TODAY).state == "dead"

    def test_a_stale_verification_downgrades_to_accessible_stale(self) -> None:
        tool = make_strong_tool(last_verified_date=date(2025, 1, 1))
        assessment = assess_currency(tool, TODAY)
        assert assessment.state == "accessible_stale"
        assert assessment.factor == 0.5
        assert assessment.days_since_verification is not None
        assert assessment.days_since_verification > STALE_VERIFICATION_DAYS
        assert str(STALE_VERIFICATION_DAYS) in assessment.reason

    def test_an_undated_accessibility_check_is_weaker_than_a_dated_one(self) -> None:
        tool = make_strong_tool(last_verified_date=None)
        assessment = assess_currency(tool, TODAY)
        assert assessment.state == "accessible_stale"
        assert assessment.factor == 0.75
        assert assessment.days_since_verification is None

    @pytest.mark.parametrize("status", LIVE_STATUSES)
    def test_a_directory_claimed_live_status_alone_is_not_current(
        self, status: ToolStatus
    ) -> None:
        tool = make_strong_tool(
            status=status, verification={"status": VerificationStatus.UNVERIFIED}
        )
        assessment = assess_currency(tool, TODAY)
        assert assessment.state == "accessible_stale"
        assert assessment.factor == 0.6
        assert "never confirmed" in assessment.reason

    def test_no_evidence_at_all_is_unknown_and_never_current(self) -> None:
        assessment = assess_currency(make_tool(), TODAY)
        assert assessment.state == "unknown"
        assert assessment.factor == 0.3
        assert not assessment.has_current_evidence
        assert not assessment.is_current

    def test_the_assessment_is_serializable_for_the_audit_trail(self) -> None:
        payload = assess_currency(make_strong_tool(), TODAY).to_dict()
        assert payload["state"] == "current"
        assert payload["factor"] == 1.0
        assert payload["accessible"] is True
        assert payload["days_since_verification"] == 14
        assert payload["reason"]

    def test_the_state_and_reason_reach_the_breakdown(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        assert result.currency_state == "current"
        assert result.currency_reason


class TestCurrencyGating:
    """A dead or unverifiable product cannot ride past a threshold on old fame."""

    def test_a_dead_tool_gets_no_current_usage_credit(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(status=ToolStatus.DISCONTINUED)
        result = scorer.score(tool)
        assert result.current_usage_adoption == 0.0
        assert result.currency_state == "dead"

    def test_a_dead_tool_gets_no_recency_credit_even_if_just_launched(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(
            status=ToolStatus.DISCONTINUED, launch_date=date(2026, 9, 1)
        )
        result = scorer.score(tool)
        assert result.recency_momentum == 0.0
        assert "recency_momentum.launch_recency" in result.missing_evidence

    @pytest.mark.parametrize("status", DEAD_STATUSES)
    def test_a_dead_tool_gets_no_operational_status_credit(
        self, scorer: QualityScorer, status: ToolStatus
    ) -> None:
        result = scorer.score(make_strong_tool(status=status))
        assert "activity_maintenance.operational_status" in result.missing_evidence
        assert "operational_status" not in result.criteria["activity_maintenance"]

    def test_a_dead_tool_scores_strictly_below_an_identical_live_one(
        self, scorer: QualityScorer
    ) -> None:
        live = scorer.score(make_strong_tool())
        dead = scorer.score(make_strong_tool(status=ToolStatus.DISCONTINUED))
        assert dead.total < live.total
        assert dead.total < INCLUDE_AT_OR_ABOVE

    def test_an_inaccessible_site_loses_adoption_recency_and_activity_credit(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(
            launch_date=date(2026, 9, 1),
            verification={
                "status": VerificationStatus.UNREACHABLE,
                "is_accessible": False,
            },
        )
        result = scorer.score(tool)
        assert result.current_usage_adoption == 0.0
        assert result.recency_momentum == 0.0
        assert "activity_maintenance.site_accessible" in result.missing_evidence

    def test_a_current_verified_tool_does_receive_current_evidence_credit(
        self, scorer: QualityScorer
    ) -> None:
        result = scorer.score(make_strong_tool())
        assert result.currency_state == "current"
        assert result.current_usage_adoption > 0.0
        assert result.recency_momentum == float(WEIGHTS["recency_momentum"])
        assert result.activity_maintenance == float(WEIGHTS["activity_maintenance"])
        assert "current_usage_adoption" not in result.adjustments
        assert "recency_momentum" not in result.adjustments

    def test_stale_verification_discounts_adoption_and_freshness(
        self, scorer: QualityScorer
    ) -> None:
        fresh = scorer.score(make_strong_tool())
        stale = scorer.score(make_strong_tool(last_verified_date=date(2025, 1, 1)))
        assert stale.currency_state == "accessible_stale"
        assert stale.current_usage_adoption < fresh.current_usage_adoption
        assert stale.activity_maintenance < fresh.activity_maintenance
        assert "activity_maintenance.verification_freshness" in stale.missing_evidence
        assert stale.total < fresh.total

    def test_verification_freshness_is_graded_by_age(
        self, scorer: QualityScorer
    ) -> None:
        ages = [1, 30, FRESH_VERIFICATION_DAYS, STALE_VERIFICATION_DAYS]
        awards = [
            scorer.score(
                make_strong_tool(
                    last_verified_date=date.fromordinal(TODAY.toordinal() - age)
                )
            ).criteria["activity_maintenance"].get("verification_freshness", 0.0)
            for age in ages
        ]
        assert awards == sorted(awards, reverse=True)
        assert awards[-1] > 0.0

    def test_unknown_currency_discounts_adoption_without_zeroing_it(
        self, scorer: QualityScorer
    ) -> None:
        tool = make_strong_tool(
            status=None,
            last_verified_date=None,
            verification={"status": VerificationStatus.UNVERIFIED},
        )
        result = scorer.score(tool)
        assert result.currency_state == "unknown"
        assert 0.0 < result.current_usage_adoption < float(
            WEIGHTS["current_usage_adoption"]
        )
        assert result.total < INCLUDE_AT_OR_ABOVE

    def test_an_old_product_earns_momentum_only_from_current_evidenced_use(
        self, scorer: QualityScorer
    ) -> None:
        evidenced = scorer.score(make_strong_tool(launch_date=date(2015, 1, 1)))
        assert evidenced.recency_momentum > 0.0

        unevidenced = scorer.score(
            make_strong_tool(
                launch_date=date(2015, 1, 1),
                adoption={},
                status=None,
                last_verified_date=None,
                verification={"status": VerificationStatus.UNVERIFIED},
            )
        )
        assert unevidenced.recency_momentum == 0.0

    def test_recency_decays_monotonically_with_age(self, scorer: QualityScorer) -> None:
        awards = [
            scorer.score(
                make_strong_tool(
                    launch_date=date.fromordinal(TODAY.toordinal() - age)
                )
            ).recency_momentum
            for age in (10, 200, 500, 1_000, 3_000)
        ]
        assert awards == sorted(awards, reverse=True)

    def test_a_year_precision_launch_date_is_weaker_evidence(
        self, scorer: QualityScorer
    ) -> None:
        precise = scorer.score(make_strong_tool(launch_date_precision="day"))
        vague = scorer.score(make_strong_tool(launch_date_precision="year"))
        assert vague.recency_momentum < precise.recency_momentum


# ===================================================== adjustment handling
class TestAdjustments:
    """Notes must not discount, discounts must be visible, and never twice."""

    def _builder(self) -> _ComponentBuilder:
        return _ComponentBuilder(
            "current_usage_adoption", {k: float(v) for k, v in WEIGHTS.items()}
        )

    def test_a_note_is_recorded_without_touching_the_points(self) -> None:
        builder = self._builder()
        builder.award("monthly_visits", 1.0, "top tier traffic")
        undisturbed = self._builder()
        undisturbed.award("monthly_visits", 1.0, "top tier traffic")

        builder.note_adjustment("momentum halved elsewhere — recorded, not applied")
        noted = builder.build(evidenced=True)

        assert noted.points == undisturbed.build(evidenced=True).points
        assert noted.adjustment == "momentum halved elsewhere — recorded, not applied"
        assert noted.adjustment in noted.reasons

    def test_a_discount_moves_the_points_and_is_visible(self) -> None:
        full = self._builder()
        full.award("monthly_visits", 1.0)
        baseline = full.build(evidenced=True).points

        discounted = self._builder()
        discounted.award("monthly_visits", 1.0)
        discounted.discount(0.5, "adoption discounted x0.5 — stale verification")
        result = discounted.build(evidenced=True)

        assert result.points == quantize(baseline * 0.5)
        assert result.adjustment is not None
        assert "x0.5" in result.adjustment

    def test_a_discount_is_not_applied_twice(self) -> None:
        full = self._builder()
        full.award("monthly_visits", 1.0)
        baseline = full.build(evidenced=True).points

        twice = self._builder()
        twice.award("monthly_visits", 1.0)
        twice.discount(0.5, "first")
        twice.discount(0.5, "second")
        result = twice.build(evidenced=True)

        # The factor is *set*, not compounded: 0.5 once, never 0.25.
        assert result.points == quantize(baseline * 0.5)
        assert result.adjustment == "second"

    def test_a_discount_factor_is_clamped_into_zero_to_one(self) -> None:
        full = self._builder()
        full.award("monthly_visits", 1.0)
        baseline = full.build(evidenced=True).points

        inflating = self._builder()
        inflating.award("monthly_visits", 1.0)
        inflating.discount(4.0, "cannot inflate")
        assert inflating.build(evidenced=True).points == baseline

        negative = self._builder()
        negative.award("monthly_visits", 1.0)
        negative.discount(-2.0, "cannot go negative")
        assert negative.build(evidenced=True).points == 0.0

    def test_the_adoption_discount_matches_the_currency_factor(
        self, scorer: QualityScorer
    ) -> None:
        """The component is scaled by exactly the currency factor.

        The discount is applied *per criterion* and each award is then
        quantized to 2 decimals, so the component total can differ from
        ``factor * baseline`` by at most half a cent per criterion. The
        tolerance below is that rounding budget — not slack in the rule.
        """
        stale_tool = make_strong_tool(last_verified_date=date(2025, 1, 1))
        current = scorer.score(make_strong_tool())
        stale = scorer.score(stale_tool)
        factor = assess_currency(stale_tool, TODAY).factor

        awarded_criteria = len(stale.criteria["current_usage_adoption"])
        budget = 0.005 * awarded_criteria
        assert stale.current_usage_adoption == pytest.approx(
            current.current_usage_adoption * factor, abs=budget
        )
        assert stale.current_usage_adoption < current.current_usage_adoption
        assert "current_usage_adoption" in stale.adjustments
        assert f"x{factor:g}" in stale.adjustments["current_usage_adoption"]

    def test_the_discounted_criteria_still_sum_to_the_component_points(
        self, scorer: QualityScorer
    ) -> None:
        """Whatever the rounding, the audit trail must reconcile exactly."""
        stale = scorer.score(make_strong_tool(last_verified_date=date(2025, 1, 1)))
        awards = stale.criteria["current_usage_adoption"]
        assert quantize(sum(awards.values())) == stale.current_usage_adoption

    def test_the_momentum_note_does_not_discount_a_second_time(
        self, scorer: QualityScorer
    ) -> None:
        """``_recency`` halves the *fraction* and records a note — not both."""
        tool = make_strong_tool(
            launch_date=date(2026, 9, 1),
            status=None,
            last_verified_date=None,
            verification={"status": VerificationStatus.UNVERIFIED},
        )
        result = scorer.score(tool)
        full = scorer.score(make_strong_tool(launch_date=date(2026, 9, 1)))

        assert result.currency_state == "unknown"
        assert "momentum halved" in result.adjustments["recency_momentum"]
        # Exactly one halving: not 0.25 of the full award.
        assert result.recency_momentum == quantize(full.recency_momentum * 0.5)

    def test_adjustments_only_contain_components_that_were_adjusted(
        self, scorer: QualityScorer
    ) -> None:
        clean = scorer.score(make_strong_tool())
        assert clean.adjustments == {}

        adjusted = scorer.score(make_strong_tool(status=ToolStatus.DISCONTINUED))
        assert set(adjusted.adjustments) <= set(COMPONENTS)
        assert "current_usage_adoption" in adjusted.adjustments
        for note in adjusted.adjustments.values():
            assert note


class TestInjectableDate:
    """Date-sensitive scoring must depend on the injected date, not the clock."""

    def test_the_scorer_uses_the_injected_date(self) -> None:
        assert QualityScorer(ScoringConfig(), today=TODAY).today == TODAY

    def test_the_same_record_scores_differently_as_time_passes(self) -> None:
        tool = make_strong_tool()
        now = QualityScorer(ScoringConfig(), today=TODAY).score(tool)
        later = QualityScorer(
            ScoringConfig(), today=date(TODAY.year + 4, TODAY.month, TODAY.day)
        ).score(tool)

        assert now.recency_momentum > later.recency_momentum
        assert now.currency_state == "current"
        assert later.currency_state == "accessible_stale"
        assert later.total < now.total

    def test_scoring_the_same_date_twice_is_reproducible(self) -> None:
        tool = make_strong_tool()
        for day in (TODAY, date(2027, 1, 1), date(2031, 6, 30)):
            first = QualityScorer(ScoringConfig(), today=day).score(tool)
            second = QualityScorer(ScoringConfig(), today=day).score(tool)
            assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_an_older_record_gains_operating_history(self) -> None:
        tool = make_strong_tool()
        young = QualityScorer(ScoringConfig(), today=TODAY).score(tool)
        aged = QualityScorer(ScoringConfig(), today=date(2029, 5, 1)).score(tool)
        assert (
            aged.criteria["product_maturity_reliability"]["operating_history"]
            > young.criteria["product_maturity_reliability"]["operating_history"]
        )


# ================================================== thresholds / filtering
class TestThresholds:
    """§5 verbatim: <60 reject, 60-69 skip, 70-79 selective, >=80 include."""

    @pytest.mark.parametrize(
        ("total", "outcome"),
        [
            (0.0, Outcome.REJECT),
            (35.5, Outcome.REJECT),
            (59.99, Outcome.REJECT),
            (60.0, Outcome.SKIP),
            (65.0, Outcome.SKIP),
            (69.99, Outcome.SKIP),
            (70.0, Outcome.INCLUDE_SELECTIVE),
            (75.0, Outcome.INCLUDE_SELECTIVE),
            (79.99, Outcome.INCLUDE_SELECTIVE),
            (80.0, Outcome.INCLUDE),
            (89.99, Outcome.INCLUDE),
            (90.0, Outcome.INCLUDE),
            (100.0, Outcome.INCLUDE),
        ],
    )
    def test_the_band_boundaries_are_exact(
        self, fltr: QualityFilter, total: float, outcome: str
    ) -> None:
        decision = fltr.decide(scored_tool(total))
        assert decision.outcome == outcome
        assert decision.score == total

    @pytest.mark.parametrize(
        ("total", "band"),
        [
            (0.0, "reject"),
            (59.99, "reject"),
            (60.0, "average"),
            (70.0, "good"),
            (80.0, "excellent"),
            (90.0, "exceptional"),
            (100.0, "exceptional"),
        ],
    )
    def test_the_decision_carries_the_band(
        self, fltr: QualityFilter, total: float, band: str
    ) -> None:
        assert fltr.decide(scored_tool(total)).band == band

    def test_a_rejected_record_keeps_its_reason(self, fltr: QualityFilter) -> None:
        tool = scored_tool(42.0)
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.REJECT
        assert decision.rejection_reason == str(RejectionReason.BELOW_SCORE_THRESHOLD)
        assert "reject threshold" in decision.reason
        assert tool.rejected
        assert RejectionReason.BELOW_SCORE_THRESHOLD in tool.rejection_reasons
        assert decision.reason in tool.rejection_notes

    def test_a_skipped_record_keeps_its_reason_and_is_flagged(
        self, fltr: QualityFilter
    ) -> None:
        tool = scored_tool(64.0)
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.SKIP
        assert decision.rejection_reason == str(RejectionReason.VERY_LOW_QUALITY)
        assert "usually skip" in decision.reason
        assert tool.rejected
        assert tool.needs_human_review
        assert tool.review_notes

    def test_an_included_record_is_not_rejected_or_flagged(
        self, fltr: QualityFilter
    ) -> None:
        tool = scored_tool(88.0)
        decision = fltr.decide(tool)
        assert decision.included
        assert not tool.rejected
        assert not tool.needs_human_review
        assert tool.rejection_reasons == []

    def test_a_selective_inclusion_is_flagged_for_confirmation(
        self, fltr: QualityFilter
    ) -> None:
        tool = scored_tool(74.0)
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.INCLUDE_SELECTIVE
        assert decision.included
        assert not tool.rejected
        assert tool.needs_human_review
        assert any("selective inclusion" in note for note in tool.review_notes)

    def test_an_unscored_record_can_never_be_included(
        self, fltr: QualityFilter
    ) -> None:
        tool = make_tool()
        assert tool.quality is None
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.UNSCORED
        assert not decision.included
        assert decision.rejection_reason == str(
            RejectionReason.UNVERIFIABLE_INFORMATION
        )
        assert tool.rejected
        assert tool.rejection_notes

    def test_an_upstream_rejection_is_preserved_untouched(
        self, fltr: QualityFilter
    ) -> None:
        tool = scored_tool(95.0)
        tool.reject(RejectionReason.DEAD_OR_SHUTDOWN, "official website returns 410")
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.PRE_REJECTED
        assert not decision.included
        assert decision.rejection_reason == str(RejectionReason.DEAD_OR_SHUTDOWN)
        assert "official website returns 410" in tool.rejection_notes
        assert tool.rejection_reasons == [RejectionReason.DEAD_OR_SHUTDOWN]

    def test_the_filter_refuses_unordered_thresholds(self) -> None:
        with pytest.raises(ValueError):
            QualityFilter(ScoringConfig(), reject_below=80, skip_below=70)

    def test_the_filter_defaults_to_the_rubric_thresholds(self) -> None:
        default = QualityFilter(ScoringConfig())
        assert default.reject_below == float(REJECT_BELOW)
        assert default.skip_below == float(SKIP_BELOW)
        assert default.include_at_or_above == float(INCLUDE_AT_OR_ABOVE)


class TestSelectiveGates:
    """A 70-79 record has to *prove* it belongs."""

    def test_all_gates_pass_for_a_well_evidenced_borderline_record(self) -> None:
        gates = selective_evidence(scored_tool(75.0))
        assert set(gates) == {gate for gate, _ in SELECTIVE_REQUIREMENTS}
        assert all(gates.values())

    @pytest.mark.parametrize(
        ("gate", "mutate"),
        [
            (
                "currently_alive",
                lambda tool: setattr(tool.quality, "currency_state", "dead"),
            ),
            (
                "verified",
                lambda tool: setattr(
                    tool.verification, "status", VerificationStatus.UNVERIFIED
                ),
            ),
            ("has_provenance", lambda tool: setattr(tool, "discovery_sources", [])),
            (
                "describable",
                lambda tool: setattr(tool, "short_description", "short"),
            ),
            (
                "not_thin",
                lambda tool: setattr(tool.quality, "evidence_confidence", 0.2),
            ),
        ],
    )
    def test_a_failed_gate_skips_the_record_and_names_the_gate(
        self, fltr: QualityFilter, gate: str, mutate
    ) -> None:
        tool = scored_tool(75.0)
        mutate(tool)
        decision = fltr.decide(tool)

        assert decision.outcome == Outcome.SKIP
        assert not decision.included
        assert decision.failed_gates == [gate]
        requirement = dict(SELECTIVE_REQUIREMENTS)[gate]
        assert requirement in decision.reason
        assert decision.rejection_reason == str(
            RejectionReason.UNVERIFIABLE_INFORMATION
        )
        assert tool.rejected
        assert decision.reason in tool.rejection_notes

    def test_gates_are_not_applied_at_or_above_the_include_threshold(
        self, fltr: QualityFilter
    ) -> None:
        """80+ is 'include' — selective gating is a 70-79 policy only."""
        tool = scored_tool(85.0, discovery_sources=[])
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.INCLUDE
        assert decision.failed_gates == []

    def test_the_thin_record_gate_uses_the_documented_floor(self) -> None:
        at_floor = scored_tool(75.0)
        at_floor.quality.evidence_confidence = MIN_SELECTIVE_EVIDENCE_CONFIDENCE
        assert selective_evidence(at_floor)["not_thin"] is True

        under = scored_tool(75.0)
        under.quality.evidence_confidence = MIN_SELECTIVE_EVIDENCE_CONFIDENCE - 0.01
        assert selective_evidence(under)["not_thin"] is False

    def test_an_unknown_currency_state_is_not_currently_alive(self) -> None:
        tool = scored_tool(75.0)
        tool.quality.currency_state = "unknown"
        assert selective_evidence(tool)["currently_alive"] is False

    def test_an_unscored_record_fails_every_evidence_dependent_gate(self) -> None:
        gates = selective_evidence(make_tool())
        assert gates["currently_alive"] is False
        assert gates["not_thin"] is False


class TestFilterBatch:
    """Batch filtering is deterministic, complete and never padded."""

    def _batch(self) -> list[Tool]:
        return [
            scored_tool(95.0),
            scored_tool(82.0),
            scored_tool(74.0),
            scored_tool(64.0),
            scored_tool(31.0),
            make_tool(),
        ]

    def test_decisions_are_deterministic_across_runs(self) -> None:
        first = [
            d.to_dict() for d in QualityFilter(ScoringConfig()).filter(self._batch())[1]
        ]
        second = [
            d.to_dict() for d in QualityFilter(ScoringConfig()).filter(self._batch())[1]
        ]
        for a, b in zip(first, second, strict=True):
            a.pop("tool_id", None)
            b.pop("tool_id", None)
            assert a == b

    def test_every_input_record_gets_exactly_one_decision_in_order(
        self, fltr: QualityFilter
    ) -> None:
        batch = self._batch()
        kept, decisions, report = fltr.filter(batch)
        assert len(decisions) == len(batch)
        assert [d.score for d in decisions] == [
            t.quality.total if t.quality else None for t in batch
        ]
        assert report.input_count == len(batch)
        assert report.kept_count == len(kept)

    def test_only_included_records_are_kept(self, fltr: QualityFilter) -> None:
        kept, decisions, _ = fltr.filter(self._batch())
        assert len(kept) == sum(1 for d in decisions if d.included)
        assert [d.outcome for d in decisions] == [
            Outcome.INCLUDE,
            Outcome.INCLUDE,
            Outcome.INCLUDE_SELECTIVE,
            Outcome.SKIP,
            Outcome.REJECT,
            Outcome.UNSCORED,
        ]

    def test_the_report_counts_outcomes_bands_and_reasons(
        self, fltr: QualityFilter
    ) -> None:
        _, _, report = fltr.filter(self._batch())
        assert report.outcome_counts[Outcome.INCLUDE] == 2
        assert report.outcome_counts[Outcome.SKIP] == 1
        assert report.outcome_counts[Outcome.REJECT] == 1
        assert report.outcome_counts[Outcome.UNSCORED] == 1
        assert report.rejection_reasons[str(RejectionReason.BELOW_SCORE_THRESHOLD)] == 1
        assert report.thresholds == {
            "reject_below": float(REJECT_BELOW),
            "skip_below": float(SKIP_BELOW),
            "include_at_or_above": float(INCLUDE_AT_OR_ABOVE),
        }
        assert report.mean_score is not None

    def test_every_dropped_record_is_persistable_with_its_reason(
        self, fltr: QualityFilter
    ) -> None:
        _, decisions, _ = fltr.filter(self._batch())
        rows = rejection_rows(decisions)
        assert len(rows) == sum(1 for d in decisions if not d.included)
        for row in rows:
            assert row["reason"]
            assert row["outcome"] in (
                Outcome.SKIP,
                Outcome.REJECT,
                Outcome.UNSCORED,
                Outcome.PRE_REJECTED,
            )

    def test_an_all_failing_batch_is_reported_empty_never_padded(
        self, fltr: QualityFilter
    ) -> None:
        kept, decisions, report = fltr.filter([scored_tool(12.0), scored_tool(40.0)])
        assert kept == []
        assert report.kept_count == 0
        assert any("never padded" in note or "padded" in note for note in report.notes)
        assert all(not d.included for d in decisions)

    def test_an_empty_batch_produces_no_notes(self, fltr: QualityFilter) -> None:
        kept, decisions, report = fltr.filter([])
        assert (kept, decisions, report.notes) == ([], [], [])


# ==================================================== end-to-end coherence
class TestScoreThenFilter:
    """The two stages agree: the score decides, the filter only applies §5."""

    def test_a_strong_current_record_is_included(self, fltr: QualityFilter) -> None:
        tool = make_strong_tool()
        QualityScorer(ScoringConfig(), today=TODAY).apply(tool)
        decision = fltr.decide(tool)
        assert tool.quality.total >= INCLUDE_AT_OR_ABOVE
        assert decision.outcome == Outcome.INCLUDE
        assert not tool.rejected

    def test_a_thin_record_is_rejected_with_a_recorded_reason(
        self, fltr: QualityFilter
    ) -> None:
        tool = make_tool()
        QualityScorer(ScoringConfig(), today=TODAY).apply(tool)
        decision = fltr.decide(tool)
        assert decision.outcome == Outcome.REJECT
        assert tool.rejected
        assert tool.rejection_notes

    def test_a_dead_record_cannot_reach_the_include_threshold(
        self, fltr: QualityFilter
    ) -> None:
        tool = make_strong_tool(status=ToolStatus.DISCONTINUED)
        QualityScorer(ScoringConfig(), today=TODAY).apply(tool)
        decision = fltr.decide(tool)
        assert tool.quality.total < INCLUDE_AT_OR_ABOVE
        assert decision.outcome != Outcome.INCLUDE

    def test_unscored_and_low_scoring_refusals_are_different_outcomes(
        self, fltr: QualityFilter
    ) -> None:
        """Regression: ``score()`` is pure, so it does not make a record scored.

        ``run.py selfcheck`` used to score a record with ``QualityScorer.score``
        (which returns a breakdown without attaching it) and then assert the
        filter said ``reject``. The filter correctly said ``unscored``, because
        the record still had no ``quality``. Both refusals are legitimate and
        must stay distinguishable: ``unscored`` means "never assessed",
        ``reject`` means "assessed and below 60".
        """
        scorer = QualityScorer(ScoringConfig(), today=TODAY)

        never_scored = make_tool()
        scorer.score(never_scored)  # pure: attaches nothing
        assert never_scored.quality is None
        unscored = fltr.decide(never_scored)
        assert unscored.outcome == Outcome.UNSCORED
        assert unscored.score is None
        assert unscored.rejection_reason == str(
            RejectionReason.UNVERIFIABLE_INFORMATION
        )

        actually_scored = scorer.apply(make_tool())
        assert actually_scored.quality is not None
        rejected = fltr.decide(actually_scored)
        assert rejected.outcome == Outcome.REJECT
        assert rejected.score is not None and rejected.score < REJECT_BELOW
        assert rejected.rejection_reason == str(RejectionReason.BELOW_SCORE_THRESHOLD)

    def test_the_shipped_selfcheck_passes(self) -> None:
        """``run.py selfcheck`` is part of the contract, so it is tested."""
        import run as cli

        assert cli.cmd_selfcheck(argparse.Namespace()) == 0

    def test_the_pipeline_is_reproducible_end_to_end(self) -> None:
        def run() -> list[tuple[str, float]]:
            tools = [make_strong_tool(), make_tool()]
            scorer = QualityScorer(ScoringConfig(), today=TODAY)
            for tool in tools:
                scorer.apply(tool)
            _, decisions, _ = QualityFilter(ScoringConfig()).filter(tools)
            return [(d.outcome, d.score or 0.0) for d in decisions]

        assert run() == run()
