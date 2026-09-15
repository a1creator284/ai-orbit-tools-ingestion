"""Creati.ai discovery adapter (tier-1 primary source).

Listing surfaces actually observed on ``https://creati.ai/ai-tools/``:

1. **Grid cards** (``div.cardContainer``) on the main listing — name, tagline,
   category tags and either an outbound product link (tracking-wrapped) or an
   internal Creati detail page.
2. **List cards** (``li.grid-item-container``) on category pages
   (``/ai-tools/categories/<slug>/``) — name, description, an explicit
   "Visit AI" outbound link, plus visible rating/save counters.

Verified behaviour that shapes this adapter:

* ``?page=N`` returns the *same* markup as page 1, so numeric pagination is not
  trusted: breadth comes from category pages and the paginator's
  repeated-content guard stops any attempted page walk.
* Outbound links carry ``?utm_source=creati.ai``; :func:`normalize_url` strips it.

Only values present on the page are captured. Ratings/counters are stored as
*raw, unverified* observations in ``raw_signals`` — never as facts.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from bs4.element import Tag

from src.core.text import clean_text
from src.discovery.base import CandidateTool
from src.discovery.html_source import HtmlListingSource, ListingPlan
from src.discovery.pagination import PageResult
from src.discovery.registry import register_source
from src.extraction.html import (
    absolutize,
    best_label,
    is_asset_url,
    is_internal,
    node_text,
    parse_html,
)

#: Internal detail-page path: ``/ai-tools/<slug>/`` (optionally locale-prefixed).
DETAIL_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?ai-tools/([a-z0-9][a-z0-9._-]*)/?$")
CATEGORY_PATH_RE = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?ai-tools/categories/([a-z0-9][a-z0-9._-]*)/?$"
)
#: Listing sub-views that are not individual tools.
NON_TOOL_SLUGS = {"new", "most-saved", "most-reviewed", "categories", "platform", "submit"}

_INT_RE = re.compile(r"^\d[\d,\.]*$")


@register_source("creati")
class CreatiSource(HtmlListingSource):
    """Discovery adapter for Creati.ai."""

    card_selectors = ("div.cardContainer", "li.grid-item-container")
    default_max_pages = 1

    # ------------------------------------------------------------------ plans
    def listing_plans(
        self,
        *,
        include_categories: bool = True,
        max_categories: int | None = None,
        category_slugs: Sequence[str] | None = None,
        **_: Any,
    ) -> list[ListingPlan]:
        """Main listing plus (optionally) category listings.

        Category slugs are read from the live main listing when not supplied, so
        no category list is hard-coded and new categories are picked up
        automatically.
        """
        base = self.config.listing_url or "https://creati.ai/ai-tools/"
        plans = [
            ListingPlan(
                label="all",
                first_page_url=base,
                # ?page=N is known to echo page 1; the repeated-content guard
                # stops the walk immediately if that ever changes silently.
                page_url_template=f"{base.rstrip('/')}/?page={{page}}",
                max_pages=1,
                context={"view": "all_tools"},
            )
        ]
        if not include_categories:
            return plans

        slugs = list(category_slugs or [])
        if not slugs:
            slugs = self.discover_category_slugs(base)
        cap = max_categories if max_categories is not None else int(
            self.config.extra.get("max_categories", 8) or 8
        )
        for slug in slugs[:cap]:
            plans.append(
                ListingPlan(
                    label=f"category:{slug}",
                    first_page_url=f"https://creati.ai/ai-tools/categories/{slug}/",
                    page_url_template=None,
                    max_pages=1,
                    context={"view": "category", "category_slug": slug},
                )
            )
        return plans

    def discover_category_slugs(self, listing_url: str) -> list[str]:
        """Read category slugs off the main listing page (empty list on failure)."""
        result = self.client.try_fetch(listing_url)
        if result is None or not result.ok:
            self.stats.errors.append("category enumeration failed: listing unreachable")
            return []
        soup = parse_html(result.text)
        if soup is None:
            self.stats.errors.append("category enumeration failed: unparseable listing")
            return []
        slugs: list[str] = []
        for anchor in soup.find_all("a", href=True):
            match = CATEGORY_PATH_RE.match(str(anchor.get("href")).split("?")[0])
            if match and match.group(1) not in slugs:
                slugs.append(match.group(1))
        return slugs

    # ------------------------------------------------------------------ cards
    def parse_card(self, card: Tag, page: PageResult, plan: ListingPlan) -> CandidateTool | None:
        site = page.final_url or page.url
        name = self._card_name(card)
        website = self._outbound(card, site)
        listing_url = self._detail_url(card, site)

        if not website and not listing_url:
            return None

        return self.make_candidate(
            name=name,
            website=website,
            listing_url=listing_url,
            tagline=self._card_tagline(card),
            categories=self._card_categories(card),
            raw_signals=self._card_signals(card),
            page=page,
            plan=plan,
            extra_provenance={
                "card_layout": "grid" if "cardContainer" in (card.get("class") or []) else "list",
                "outbound_link_present": bool(website),
            },
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _card_name(card: Tag) -> str | None:
        for selector in ("h3 a", "h3", "h2 a", "h2", "a[title]", "img[alt]"):
            node = card.select_one(selector)
            label = best_label(node, max_length=200)
            if label:
                return label
        return None

    @staticmethod
    def _card_tagline(card: Tag) -> str | None:
        for selector in ("div.description", ".desc-wrap-anywhere", "p"):
            node = card.select_one(selector)
            if node is None:
                continue
            # The full string lives in @title; visible text may be clamped.
            value = clean_text(node.get("title"), max_length=320) or node_text(
                node, max_length=320
            )
            if value:
                return value
        return None

    @staticmethod
    def _card_categories(card: Tag) -> list[str]:
        out: list[str] = []
        for anchor in card.select("a.category-tag, a[href*='/ai-tools/categories/']"):
            label = best_label(anchor, max_length=80)
            if label and label not in out:
                out.append(label)
        return out

    @staticmethod
    def _outbound(card: Tag, site: str | None) -> str | None:
        """Outbound product link; prefers the explicit "Visit AI" button."""
        preferred = card.select("a[rel*='sponsored'], a.aLink, a[href^='http']")
        for anchor in preferred:
            url = absolutize(anchor.get("href"), site)
            if not url or is_internal(url, site) or is_asset_url(url):
                continue
            return url
        for anchor in card.find_all("a", href=True):
            url = absolutize(anchor.get("href"), site)
            if url and not is_internal(url, site) and not is_asset_url(url):
                return url
        return None

    @staticmethod
    def _detail_url(card: Tag, site: str | None) -> str | None:
        for anchor in card.find_all("a", href=True):
            href = str(anchor.get("href"))
            match = DETAIL_PATH_RE.match(href.split("?")[0])
            if not match or match.group(1) in NON_TOOL_SLUGS:
                continue
            url = absolutize(href, site)
            if url:
                return url
        return None

    @staticmethod
    def _card_signals(card: Tag) -> dict[str, Any]:
        """Raw, *unverified* observations visible on the card."""
        signals: dict[str, Any] = {}
        rating = card.select_one("div.rate-container")
        if rating is not None:
            filled = len(rating.select("span.filled"))
            total = len(rating.select("span.star")) or None
            if total:
                signals["directory_rating_stars_filled"] = filled
                signals["directory_rating_stars_total"] = total
        counters = [
            value
            for value in (
                node_text(node) for node in card.select("span.text-medium, div.text-medium")
            )
            if value and _INT_RE.match(value)
        ]
        if counters:
            signals["directory_counters_raw"] = counters
        badge = card.select_one("a[rel*='sponsored']")
        if badge is not None:
            signals["directory_sponsored_placement"] = True
        return signals
