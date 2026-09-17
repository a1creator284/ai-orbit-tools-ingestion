"""Record cleaning + normalization.

Turns an unverified :class:`~src.discovery.base.CandidateTool` (or an arbitrary
raw dict) into a canonical :class:`~src.models.tool.Tool`.

Hard rules enforced here:

* nothing is invented — unmappable or vague values become ``None``/``[]``;
* text is sanitized (HTML/RSS entities stripped);
* URLs are normalized (:mod:`src.core.urls`);
* IDs are deterministic (:mod:`src.core.ids`);
* directory-only facts are recorded as *discovery* data, never as verified data.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

from dateutil import parser as date_parser

from src.core.ids import make_entity_id
from src.core.logging_setup import get_logger
from src.core.text import canonical_name, clean_text, slugify
from src.core.urls import extract_registrable_domain, normalize_url
from src.discovery.base import CandidateTool
from src.models.base import SourceRef
from src.models.enums import EntityType, ToolStatus
from src.models.tool import AdoptionSignals, DedupInfo, Tool

logger = get_logger("cleaning.normalizer")

#: Vague values banned by guideline §9 ("Do not write vague information").
VAGUE_VALUES = {
    "ai content", "ai", "content", "ai output", "ai input", "data", "anything",
    "everything", "various", "misc", "miscellaneous", "other", "others", "n/a",
    "generic", "all formats", "any format", "any", "stuff", "information",
    "ai generated content", "ai-generated content", "output", "input",
}

#: Category/tag → primary_task mapping. Deliberately conservative: an unknown
#: category yields ``None`` rather than a guessed task.
PRIMARY_TASK_MAP: dict[str, str] = {
    "writing": "AI writing",
    "copywriting": "AI writing",
    "content": "AI writing",
    "blog": "AI writing",
    "seo": "SEO optimization",
    "image": "AI image generation",
    "image generation": "AI image generation",
    "art": "AI image generation",
    "design": "AI design",
    "logo": "AI logo design",
    "video": "AI video generation",
    "video editing": "AI video editing",
    "audio": "AI audio generation",
    "music": "AI music generation",
    "voice": "AI voice generation",
    "text to speech": "AI text-to-speech",
    "speech to text": "AI transcription",
    "transcription": "AI transcription",
    "translation": "AI translation",
    "chatbot": "AI chatbot",
    "chat": "AI chatbot",
    "assistant": "AI assistant",
    "agent": "AI agents",
    "agents": "AI agents",
    "code": "AI coding assistance",
    "coding": "AI coding assistance",
    "developer tools": "AI coding assistance",
    "data analysis": "AI data analysis",
    "analytics": "AI data analysis",
    "research": "AI research",
    "search": "AI search",
    "summarization": "AI summarization",
    "productivity": "AI productivity",
    "automation": "AI workflow automation",
    "workflow": "AI workflow automation",
    "marketing": "AI marketing",
    "advertising": "AI advertising",
    "sales": "AI sales enablement",
    "customer support": "AI customer support",
    "email": "AI email assistance",
    "presentation": "AI presentation generation",
    "slides": "AI presentation generation",
    "spreadsheet": "AI spreadsheet assistance",
    "resume": "AI resume building",
    "education": "AI education",
    "legal": "AI legal assistance",
    "healthcare": "AI healthcare",
    "finance": "AI finance",
    "recruiting": "AI recruiting",
    "3d": "AI 3D generation",
    "avatar": "AI avatar generation",
    "presentation maker": "AI presentation generation",
    "note taking": "AI note taking",
    "meeting": "AI meeting assistance",
    "social media": "AI social media management",
    "ecommerce": "AI ecommerce",
    "photo editing": "AI image editing",
    "background remover": "AI image editing",
}

_PRICE_RE = re.compile(
    r"(?P<currency>[$€£¥]|usd|eur|gbp)\s?(?P<amount>\d{1,5}(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
_CURRENCY_CODES = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "usd": "USD", "eur": "EUR", "gbp": "GBP"}


def is_vague(value: str | None) -> bool:
    """True when a value is too generic to be stored (guideline §9)."""
    cleaned = clean_text(value)
    if not cleaned:
        return True
    return cleaned.strip().lower() in VAGUE_VALUES


def drop_vague(values: list[str] | None) -> list[str]:
    return [v for v in (values or []) if not is_vague(v)]


def parse_date_safe(value: Any) -> tuple[date | None, str | None]:
    """Parse a date, returning ``(date, precision)``.

    Never guesses a day for a "2024" string beyond recording the precision, so
    downstream consumers know how trustworthy the value is. Returns
    ``(None, None)`` when unparseable.
    """
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), "day"
    if isinstance(value, date):
        return value, "day"
    text = clean_text(str(value))
    if not text:
        return None, None
    if re.fullmatch(r"(19|20)\d{2}", text):
        return date(int(text), 1, 1), "year"
    if re.fullmatch(r"(19|20)\d{2}-(0[1-9]|1[0-2])", text):
        year, month = text.split("-")
        return date(int(year), int(month), 1), "month"
    try:
        parsed = date_parser.parse(text, fuzzy=False, default=datetime(2000, 1, 1))
    except (ValueError, OverflowError, TypeError):
        return None, None
    if parsed.year < 1990 or parsed.date() > datetime.now(timezone.utc).date():
        return None, None
    # Record how precise the source actually was: "March 2023" is month-level.
    digit_groups = re.findall(r"\d+", text)
    precision = "day" if len([g for g in digit_groups if len(g) <= 2]) >= 1 else "month"
    return parsed.date(), precision


def parse_price(value: Any) -> tuple[float | None, str | None, str | None]:
    """Extract ``(amount, currency, raw)`` from a price string; no guessing."""
    raw = clean_text(value)
    if not raw:
        return None, None, None
    lowered = raw.lower()
    if any(token in lowered for token in ("free", "$0", "no cost")) and not _PRICE_RE.search(raw):
        return 0.0, None, raw
    match = _PRICE_RE.search(raw)
    if not match:
        return None, None, raw
    amount_text = match.group("amount").replace(",", ".")
    try:
        amount = float(amount_text)
    except ValueError:
        return None, None, raw
    currency = _CURRENCY_CODES.get(match.group("currency").lower())
    return amount, currency, raw


def infer_primary_task(categories: list[str] | None, tags: list[str] | None = None) -> str | None:
    """Map categories/tags onto a primary task, or ``None`` when unclear."""
    for value in [*(categories or []), *(tags or [])]:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.strip().lower()
        if key in PRIMARY_TASK_MAP:
            return PRIMARY_TASK_MAP[key]
        for token, task in PRIMARY_TASK_MAP.items():
            if token in key and len(token) > 3:
                return task
    return None


class ToolNormalizer:
    """Builds canonical :class:`Tool` records from raw/discovery input."""

    def __init__(self, *, batch_number: int | None = None) -> None:
        self.batch_number = batch_number

    # --------------------------------------------------------------- public
    def from_candidate(self, candidate: CandidateTool) -> Tool | None:
        """Normalize a discovery candidate. Returns ``None`` if unidentifiable."""
        if not candidate.is_usable:
            logger.debug("unusable candidate dropped", extra={"name": candidate.name})
            return None

        source = SourceRef(
            name=candidate.source_name,
            url=candidate.listing_url,
            kind="directory",
            retrieved_at=candidate.discovered_at,
        )
        payload: dict[str, Any] = {
            "name": candidate.name,
            "website": candidate.website,
            "short_description": candidate.tagline,
            "categories": candidate.categories,
            "tags": candidate.categories,
            "discovery_sources": [source],
            "source": source,
            "raw_signals": candidate.raw_signals,
        }
        return self.from_raw(payload)

    def from_raw(self, raw: Mapping[str, Any]) -> Tool | None:
        """Normalize an arbitrary raw mapping into a :class:`Tool`."""
        name = clean_text(raw.get("name"), max_length=200)
        website = normalize_url(raw.get("website") or raw.get("url"))
        product_url = normalize_url(raw.get("product_url") or raw.get("listing_url"))
        company = clean_text(raw.get("company") or raw.get("developer"))

        if not name:
            logger.debug("record dropped: no usable name")
            return None

        try:
            tool_id, identity = make_entity_id(
                EntityType.TOOL.value,
                name=name,
                website=website,
                product_url=product_url,
                company=company,
            )
        except ValueError as exc:
            logger.debug("record dropped: %s", exc, extra={"name": name})
            return None

        categories = [c for c in (clean_text(x, max_length=80) for x in raw.get("categories") or []) if c]
        tags = [t for t in (clean_text(x, max_length=80) for x in raw.get("tags") or []) if t]
        primary_task = clean_text(raw.get("primary_task")) or infer_primary_task(categories, tags)

        launch_date, precision = parse_date_safe(raw.get("launch_date") or raw.get("released_at"))
        sources = self._coerce_sources(raw.get("discovery_sources"))

        tool = Tool(
            id=tool_id,
            entity_type=EntityType.TOOL,
            name=name,
            website=website,
            url=website or product_url,
            company=company,
            logo_url=normalize_url(raw.get("logo_url")) if raw.get("logo_url") else None,
            country=raw.get("country"),
            version=raw.get("version"),
            launch_date=launch_date,
            launch_date_precision=precision,
            status=ToolStatus.coerce(raw.get("status")),
            short_description=raw.get("short_description") or raw.get("tagline") or raw.get("description"),
            detailed_overview=raw.get("detailed_overview"),
            categories=categories,
            tags=tags or categories,
            primary_task=primary_task,
            key_features=drop_vague(raw.get("key_features")),
            use_cases=drop_vague(raw.get("use_cases")),
            ai_capabilities=raw.get("ai_capabilities") or [],
            inputs=drop_vague(raw.get("inputs")),
            outputs=drop_vague(raw.get("outputs")),
            platforms=raw.get("platforms") or [],
            integrations=drop_vague(raw.get("integrations")),
            has_api=_tri_state(raw.get("has_api")),
            api_docs_url=raw.get("api_docs_url"),
            open_source_status=raw.get("open_source_status"),
            repository_url=raw.get("repository_url"),
            signup_requirement=raw.get("signup_requirement"),
            pros=drop_vague(raw.get("pros")),
            cons=drop_vague(raw.get("cons")),
            limitations=drop_vague(raw.get("limitations")),
            aiorbit_summary=clean_text(raw.get("aiorbit_summary")),
            discovery_sources=sources,
            source=sources[0] if sources else None,
            adoption=self._normalize_adoption(raw),
            directory_evidence=dict(raw.get("directory_evidence") or {}),
            dedup=DedupInfo(
                identity_key=identity.value,
                identity_basis=identity.basis,
                canonical_domain=extract_registrable_domain(website),
                canonical_name_key=canonical_name(name),
                aliases=[name],
            ),
            batch_number=self.batch_number,
        )
        self._apply_pricing(tool, raw)
        if identity.basis == "name":
            tool.flag_for_review(
                "identity derived from name only (no official domain resolved yet)"
            )
        return tool

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _coerce_sources(value: Any) -> list[SourceRef]:
        out: list[SourceRef] = []
        for item in value or []:
            if isinstance(item, SourceRef):
                out.append(item)
            elif isinstance(item, Mapping) and item.get("name"):
                try:
                    out.append(SourceRef(**{k: v for k, v in item.items() if k in SourceRef.model_fields}))
                except (TypeError, ValueError):
                    continue
        return out

    @staticmethod
    def _normalize_adoption(raw: Mapping[str, Any]) -> AdoptionSignals:
        """Copy only adoption values that were actually observed."""
        signals = raw.get("raw_signals") or {}
        merged: dict[str, Any] = {}
        for field_name in AdoptionSignals.model_fields:
            if field_name in ("signal_sources", "observed_at"):
                continue
            value = raw.get(field_name, signals.get(field_name))
            if value not in (None, "", [], {}):
                merged[field_name] = value
        try:
            return AdoptionSignals(**merged)
        except (TypeError, ValueError) as exc:
            logger.debug("adoption normalization failed: %s", exc)
            return AdoptionSignals()

    @staticmethod
    def _apply_pricing(tool: Tool, raw: Mapping[str, Any]) -> None:
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), Mapping) else {}
        model = pricing.get("model") or raw.get("pricing_model")
        amount, currency, price_raw = parse_price(
            pricing.get("starting_price_raw") or raw.get("starting_price")
        )
        from src.models.enums import PricingModel

        tool.pricing.model = PricingModel.coerce(model)
        if amount is not None:
            tool.pricing.starting_price_amount = amount
        if currency:
            tool.pricing.starting_price_currency = currency
        if price_raw:
            tool.pricing.starting_price_raw = price_raw
        free_plan = _tri_state(pricing.get("has_free_plan", raw.get("has_free_plan")))
        if free_plan is not None:
            tool.pricing.has_free_plan = free_plan
        free_trial = _tri_state(pricing.get("has_free_trial", raw.get("has_free_trial")))
        if free_trial is not None:
            tool.pricing.has_free_trial = free_trial
        limits = drop_vague(pricing.get("usage_limits") or raw.get("usage_limits"))
        if limits:
            tool.pricing.usage_limits = limits
        pricing_url = normalize_url(pricing.get("pricing_url") or raw.get("pricing_url"))
        if pricing_url:
            tool.pricing.pricing_url = pricing_url


def _tri_state(value: Any) -> bool | None:
    """Parse a boolean, returning ``None`` for unknown (never defaulting to False)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "available", "supported"}:
        return True
    if text in {"false", "no", "n", "0", "unavailable", "unsupported", "none"}:
        return False
    return None


def make_slug(tool: Tool) -> str | None:
    """Stable URL slug for a tool (used by the platform, not for identity)."""
    return slugify(tool.name)
