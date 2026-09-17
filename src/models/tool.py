"""Canonical Tool entity.

Extends the common entity schema with every field required by the Tools
guideline section 8 (Basic / Product / Pricing / Quality) plus the scoring and
verification metadata the pipeline needs.

Design rules encoded here:

* **No guessing.** Every enrichable field is Optional and defaults to ``None``.
  A value of ``None`` means "not verified", which is exactly what the guideline
  demands ("Leave the field blank. Do not guess.").
* **Specific I/O only.** ``inputs``/``outputs`` are typed with
  :class:`~src.models.enums.IOFormat`; vague free text cannot be stored.
* **Auditability.** ``discovery_sources``, ``verification``, ``quality`` and
  ``provenance`` travel with the record.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import Field, computed_field, field_validator, model_validator

from src.core.product_identity import product_identity
from src.core.text import clean_text
from src.core.urls import extract_registrable_domain, normalize_url
from src.models.base import AIOrbitModel, BaseEntity, SourceRef
from src.models.enums import (
    AICapability,
    EntityType,
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


def _coerce_enum_list(enum_cls: type, value: Any) -> list:
    """Map free-text values onto a controlled vocabulary, dropping unknowns.

    Unknown values are dropped rather than passed through: an unverifiable
    value must not enter the dataset.
    """
    if value is None:
        return []
    if isinstance(value, (str, enum_cls)):
        value = [value]
    out: list = []
    for item in value:
        member = enum_cls.coerce(item) if hasattr(enum_cls, "coerce") else None
        if member is not None and member not in out:
            out.append(member)
    return out


def _clean_str_list(value: Any, *, max_items: int | None = None, max_length: int = 300) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    seen: dict[str, str] = {}
    for item in value:
        cleaned = clean_text(item, max_length=max_length)
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)
    items = list(seen.values())
    return items[:max_items] if max_items else items


class PricingInfo(AIOrbitModel):
    """Pricing block (guideline section 8 → Pricing)."""

    model: PricingModel | None = None
    starting_price_amount: float | None = Field(None, ge=0)
    starting_price_currency: str | None = Field(None, max_length=3)
    starting_price_period: str | None = Field(
        None, description="month | year | one_time | per_credit | per_seat | per_request"
    )
    #: Raw price string exactly as shown on the official pricing page.
    starting_price_raw: str | None = None
    has_free_plan: bool | None = None
    has_free_trial: bool | None = None
    free_trial_days: int | None = Field(None, ge=0)
    usage_limits: list[str] = Field(
        default_factory=list, description="Important verified limits, e.g. '50 images/month on free plan'"
    )
    pricing_url: str | None = None
    pricing_verified_at: date | None = None

    @field_validator("starting_price_currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> Any:
        cleaned = clean_text(value)
        return cleaned.upper()[:3] if cleaned else None

    @field_validator("pricing_url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @field_validator("usage_limits", mode="before")
    @classmethod
    def _limits(cls, value: Any) -> Any:
        return _clean_str_list(value, max_items=10)


class AdoptionSignals(AIOrbitModel):
    """Verified usage/adoption evidence (guideline sections 6 and 8).

    Every field is Optional: adoption numbers are frequently unavailable, and
    fabricating them is explicitly forbidden.
    """

    monthly_visits: int | None = Field(None, ge=0)
    monthly_visits_source: str | None = None
    directory_saves: int | None = Field(None, ge=0)
    directory_upvotes: int | None = Field(None, ge=0)
    product_hunt_upvotes: int | None = Field(None, ge=0)
    github_stars: int | None = Field(None, ge=0)
    review_count: int | None = Field(None, ge=0)
    review_rating: float | None = Field(None, ge=0, le=5)
    review_platform: str | None = None
    stated_user_count: str | None = Field(
        None, description="Verbatim claim from the official site, e.g. '2M+ users'"
    )
    notable_customers: list[str] = Field(default_factory=list)
    social_followers: int | None = Field(None, ge=0)
    funding_raised_usd: float | None = Field(None, ge=0)
    signal_sources: list[SourceRef] = Field(default_factory=list)
    observed_at: date | None = None

    @field_validator("notable_customers", mode="before")
    @classmethod
    def _customers(cls, value: Any) -> Any:
        return _clean_str_list(value, max_items=20, max_length=120)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_any_signal(self) -> bool:
        """True when at least one adoption datapoint was actually verified."""
        return any(
            getattr(self, name) not in (None, [], 0)
            for name in (
                "monthly_visits", "directory_saves", "directory_upvotes",
                "product_hunt_upvotes", "github_stars", "review_count",
                "stated_user_count", "notable_customers", "social_followers",
                "funding_raised_usd",
            )
        )


class ScoreBreakdown(AIOrbitModel):
    """Transparent, explainable 100-point score (guideline section 5).

    Each component stores the awarded points, the maximum weight and the
    human-readable reasons, so a reviewer can audit any decision. The field
    bounds below are the rubric weights themselves, so a component that
    somehow exceeded its weight is a schema error rather than a silent
    inflation of the total.
    """

    #: Component scores. ``le=`` is the guideline §5 weight, verbatim.
    product_quality_capability: float = Field(0, ge=0, le=25)
    real_user_value: float = Field(0, ge=0, le=20)
    current_usage_adoption: float = Field(0, ge=0, le=15)
    activity_maintenance: float = Field(0, ge=0, le=15)
    product_maturity_reliability: float = Field(0, ge=0, le=10)
    recency_momentum: float = Field(0, ge=0, le=5)
    differentiation: float = Field(0, ge=0, le=5)
    information_quality_verifiability: float = Field(0, ge=0, le=5)

    reasons: dict[str, list[str]] = Field(
        default_factory=dict, description="component -> list of explanations"
    )
    #: component -> {criterion: points} — the full per-criterion audit trail.
    criteria: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: component -> rubric weight actually used when scoring this record.
    max_points: dict[str, float] = Field(default_factory=dict)
    #: ``component.criterion`` entries that had no evidence (scored nothing).
    missing_evidence: list[str] = Field(default_factory=list)
    #: Components that could not be evidenced at all (scored 0, not guessed).
    unevidenced_components: list[str] = Field(default_factory=list)
    #: component -> transparent note about a discount that was applied.
    adjustments: dict[str, str] = Field(default_factory=dict)
    #: Share of the 100 points that was even assessable from the evidence.
    evidence_confidence: float = Field(0, ge=0, le=1)
    #: ``current`` | ``accessible_stale`` | ``unknown`` | ``dead``.
    currency_state: str | None = None
    currency_reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        """Deterministic sum of the eight components, out of 100."""
        from src.scoring.rubric import COMPONENTS, MAX_TOTAL, quantize

        total = quantize(sum(getattr(self, name) for name in COMPONENTS))
        # Structurally impossible given the field bounds, but the dataset
        # contract is "0-100" and it is cheap to guarantee it here too.
        return min(max(total, 0.0), MAX_TOTAL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def band(self) -> QualityBand:
        from src.scoring.rubric import BANDS

        total = self.total
        if total >= BANDS["exceptional"]:
            return QualityBand.EXCEPTIONAL
        if total >= BANDS["excellent"]:
            return QualityBand.EXCELLENT
        if total >= BANDS["good"]:
            return QualityBand.GOOD
        if total >= BANDS["average"]:
            return QualityBand.AVERAGE
        return QualityBand.REJECT

    def component_points(self) -> dict[str, float]:
        """Component -> awarded points, in rubric order."""
        from src.scoring.rubric import COMPONENTS

        return {name: getattr(self, name) for name in COMPONENTS}


class VerificationRecord(AIOrbitModel):
    """Outcome of official-website verification (guideline section 10)."""

    status: VerificationStatus = VerificationStatus.UNVERIFIED
    official_url_checked: str | None = None
    http_status: int | None = None
    final_url: str | None = Field(None, description="URL after redirect resolution")
    is_accessible: bool | None = None
    verified_fields: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(
        default_factory=list, description="Fields where sources disagreed; official value kept"
    )
    unverifiable_fields: list[str] = Field(
        default_factory=list, description="Fields deliberately left null"
    )
    notes: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None
    verification_sources: list[SourceRef] = Field(default_factory=list)

    @field_validator("official_url_checked", "final_url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None


class DedupInfo(AIOrbitModel):
    """Entity-resolution audit trail (guideline section 7).

    Everything a reviewer needs to audit *why* two sightings became one record
    (or deliberately did not) travels here: the identity key and its basis, the
    canonical product key (host + product path), every alias and listing URL
    seen, the evidence behind each merge, and an explicit reason per record
    queued for human review.
    """

    identity_key: str | None = None
    identity_basis: str | None = Field(
        None,
        description=(
            "canonical_url | website_product_url | website_domain | "
            "listing_product_url | listing_domain | name+company | name"
        ),
    )
    #: Registrable domain of the official site — a *company/site* signal. On its
    #: own it is never proof of product identity (distinct products share it).
    canonical_domain: str | None = None
    #: Canonical product identity: full host plus product-identifying path,
    #: e.g. ``company.com/product-a``. See :mod:`src.core.product_identity`.
    product_key: str | None = None
    #: Explicitly resolved canonical product URL (redirect target,
    #: ``rel=canonical``, verified official URL) — the strongest identity.
    canonical_url: str | None = None
    canonical_name_key: str | None = None
    aliases: list[str] = Field(default_factory=list, description="Observed name variations")
    #: Every directory detail/listing URL this product was sighted at.
    listing_urls: list[str] = Field(default_factory=list)
    merged_from_ids: list[str] = Field(default_factory=list)
    #: Identity keys of every record folded into this one.
    merged_identity_keys: list[str] = Field(default_factory=list)
    merged_source_count: int = 1
    #: Why each merge happened, e.g. ``"tier_b: product identity company.com/a"``.
    merge_evidence: list[str] = Field(default_factory=list)
    duplicate_of: str | None = Field(None, description="Set on non-canonical records")
    review_candidates: list[str] = Field(
        default_factory=list, description="IDs flagged as possible duplicates for human review"
    )
    #: One explicit ``"<other id>: <reason>"`` entry per review candidate.
    review_reasons: list[str] = Field(default_factory=list)

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> Any:
        return _clean_str_list(value, max_items=25, max_length=200)

    @field_validator("canonical_url", mode="before")
    @classmethod
    def _canonical_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @field_validator("listing_urls", mode="before")
    @classmethod
    def _listing_urls(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        seen: list[str] = []
        for item in value:
            normalized = normalize_url(item) if item else None
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen

    @field_validator("merge_evidence", "review_reasons", "merged_identity_keys", mode="before")
    @classmethod
    def _audit_lists(cls, value: Any) -> Any:
        return _clean_str_list(value, max_items=200, max_length=400)


class Tool(BaseEntity):
    """A curated AI tool record."""

    entity_type: EntityType = EntityType.TOOL

    # ------------------------------------------------------------ 8. Basic
    company: str | None = Field(None, description="Company / developer name")
    company_id: str | None = Field(None, description="Deterministic ID of the company entity")
    website: str | None = Field(None, description="Official website (verification source)")
    logo_url: str | None = Field(None, description="Logo/image URL; never fabricated")
    country: str | None = Field(None, description="ISO-3166 alpha-2 when known")
    version: str | None = None
    launch_date: date | None = Field(None, description="Verified launch/release date only")
    launch_date_precision: str | None = Field(
        None, description="day | month | year — records how precise launch_date really is"
    )
    status: ToolStatus | None = None
    short_description: str | None = Field(None, max_length=320)
    detailed_overview: str | None = None

    # ---------------------------------------------------------- 8. Product
    primary_task: str | None = Field(
        None, description="The single main job the tool does (links to a Task entity)"
    )
    primary_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    key_features: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    ai_capabilities: list[AICapability] = Field(default_factory=list)
    inputs: list[IOFormat] = Field(
        default_factory=list, description="Specific verified input formats (guideline §9)"
    )
    outputs: list[IOFormat] = Field(
        default_factory=list, description="Specific verified output formats (guideline §9)"
    )
    platforms: list[Platform] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    has_api: bool | None = None
    api_docs_url: str | None = None
    open_source_status: OpenSourceStatus | None = None
    repository_url: str | None = None
    signup_requirement: SignupRequirement | None = None

    # ---------------------------------------------------------- 8. Pricing
    pricing: PricingInfo = Field(default_factory=PricingInfo)

    # ---------------------------------------------------------- 8. Quality
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    aiorbit_summary: str | None = Field(None, description="AI Orbit verdict / editorial summary")
    adoption: AdoptionSignals = Field(default_factory=AdoptionSignals)
    #: Literal counters/ratings observed on a discovery directory. These stay
    #: separate from official product facts; only explicitly understood
    #: counters may additionally be mapped to ``adoption`` during promotion.
    directory_evidence: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    last_verified_date: date | None = None
    discovery_sources: list[SourceRef] = Field(
        default_factory=list, description="Every directory/listing the tool was discovered in"
    )
    verification: VerificationRecord = Field(default_factory=VerificationRecord)
    #: ``field name -> what on the official page justified that value``.
    #: Written by :class:`src.extraction.official_page.OfficialFactsExtractor`
    #: so any published fact can be audited without re-fetching the site. An
    #: entry here is a *claim about our own evidence*, never about the product.
    official_evidence: dict[str, str] = Field(
        default_factory=dict,
        description="Per-field justification read off the official website",
    )
    #: Fields the official page was searched for and did **not** support. Kept
    #: explicitly so \"blank\" is visibly *unverified*, not merely forgotten.
    official_unverified_fields: list[str] = Field(default_factory=list)

    # ------------------------------------------------- pipeline metadata
    quality: ScoreBreakdown | None = None
    dedup: DedupInfo = Field(default_factory=DedupInfo)
    rejected: bool = False
    rejection_reasons: list[RejectionReason] = Field(default_factory=list)
    rejection_notes: list[str] = Field(default_factory=list)
    needs_human_review: bool = False
    review_notes: list[str] = Field(default_factory=list)
    batch_number: int | None = None

    # -------------------------------------------------------- validators
    @field_validator("company", "version", "primary_task", "aiorbit_summary", mode="before")
    @classmethod
    def _clean_simple(cls, value: Any) -> Any:
        return clean_text(value)

    @field_validator("short_description", mode="before")
    @classmethod
    def _clean_short(cls, value: Any) -> Any:
        return clean_text(value, max_length=320)

    @field_validator("detailed_overview", mode="before")
    @classmethod
    def _clean_overview(cls, value: Any) -> Any:
        return clean_text(value, max_length=4000)

    @field_validator("website", "logo_url", "api_docs_url", "repository_url", mode="before")
    @classmethod
    def _clean_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @field_validator("country", mode="before")
    @classmethod
    def _clean_country(cls, value: Any) -> Any:
        cleaned = clean_text(value)
        if not cleaned:
            return None
        return cleaned.upper() if len(cleaned) == 2 else cleaned

    @field_validator(
        "tags", "key_features", "use_cases", "integrations", "pros", "cons",
        "limitations", "review_notes", "rejection_notes",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: Any) -> Any:
        return _clean_str_list(value, max_items=40)

    @field_validator("ai_capabilities", mode="before")
    @classmethod
    def _caps(cls, value: Any) -> Any:
        return _coerce_enum_list(AICapability, value)

    @field_validator("inputs", "outputs", mode="before")
    @classmethod
    def _io(cls, value: Any) -> Any:
        return _coerce_enum_list(IOFormat, value)

    @field_validator("platforms", mode="before")
    @classmethod
    def _platforms(cls, value: Any) -> Any:
        return _coerce_enum_list(Platform, value)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> Any:
        return ToolStatus.coerce(value) if value is not None else None

    @field_validator("open_source_status", mode="before")
    @classmethod
    def _oss(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return OpenSourceStatus.OPEN_SOURCE if value else OpenSourceStatus.PROPRIETARY
        return OpenSourceStatus.coerce(value) if value is not None else None

    @field_validator("signup_requirement", mode="before")
    @classmethod
    def _signup(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return SignupRequirement.REQUIRED if value else SignupRequirement.NONE
        return SignupRequirement.coerce(value) if value is not None else None

    @field_validator("launch_date_precision", mode="before")
    @classmethod
    def _precision(cls, value: Any) -> Any:
        cleaned = clean_text(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        return cleaned if cleaned in {"day", "month", "year"} else None

    @model_validator(mode="after")
    def _derive(self) -> "Tool":
        # url mirrors the official website so the common schema stays populated
        if not self.url and self.website:
            object.__setattr__(self, "url", self.website)
        if not self.website and self.url:
            object.__setattr__(self, "website", self.url)
        # description mirrors short_description (common schema field)
        if not self.description and self.short_description:
            object.__setattr__(self, "description", self.short_description)
        if not self.short_description and self.description:
            object.__setattr__(self, "short_description", self.description[:320])
        # keep dedup identity hints in sync with the official site
        domain = extract_registrable_domain(self.website)
        if domain and not self.dedup.canonical_domain:
            self.dedup.canonical_domain = domain
        # The canonical *product* key is host + product path, so distinct
        # products under one domain stay distinguishable (guideline §7).
        # A shared-host *root* (a directory/app-store/site-builder landing page)
        # is skipped: it identifies no single product, and storing it here would
        # let two unrelated records claim the same explicit identity.
        if not self.dedup.product_key:
            identity = product_identity(self.dedup.canonical_url or self.website)
            if identity and identity.identifies_a_product:
                self.dedup.product_key = identity.key
        if self.rejection_reasons and not self.rejected:
            object.__setattr__(self, "rejected", True)
        object.__setattr__(self, "last_updated_at", datetime.now(timezone.utc))
        return self

    # ------------------------------------------------------------ helpers
    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float | None:
        """Total quality score, or ``None`` if not yet scored."""
        return self.quality.total if self.quality else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def band(self) -> str | None:
        return str(self.quality.band) if self.quality else None

    @property
    def is_verified(self) -> bool:
        return self.verification.status in (
            VerificationStatus.VERIFIED,
            VerificationStatus.VERIFIED.value,
            VerificationStatus.PARTIALLY_VERIFIED,
            VerificationStatus.PARTIALLY_VERIFIED.value,
        )

    def completeness(self) -> float:
        """Fraction of guideline section-8 fields that carry a verified value."""
        checks = [
            self.name, self.company, self.website, self.logo_url, self.country,
            self.launch_date, self.status, self.short_description, self.detailed_overview,
            self.primary_task, self.tags, self.key_features, self.use_cases,
            self.ai_capabilities, self.inputs, self.outputs, self.platforms,
            self.integrations, self.has_api, self.open_source_status,
            self.signup_requirement, self.pricing.model, self.pricing.has_free_plan,
            self.pros, self.cons, self.aiorbit_summary, self.last_verified_date,
            self.discovery_sources,
        ]
        filled = sum(1 for value in checks if value not in (None, [], "", {}))
        return round(filled / len(checks), 3)

    def add_discovery_source(self, source: SourceRef) -> None:
        """Record an additional sighting without creating a duplicate record."""
        key = (source.name.lower(), source.url or "")
        existing = {(s.name.lower(), s.url or "") for s in self.discovery_sources}
        if key not in existing:
            self.discovery_sources = [*self.discovery_sources, source]
        self.dedup.merged_source_count = max(
            self.dedup.merged_source_count, len(self.discovery_sources)
        )

    def reject(self, reason: RejectionReason, note: str | None = None) -> None:
        if reason not in self.rejection_reasons:
            self.rejection_reasons = [*self.rejection_reasons, reason]
        if note:
            self.rejection_notes = [*self.rejection_notes, note]
        self.rejected = True

    def flag_for_review(self, note: str) -> None:
        self.needs_human_review = True
        if note not in self.review_notes:
            self.review_notes = [*self.review_notes, note]
