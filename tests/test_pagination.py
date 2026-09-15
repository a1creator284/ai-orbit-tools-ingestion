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
