"""The canonical 100-point rubric.

Weights are verbatim from "AI Tools: Data Extraction & Curation Guidelines"
§5 and are the *single* definition used by the scorer, the filter, the config
validator and the tests::

    Product quality & capability          25
    Real user value / usefulness          20
    Current usage / adoption              15
    Activity & maintenance                15
    Product maturity / reliability        10
    Recency / momentum                     5
    Differentiation                        5
    Information quality / verifiability    5
    ----------------------------------------
    Total                                100

Bands (§5)::

    90-100  exceptional   strongly prioritize
    80- 89  excellent     include
    70- 79  good          include selectively
    60- 69  average       usually skip
      < 60  reject        reject

Why a rubric module
-------------------
The scorer used to hard-code magnitudes like ``maximum * 0.32`` inline, which
made two things impossible to prove: that a component's sub-criteria can never
exceed its weight, and that the total is really out of 100. Here every
component declares its sub-criteria as **shares of its own weight**, and
:func:`validate_rubric` asserts that

* the eight component weights sum to exactly 100;
* each component's shares sum to at most 1.0.

So a component can never overflow its weight, the total can never exceed 100,
and the weights in ``config/settings.yaml`` can be checked against this module
instead of being trusted.

Determinism
-----------
Points are quantized with :func:`quantize` (half-up, 2 decimals) so the same
record always produces the exact same number — no float drift between runs,
platforms or Python versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

__all__ = [
    "COMPONENTS",
    "WEIGHTS",
    "BANDS",
    "REJECT_BELOW",
    "SKIP_BELOW",
    "INCLUDE_AT_OR_ABOVE",
    "MAX_TOTAL",
    "CRITERIA",
    "Criterion",
    "quantize",
    "share_points",
    "validate_rubric",
    "weights_match_rubric",
]

#: Component order is part of the contract: reports and breakdowns use it.
COMPONENTS: tuple[str, ...] = (
    "product_quality_capability",
    "real_user_value",
    "current_usage_adoption",
    "activity_maintenance",
    "product_maturity_reliability",
    "recency_momentum",
    "differentiation",
    "information_quality_verifiability",
)

#: The rubric weights. Guideline §5, verbatim.
WEIGHTS: Mapping[str, int] = {
    "product_quality_capability": 25,
    "real_user_value": 20,
    "current_usage_adoption": 15,
    "activity_maintenance": 15,
    "product_maturity_reliability": 10,
    "recency_momentum": 5,
    "differentiation": 5,
    "information_quality_verifiability": 5,
}

#: Inclusive lower bound of each band.
BANDS: Mapping[str, int] = {
    "exceptional": 90,
    "excellent": 80,
    "good": 70,
    "average": 60,
}

#: Below this the record is rejected outright ("<60 → Reject").
REJECT_BELOW: int = 60

#: 60-69 is the "average / usually skip" band.
SKIP_BELOW: int = 70

#: 80+ is included; 70-79 is included *selectively* (evidence-gated).
INCLUDE_AT_OR_ABOVE: int = 80

#: The score is always out of 100 — nothing may exceed it.
MAX_TOTAL: float = 100.0


@dataclass(frozen=True)
class Criterion:
    """One auditable sub-criterion inside a component.

    ``share`` is the fraction of the component's weight this criterion can
    award at most, so criteria are expressed independently of the weight and a
    weight change cannot silently break the arithmetic.
    """

    key: str
    share: float
    description: str
    #: Set when the criterion can only be met from verified evidence, which is
    #: what makes "missing evidence scores nothing" auditable rather than
    #: implicit.
    requires_evidence: bool = True
    tiers: tuple[tuple[float, float], ...] = field(default=())


def _c(key: str, share: float, description: str, **kwargs: object) -> Criterion:
    return Criterion(key=key, share=share, description=description, **kwargs)  # type: ignore[arg-type]


#: Sub-criteria per component. Shares within a component sum to <= 1.0
#: (asserted by :func:`validate_rubric`), so a component can never exceed its
#: rubric weight and the total can never exceed 100.
CRITERIA: Mapping[str, tuple[Criterion, ...]] = {
    "product_quality_capability": (
        _c("key_features", 0.30, "verified key features (graded by count)"),
        _c("ai_capabilities", 0.20, "identified AI capabilities (graded by count)"),
        _c("specific_io", 0.20, "specific verified input *and* output formats (§9)"),
        _c("platforms", 0.10, "platform coverage"),
        _c("public_api", 0.10, "public API available"),
        _c("integrations", 0.10, "verified integrations"),
    ),
    "real_user_value": (
        _c("primary_task", 0.20, "a single clear primary task"),
        _c("use_cases", 0.25, "documented real use cases (graded by count)"),
        _c("overview_depth", 0.20, "substantive product overview"),
        _c("pros", 0.10, "curated pros"),
        _c("limitations", 0.10, "limitations documented honestly"),
        _c("low_friction_access", 0.10, "free plan or free trial"),
        _c("pricing_transparency", 0.05, "published pricing model"),
    ),
    "current_usage_adoption": (
        _c("monthly_visits", 0.40, "measured monthly traffic"),
        _c("github_stars", 0.20, "GitHub stars"),
        _c("product_hunt_upvotes", 0.15, "Product Hunt upvotes"),
        _c("directory_signals", 0.10, "directory saves/upvotes"),
        _c("reviews", 0.10, "review volume weighted by rating"),
        _c("notable_customers", 0.05, "named customers"),
    ),
    "activity_maintenance": (
        _c("operational_status", 0.35, "product status is active/beta"),
        _c("site_accessible", 0.25, "official website verified accessible"),
        _c("verification_freshness", 0.20, "how recently we verified it ourselves"),
        _c("version_published", 0.10, "a current version is published"),
        _c("pricing_verified", 0.10, "pricing verified recently"),
    ),
    "product_maturity_reliability": (
        _c("company_identified", 0.20, "identified developer/company"),
        _c("pricing_model", 0.20, "published pricing model"),
        _c("documented_api", 0.15, "documented API"),
        _c("open_source_repo", 0.10, "open source with a public repository"),
        _c("operating_history", 0.20, "time in operation (graded)"),
        _c("company_location", 0.10, "company location known"),
        _c("official_site", 0.05, "official website recorded"),
    ),
    "recency_momentum": (
        _c("launch_recency", 1.00, "how recently the product launched (graded)"),
    ),
    "differentiation": (
        _c("editorial_verdict", 0.25, "editorial verdict written from verified facts"),
        _c("feature_depth", 0.20, "feature depth suggests a distinct product"),
        _c("capability_breadth", 0.20, "multi-capability product"),
        _c("open_source", 0.10, "open source"),
        _c("programmatic_access", 0.10, "API differentiates from UI-only clones"),
        _c("funding", 0.10, "verified funding indicates a real company"),
        _c("customer_proof", 0.05, "named customers"),
    ),
    "information_quality_verifiability": (
        _c("verification_status", 0.40, "verified against the official website"),
        _c("field_completeness", 0.25, "share of guideline §8 fields actually filled"),
        _c("source_corroboration", 0.15, "number of independent discovery sources"),
        _c("verification_source", 0.10, "verification source reference recorded"),
        _c("verification_recent", 0.10, "verification is recent"),
    ),
}


def quantize(value: float, places: int = 2) -> float:
    """Deterministic half-up rounding.

    ``round()`` uses banker's rounding and inherits binary float artefacts, so
    two runs could differ in the last digit. Scores are persisted and compared,
    so they go through :class:`~decimal.Decimal` instead.
    """
    exponent = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(exponent, rounding=ROUND_HALF_UP))


def share_points(component: str, share: float, weights: Mapping[str, float] | None = None) -> float:
    """Convert a share of a component's weight into points."""
    table = weights or WEIGHTS
    return quantize(float(table[component]) * float(share), 4)


