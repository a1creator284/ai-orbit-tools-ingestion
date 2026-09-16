"""Shared base for HTML-listing discovery adapters.

Every directory works the same way: one or more listing/category entry points,
each paginated, each page containing repeated "cards". The only source-specific
part is *how a card is parsed*, so that is the only thing a subclass implements:

* :meth:`HtmlListingSource.listing_plans` — where to look;
* :meth:`HtmlListingSource.parse_card` — how to read one card.

Everything else (pagination guards, per-run de-duplication, provenance
stamping, error containment, statistics) is provided here.

Hard rules (Tools guideline §2/§10):

* a directory produces *candidates only* — presence in a directory is never
  treated as evidence that a tool is active, popular or high quality;
* only values literally present on the page are stored; anything absent stays
  ``None``. No inference, no defaults, no fabricated dates/pricing/user counts.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from bs4.element import Tag

from src.core.http_client import HttpClient
from src.core.urls import normalize_url
from src.discovery.base import CandidateTool, DiscoverySource, SourceConfig
from src.discovery.identity import candidate_key
from src.discovery.pagination import (
    STOP_EMPTY_PAGE,
    PageResult,
    Paginator,
    PageWalkStats,
)
from src.extraction.html import (
    anchor_targets,
    is_asset_url,
    is_internal,
    parse_html,
    select_all,
)

#: Discovery method recorded in provenance.
DISCOVERY_METHOD_HTML_LISTING = "html_listing"


@dataclass
class ListingPlan:
    """One paginated entry point into a directory."""

    #: Human label for logs/provenance, e.g. ``"all"`` or ``"category:ai-video"``.
    label: str
    #: URL of the first page.
    first_page_url: str
    #: URL template containing ``{page}``; ``None`` = single page only.
    page_url_template: str | None = None
    max_pages: int = 1
    #: Arbitrary context copied into every candidate's provenance.
    context: dict[str, Any] = field(default_factory=dict)

    def url_for_page(self, page: int) -> str | None:
        if page <= 1:
            return normalize_url(self.first_page_url, force_https=False)
        if not self.page_url_template or "{page}" not in self.page_url_template:
            return None
        return normalize_url(self.page_url_template.format(page=page), force_https=False)


@dataclass
class DiscoveryStats:
    """Per-source audit trail for the run report."""

    source_key: str
    plans_attempted: int = 0
    plans_blocked: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    cards_seen: int = 0
    cards_unparsed: int = 0
    candidates_emitted: int = 0
    duplicates_skipped: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def note_stop(self, reason: str | None) -> None:
        if reason:
            self.stop_reasons[reason] = self.stop_reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_key,
            "plans_attempted": self.plans_attempted,
            "plans_blocked": self.plans_blocked,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "cards_seen": self.cards_seen,
            "cards_unparsed": self.cards_unparsed,
            "candidates_emitted": self.candidates_emitted,
            "duplicates_skipped": self.duplicates_skipped,
            "stop_reasons": self.stop_reasons,
            "errors": self.errors[:10],
        }


class HtmlListingSource(DiscoverySource):
    """Base class for directory adapters backed by paginated HTML listings."""

    #: CSS selectors identifying a card, tried in order (first match wins).
    card_selectors: Sequence[str] = ()
    #: Default page cap; can be raised per-plan or via ``max_pages`` kwarg.
    default_max_pages: int = 3

    def __init__(self, config: SourceConfig, client: HttpClient | None = None) -> None:
        super().__init__(config, client)
        self.stats = DiscoveryStats(source_key=config.key)

    # ------------------------------------------------------------- subclass API
    @abc.abstractmethod
    def listing_plans(self, **kwargs: Any) -> Sequence[ListingPlan]:
        """Entry points to walk for this source."""

    @abc.abstractmethod
    def parse_card(self, card: Tag, page: PageResult, plan: ListingPlan) -> CandidateTool | None:
        """Parse one card into a candidate, or ``None`` when unusable."""

    # ----------------------------------------------------------------- helpers
    def find_cards(self, page: PageResult) -> list[Tag]:
        """Locate card nodes on ``page`` using :attr:`card_selectors`."""
        soup = parse_html(page.html)
        if soup is None:
            return []
        return select_all(soup, self.card_selectors)

    def outbound_website(self, card: Tag, page: PageResult) -> str | None:
        """First external, non-asset link in ``card`` — the candidate's website.

        Directories wrap outbound links with tracking parameters; those are
        stripped by :func:`src.core.urls.normalize_url`.
        """
        site = page.final_url or page.url
        for _anchor, url in anchor_targets(card, site):
            if is_internal(url, site) or is_asset_url(url):
                continue
            return url
        return None

    def internal_detail_url(self, card: Tag, page: PageResult) -> str | None:
        """First same-site link in ``card`` — the directory's own detail page."""
        site = page.final_url or page.url
        for _anchor, url in anchor_targets(card, site):
            if is_internal(url, site) and not is_asset_url(url):
                return url
        return None

    def make_candidate(
        self,
        *,
        name: str | None,
        website: str | None,
        listing_url: str | None,
        tagline: str | None = None,
        categories: Sequence[str] | None = None,
        raw_signals: dict[str, Any] | None = None,
        page: PageResult,
        plan: ListingPlan,
        extra_provenance: dict[str, Any] | None = None,
    ) -> CandidateTool | None:
        """Build a provenance-stamped candidate, or ``None`` when unidentifiable."""
        if not name:
            return None
        provenance: dict[str, Any] = {
            "discovery_method": DISCOVERY_METHOD_HTML_LISTING,
            "source_key": self.config.key,
            "source_name": self.config.name,
            "source_tier": self.config.tier,
            "source_trust": self.config.trust,
            "source_roles": list(self.config.roles),
            "plan": plan.label,
            "plan_context": dict(plan.context),
            "page_number": page.page_number,
            "page_url": page.url,
            "page_final_url": page.final_url,
            "page_status": page.status,
            "from_cache": page.from_cache,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            # A directory listing never proves a tool is live or good.
            "verified": False,
            "evidence_level": "directory_listing",
        }
        if extra_provenance:
            provenance.update(extra_provenance)

        candidate = CandidateTool(
            name=name,
            source_key=self.config.key,
            source_name=self.config.name,
            listing_url=listing_url,
            website=website,
            tagline=tagline,
            categories=list(categories or []),
            raw_signals=dict(raw_signals or {}),
            raw_payload=provenance,
        )
        return candidate if candidate.is_usable else None

    @staticmethod
    def dedup_key(candidate: CandidateTool) -> str:
        """Within-run identity — shared with the cross-source merger.

        See :mod:`src.discovery.identity` for why a bare registrable domain is
        *not* used (it collapsed 13 distinct products on one live listing).
        """
        return candidate_key(
            website=candidate.website,
            listing_url=candidate.listing_url,
            name=candidate.name,
        )

    # -------------------------------------------------------------- discovery
    def discover(
        self,
        *,
        limit: int | None = None,
        max_pages: int | None = None,
        use_cache: bool = True,
        **kwargs: Any,
    ) -> Iterator[CandidateTool]:
        """Yield de-duplicated candidates from every listing plan."""
        seen: set[str] = set()
        emitted = 0

        try:
            plans = list(self.listing_plans(**kwargs))
        except Exception as exc:  # noqa: BLE001 - a broken plan must not kill the run
            self.stats.errors.append(f"listing_plans failed: {exc}")
            self.logger.warning("listing plan construction failed", extra={"error": str(exc)})
            return

        for plan in plans:
            if limit is not None and emitted >= limit:
                self.stats.note_stop("limit_reached")
                break
            self.stats.plans_attempted += 1
            pages = max_pages if max_pages is not None else max(plan.max_pages, 1)
            walk_stats = PageWalkStats()
            paginator = Paginator(
                self.client,
                max_pages=pages,
                use_cache=use_cache,
            )

            try:
                for page in paginator.walk(plan.url_for_page, stats=walk_stats):
                    cards = self.find_cards(page)
                    if not cards:
                        # End of the catalogue. Several directories answer an
                        # out-of-range ``?page=N`` with HTTP 200 and an empty
                        # results list rather than a 404 (verified on dang.ai:
                        # page 500 is 200 with zero cards), so status alone
                        # cannot end the walk. Only the adapter knows what a
                        # card looks like, which is why this guard lives here
                        # rather than in the paginator.
                        self.logger.info(
                            "no cards parsed on listing page; ending walk",
                            extra={"source": self.key, "url": page.url},
                        )
                        walk_stats.stop_reason = STOP_EMPTY_PAGE
                        break
                    for card in cards:
                        self.stats.cards_seen += 1
                        try:
                            candidate = self.parse_card(card, page, plan)
                        except Exception as exc:  # noqa: BLE001 - per-card containment
                            self.stats.cards_unparsed += 1
                            self.stats.errors.append(f"card parse failed: {exc}")
                            continue
                        if candidate is None:
                            self.stats.cards_unparsed += 1
                            continue
                        key = self.dedup_key(candidate)
                        if key in seen:
                            self.stats.duplicates_skipped += 1
                            continue
                        seen.add(key)
                        emitted += 1
                        self.stats.candidates_emitted += 1
                        yield candidate
                        if limit is not None and emitted >= limit:
                            break
                    if limit is not None and emitted >= limit:
                        break
            except Exception as exc:  # noqa: BLE001 - one bad plan, not the whole source
                self.stats.errors.append(f"plan {plan.label} failed: {exc}")
                self.logger.warning(
                    "listing walk failed", extra={"plan": plan.label, "error": str(exc)}
                )
            finally:
                self.stats.pages_fetched += walk_stats.pages_fetched
                self.stats.pages_failed += walk_stats.pages_failed
                self.stats.errors.extend(walk_stats.errors[:3])
                self.stats.note_stop(walk_stats.stop_reason)
                if walk_stats.stop_reason == "bot_challenge":
                    self.stats.plans_blocked += 1

    def stats_dict(self) -> dict[str, Any]:
        return self.stats.to_dict()
