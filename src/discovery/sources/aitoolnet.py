"""AIToolNet discovery adapter (tier-2 discovery / cross-check source).

Why this adapter exists
-----------------------
The two tier-1 sources are only partly usable from a server:

* **TAAFT** answers plain HTTP with a Cloudflare challenge (403), which the
  existing adapter already reports as ``blocked_by_bot_challenge`` instead of
  fabricating rows;
* **Creati.ai** is reachable and productive, but a single directory cannot give
  the breadth the batch needs, and one directory's editorial bias would shape
  the whole dataset.

AIToolNet was picked as the next source because — of the approved tier-2 list —
it is the only one that both answers a server request **and** publishes the
**outbound official product URL directly in the listing card**:

.. code-block:: html

    <div class="boxitem ...">
      <a class="aitool" href="/deepseek-harness" title="DeepSeek Harness">…</a>
      <a href="https://www.deepseek.com/harness/?ref=aitoolnet.com" …>
      <div class="info …">DeepSeek Harness is an open-source, plugin-driven…</div>
      <a class="aitool …" href="/code-assistant">#Code Assistant</a>
    </div>

That matters because official-website verification is the stage that admits a
record (guideline §10). A directory that only links to its *own* detail page
(OpenTools, PoweredByAI, EasyWithAI, AI Valley, Futurepedia, Foundr — all
verified 2026-09-16) would require a second fetch per candidate before
verification could even start, so those are deliberately left for a later run
rather than half-implemented here.

Observed behaviour that shapes this adapter (verified 2026-09-16)
-----------------------------------------------------------------
* the homepage and ``/new`` render ``div.boxitem`` cards server-side;
* category pages (``/code-assistant`` etc.) render an **empty** ``#main_list``
  and fill it client-side, so they yield nothing to an HTTP client and are not
  walked;
* ``?page=2`` returns **404**, so numeric pagination is not attempted at all;
* outbound links carry ``?ref=aitoolnet.com``, which
  :func:`src.core.urls.normalize_url` strips.

Only values literally present on the card are captured. The visible like/heart
counter is stored as a *raw, unverified* observation in ``raw_signals`` — never
as a fact, and never as an adoption number.
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

#: Internal detail-page path: a single flat slug, e.g. ``/deepseek-harness``.
DETAIL_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9._-]*)/?$")

#: Flat slugs that are site furniture or category listings, not products.
#: Anything matched here can never become a candidate's listing URL.
NON_TOOL_SLUGS = frozenset(
    {
        "about", "blog", "new", "categories", "category", "submit", "sitemap",
        "privacy", "terms", "contact", "login", "signup", "search", "advertise",
        "es", "fr", "ja", "ko", "de", "pt", "ru", "zh", "it", "nl", "tr", "vi",
        # top-level category listings observed on /categories
        "3d", "ai-content-detection", "art", "audio", "automation", "avatars",
        "business", "chatbots", "code-assistant", "copywriting",
        "customer-support", "data", "design-assistant", "developer-tools",
        "ecommerce", "education-assistant", "email-assistant", "face-swap",
        "fashion", "finance", "fun-tools", "gaming", "grammar-checking",
        "healthcare", "human-resources", "image-editing", "image-generator",
        "large-language-models", "learning", "legal-assistant", "life-assistant",
        "machine-learning", "marketing", "meeting-assistant", "music",
        "paraphrasing", "productivity", "prompt", "research", "sales",
        "seo", "social-media", "speech-to-text", "summarizer", "text-to-speech",
        "translation", "video-editing", "video-generator", "voice", "writing",
    }
)

_COUNTER_RE = re.compile(r"^\d[\d,]*$")


@register_source("aitoolnet")
class AiToolNetSource(HtmlListingSource):
    """Discovery adapter for AIToolNet."""

    card_selectors = ("div.boxitem",)
    default_max_pages = 1

    # ------------------------------------------------------------------ plans
    def listing_plans(self, **_: Any) -> list[ListingPlan]:
        """Server-rendered listing surfaces only.

        ``page_url_template`` is deliberately ``None`` on every plan: ``?page=2``
        is a verified 404 on this site, so attempting a numeric walk would only
        produce failed fetches. Category pages are omitted because they render
        an empty list server-side — including them would add fetches that
        cannot yield a candidate.
        """
        homepage = (self.config.homepage or "https://www.aitoolnet.com/").rstrip("/")
        return [
            ListingPlan(
                label="home",
                first_page_url=f"{homepage}/",
                page_url_template=None,
                max_pages=1,
                context={"view": "homepage"},
            ),
            ListingPlan(
                label="new",
                first_page_url=f"{homepage}/new",
                page_url_template=None,
                max_pages=1,
                context={"view": "newest"},
            ),
        ]

    # ------------------------------------------------------------------ cards
    def parse_card(
        self, card: Tag, page: PageResult, plan: ListingPlan
    ) -> CandidateTool | None:
        site = page.final_url or page.url
        name = self._card_name(card)
        website = self._outbound(card, site)
        listing_url = self._detail_url(card, site)

        # A card with neither an official URL nor a directory detail page
        # cannot be followed up, so it is not a usable candidate.
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
                "card_layout": "grid",
                "outbound_link_present": bool(website),
            },
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _card_name(card: Tag) -> str | None:
        """Product name from the card's own title slots.

        ``a[title]`` is preferred: the site puts the untruncated name there,
        while the visible ``<h3>`` can be clamped by CSS.
        """
        for selector in ("a.aitool[title]", "h3", "div.item-title", "img[alt]"):
            label = best_label(card.select_one(selector), max_length=200)
            if label:
                # Icon alt text is "<name> ico" on this site.
                return re.sub(r"\s+ico$", "", label).strip() or None
        return None

    @staticmethod
    def _card_tagline(card: Tag) -> str | None:
        for selector in ("div.info", "div.tool-info p", "p"):
            node = card.select_one(selector)
            value = node_text(node, max_length=320)
            if value:
                return value
        return None

    @staticmethod
    def _card_categories(card: Tag) -> list[str]:
        """Category tags, rendered as ``#Code Assistant`` links."""
        out: list[str] = []
        for anchor in card.find_all("a", href=True):
            href = str(anchor.get("href") or "").split("?")[0]
            match = DETAIL_PATH_RE.match(href)
            if not match or match.group(1) not in NON_TOOL_SLUGS:
                continue
            label = clean_text(anchor.get_text(" ", strip=True), max_length=80)
            if not label:
                continue
            label = label.lstrip("#").strip()
            if label and label not in out:
                out.append(label)
        return out

    @staticmethod
    def _outbound(card: Tag, site: str | None) -> str | None:
        """The card's outbound official product URL, if it published one."""
        for anchor in card.find_all("a", href=True):
            url = absolutize(anchor.get("href"), site)
            if not url or is_internal(url, site) or is_asset_url(url):
                continue
            return url
        return None

    @staticmethod
    def _detail_url(card: Tag, site: str | None) -> str | None:
        for anchor in card.find_all("a", href=True):
            href = str(anchor.get("href") or "").split("?")[0]
            match = DETAIL_PATH_RE.match(href)
            if not match or match.group(1) in NON_TOOL_SLUGS:
                continue
            url = absolutize(href, site)
            if url:
                return url
        return None

    @staticmethod
    def _card_signals(card: Tag) -> dict[str, Any]:
        """Raw, *unverified* observations visible on the card.

        The heart counter is a directory-local number of unknown provenance, so
        it is recorded verbatim under a namespaced key and is never promoted to
        an adoption field.
        """
        signals: dict[str, Any] = {}
        for node in card.select("div.tool-title span, span"):
            value = node_text(node)
            if value and _COUNTER_RE.match(value):
                signals["directory_like_counter_raw"] = value
                break
        return signals


def category_slugs() -> Sequence[str]:
    """The non-product flat slugs this adapter refuses to treat as tools.

    Exposed for tests so the exclusion list is asserted rather than trusted.
    """
    return tuple(sorted(NON_TOOL_SLUGS))
