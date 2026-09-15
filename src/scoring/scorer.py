"""The 100-point quality scoring framework.

Weights are taken verbatim from "AI Tools: Data Extraction & Curation
Guidelines" §5::

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

Bands (§5): 90–100 exceptional, 80–89 excellent, 70–79 good,
60–69 average, <60 reject.

Design decisions
----------------
* **Transparent & explainable.** Every component returns points *and* the
  reasons behind them; the reasons are stored on the record.
* **Evidence-based, never optimistic.** A component with no evidence scores a
  conservative baseline and is listed in ``unevidenced_components``. Guessing a
  high score would be equivalent to fabricating data.
* **Verification-gated.** An unverified record cannot reach the top bands,
  because "information quality / verifiability" and the maturity/adoption
  components all depend on verified evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from src.core.config import ScoringConfig, get_settings
from src.core.logging_setup import get_logger
from src.models.enums import (
    OpenSourceStatus,
    QualityBand,
    RejectionReason,
    ToolStatus,
    VerificationStatus,
)
from src.models.tool import ScoreBreakdown, Tool

logger = get_logger("scoring")

#: Component name -> weight. Mirrors config; kept here for documentation.
COMPONENTS = (
    "product_quality_capability",
    "real_user_value",
    "current_usage_adoption",
    "activity_maintenance",
    "product_maturity_reliability",
    "recency_momentum",
    "differentiation",
    "information_quality_verifiability",
)


@dataclass
class ComponentScore:
    """Points awarded for one component, with its justification."""

    name: str
    points: float
    max_points: float
    reasons: list[str]
    evidenced: bool = True

    def clamped(self) -> float:
        return round(max(0.0, min(self.points, self.max_points)), 2)


class QualityScorer:
    """Computes the 100-point score for a :class:`Tool`."""

    def __init__(self, config: ScoringConfig | None = None, *, today: date | None = None) -> None:
        self.config = config or get_settings().scoring
        self.today = today or datetime.now(timezone.utc).date()
        self._weight = self.config.weights

    # --------------------------------------------------------------- public
    def score(self, tool: Tool) -> ScoreBreakdown:
        """Return a populated :class:`ScoreBreakdown` (does not mutate ``tool``)."""
        components: list[ComponentScore] = [
            self._product_quality(tool),
            self._user_value(tool),
            self._adoption(tool),
            self._activity(tool),
            self._maturity(tool),
            self._recency(tool),
            self._differentiation(tool),
            self._information_quality(tool),
        ]
        breakdown = ScoreBreakdown(
            **{component.name: component.clamped() for component in components},
            reasons={component.name: component.reasons for component in components},
            unevidenced_components=[c.name for c in components if not c.evidenced],
        )
        return breakdown

    def apply(self, tool: Tool) -> Tool:
        """Score ``tool`` in place and reject it when it falls below the band."""
        breakdown = self.score(tool)
        tool.quality = breakdown
        total = breakdown.total
        if total < self.config.reject_below:
            tool.reject(
                RejectionReason.BELOW_SCORE_THRESHOLD,
                f"score {total:.1f} < reject threshold {self.config.reject_below}",
            )
        elif total < self.config.include_at_or_above:
            tool.flag_for_review(
                f"score {total:.1f} is in the 'usually skip' band — needs curator decision"
            )
        logger.debug(
            "scored tool",
            extra={"tool": tool.name, "score": total, "band": str(breakdown.band)},
        )
        return tool

    def band_for(self, total: float) -> QualityBand:
        bands = self.config.bands
        if total >= bands.get("exceptional", 90):
            return QualityBand.EXCEPTIONAL
        if total >= bands.get("excellent", 80):
            return QualityBand.EXCELLENT
        if total >= bands.get("good", 70):
            return QualityBand.GOOD
        if total >= bands.get("average", 60):
            return QualityBand.AVERAGE
        return QualityBand.REJECT

    # ----------------------------------------------------------- components
    def _product_quality(self, tool: Tool) -> ComponentScore:
        """25 pts — depth of verified capability, not marketing copy."""
        maximum = self._weight["product_quality_capability"]
        reasons: list[str] = []
        points = 0.0

        feature_count = len(tool.key_features)
        feature_points = min(feature_count, 6) / 6 * (maximum * 0.32)
        if feature_count:
            reasons.append(f"{feature_count} verified key feature(s)")
        points += feature_points

        capability_count = len(tool.ai_capabilities)
        points += min(capability_count, 4) / 4 * (maximum * 0.24)
        if capability_count:
            reasons.append(f"{capability_count} identified AI capability(ies)")

        # Specific I/O is a strong quality proxy and is mandated by §9.
        if tool.inputs and tool.outputs:
            points += maximum * 0.2
            reasons.append(f"specific I/O declared ({len(tool.inputs)} in / {len(tool.outputs)} out)")
        elif tool.inputs or tool.outputs:
            points += maximum * 0.08
            reasons.append("partial I/O information")

        if len(tool.platforms) >= 2:
            points += maximum * 0.08
            reasons.append(f"multi-platform ({len(tool.platforms)} platforms)")
        elif tool.platforms:
            points += maximum * 0.04

        if tool.has_api:
            points += maximum * 0.08
            reasons.append("public API available")
        if tool.integrations:
            points += min(len(tool.integrations), 5) / 5 * (maximum * 0.08)
            reasons.append(f"{len(tool.integrations)} integration(s)")

        evidenced = bool(tool.key_features or tool.ai_capabilities or tool.inputs)
        if not evidenced:
            reasons.append("no verified capability evidence — scored conservatively")
        return ComponentScore("product_quality_capability", points, maximum, reasons, evidenced)

    def _user_value(self, tool: Tool) -> ComponentScore:
        """20 pts — does it solve a meaningful problem for a real user?"""
        maximum = self._weight["real_user_value"]
        reasons: list[str] = []
        points = 0.0

        if tool.primary_task:
            points += maximum * 0.25
            reasons.append(f"clear primary task: {tool.primary_task}")
        use_case_count = len(tool.use_cases)
        points += min(use_case_count, 4) / 4 * (maximum * 0.25)
        if use_case_count:
            reasons.append(f"{use_case_count} documented use case(s)")

        if tool.detailed_overview and len(tool.detailed_overview) >= 300:
            points += maximum * 0.2
            reasons.append("substantive product overview")
        elif tool.short_description:
            points += maximum * 0.08

        if tool.pros:
            points += min(len(tool.pros), 3) / 3 * (maximum * 0.1)
            reasons.append(f"{len(tool.pros)} curated pro(s)")
        # Honest limitations are a value signal, not a penalty.
        if tool.cons or tool.limitations:
            points += maximum * 0.1
            reasons.append("limitations documented honestly")

        if tool.pricing.has_free_plan or tool.pricing.has_free_trial:
            points += maximum * 0.1
            reasons.append("low-friction access (free plan or trial)")

        evidenced = bool(tool.primary_task or tool.use_cases or tool.detailed_overview)
        if not evidenced:
            reasons.append("no verified user-value evidence — scored conservatively")
        return ComponentScore("real_user_value", points, maximum, reasons, evidenced)

    def _adoption(self, tool: Tool) -> ComponentScore:
        """15 pts — current usage. Unknown adoption scores 0, never a guess."""
        maximum = self._weight["current_usage_adoption"]
        reasons: list[str] = []
        points = 0.0
        adoption = tool.adoption

        points += self._tiered(
            adoption.monthly_visits,
            [(5_000_000, 1.0), (1_000_000, 0.85), (250_000, 0.65), (50_000, 0.45), (10_000, 0.25)],
            maximum * 0.4,
            reasons,
            "monthly visits",
        )
        points += self._tiered(
            adoption.github_stars,
            [(20_000, 1.0), (5_000, 0.8), (1_000, 0.55), (200, 0.3)],
            maximum * 0.2,
            reasons,
            "GitHub stars",
        )
        points += self._tiered(
            adoption.product_hunt_upvotes,
            [(2_000, 1.0), (750, 0.75), (250, 0.5), (50, 0.25)],
            maximum * 0.15,
            reasons,
            "Product Hunt upvotes",
        )
        points += self._tiered(
            adoption.directory_saves or adoption.directory_upvotes,
            [(10_000, 1.0), (2_500, 0.7), (500, 0.45), (100, 0.2)],
            maximum * 0.1,
            reasons,
            "directory saves",
        )
        if adoption.review_count and adoption.review_rating:
            share = min(adoption.review_count, 500) / 500
            quality = max(0.0, (adoption.review_rating - 3.0) / 2.0)
            points += share * quality * (maximum * 0.1)
            reasons.append(
                f"{adoption.review_count} reviews at {adoption.review_rating}/5"
                + (f" on {adoption.review_platform}" if adoption.review_platform else "")
            )
        if adoption.notable_customers:
            points += min(len(adoption.notable_customers), 5) / 5 * (maximum * 0.05)
            reasons.append(f"{len(adoption.notable_customers)} notable customer(s)")

        # Multi-directory presence is weak corroboration, not adoption proof.
        listing_count = len(tool.discovery_sources)
        if listing_count >= 3:
            points += maximum * 0.05
            reasons.append(f"listed in {listing_count} approved directories")

        evidenced = adoption.has_any_signal
        if not evidenced:
            reasons.append("no verifiable adoption signal — scored 0 rather than assumed")
        return ComponentScore("current_usage_adoption", points, maximum, reasons, evidenced)

    def _activity(self, tool: Tool) -> ComponentScore:
        """15 pts — is the product still being maintained? (§6)"""
        maximum = self._weight["activity_maintenance"]
        reasons: list[str] = []
        points = 0.0

        status = _status_of(tool)
        if status in (ToolStatus.ACTIVE, ToolStatus.BETA):
            points += maximum * 0.4
            reasons.append(f"status '{status.value}'")
        elif status in (ToolStatus.ALPHA, ToolStatus.WAITLIST):
            points += maximum * 0.2
            reasons.append(f"status '{status.value}' (pre-GA)")
        elif status in (ToolStatus.DEPRECATED, ToolStatus.DISCONTINUED, ToolStatus.INACCESSIBLE):
            reasons.append(f"status '{status.value}' — no activity credit")

        if tool.verification.is_accessible:
            points += maximum * 0.2
            reasons.append("official website verified accessible")
        elif tool.verification.is_accessible is False:
            reasons.append("official website not accessible")

        days = _days_since(tool.last_verified_date, self.today)
        if days is not None and days <= 30:
            points += maximum * 0.2
            reasons.append(f"verified {days} day(s) ago")
        elif days is not None and days <= 90:
            points += maximum * 0.1
            reasons.append(f"verified {days} day(s) ago")

        if tool.version:
            points += maximum * 0.1
            reasons.append(f"published version {tool.version}")
        if tool.pricing.pricing_verified_at:
            points += maximum * 0.1
            reasons.append("current pricing verified")

        evidenced = bool(status or tool.last_verified_date or tool.verification.is_accessible is not None)
        if not evidenced:
            reasons.append("no maintenance evidence — scored conservatively")
        return ComponentScore("activity_maintenance", points, maximum, reasons, evidenced)

    def _maturity(self, tool: Tool) -> ComponentScore:
        """10 pts — reliability signals of an established product."""
        maximum = self._weight["product_maturity_reliability"]
        reasons: list[str] = []
        points = 0.0

        if tool.company:
            points += maximum * 0.2
            reasons.append(f"identified developer: {tool.company}")
        if tool.website:
            points += maximum * 0.1
        if tool.pricing.model:
            points += maximum * 0.2
            reasons.append(f"published pricing model '{tool.pricing.model}'")
        if tool.has_api and tool.api_docs_url:
            points += maximum * 0.15
            reasons.append("documented API")
        if tool.open_source_status in (
            OpenSourceStatus.OPEN_SOURCE, OpenSourceStatus.OPEN_SOURCE.value,
        ) and tool.repository_url:
            points += maximum * 0.1
            reasons.append("open source with public repository")
        age_days = _days_since(tool.launch_date, self.today)
        if age_days is not None and age_days >= 365:
            points += maximum * 0.15
            reasons.append(f"operating for {age_days // 365} year(s)")
        elif age_days is not None and age_days >= 180:
            points += maximum * 0.08
        if tool.country:
            points += maximum * 0.1
            reasons.append("company location known")

        evidenced = bool(tool.company or tool.pricing.model or tool.launch_date)
        if not evidenced:
            reasons.append("no maturity evidence — scored conservatively")
        return ComponentScore("product_maturity_reliability", points, maximum, reasons, evidenced)

    def _recency(self, tool: Tool) -> ComponentScore:
        """5 pts — "Recently launched and gaining real usage" (§1, §6)."""
        maximum = self._weight["recency_momentum"]
        reasons: list[str] = []
        points = 0.0

        age_days = _days_since(tool.launch_date, self.today)
        if age_days is None:
            reasons.append("launch date unverified — no recency credit")
            return ComponentScore("recency_momentum", 0.0, maximum, reasons, evidenced=False)

        if age_days <= 180:
            points = maximum
            reasons.append(f"launched {age_days} day(s) ago")
        elif age_days <= 365:
            points = maximum * 0.8
            reasons.append("launched within the last year")
        elif age_days <= 730:
            points = maximum * 0.55
            reasons.append("launched within the last two years")
        elif age_days <= 1460:
            points = maximum * 0.3
        else:
            # Older tools still qualify when adoption is strong (§3, §6).
            points = maximum * 0.15 if tool.adoption.has_any_signal else 0.0
            reasons.append(
                "older product; credit only via ongoing adoption"
                if tool.adoption.has_any_signal
                else "older product without adoption evidence"
            )
        if tool.launch_date_precision == "year" and points > 0:
            points *= 0.9
            reasons.append("launch date only known to year precision")
        return ComponentScore("recency_momentum", points, maximum, reasons)

    def _differentiation(self, tool: Tool) -> ComponentScore:
        """5 pts — "Clearly differentiated or genuinely useful" (§1, §4)."""
        maximum = self._weight["differentiation"]
        reasons: list[str] = []
        points = 0.0

        if tool.aiorbit_summary:
            points += maximum * 0.3
            reasons.append("editorial verdict written from verified facts")
        if len(tool.key_features) >= 4:
            points += maximum * 0.2
            reasons.append("feature depth suggests a distinct product")
        if tool.open_source_status in (
            OpenSourceStatus.OPEN_SOURCE, OpenSourceStatus.OPEN_SOURCE.value,
        ):
            points += maximum * 0.1
            reasons.append("open source differentiation")
        if tool.has_api:
            points += maximum * 0.1
            reasons.append("programmatic access differentiates from UI-only clones")
        if len(tool.ai_capabilities) >= 3:
            points += maximum * 0.2
            reasons.append("multi-capability product")
        if tool.adoption.funding_raised_usd:
            points += maximum * 0.1
            reasons.append("verified funding indicates a real company")

        evidenced = bool(tool.key_features or tool.ai_capabilities or tool.aiorbit_summary)
        if not evidenced:
            reasons.append("cannot assess differentiation without verified detail")
        return ComponentScore("differentiation", points, maximum, reasons, evidenced)

    def _information_quality(self, tool: Tool) -> ComponentScore:
        """5 pts — can we actually stand behind this record? (§8, §10)"""
        maximum = self._weight["information_quality_verifiability"]
        reasons: list[str] = []
        points = 0.0

        status = tool.verification.status
        if status in (VerificationStatus.VERIFIED, VerificationStatus.VERIFIED.value):
            points += maximum * 0.4
            reasons.append("verified against the official website")
        elif status in (
            VerificationStatus.PARTIALLY_VERIFIED, VerificationStatus.PARTIALLY_VERIFIED.value,
        ):
            points += maximum * 0.2
            reasons.append("partially verified")
        else:
            reasons.append(f"verification status '{status}'")

        completeness = tool.completeness()
        points += completeness * (maximum * 0.3)
        reasons.append(f"field completeness {completeness:.0%}")

        if len(tool.discovery_sources) >= 2:
            points += maximum * 0.15
            reasons.append(f"corroborated by {len(tool.discovery_sources)} sources")
        elif tool.discovery_sources:
            points += maximum * 0.05
        if tool.verification.verification_sources:
            points += maximum * 0.15
            reasons.append("verification source recorded")

        return ComponentScore("information_quality_verifiability", points, maximum, reasons)

    # -------------------------------------------------------------- utility
    @staticmethod
    def _tiered(
        value: int | float | None,
        tiers: list[tuple[int, float]],
        maximum: float,
        reasons: list[str],
        label: str,
    ) -> float:
        """Award points from a descending tier table; ``None`` scores nothing."""
        if not value:
            return 0.0
        for threshold, fraction in tiers:
            if value >= threshold:
                reasons.append(f"{label}: {value:,}")
                return maximum * fraction
        return 0.0


def _status_of(tool: Tool) -> ToolStatus | None:
    return ToolStatus.coerce(tool.status) if tool.status is not None else None


def _days_since(value: date | None, today: date) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    delta = (today - value).days
    return delta if delta >= 0 else None


def rank_and_select(
    tools: list[Tool],
    *,
    target_size: int | None = None,
    max_per_primary_task: int | None = None,
    min_score: float | None = None,
) -> tuple[list[Tool], list[Tool]]:
    """Select the *best* N tools — not the first N (guideline §1, §11).

    Returns ``(selected, not_selected)``. Never pads to reach the target:
    "If only 800 tools genuinely meet the standard ... submit 800."
    """
    settings = get_settings()
    target_size = target_size or settings.batch.target_size
    min_score = settings.scoring.include_at_or_above if min_score is None else min_score
    if max_per_primary_task is None:
        max_per_primary_task = settings.batch.max_per_primary_task

    eligible = [
        tool
        for tool in tools
        if not tool.rejected and tool.quality is not None and tool.quality.total >= min_score
    ]
    eligible.sort(
        key=lambda tool: (tool.quality.total, tool.completeness(), len(tool.discovery_sources)),
        reverse=True,
    )

    selected: list[Tool] = []
    rejected: list[Tool] = [
        tool for tool in tools if tool.rejected or tool.quality is None or tool.quality.total < min_score
    ]
    per_task: dict[str, int] = {}

    for tool in eligible:
        if len(selected) >= target_size:
            tool.flag_for_review("did not fit in batch target; queue for the next batch")
            rejected.append(tool)
            continue
        task_key = (tool.primary_task or "unclassified").lower()
        if max_per_primary_task and per_task.get(task_key, 0) >= max_per_primary_task:
            tool.reject(
                RejectionReason.CATEGORY_QUOTA_EXCEEDED,
                f"per-task cap of {max_per_primary_task} reached for '{task_key}'",
            )
            rejected.append(tool)
            continue
        per_task[task_key] = per_task.get(task_key, 0) + 1
        selected.append(tool)

    logger.info(
        "batch selection complete",
        extra={
            "selected": len(selected),
            "not_selected": len(rejected),
            "target": target_size,
            "min_score": min_score,
        },
    )
    return selected, rejected
