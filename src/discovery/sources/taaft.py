"""There's An AI For That (TAAFT) discovery adapter (tier-1 primary source).

Access reality (verified 2026-09-15): ``theresanaiforthat.com`` sits behind a
Cloudflare browser challenge. Plain HTTP clients — including this pipeline's
polite client — receive ``403`` with a "Just a moment..." interstitial, and
``/sitemap.xml`` is equally blocked. The adapter therefore:

* detects the challenge and stops the walk with ``stop_reason="bot_challenge"``
  instead of emitting anything (guideline §9: never invent data, never treat a
  blocked page as an empty catalogue);
* parses real listing markup as soon as a page *is* retrievable (e.g. via a
  future authorised API key, partner feed, or an operator-supplied HTML cache),
  which is what the fixture tests exercise.

Parsing is deliberately **selector-driven with heuristic fallback** so a markup
change on the directory's side degrades to fewer fields rather than to wrong
fields. Selectors can be overridden per deployment in ``config/sources.yaml``
(``extra.selectors``) without code changes.

Only what is literally on the page is captured. TAAFT's save/upvote counters
are recorded as raw, unverified observations in ``raw_signals``.
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
)

DEFAULT_CARD_SELECTORS = (
    "div.ai",
    "li.li",
    "div.tool_card",
    "article.tool",
)
DEFAULT_NAME_SELECTORS = ("a.taaft_title", ".taaft_title", "h2 a", "h3 a", "h2", "h3", "a[title]")
DEFAULT_TAGLINE_SELECTORS = (".description", ".ai_description", "p.description", "p")
DEFAULT_CATEGORY_SELECTORS = (".task_label", ".tags a", "a.task", ".category")
DEFAULT_OUTBOUND_SELECTORS = ("a.ai_link", "a.visit", "a[rel*='nofollow'][href^='http']")

#: Detail-page path on TAAFT: ``/ai/<slug>/``.
DETAIL_PATH_RE = re.compile(r"^/ai/([a-z0-9][a-z0-9._-]*)/?$", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d[\d,\.]*)\s*(saves?|upvotes?|users?|likes?)", re.IGNORECASE)


@register_source("taaft")
class TaaftSource(HtmlListingSource):
    """Discovery adapter for theresanaiforthat.com."""

    card_selectors = DEFAULT_CARD_SELECTORS
    default_max_pages = 3

    def __init__(self, config: Any, client: Any = None) -> None:  # type: ignore[override]
        super().__init__(config, client)
        overrides = (self.config.extra.get("selectors") or {}) if self.config.extra else {}
        self.card_selectors = tuple(overrides.get("card") or DEFAULT_CARD_SELECTORS)
        self._name_selectors = tuple(overrides.get("name") or DEFAULT_NAME_SELECTORS)
        self._tagline_selectors = tuple(overrides.get("tagline") or DEFAULT_TAGLINE_SELECTORS)
        self._category_selectors = tuple(overrides.get("category") or DEFAULT_CATEGORY_SELECTORS)
        self._outbound_selectors = tuple(overrides.get("outbound") or DEFAULT_OUTBOUND_SELECTORS)

    # ------------------------------------------------------------------ plans
    def listing_plans(
        self,
        *,
        max_pages: int | None = None,
        category_slugs: Sequence[str] | None = None,
        **_: Any,
    ) -> list[ListingPlan]:
        base = (self.config.listing_url or "https://theresanaiforthat.com/tools/").rstrip("/")
        pages = max_pages or int(self.config.extra.get("max_pages", self.default_max_pages) or 1)
        plans = [
            ListingPlan(
                label="all",
                first_page_url=f"{base}/",
                page_url_template=f"{base}/?page={{page}}",
                max_pages=pages,
                context={"view": "all_tools"},
            )
        ]
        for slug in category_slugs or ():
            plans.append(
                ListingPlan(
                    label=f"category:{slug}",
                    first_page_url=f"https://theresanaiforthat.com/s/{slug}/",
                    page_url_template=f"https://theresanaiforthat.com/s/{slug}/?page={{page}}",
                    max_pages=pages,
                    context={"view": "category", "category_slug": slug},
                )
            )
        return plans

    # ------------------------------------------------------------------ cards
    def parse_card(self, card: Tag, page: PageResult, plan: ListingPlan) -> CandidateTool | None:
        site = page.final_url or page.url
        name = self._pick_label(card, self._name_selectors, 200)
        website = self._outbound(card, site)
        listing_url = self._detail_url(card, site)
        if not website and not listing_url:
            return None
        return self.make_candidate(
            name=name,
            website=website,
            listing_url=listing_url,
            tagline=self._pick_label(card, self._tagline_selectors, 320),
            categories=self._categories(card),
            raw_signals=self._signals(card),
            page=page,
            plan=plan,
            extra_provenance={"outbound_link_present": bool(website)},
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _pick_label(card: Tag, selectors: Sequence[str], max_length: int) -> str | None:
        for selector in selectors:
            try:
                node = card.select_one(selector)
            except Exception:  # noqa: BLE001 - tolerate bad configured selectors
                continue
            label = best_label(node, max_length=max_length)
            if label:
                return label
        return None

    def _categories(self, card: Tag) -> list[str]:
        out: list[str] = []
        for selector in self._category_selectors:
            try:
                nodes = card.select(selector)
            except Exception:  # noqa: BLE001
                continue
            for node in nodes:
                label = best_label(node, max_length=80)
                if label and label not in out:
                    out.append(label)
        return out

    def _outbound(self, card: Tag, site: str | None) -> str | None:
        for selector in (*self._outbound_selectors, "a[href^='http']"):
            try:
                nodes = card.select(selector)
            except Exception:  # noqa: BLE001
                continue
            for anchor in nodes:
                url = absolutize(anchor.get("href"), site)
                if not url or is_internal(url, site) or is_asset_url(url):
                    continue
                return url
        return None

    @staticmethod
    def _detail_url(card: Tag, site: str | None) -> str | None:
        for anchor in card.find_all("a", href=True):
            href = str(anchor.get("href")).split("?")[0]
            if DETAIL_PATH_RE.match(href):
                url = absolutize(href, site)
                if url:
                    return url
        return None

    @staticmethod
    def _signals(card: Tag) -> dict[str, Any]:
        """Raw counters observed on the card (never normalized into facts)."""
        signals: dict[str, Any] = {}
        text = node_text(card) or ""
        observed: dict[str, str] = {}
        for value, label in _COUNT_RE.findall(text):
            observed.setdefault(label.lower().rstrip("s"), value)
        if observed:
            signals["directory_counters_raw"] = observed
        for attr in ("data-saves", "data-upvotes", "data-votes"):
            raw = clean_text(card.get(attr))
            if raw:
                signals[f"directory_{attr.replace('data-', '')}_raw"] = raw
        return signals
