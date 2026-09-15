"""Creati.ai adapter tests against captured live markup (offline fixtures)."""

from __future__ import annotations

from src.discovery.sources.creati import CreatiSource
from tests.conftest import FakeHttpClient, load_fixture

LISTING_URL = "https://creati.ai/ai-tools"
GRID_PAGE = load_fixture("creati_listing_page1.html")
CATEGORY_PAGE = load_fixture("creati_category_page.html")
MALFORMED = load_fixture("creati_malformed.html")


def make_source(responses: dict[str, object], config) -> CreatiSource:
    return CreatiSource(config, FakeHttpClient(responses=responses))  # type: ignore[arg-type]


class TestGridListing:
    def test_parses_candidates_from_grid_cards(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        candidates = list(source.discover(include_categories=False))

        assert candidates, "expected candidates from the captured grid listing"
        for candidate in candidates:
            assert candidate.name
            assert candidate.website or candidate.listing_url
            assert candidate.source_key == "creati"

    def test_tracking_params_stripped_from_outbound_url(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        websites = [c.website for c in source.discover(include_categories=False) if c.website]
        assert websites
        for url in websites:
            assert "utm_source" not in url
            assert url.startswith("https://")
            assert "creati.ai" not in url

    def test_tagline_and_categories_captured_when_present(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        candidates = list(source.discover(include_categories=False))
        assert any(c.tagline for c in candidates)
        assert any(c.categories for c in candidates)

    def test_internal_detail_url_recorded_as_listing_url(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        candidates = list(source.discover(include_categories=False))
        listing_urls = [c.listing_url for c in candidates if c.listing_url]
        assert all("creati.ai/ai-tools/" in url for url in listing_urls)


class TestCategoryListing:
    def test_parses_list_layout_cards(self, creati_config) -> None:
        category_url = "https://creati.ai/ai-tools/categories/ai-video-generator"
        source = make_source(
            {LISTING_URL: GRID_PAGE, category_url: CATEGORY_PAGE}, creati_config
        )
        candidates = list(
            source.discover(category_slugs=["ai-video-generator"], max_categories=1)
        )
        from_category = [
            c for c in candidates if c.raw_payload.get("plan") == "category:ai-video-generator"
        ]
        assert from_category
        assert all(c.name for c in from_category)
        assert any(c.website for c in from_category)

    def test_category_slugs_discovered_from_listing(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        slugs = source.discover_category_slugs(LISTING_URL)
        assert slugs, "captured markup contains category links"
        assert all("/" not in slug for slug in slugs)

    def test_category_enumeration_failure_is_graceful(self, creati_config) -> None:
        source = make_source({}, creati_config)
        assert source.discover_category_slugs(LISTING_URL) == []
        assert source.stats.errors


class TestRobustness:
    def test_malformed_cards_are_skipped_not_fatal(self, creati_config) -> None:
        source = make_source({LISTING_URL: MALFORMED}, creati_config)
        candidates = list(source.discover(include_categories=False))
        names = {c.name for c in candidates}

        # Cards without any identity pointer must be dropped.
        assert "Nameless Nowhere" not in names
        assert "Asset Only" not in names
        # A detail-page-only card is still a usable candidate.
        assert "Internal Only Tool" in names
        assert source.stats.cards_unparsed >= 2

    def test_asset_links_never_become_websites(self, creati_config) -> None:
        source = make_source({LISTING_URL: MALFORMED}, creati_config)
        for candidate in source.discover(include_categories=False):
            assert not (candidate.website or "").startswith("https://cdn-image.")

    def test_unreachable_listing_yields_nothing(self, creati_config) -> None:
        source = make_source({}, creati_config)
        assert list(source.discover(include_categories=False)) == []
        assert source.stats.candidates_emitted == 0

    def test_page_echo_does_not_duplicate_candidates(self, creati_config) -> None:
        """?page=2 echoing page 1 must not double the candidate count."""
        source = make_source(
            {
                LISTING_URL: GRID_PAGE,
                "https://creati.ai/ai-tools?page=2": GRID_PAGE,
            },
            creati_config,
        )
        single = len(list(make_source({LISTING_URL: GRID_PAGE}, creati_config).discover(
            include_categories=False
        )))
        walked = list(source.discover(include_categories=False, max_pages=3))
        assert len(walked) == single

    def test_limit_is_respected(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        assert len(list(source.discover(limit=1, include_categories=False))) == 1


class TestProvenance:
    def test_every_candidate_carries_full_provenance(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        for candidate in source.discover(include_categories=False):
            payload = candidate.raw_payload
            assert payload["source_key"] == "creati"
            assert payload["source_name"] == "Creati.ai"
            assert payload["source_tier"] == 1
            assert payload["discovery_method"] == "html_listing"
            assert payload["page_url"] == LISTING_URL
            assert payload["page_number"] == 1
            assert payload["retrieved_at"]
            # A directory listing is never treated as verification.
            assert payload["verified"] is False
            assert payload["evidence_level"] == "directory_listing"

    def test_directory_counters_stored_as_raw_signals_only(self, creati_config) -> None:
        category_url = "https://creati.ai/ai-tools/categories/ai-video-generator"
        source = make_source(
            {LISTING_URL: GRID_PAGE, category_url: CATEGORY_PAGE}, creati_config
        )
        candidates = list(
            source.discover(category_slugs=["ai-video-generator"], max_categories=1)
        )
        signal_keys = {k for c in candidates for k in c.raw_signals}
        # Whatever is observed must be namespaced as a *directory* observation.
        assert all(key.startswith("directory_") for key in signal_keys)
        # No fabricated fact fields.
        for candidate in candidates:
            assert "pricing" not in candidate.raw_signals
            assert "launch_date" not in candidate.raw_signals
            assert "users" not in candidate.raw_signals

    def test_stats_dict_is_serialisable(self, creati_config) -> None:
        source = make_source({LISTING_URL: GRID_PAGE}, creati_config)
        list(source.discover(include_categories=False))
        stats = source.stats_dict()
        assert stats["source"] == "creati"
        assert stats["candidates_emitted"] >= 1
        assert stats["pages_fetched"] >= 1
