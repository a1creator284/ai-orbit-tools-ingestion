"""Safe pagination for listing pages.

Directory listings are the only place where a discovery adapter can loop
forever, so every guard lives here rather than in the individual adapters:

* hard page cap (``max_pages``) and hard candidate cap;
* URL de-duplication (a "next" link pointing back is ignored);
* **content fingerprinting** — several directories silently return page 1 for
  an unsupported ``?page=N``; a repeated fingerprint stops the walk instead of
  producing thousands of duplicate candidates;
* an empty page (no items parsed) stops the walk;
* consecutive-failure budget so a flaky/rate-limited host stops the walk
  instead of being hammered;
* bot-challenge detection: a blocked page ends the walk cleanly with a reason.

Nothing here fabricates data: when a page cannot be read, the walk records the
reason and yields fewer candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from src.core.http_client import FetchResult, HttpClient
from src.core.logging_setup import get_logger
from src.core.urls import normalize_url
from src.extraction.html import looks_like_challenge, page_fingerprint

logger = get_logger("discovery.pagination")

#: Reason codes recorded in :class:`PageWalkStats` (stable, machine-readable).
STOP_MAX_PAGES = "max_pages_reached"
STOP_EMPTY_PAGE = "empty_page"
STOP_REPEAT_CONTENT = "repeated_content"
STOP_NO_NEXT = "no_next_page"
STOP_CHALLENGE = "bot_challenge"
STOP_FETCH_FAILURES = "consecutive_fetch_failures"
STOP_LIMIT = "limit_reached"
STOP_SEEN_URL = "url_already_visited"


@dataclass
class PageResult:
    """One successfully fetched listing page."""

    url: str
    page_number: int
    html: str
    final_url: str | None = None
    status: int | None = None
    from_cache: bool = False


@dataclass
class PageWalkStats:
    """Audit trail of one pagination walk."""

    pages_fetched: int = 0
    pages_failed: int = 0
    stop_reason: str | None = None
    visited: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "stop_reason": self.stop_reason,
            "visited": self.visited,
            "errors": self.errors[:10],
        }


def page_url(template: str, page: int, *, first_page_url: str | None = None) -> str | None:
    """Build the URL for ``page``.

    ``template`` must contain ``{page}``. Page 1 uses ``first_page_url`` when
    given (most directories expose page 1 without a page parameter).
    """
    if page <= 1 and first_page_url:
        return normalize_url(first_page_url, force_https=False)
    if "{page}" not in template:
        return normalize_url(template, force_https=False) if page <= 1 else None
    return normalize_url(template.format(page=page), force_https=False)


class Paginator:
    """Walks a paginated listing with all the guards described above."""

    def __init__(
        self,
        client: HttpClient,
        *,
        max_pages: int = 5,
        max_consecutive_failures: int = 2,
        detect_repeats: bool = True,
        use_cache: bool = True,
    ) -> None:
        self.client = client
        self.max_pages = max(1, max_pages)
        self.max_consecutive_failures = max(1, max_consecutive_failures)
        self.detect_repeats = detect_repeats
        self.use_cache = use_cache

    # ------------------------------------------------------------------ walk
    def walk(
        self,
        url_for_page: Callable[[int], str | None],
        *,
        start_page: int = 1,
        stats: PageWalkStats | None = None,
    ) -> Iterator[PageResult]:
        """Yield pages until a guard fires."""
        stats = stats if stats is not None else PageWalkStats()
        seen_urls: set[str] = set()
        seen_prints: set[str] = set()
        failures = 0
        page = start_page

        while page < start_page + self.max_pages:
            url = url_for_page(page)
            if not url:
                stats.stop_reason = stats.stop_reason or STOP_NO_NEXT
                return
            if url in seen_urls:
                stats.stop_reason = STOP_SEEN_URL
                return
            seen_urls.add(url)

            result: FetchResult | None = self.client.try_fetch(url, use_cache=self.use_cache)
            if result is None or not result.ok:
                failures += 1
                stats.pages_failed += 1
                status = result.status if result else None
                stats.errors.append(f"page {page}: fetch failed (status={status})")
                if result is not None and looks_like_challenge(result.text, status):
                    stats.stop_reason = STOP_CHALLENGE
                    logger.warning("listing page blocked by bot challenge", extra={"url": url})
                    return
                if failures >= self.max_consecutive_failures:
                    stats.stop_reason = STOP_FETCH_FAILURES
                    return
                page += 1
                continue

            if looks_like_challenge(result.text, result.status):
                stats.stop_reason = STOP_CHALLENGE
                stats.pages_failed += 1
                stats.errors.append(f"page {page}: bot challenge")
                logger.warning("listing page blocked by bot challenge", extra={"url": url})
                return

            failures = 0
            if self.detect_repeats:
                fingerprint = page_fingerprint(result.text)
                if fingerprint and fingerprint in seen_prints:
                    stats.stop_reason = STOP_REPEAT_CONTENT
                    logger.info(
                        "pagination stopped: page repeats previous content",
                        extra={"url": url, "page": page},
                    )
                    return
                if fingerprint:
                    seen_prints.add(fingerprint)

            stats.pages_fetched += 1
            stats.visited.append(url)
            yield PageResult(
                url=url,
                page_number=page,
                html=result.text,
                final_url=result.final_url,
                status=result.status,
                from_cache=result.from_cache,
            )
            page += 1

        stats.stop_reason = stats.stop_reason or STOP_MAX_PAGES
