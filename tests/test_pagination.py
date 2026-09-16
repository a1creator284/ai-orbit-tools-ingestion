"""Pagination safety tests: caps, repeats, empties, failures, challenges."""

from __future__ import annotations

from src.discovery.pagination import (
    STOP_CHALLENGE,
    STOP_FETCH_FAILURES,
    STOP_MAX_PAGES,
    STOP_REPEAT_CONTENT,
    STOP_SEEN_URL,
    Paginator,
    PageWalkStats,
    page_url,
)
from src.discovery.base import SourceConfig
from tests.conftest import FakeHttpClient, load_fixture

PAGE1 = load_fixture("taaft_listing_page1.html")
PAGE2 = load_fixture("taaft_listing_page2.html")
CHALLENGE = load_fixture("cloudflare_challenge.html")


def url_for(template: str):
    return lambda page: page_url(template, page, first_page_url=template.split("?")[0])


class TestPageUrl:
    def test_first_page_uses_plain_url(self) -> None:
        assert page_url(
            "https://x.example/tools/?page={page}", 1, first_page_url="https://x.example/tools/"
        ) == "https://x.example/tools"

    def test_numbered_page(self) -> None:
        assert page_url("https://x.example/tools/?page={page}", 3) == (
            "https://x.example/tools?page=3"
        )

    def test_template_without_placeholder_stops_after_page_one(self) -> None:
        assert page_url("https://x.example/tools/", 2) is None


class TestWalk:
    def test_walks_multiple_distinct_pages(self) -> None:
        client = FakeHttpClient(
            responses={
                "https://x.example/tools": PAGE1,
                "https://x.example/tools?page=2": PAGE2,
            }
        )
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=2).walk(url_for("https://x.example/tools/?page={page}"), stats=stats)
        )
        assert [p.page_number for p in pages] == [1, 2]
        assert stats.pages_fetched == 2
        assert stats.stop_reason == STOP_MAX_PAGES

    def test_repeated_content_stops_walk(self) -> None:
        """A site echoing page 1 for ?page=N must not yield duplicate pages."""
        client = FakeHttpClient(
            responses={
                "https://x.example/tools": PAGE1,
                "https://x.example/tools?page=2": PAGE1,
                "https://x.example/tools?page=3": PAGE1,
            }
        )
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=5).walk(url_for("https://x.example/tools/?page={page}"), stats=stats)
        )
        assert len(pages) == 1
        assert stats.stop_reason == STOP_REPEAT_CONTENT

    def test_bot_challenge_stops_walk_with_reason(self) -> None:
        client = FakeHttpClient(responses={"https://x.example/tools": (403, CHALLENGE)})
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=3).walk(url_for("https://x.example/tools/?page={page}"), stats=stats)
        )
        assert pages == []
        assert stats.stop_reason == STOP_CHALLENGE
        assert stats.pages_failed == 1

    def test_consecutive_failures_stop_walk(self) -> None:
        client = FakeHttpClient(responses={})  # every fetch returns None
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=10, max_consecutive_failures=2).walk(
                url_for("https://x.example/tools/?page={page}"), stats=stats
            )
        )
        assert pages == []
        assert stats.stop_reason == STOP_FETCH_FAILURES
        assert stats.pages_failed == 2

    def test_transient_failure_is_skipped_not_fatal(self) -> None:
        client = FakeHttpClient(
            responses={
                "https://x.example/tools": PAGE1,
                # page 2 missing -> single failure
                "https://x.example/tools?page=3": PAGE2,
            }
        )
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=3, max_consecutive_failures=2).walk(
                url_for("https://x.example/tools/?page={page}"), stats=stats
            )
        )
        assert [p.page_number for p in pages] == [1, 3]
        assert stats.pages_failed == 1

    def test_hard_page_cap_respected(self) -> None:
        client = FakeHttpClient(
            responses={f"https://x.example/tools?page={i}": PAGE1 for i in range(2, 60)}
        )
        client.responses["https://x.example/tools"] = PAGE2
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=2, detect_repeats=False).walk(
                url_for("https://x.example/tools/?page={page}"), stats=stats
            )
        )
        assert len(pages) == 2
        assert stats.stop_reason == STOP_MAX_PAGES

    def test_repeated_url_stops_walk(self) -> None:
        client = FakeHttpClient(responses={"https://x.example/tools": PAGE1})
        stats = PageWalkStats()
        pages = list(
            Paginator(client, max_pages=3).walk(lambda _page: "https://x.example/tools", stats=stats)
        )
        assert len(pages) == 1
        assert stats.stop_reason == STOP_SEEN_URL

    def test_stats_serialisable(self) -> None:
        stats = PageWalkStats(pages_fetched=2, stop_reason=STOP_MAX_PAGES)
        assert stats.to_dict()["stop_reason"] == STOP_MAX_PAGES


