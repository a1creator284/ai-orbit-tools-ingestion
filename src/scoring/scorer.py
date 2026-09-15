"""The 100-point quality scoring stage.

The rubric itself (weights, bands, sub-criteria) lives in
:mod:`src.scoring.rubric`; this module is the *evaluator*: it turns a
:class:`~src.models.tool.Tool` into a :class:`~src.models.tool.ScoreBreakdown`
with a per-criterion audit trail.

Design decisions
----------------
**1. Exactly eight components, exactly the rubric weights.**
Every candidate is scored on all eight components. A component's sub-criteria
are declared as shares of its own weight (:data:`src.scoring.rubric.CRITERIA`),
so a component can never exceed its weight and the total can never exceed 100.

**2. Deterministic.**
No clocks, no randomness, no dict-iteration order dependence and no float
drift: every award is quantized half-up (:func:`src.scoring.rubric.quantize`),
and ``today`` is injectable so date-sensitive components are reproducible.

**3. Missing evidence scores nothing and says so.**
An absent field never earns points and is never replaced by a plausible
default. Each unmet criterion is recorded in ``missing_evidence`` and each
component that had no evidence at all is listed in ``unevidenced_components``.
``evidence_confidence`` reports how much of the 100 points was even
*assessable*, so a thin record is visibly thin instead of quietly average.

**4. "Current usage" means current.**
Adoption, activity and recency are gated on a single, shared
:class:`CurrencyAssessment` computed from current-accessibility evidence,
operational status and verification freshness. A historically popular product
whose site is dead, or which we have no current evidence for, has its adoption
points discounted and its recency zeroed — so it cannot ride past the
thresholds on old fame. The discount is written into the breakdown
(``adjustments``) rather than applied silently.

Threshold decisions (reject / skip / selective / include) are **not** made
here; they belong to :mod:`src.scoring.filter`, which consumes this breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Mapping, Sequence

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
from src.scoring.rubric import (
    BANDS,
    COMPONENTS,
    CRITERIA,
    MAX_TOTAL,
    SKIP_BELOW,
    WEIGHTS,
    quantize,
    share_points,
    validate_rubric,
)

logger = get_logger("scoring")

__all__ = [
    "COMPONENTS",
    "ComponentScore",
    "CurrencyAssessment",
    "QualityScorer",
    "assess_currency",
    "rank_and_select",
]

#: Verification older than this is no longer evidence of *current* activity.
STALE_VERIFICATION_DAYS = 180

#: Verification within this window counts as fresh.
FRESH_VERIFICATION_DAYS = 90

#: Statuses that prove the product is not currently operating (§4).
DEAD_STATUSES = (
    ToolStatus.DEPRECATED,
    ToolStatus.DISCONTINUED,
    ToolStatus.INACCESSIBLE,
)

#: Statuses that prove it is.
LIVE_STATUSES = (ToolStatus.ACTIVE, ToolStatus.BETA)

#: Pre-GA statuses: real, but not yet a shipping product.
PRE_GA_STATUSES = (ToolStatus.ALPHA, ToolStatus.WAITLIST)


# ======================================================== component scores
@dataclass
class ComponentScore:
    """Points awarded for one rubric component, with its full justification."""

    name: str
    points: float
    max_points: float
    reasons: list[str] = field(default_factory=list)
    criteria: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    evidenced: bool = True
    adjustment: str | None = None

    def clamped(self) -> float:
        """Points, clamped into ``[0, max_points]`` and quantized."""
        return quantize(max(0.0, min(self.points, self.max_points)))


class _ComponentBuilder:
    """Accumulates per-criterion awards for one component.

    Every award goes through the rubric, so a criterion can only ever pay out
    its declared share of the component weight, and every *unmet* criterion is
    recorded — that record is what makes "we had no evidence" auditable.
    """

    def __init__(self, component: str, weights: Mapping[str, float]) -> None:
        self.component = component
        self.weights = weights
        self.weight = float(weights[component])
        self._criteria = {c.key: c for c in CRITERIA[component]}
        self.awarded: dict[str, float] = {}
        self.reasons: list[str] = []
        self.missing: list[str] = []
        self._factor: float = 1.0
        self.adjustment: str | None = None

    # ------------------------------------------------------------- awarding
    def award(self, key: str, fraction: float = 1.0, reason: str | None = None) -> float:
        """Award ``fraction`` (0-1) of criterion ``key``'s share."""
        criterion = self._criteria[key]
        fraction = max(0.0, min(float(fraction), 1.0))
        if fraction <= 0:
            self.miss(key, reason)
            return 0.0
        points = share_points(self.component, criterion.share * fraction, self.weights)
        self.awarded[key] = quantize(self.awarded.get(key, 0.0) + points, 4)
        if reason:
            self.reasons.append(reason)
        return points

    def miss(self, key: str, reason: str | None = None) -> None:
        """Record that a criterion could not be met (no points, no guess)."""
        if key in self._criteria and key not in self.missing:
            self.missing.append(key)
        if reason:
            self.reasons.append(reason)

    def graded(
        self,
        key: str,
        count: int,
        full_at: int,
        reason: str | None = None,
    ) -> float:
        """Award a count-graded criterion (``full_at`` items = full share)."""
        if not count:
            self.miss(key)
            return 0.0
        return self.award(key, min(count, full_at) / full_at, reason)

    def tiered(
        self,
        key: str,
        value: float | int | None,
        tiers: Sequence[tuple[float, float]],
        label: str,
    ) -> float:
        """Award from a descending tier table; ``None``/0 awards nothing."""
        if not value:
            self.miss(key)
            return 0.0
        for threshold, fraction in tiers:
            if value >= threshold:
                return self.award(key, fraction, f"{label}: {value:,}")
        self.miss(key, f"{label}: {value:,} below the lowest credited tier")
        return 0.0

    def discount(self, factor: float, note: str) -> None:
        """Scale the whole component (used for currency discounts)."""
        self._factor = max(0.0, min(float(factor), 1.0))
        self.note_adjustment(note)

    def note_adjustment(self, note: str) -> None:
        """Record a transparent adjustment without scaling anything."""
        self.adjustment = note
        self.reasons.append(note)

    # -------------------------------------------------------------- output
    def build(self, *, evidenced: bool, note: str | None = None) -> ComponentScore:
        if note:
            self.reasons.append(note)
        criteria = {key: quantize(value * self._factor) for key, value in self.awarded.items()}
        total = quantize(sum(criteria.values()))
        return ComponentScore(
            name=self.component,
            points=total,
            max_points=self.weight,
            reasons=list(self.reasons),
            criteria=criteria,
            missing=list(self.missing),
            evidenced=evidenced,
            adjustment=self.adjustment,
        )


