"""Dang.ai discovery adapter (tier-2 discovery / cross-check source).

Why this adapter exists
-----------------------
Batch 1 needs breadth that the already-enabled sources cannot supply on their
own:

* **TAAFT** (tier 1) answers a server request with a Cloudflare challenge, so
  the existing adapter correctly reports ``blocked_by_bot_challenge`` and
  emits nothing;
* **Creati.ai** (tier 1) is productive but its breadth comes from walking
  ~160 category pages, each of which is a bounded, non-paginated list;
* **AIToolNet** (tier 2) publishes outbound URLs but exposes only two
  server-rendered surfaces (~48 candidates), because ``?page=2`` is a 404.

Dang.ai was picked next, from the approved tier-2 list, because it is the only
accessible directory verified to offer **deep, genuinely-different numeric
pagination**: ``/?page=N`` returns a fresh set of tool cards per page well past
page 200 (re-verified 2026-09-16), against a self-declared catalogue of 5000+
tools. That makes it the single highest-yield approved surface available to an
HTTP client, and its pages are server-rendered, so no browser is needed.

Pagination yield, measured on the live site rather than assumed
---------------------------------------------------------------
Pages 1, 2, 5, 20, 40, 60 and 200 each render **24-29 ``<article>`` cards with
zero slug overlap between them**, so every page is genuine new inventory. Page
500 is past the end: it returns HTTP 200 with **zero** cards, which the shared
paginator already treats as ``empty_page`` and stops on — no special-casing is
needed here, and an out-of-range page can never synthesise candidates.

Note the listing page also carries ``/tool/`` links *outside* the cards (the
sidebar "related"/nav blocks). Those are navigation, not listing inventory, so
only ``<article>`` descendants are parsed; scraping every ``/tool/`` anchor
would inflate counts with the same handful of promoted products per page.

Observed behaviour that shapes this adapter (verified 2026-09-16)
------------------------------------------------------------------
* each listing card is an ``<article>`` containing ``a[href^="/tool/"]`` (the
  product name + Dang's own detail page), a short description paragraph, and
  ``a[href^="/category/"]`` topic links;
* the listing card does **not** publish the outbound official URL — that lives
  on the detail page's ``SoftwareApplication`` JSON-LD (``url``/``sameAs``).
  Discovery therefore records the Dang detail URL as the candidate pointer and
  leaves ``website`` ``None`` rather than guessing; official-website
  resolution is a later stage's job (guideline §10), and a candidate with a
  detail URL is still fully usable;
* ``robots.txt`` allows ``/`` and the listing paths (``/login``, ``/dashboard``,
  ``/account``, ``/submit``, ``/api`` are disallowed and are never requested);
* ``/categories`` enumerates **103** ``/category/<slug>`` pages (verified
  2026-09-16), and those paginate the same way, giving a second breadth axis
  without re-walking the global list.

As with every directory adapter, presence here is **not** evidence of quality,
activity or adoption. Only values literally on the page are captured; the
upvote counter is stored as a raw, unverified observation, and the "Pro
featured on Dang" badge is recorded as *paid placement* so a later stage can
discount it rather than read it as organic prominence.
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

#: Detail-page path: ``/tool/<slug>``.
DETAIL_PATH_RE = re.compile(r"^/tool/([a-z0-9][a-z0-9._-]*)/?$")
#: Category-listing path: ``/category/<slug>``.
CATEGORY_PATH_RE = re.compile(r"^/category/([a-z0-9][a-z0-9._-]*)/?$")

#: Paths robots.txt disallows, or that are site furniture rather than listings.
EXCLUDED_PATHS = frozenset(
    {"login", "dashboard", "account", "submit", "api", "pricing", "about", "deals"}
)

#: ``aria-label="Upvote 8"`` / ``title="Upvote 8"`` on the card's vote button.
_UPVOTE_RE = re.compile(r"upvote\s+([\d,]+)", re.IGNORECASE)
#: ``aria-label="Pro featured on Dang"`` on the card's badge.
_FEATURED_RE = re.compile(r"featured", re.IGNORECASE)


@register_source("dang")
class DangSource(HtmlListingSource):
    """Discovery adapter for Dang.ai."""

    card_selectors = ("article",)
    default_max_pages = 10

    # ------------------------------------------------------------------ plans
    def listing_plans(
        self,
        *,
        include_categories: bool = True,
        max_categories: int | None = None,
        max_pages: int | None = None,
        category_slugs: Sequence[str] | None = None,
        **_: Any,
    ) -> list[ListingPlan]:
        """The paginated global listing, plus (optionally) category listings.

        Unlike the other enabled directories, the global list here paginates
        deeply and returns different products on each page, so it is the
        primary plan rather than a single capped page.
        """
        homepage = (self.config.homepage or "https://dang.ai/").rstrip("/")
        pages = max_pages if max_pages is not None else int(
            self.config.extra.get("max_pages", self.default_max_pages)
            or self.default_max_pages
        )
        plans = [
            ListingPlan(
                label="all",
                first_page_url=f"{homepage}/",
                page_url_template=f"{homepage}/?page={{page}}",
                max_pages=pages,
                context={"view": "all_tools"},
            )
        ]
        if not include_categories:
            return plans

        cap = max_categories if max_categories is not None else int(
            self.config.extra.get("max_categories", 0) or 0
        )
        if cap <= 0:
            # Nothing would be walked, so do not spend a request enumerating.
            return plans

        slugs = list(category_slugs or [])
        if not slugs:
            slugs = self.discover_category_slugs(f"{homepage}/categories")
        for slug in slugs[:cap]:
            plans.append(
                ListingPlan(
                    label=f"category:{slug}",
                    first_page_url=f"{homepage}/category/{slug}",
                    page_url_template=f"{homepage}/category/{slug}?page={{page}}",
                    max_pages=max(1, min(pages, 5)),
                    context={"view": "category", "category_slug": slug},
                )
            )
        return plans

    def discover_category_slugs(self, categories_url: str) -> list[str]:
        """Read category slugs off the live site (empty list on failure)."""
        result = self.client.try_fetch(categories_url)
        if result is None or not result.ok:
            self.stats.errors.append("category enumeration failed: unreachable")
            return []
        soup = parse_html(result.text)
        if soup is None:
            self.stats.errors.append("category enumeration failed: unparseable")
            return []
        slugs: list[str] = []
        for anchor in soup.find_all("a", href=True):
            match = CATEGORY_PATH_RE.match(str(anchor.get("href")).split("?")[0])
            if match and match.group(1) not in slugs:
                slugs.append(match.group(1))
        return slugs

    # ------------------------------------------------------------------ cards
    def parse_card(
        self, card: Tag, page: PageResult, plan: ListingPlan
    ) -> CandidateTool | None:
        site = page.final_url or page.url
        listing_url = self._detail_url(card, site)
        # Every real product card on this site links to its own detail page;
        # an <article> without one is a blog/teaser block, not a tool.
        if not listing_url:
            return None

        return self.make_candidate(
            name=self._card_name(card),
            website=self._outbound(card, site),
            listing_url=listing_url,
            tagline=self._card_tagline(card),
            categories=self._card_categories(card),
            raw_signals=self._card_signals(card),
            page=page,
            plan=plan,
            extra_provenance={
                "card_layout": "list",
                # Recorded explicitly: this directory withholds the official
                # URL from its listing, so a later stage must resolve it.
                "outbound_link_present": False,
                "official_url_source": "detail_page_required",
            },
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _card_name(card: Tag) -> str | None:
        """Product name — the text of the card's ``/tool/`` anchor."""
        for anchor in card.find_all("a", href=True):
            if not DETAIL_PATH_RE.match(str(anchor.get("href") or "").split("?")[0]):
                continue
            label = best_label(anchor, max_length=200)
            if label:
                return label
        return best_label(card.select_one("h2, h3"), max_length=200)

    @staticmethod
    def _card_tagline(card: Tag) -> str | None:
        node = card.select_one("p")
        return node_text(node, max_length=320)

    @staticmethod
    def _card_categories(card: Tag) -> list[str]:
        out: list[str] = []
        for anchor in card.find_all("a", href=True):
            if not CATEGORY_PATH_RE.match(str(anchor.get("href") or "").split("?")[0]):
                continue
            label = clean_text(anchor.get_text(" ", strip=True), max_length=80)
            if label and label not in out:
                out.append(label)
        return out

    @staticmethod
    def _detail_url(card: Tag, site: str | None) -> str | None:
        """Dang's own ``/tool/<slug>`` detail page for this card."""
        for anchor in card.find_all("a", href=True):
            href = str(anchor.get("href") or "").split("?")[0]
            match = DETAIL_PATH_RE.match(href)
            if not match or match.group(1) in EXCLUDED_PATHS:
                continue
            url = absolutize(href, site)
            if url:
                return url
        return None

    @staticmethod
    def _outbound(card: Tag, site: str | None) -> str | None:
        """Outbound official URL *if the card happens to publish one*.

        Normally ``None`` on this site. Never guessed from the slug: an
        invented URL would poison official-website verification.
        """
        for anchor in card.find_all("a", href=True):
            url = absolutize(anchor.get("href"), site)
            if not url or is_internal(url, site) or is_asset_url(url):
                continue
            return url
        return None

    @staticmethod
    def _card_signals(card: Tag) -> dict[str, Any]:
        """Raw, *unverified* observations visible on the card.

        Only *labelled* values are captured. There is deliberately **no**
        "first number on the card" fallback: verified against live markup
        (2026-09-16) the cards carry other bare integers — e.g. the
        ``Adult content`` badge renders the literal text ``18`` — so such a
        fallback silently files a content rating as an upvote count. A missing
        counter must stay missing (guideline: never fabricate a field).
        """
        signals: dict[str, Any] = {}

        for button in card.find_all("button"):
            for attr in ("aria-label", "title"):
                match = _UPVOTE_RE.search(str(button.get(attr) or ""))
                if match:
                    signals["directory_upvotes_raw"] = match.group(1)
                    break
            if "directory_upvotes_raw" in signals:
                break

        # Paid placement, not a quality signal — recorded so downstream stages
        # can *discount* it rather than mistake it for organic prominence.
        for node in card.find_all(attrs={"title": True}):
            if _FEATURED_RE.search(str(node.get("title") or "")):
                signals["directory_featured_placement"] = True
                break
        if "directory_featured_placement" not in signals:
            for node in card.find_all(attrs={"aria-label": True}):
                if _FEATURED_RE.search(str(node.get("aria-label") or "")):
                    signals["directory_featured_placement"] = True
                    break
        return signals


def excluded_paths() -> Sequence[str]:
    """Paths this adapter never requests (robots.txt + site furniture).

    Exposed so the exclusion list is asserted by tests rather than trusted.
    """
    return tuple(sorted(EXCLUDED_PATHS))
