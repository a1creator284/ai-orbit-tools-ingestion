"""TAAFT adapter tests: parsing, pagination, and blocked-source behaviour."""

from __future__ import annotations

from src.discovery.sources.taaft import TaaftSource
from tests.conftest import FakeHttpClient, load_fixture

LISTING_URL = "https://theresanaiforthat.com/tools"
PAGE1 = load_fixture("taaft_listing_page1.html")
PAGE2 = load_fixture("taaft_listing_page2.html")
CHALLENGE = load_fixture("cloudflare_challenge.html")


def make_source(responses: dict[str, object], config) -> TaaftSource:
    return TaaftSource(config, FakeHttpClient(responses=responses))  # type: ignore[arg-type]


class TestParsing:
    def test_parses_cards_from_listing(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        candidates = list(source.discover(max_pages=1))
        names = {c.name for c in candidates}
        assert {"Alpha Writer", "Beta Vision", "Gamma Notes"} <= names

    def test_identityless_card_dropped(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        list(source.discover(max_pages=1))
        assert source.stats.cards_unparsed >= 1

    def test_outbound_url_normalized(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        by_name = {c.name: c for c in source.discover(max_pages=1)}
        # Root URLs keep their single trailing slash; tracking params are stripped.
        assert by_name["Alpha Writer"].website == "https://alphawriter.example/"
        assert by_name["Beta Vision"].website == "https://betavision.example/pricing"

    def test_missing_outbound_falls_back_to_detail_page(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        by_name = {c.name: c for c in source.discover(max_pages=1)}
        gamma = by_name["Gamma Notes"]
        assert gamma.website is None
        assert gamma.listing_url == "https://theresanaiforthat.com/ai/gamma-notes"
        assert gamma.raw_payload["outbound_link_present"] is False

    def test_tagline_and_task_label_captured(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        by_name = {c.name: c for c in source.discover(max_pages=1)}
        assert by_name["Alpha Writer"].tagline == "Drafts long-form articles from an outline."
        assert by_name["Alpha Writer"].categories == ["Writing"]

    def test_counters_kept_as_raw_signals(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        by_name = {c.name: c for c in source.discover(max_pages=1)}
        signals = by_name["Alpha Writer"].raw_signals
        assert signals["directory_counters_raw"]["save"] == "1,204"
        assert signals["directory_saves_raw"] == "1,204"
        # Raw strings are preserved verbatim; no normalization into "users".
        assert "users" not in signals

    def test_selector_overrides_from_config(self, taaft_config) -> None:
        taaft_config.extra = {"selectors": {"card": ["div.nonexistent"]}}
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        assert list(source.discover(max_pages=1)) == []


class TestPagination:
    def test_second_page_walked(self, taaft_config) -> None:
        source = make_source(
            {LISTING_URL: PAGE1, f"{LISTING_URL}?page=2": PAGE2}, taaft_config
        )
        names = {c.name for c in source.discover(max_pages=2)}
        assert "Delta Voice" in names
        assert source.stats.pages_fetched == 2

    def test_relisted_same_product_skipped_within_source(self, taaft_config) -> None:
        """The same product re-listed on page 2 must not be emitted twice.

        Page 2 repeats Alpha Writer with ``www.``, a generic ``/pricing`` path
        and a tracking parameter; normalization + identity must collapse it.
        """
        source = make_source(
            {LISTING_URL: PAGE1, f"{LISTING_URL}?page=2": PAGE2}, taaft_config
        )
        candidates = list(source.discover(max_pages=2))
        alpha = [c for c in candidates if c.name == "Alpha Writer"]
        assert len(alpha) == 1
        assert source.stats.duplicates_skipped >= 1

    def test_distinct_product_on_shared_host_is_kept(self, taaft_config) -> None:
        """Two products on one vendor host are both kept at discovery time.

        Discovery must never silently delete a real tool; deciding whether
        these are the same product is :mod:`src.deduplication`'s job.
        """
        source = make_source(
            {LISTING_URL: PAGE1, f"{LISTING_URL}?page=2": PAGE2}, taaft_config
        )
        names = {c.name for c in source.discover(max_pages=2)}
        assert {"Alpha Writer", "Alpha Writer Pro"} <= names


class TestBlockedSource:
    def test_cloudflare_challenge_yields_zero_candidates(self, taaft_config) -> None:
        source = make_source({LISTING_URL: (403, CHALLENGE)}, taaft_config)
        assert list(source.discover(max_pages=3)) == []
        assert source.stats.plans_blocked == 1
        assert "bot_challenge" in source.stats.stop_reasons

    def test_blocked_source_reports_no_false_emptiness(self, taaft_config) -> None:
        """A blocked page must be distinguishable from an empty catalogue."""
        blocked = make_source({LISTING_URL: (403, CHALLENGE)}, taaft_config)
        list(blocked.discover(max_pages=1))
        empty = make_source({LISTING_URL: "<html><body></body></html>"}, taaft_config)
        list(empty.discover(max_pages=1))
        assert blocked.stats.stop_reasons.get("bot_challenge")
        assert not empty.stats.stop_reasons.get("bot_challenge")

    def test_unreachable_host_is_graceful(self, taaft_config) -> None:
        source = make_source({}, taaft_config)
        assert list(source.discover(max_pages=2)) == []
        assert source.stats.pages_failed >= 1


class TestProvenance:
    def test_provenance_records_source_and_page(self, taaft_config) -> None:
        source = make_source({LISTING_URL: PAGE1}, taaft_config)
        for candidate in source.discover(max_pages=1):
            payload = candidate.raw_payload
            assert payload["source_key"] == "taaft"
            assert payload["source_tier"] == 1
            assert payload["source_trust"] == 0.8
            assert payload["page_url"] == LISTING_URL
            assert payload["verified"] is False