# ==================================================== currency assessment
@dataclass(frozen=True)
class CurrencyAssessment:
    """Is this product *currently* alive, and how well do we know?

    Shared by the adoption, activity and recency components so that "current
    usage", "activity & maintenance" and "momentum" all agree about the same
    evidence instead of each re-deriving it.
    """

    #: Multiplier applied to adoption points (historical fame is discounted
    #: when there is no evidence the product still runs).
    factor: float
    #: Stable label: ``current`` | ``accessible_stale`` | ``unknown`` | ``dead``.
    state: str
    reason: str
    status: ToolStatus | None = None
    accessible: bool | None = None
    days_since_verification: int | None = None

    @property
    def is_dead(self) -> bool:
        return self.state == "dead"

    @property
    def is_current(self) -> bool:
        return self.state == "current"

    @property
    def has_current_evidence(self) -> bool:
        """True when something we checked ourselves says it is alive today."""
        return self.state in ("current", "accessible_stale")

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "factor": self.factor,
            "reason": self.reason,
            "status": str(self.status) if self.status else None,
            "accessible": self.accessible,
            "days_since_verification": self.days_since_verification,
        }


def assess_currency(tool: Tool, today: date) -> CurrencyAssessment:
    """Classify how current our evidence about ``tool`` is.

    Deliberately conservative: *absence* of evidence is ``unknown`` (a heavy
    discount), never ``current``. Only a fetched, accessible official site or
    an explicitly live status can produce ``current``.
    """
    status = ToolStatus.coerce(tool.status) if tool.status is not None else None
    accessible = tool.verification.is_accessible
    days = _days_since(tool.last_verified_date, today)

    if status in DEAD_STATUSES:
        return CurrencyAssessment(
            0.0,
            "dead",
            f"status '{status.value}' — the product is not currently operating",
            status,
            accessible,
            days,
        )
    if accessible is False:
        return CurrencyAssessment(
            0.0,
            "dead",
            "official website is not accessible — no current-usage credit",
            status,
            accessible,
            days,
        )

    if accessible is True:
        if days is not None and days <= STALE_VERIFICATION_DAYS:
            return CurrencyAssessment(
                1.0,
                "current",
                f"official website verified accessible {days} day(s) ago",
                status,
                accessible,
                days,
            )
        if days is None:
            return CurrencyAssessment(
                0.75,
                "accessible_stale",
                "official website accessible but the check was not dated",
                status,
                accessible,
                days,
            )
        return CurrencyAssessment(
            0.5,
            "accessible_stale",
            f"last verification is {days} day(s) old (> {STALE_VERIFICATION_DAYS})",
            status,
            accessible,
            days,
        )

    # No accessibility evidence at all. A directory-claimed status is weak.
    if status in LIVE_STATUSES and days is not None and days <= STALE_VERIFICATION_DAYS:
        return CurrencyAssessment(
            0.6,
            "accessible_stale",
            f"status '{status.value}' recorded {days} day(s) ago, "
            "but accessibility was never confirmed",
            status,
            accessible,
            days,
        )
    return CurrencyAssessment(
        0.3,
        "unknown",
        "no current-accessibility evidence — adoption discounted, not assumed",
        status,
        accessible,
        days,
    )