class TestEmptyPageGuard:
    """``STOP_EMPTY_PAGE`` was a documented-but-dead constant.

    ``src/discovery/pagination.py`` advertised "an empty page (no items
    parsed) stops the walk", but nothing ever set the reason, so a source with
    deep numeric pagination kept requesting pages past the end of its
    catalogue until the consecutive-failure budget happened to trip. The guard
    belongs to :class:`HtmlListingSource`, because only an adapter knows what
    an "item" looks like.
    """

    def test_walk_ends_on_the_first_cardless_page(self) -> None:
        from src.discovery.base import CandidateTool
        from src.discovery.html_source import HtmlListingSource, ListingPlan
        from src.discovery.pagination import STOP_EMPTY_PAGE
        from tests.conftest import FakeHttpClient

        card_page = "<html><body><article><a href='/tool/x'>X</a></article></body></html>"
        # HTTP 200 with zero cards — exactly how dang.ai answers page 500.
        empty_page = "<html><body><main><p>No tools found.</p></main></body></html>"

        class _Source(HtmlListingSource):
            card_selectors = ("article",)

            def listing_plans(self, **_kwargs):
                return [
                    ListingPlan(
                        label="all",
                        first_page_url="https://x.example/",
                        page_url_template="https://x.example/?page={page}",
                        max_pages=6,
                    )
                ]

            def parse_card(self, card, page, plan):
                return self.make_candidate(
                    name=card.get_text(strip=True),
                    website=None,
                    listing_url=f"https://x.example/tool/{page.page_number}",
                    page=page,
                    plan=plan,
                )

        client = FakeHttpClient(
            responses={
                "https://x.example/": card_page,
                "https://x.example/?page=2": empty_page,
                # Present, but must never be requested.
                "https://x.example/?page=3": card_page,
                "https://x.example/?page=4": card_page,
            }
        )
        source = _Source(
            SourceConfig(key="x", name="X", homepage="https://x.example/"), client
        )
        candidates = list(source.discover())

        assert len(candidates) == 1
        assert isinstance(candidates[0], CandidateTool)
        assert source.stats.stop_reasons.get(STOP_EMPTY_PAGE) == 1
        # The decisive assertion: pages beyond the empty one are never fetched.
        assert "https://x.example/?page=3" not in client.calls
        assert client.calls == ["https://x.example/", "https://x.example/?page=2"]

    def test_empty_page_does_not_fabricate_candidates(self) -> None:
        from src.discovery.html_source import HtmlListingSource, ListingPlan
        from tests.conftest import FakeHttpClient

        class _Source(HtmlListingSource):
            card_selectors = ("article",)

            def listing_plans(self, **_kwargs):
                return [
                    ListingPlan(label="all", first_page_url="https://y.example/")
                ]

            def parse_card(self, card, page, plan):  # pragma: no cover - never reached
                raise AssertionError("parse_card must not run on a cardless page")

        client = FakeHttpClient(
            responses={"https://y.example/": "<html><body><main></main></body></html>"}
        )
        source = _Source(
            SourceConfig(key="y", name="Y", homepage="https://y.example/"), client
        )
        assert list(source.discover()) == []
        assert source.stats.candidates_emitted == 0