def validate_rubric(weights: Mapping[str, float] | None = None) -> None:
    """Assert the rubric is internally consistent.

    Raises :class:`~src.core.errors.ConfigError` when

    * a component is missing or unknown;
    * the weights do not sum to exactly 100;
    * any component's sub-criteria shares sum to more than 1.0 (which would
      let that component exceed its rubric weight).
    """
    from src.core.errors import ConfigError

    table = dict(weights or WEIGHTS)
    missing = [name for name in COMPONENTS if name not in table]
    if missing:
        raise ConfigError(f"rubric is missing component weight(s): {missing}")
    unknown = [name for name in table if name not in COMPONENTS]
    if unknown:
        raise ConfigError(f"rubric has unknown component weight(s): {unknown}")

    total = quantize(sum(float(v) for v in table.values()))
    if total != MAX_TOTAL:
        raise ConfigError(f"scoring weights must sum to 100, got {total}")

    for component, criteria in CRITERIA.items():
        share_total = quantize(sum(c.share for c in criteria), 4)
        if share_total > 1.0:
            raise ConfigError(
                f"component '{component}' sub-criteria shares sum to {share_total} "
                "(> 1.0): it could exceed its rubric weight"
            )


def weights_match_rubric(weights: Mapping[str, float]) -> bool:
    """True when a configured weight table equals the guideline rubric."""
    return {k: float(v) for k, v in weights.items()} == {
        k: float(v) for k, v in WEIGHTS.items()
    }