# =============================================================== scorer
class QualityScorer:
    """Computes the 100-point score for a :class:`~src.models.tool.Tool`."""

    def __init__(self, config: ScoringConfig | None = None, *, today: date | None = None) -> None:
        self.config = config or get_settings().scoring
        self._weight: Mapping[str, float] = {
            name: float(value) for name, value in (self.config.weights or WEIGHTS).items()
        }
        # Fail loudly on a mis-configured rubric rather than silently scoring
        # out of something other than 100.
        validate_rubric(self._weight)
        self.today = today or datetime.now(timezone.utc).date()

    # --------------------------------------------------------------- public
    def score(self, tool: Tool) -> ScoreBreakdown:
        """Return a populated :class:`ScoreBreakdown` (does not mutate ``tool``)."""
        currency = assess_currency(tool, self.today)
        components: list[ComponentScore] = [
            self._product_quality(tool),
            self._user_value(tool),
            self._adoption(tool, currency),
            self._activity(tool, currency),
            self._maturity(tool),
            self._recency(tool, currency),
            self._differentiation(tool),
            self._information_quality(tool),
        ]
        by_name = {component.name: component for component in components}
        if set(by_name) != set(COMPONENTS):  # pragma: no cover - guards a code change
            raise AssertionError(
                f"scorer must emit exactly the rubric components, got {sorted(by_name)}"
            )

        assessable = quantize(
            sum(c.max_points for c in components if c.evidenced)
        )
        return ScoreBreakdown(
            **{name: component.clamped() for name, component in by_name.items()},
            reasons={name: component.reasons for name, component in by_name.items()},
            criteria={
                name: dict(component.criteria) for name, component in by_name.items()
            },
            max_points={name: quantize(component.max_points) for name, component in by_name.items()},
            missing_evidence=[
                f"{name}.{key}"
                for name in COMPONENTS
                for key in by_name[name].missing
            ],
            unevidenced_components=[c.name for c in components if not c.evidenced],
            adjustments={
                name: component.adjustment
                for name, component in by_name.items()
                if component.adjustment
            },
            evidence_confidence=quantize(assessable / MAX_TOTAL, 3),
            currency_state=currency.state,
            currency_reason=currency.reason,
        )

    def apply(self, tool: Tool) -> Tool:
        """Score ``tool`` in place. Threshold decisions belong to the filter."""
        tool.quality = self.score(tool)
        logger.debug(
            "scored tool",
            extra={
                "tool": tool.name,
                "score": tool.quality.total,
                "band": str(tool.quality.band),
                "currency": tool.quality.currency_state,
            },
        )
        return tool

    def band_for(self, total: float) -> QualityBand:
        bands = self.config.bands or BANDS
        if total >= bands.get("exceptional", BANDS["exceptional"]):
            return QualityBand.EXCEPTIONAL
        if total >= bands.get("excellent", BANDS["excellent"]):
            return QualityBand.EXCELLENT
        if total >= bands.get("good", BANDS["good"]):
            return QualityBand.GOOD
        if total >= bands.get("average", BANDS["average"]):
            return QualityBand.AVERAGE
        return QualityBand.REJECT

    # ----------------------------------------------------------- components
    def _product_quality(self, tool: Tool) -> ComponentScore:
        """25 pts — depth of verified capability, not marketing copy."""
        b = _ComponentBuilder("product_quality_capability", self._weight)

        features = len(tool.key_features)
        b.graded("key_features", features, 6, f"{features} verified key feature(s)")

        capabilities = len(tool.ai_capabilities)
        b.graded(
            "ai_capabilities", capabilities, 4, f"{capabilities} identified AI capability(ies)"
        )

        # Specific I/O is a strong quality proxy and is mandated by §9.
        if tool.inputs and tool.outputs:
            b.award(
                "specific_io",
                1.0,
                f"specific I/O declared ({len(tool.inputs)} in / {len(tool.outputs)} out)",
            )
        elif tool.inputs or tool.outputs:
            b.award("specific_io", 0.4, "partial I/O information only")
        else:
            b.miss("specific_io", "no verified input/output formats")

        platforms = len(tool.platforms)
        if platforms >= 2:
            b.award("platforms", 1.0, f"multi-platform ({platforms} platforms)")
        elif platforms:
            b.award("platforms", 0.5, "single platform")
        else:
            b.miss("platforms")

        if tool.has_api:
            b.award("public_api", 1.0, "public API available")
        else:
            b.miss("public_api")

        integrations = len(tool.integrations)
        b.graded("integrations", integrations, 5, f"{integrations} integration(s)")

        evidenced = bool(
            tool.key_features or tool.ai_capabilities or tool.inputs or tool.outputs
        )
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "no verified capability evidence — component scored on nothing",
        )

    def _user_value(self, tool: Tool) -> ComponentScore:
        """20 pts — does it solve a meaningful problem for a real user?"""
        b = _ComponentBuilder("real_user_value", self._weight)

        if tool.primary_task:
            b.award("primary_task", 1.0, f"clear primary task: {tool.primary_task}")
        else:
            b.miss("primary_task", "no primary task identified")

        use_cases = len(tool.use_cases)
        b.graded("use_cases", use_cases, 4, f"{use_cases} documented use case(s)")

        overview = tool.detailed_overview or ""
        if len(overview) >= 300:
            b.award("overview_depth", 1.0, "substantive product overview")
        elif len(overview) >= 120:
            b.award("overview_depth", 0.5, "short product overview")
        elif tool.short_description:
            b.award("overview_depth", 0.25, "tagline only")
        else:
            b.miss("overview_depth", "no product description")

        b.graded("pros", len(tool.pros), 3, f"{len(tool.pros)} curated pro(s)")

        # Honest limitations are a value signal, not a penalty.
        if tool.cons or tool.limitations:
            b.award("limitations", 1.0, "limitations documented honestly")
        else:
            b.miss("limitations")

        if tool.pricing.has_free_plan or tool.pricing.has_free_trial:
            b.award("low_friction_access", 1.0, "low-friction access (free plan or trial)")
        else:
            b.miss("low_friction_access")

        if tool.pricing.model:
            b.award("pricing_transparency", 1.0, "pricing is published")
        else:
            b.miss("pricing_transparency")

        evidenced = bool(tool.primary_task or tool.use_cases or tool.detailed_overview)
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "no verified user-value evidence — component scored on nothing",
        )

    def _adoption(self, tool: Tool, currency: CurrencyAssessment) -> ComponentScore:
        """15 pts — **current** usage. Unknown adoption scores 0, never a guess."""
        b = _ComponentBuilder("current_usage_adoption", self._weight)
        adoption = tool.adoption

        b.tiered(
            "monthly_visits",
            adoption.monthly_visits,
            ((5_000_000, 1.0), (1_000_000, 0.85), (250_000, 0.65), (50_000, 0.45), (10_000, 0.25)),
            "monthly visits",
        )
        b.tiered(
            "github_stars",
            adoption.github_stars,
            ((20_000, 1.0), (5_000, 0.8), (1_000, 0.55), (200, 0.3)),
            "GitHub stars",
        )
        b.tiered(
            "product_hunt_upvotes",
            adoption.product_hunt_upvotes,
            ((2_000, 1.0), (750, 0.75), (250, 0.5), (50, 0.25)),
            "Product Hunt upvotes",
        )
        b.tiered(
            "directory_signals",
            adoption.directory_saves or adoption.directory_upvotes,
            ((10_000, 1.0), (2_500, 0.7), (500, 0.45), (100, 0.2)),
            "directory saves/upvotes",
        )
        if adoption.review_count and adoption.review_rating:
            volume = min(adoption.review_count, 500) / 500
            quality = max(0.0, (adoption.review_rating - 3.0) / 2.0)
            platform = f" on {adoption.review_platform}" if adoption.review_platform else ""
            b.award(
                "reviews",
                volume * quality,
                f"{adoption.review_count} reviews at {adoption.review_rating}/5{platform}",
            )
        else:
            b.miss("reviews")

        customers = len(adoption.notable_customers)
        b.graded("notable_customers", customers, 5, f"{customers} notable customer(s)")

        # "Current usage" has to be current: historical popularity is
        # discounted when nothing shows the product still runs today.
        if currency.factor < 1.0:
            b.discount(
                currency.factor,
                f"adoption discounted x{currency.factor:g} — {currency.reason}",
            )

        evidenced = bool(adoption.has_any_signal)
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "no verifiable adoption signal — scored 0 rather than assumed",
        )

    def _activity(self, tool: Tool, currency: CurrencyAssessment) -> ComponentScore:
        """15 pts — is the product still being maintained? (§6)"""
        b = _ComponentBuilder("activity_maintenance", self._weight)
        status = currency.status

        if status in LIVE_STATUSES:
            b.award("operational_status", 1.0, f"status '{status.value}'")
        elif status in PRE_GA_STATUSES:
            b.award("operational_status", 0.5, f"status '{status.value}' (pre-GA)")
        elif status in DEAD_STATUSES:
            b.miss("operational_status", f"status '{status.value}' — no activity credit")
        else:
            b.miss("operational_status", "operational status unknown")

        if currency.accessible is True:
            b.award("site_accessible", 1.0, "official website verified accessible")
        elif currency.accessible is False:
            b.miss("site_accessible", "official website not accessible")
        else:
            b.miss("site_accessible", "official website accessibility never checked")

        days = currency.days_since_verification
        if days is None:
            b.miss("verification_freshness", "never verified by us")
        elif days <= 30:
            b.award("verification_freshness", 1.0, f"verified {days} day(s) ago")
        elif days <= FRESH_VERIFICATION_DAYS:
            b.award("verification_freshness", 0.6, f"verified {days} day(s) ago")
        elif days <= STALE_VERIFICATION_DAYS:
            b.award("verification_freshness", 0.3, f"verified {days} day(s) ago")
        else:
            b.miss("verification_freshness", f"last verification is {days} day(s) old")

        if tool.version:
            b.award("version_published", 1.0, f"published version {tool.version}")
        else:
            b.miss("version_published")

        priced_days = _days_since(tool.pricing.pricing_verified_at, self.today)
        if priced_days is not None and priced_days <= STALE_VERIFICATION_DAYS:
            b.award("pricing_verified", 1.0, "current pricing verified")
        elif priced_days is not None:
            b.award("pricing_verified", 0.3, f"pricing check is {priced_days} day(s) old")
        else:
            b.miss("pricing_verified")

        evidenced = bool(
            status is not None
            or currency.accessible is not None
            or tool.last_verified_date is not None
        )
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "no maintenance evidence at all — scored 0 rather than assumed active",
        )

    def _maturity(self, tool: Tool) -> ComponentScore:
        """10 pts — reliability signals of an established product."""
        b = _ComponentBuilder("product_maturity_reliability", self._weight)

        if tool.company:
            b.award("company_identified", 1.0, f"identified developer: {tool.company}")
        else:
            b.miss("company_identified", "developer/company unknown")

        if tool.pricing.model:
            b.award(
                "pricing_model", 1.0, f"published pricing model '{tool.pricing.model}'"
            )
        else:
            b.miss("pricing_model")

        if tool.has_api and tool.api_docs_url:
            b.award("documented_api", 1.0, "documented API")
        else:
            b.miss("documented_api")

        if _is_open_source(tool) and tool.repository_url:
            b.award("open_source_repo", 1.0, "open source with a public repository")
        else:
            b.miss("open_source_repo")

        age_days = _days_since(tool.launch_date, self.today)
        if age_days is None:
            b.miss("operating_history", "launch date unverified")
        elif age_days >= 730:
            b.award("operating_history", 1.0, f"operating for {age_days // 365} year(s)")
        elif age_days >= 365:
            b.award("operating_history", 0.7, "operating for over a year")
        elif age_days >= 180:
            b.award("operating_history", 0.4, "operating for over six months")
        else:
            b.award("operating_history", 0.2, "recently launched — limited track record")

        if tool.country:
            b.award("company_location", 1.0, "company location known")
        else:
            b.miss("company_location")

        if tool.website:
            b.award("official_site", 1.0, "official website recorded")
        else:
            b.miss("official_site", "no official website recorded")

        evidenced = bool(tool.company or tool.pricing.model or tool.launch_date)
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "no maturity evidence — component scored on nothing",
        )

    def _recency(self, tool: Tool, currency: CurrencyAssessment) -> ComponentScore:
        """5 pts — "recently launched and gaining real usage" (§1, §6).

        Momentum requires *both* a recent launch and evidence the product is
        still running. An old product earns nothing here, and a dead one earns
        nothing regardless of how recently it launched.
        """
        b = _ComponentBuilder("recency_momentum", self._weight)
        age_days = _days_since(tool.launch_date, self.today)

        if currency.is_dead:
            b.miss("launch_recency", f"no momentum credit — {currency.reason}")
            return b.build(evidenced=age_days is not None)

        if age_days is None:
            b.miss("launch_recency", "launch date unverified — no recency credit")
            return b.build(evidenced=False)

        if age_days <= 180:
            fraction, why = 1.0, f"launched {age_days} day(s) ago"
        elif age_days <= 365:
            fraction, why = 0.8, "launched within the last year"
        elif age_days <= 730:
            fraction, why = 0.55, "launched within the last two years"
        elif age_days <= 1460:
            fraction, why = 0.3, "launched within the last four years"
        else:
            # Older products earn momentum only from *current*, evidenced use.
            if tool.adoption.has_any_signal and currency.has_current_evidence:
                fraction, why = 0.15, "older product with current, evidenced usage"
            else:
                fraction, why = 0.0, "older product without current-usage evidence"

        # A launch date known only to the year is weaker evidence.
        if fraction and tool.launch_date_precision == "year":
            fraction *= 0.9
            why = f"{why} (year-precision launch date)"

        # Unconfirmed liveness cannot grant full momentum either.
        if fraction and not currency.has_current_evidence:
            fraction *= 0.5
            b.note_adjustment(f"momentum halved — {currency.reason}")

        if fraction:
            b.award("launch_recency", fraction, why)
        else:
            b.miss("launch_recency", why)
        return b.build(evidenced=True)

    def _differentiation(self, tool: Tool) -> ComponentScore:
        """5 pts — "clearly differentiated or genuinely useful" (§1, §4)."""
        b = _ComponentBuilder("differentiation", self._weight)

        if tool.aiorbit_summary:
            b.award("editorial_verdict", 1.0, "editorial verdict written from verified facts")
        else:
            b.miss("editorial_verdict")

        if len(tool.key_features) >= 4:
            b.award("feature_depth", 1.0, "feature depth suggests a distinct product")
        elif len(tool.key_features) >= 2:
            b.award("feature_depth", 0.5, "some feature detail")
        else:
            b.miss("feature_depth")

        if len(tool.ai_capabilities) >= 3:
            b.award("capability_breadth", 1.0, "multi-capability product")
        elif len(tool.ai_capabilities) >= 2:
            b.award("capability_breadth", 0.5, "two identified capabilities")
        else:
            b.miss("capability_breadth")

        if _is_open_source(tool):
            b.award("open_source", 1.0, "open source differentiation")
        else:
            b.miss("open_source")

        if tool.has_api:
            b.award("programmatic_access", 1.0, "programmatic access differentiates from UI-only clones")
        else:
            b.miss("programmatic_access")

        if tool.adoption.funding_raised_usd:
            b.award("funding", 1.0, "verified funding indicates a real company")
        else:
            b.miss("funding")

        if tool.adoption.notable_customers:
            b.award("customer_proof", 1.0, "named customers")
        else:
            b.miss("customer_proof")

        evidenced = bool(tool.key_features or tool.ai_capabilities or tool.aiorbit_summary)
        return b.build(
            evidenced=evidenced,
            note=None
            if evidenced
            else "cannot assess differentiation without verified detail",
        )

    def _information_quality(self, tool: Tool) -> ComponentScore:
        """5 pts — can we actually stand behind this record? (§8, §10)"""
        b = _ComponentBuilder("information_quality_verifiability", self._weight)
        status = tool.verification.status

        if status in (VerificationStatus.VERIFIED, VerificationStatus.VERIFIED.value):
            b.award("verification_status", 1.0, "verified against the official website")
        elif status in (
            VerificationStatus.PARTIALLY_VERIFIED,
            VerificationStatus.PARTIALLY_VERIFIED.value,
        ):
            b.award("verification_status", 0.5, "partially verified")
        else:
            b.miss("verification_status", f"verification status '{status}'")

        completeness = tool.completeness()
        if completeness:
            b.award("field_completeness", completeness, f"field completeness {completeness:.0%}")
        else:
            b.miss("field_completeness")

        sources = len(tool.discovery_sources)
        if sources >= 2:
            b.award("source_corroboration", 1.0, f"corroborated by {sources} sources")
        elif sources == 1:
            b.award("source_corroboration", 0.35, "single discovery source")
        else:
            b.miss("source_corroboration", "no discovery provenance recorded")

        if tool.verification.verification_sources:
            b.award("verification_source", 1.0, "verification source recorded")
        else:
            b.miss("verification_source")

        days = _days_since(tool.last_verified_date, self.today)
        if days is not None and days <= FRESH_VERIFICATION_DAYS:
            b.award("verification_recent", 1.0, f"verification is {days} day(s) old")
        elif days is not None and days <= STALE_VERIFICATION_DAYS:
            b.award("verification_recent", 0.5, f"verification is {days} day(s) old")
        else:
            b.miss("verification_recent", "no recent verification date")

        # This component is always assessable: "we could not verify it" is
        # itself the finding.
        return b.build(evidenced=True)


# ------------------------------------------------------------------ helpers
def _is_open_source(tool: Tool) -> bool:
    return tool.open_source_status in (
        OpenSourceStatus.OPEN_SOURCE,
        OpenSourceStatus.OPEN_SOURCE.value,
    )


def _days_since(value: date | None, today: date) -> int | None:
    """Whole days between ``value`` and ``today``; ``None`` when unknown.

    A future date returns ``None`` rather than a negative age: it is bad data,
    and bad data must not become credit.
    """
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

    ``min_score`` defaults to ``skip_below`` (70), i.e. the lowest score the
    guideline allows into a batch at all. Whether a 70-79 record is *actually*
    admitted is decided earlier, by :class:`src.scoring.filter.QualityFilter`,
    which gates it on evidence; selection must not silently re-open that
    decision, and it must never relax it to reach the target.
    """
    settings = get_settings()
    target_size = target_size or settings.batch.target_size
    if min_score is None:
        min_score = getattr(settings.scoring, "skip_below", SKIP_BELOW)
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
        tool
        for tool in tools
        if tool.rejected or tool.quality is None or tool.quality.total < min_score
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
