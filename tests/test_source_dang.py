"""Dang.ai adapter tests against captured live markup (offline fixtures).

Every fixture in this module is derived from a real fetch of dang.ai on
2026-09-16, not hand-written to flatter the parser. In particular
``dang_listing_page1.html`` keeps the genuine "Bonbon AI" card, whose
``Adult content`` badge renders the literal text ``18`` — that card is what
exposed the upvote-misattribution bug this adapter now guards against.

The recurring theme is *refusal*: the adapter must decline to produce a value
it cannot see. A missing website, a missing counter and a malformed card must
all stay missing rather than be filled with a plausible guess.
"""

from __future__ import annotations

from src.discovery.sources.dang import DangSource, excluded_paths
from tests.conftest import FakeHttpClient, load_fixture

HOME = "https://dang.ai/"
PAGE2 = "https://dang.ai/?page=2"
PAGE3 = "https://dang.ai/?page=3"
CATEGORIES = "https://dang.ai/categories"

LISTING = load_fixture("dang_listing_page1.html")
LISTING2 = load_fixture("dang_listing_page2.html")
EMPTY = load_fixture("dang_listing_empty.html")
CATEGORY_INDEX = load_fixture("dang_categories.html")
MALFORMED = load_fixture("dang_malformed.html")


def make_source(responses: dict[str, object], config) -> DangSource:
    return DangSource(config, FakeHttpClient(responses=responses))  # type: ignore[arg-type]


class TestRegistration:
    def test_adapter_is_registered_under_its_config_key(self) -> None:
        """Importing the sources package must register 'dang'.

        The adapter existed for a full checkpoint without being imported in
        ``src/discovery/sources/__init__.py``, so it never self-registered and
        the CLI reported it as "configured but not implemented". This asserts
        the wiring, not the class.
        """
        from src.discovery.registry import load_adapters

        adapters = load_adapters()
        assert "dang" in adapters
        assert adapters["dang"] is DangSource

    def test_configured_source_resolves_to_the_adapter(self) -> None:
        from src.discovery.registry import SourceRegistry

        registry = SourceRegistry()
        assert "dang" in registry.implemented()
        assert "dang" not in registry.not_implemented()

    def test_enabled_source_is_active_for_discovery(self) -> None:
        from src.discovery.registry import SourceRegistry

        registry = SourceRegistry()
        assert "dang" in {source.key for source in registry.active(role="discovery")}


class TestListingParsing:
    def test_parses_candidates_from_real_cards(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        candidates = list(source.discover(include_categories=False))

        assert candidates, "expected candidates from the captured listing"
        for candidate in candidates:
            assert candidate.name
            assert candidate.listing_url
            assert candidate.source_key == "dang"
            assert candidate.is_usable

    def test_listing_url_is_the_dang_detail_page(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            assert candidate.listing_url.startswith("https://dang.ai/tool/")

    def test_names_come_from_the_product_anchor(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        names = {c.name for c in source.discover(include_categories=False)}
        assert "AXIOM" in names
        assert "Bonbon AI" in names

    def test_tagline_and_categories_captured_when_present(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        candidates = list(source.discover(include_categories=False))
        assert any(c.tagline for c in candidates)
        assert any(c.categories for c in candidates)

    def test_categories_are_labels_not_slugs(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            for category in candidate.categories:
                assert "/" not in category
                assert "category" not in category.lower()


class TestOfficialUrlIsNeverInvented:
    """The single most important guarantee of this adapter.

    Dang's listing card does not publish the outbound official URL. Guessing
    one from the ``/tool/<slug>`` slug would look productive and would poison
    official-website verification with URLs nobody ever observed.
    """

    def test_website_stays_none_when_the_card_publishes_no_outbound_link(
        self, dang_config
    ) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        candidates = list(source.discover(include_categories=False))
        assert candidates
        assert all(c.website is None for c in candidates)

    def test_slug_is_never_converted_into_a_domain(self, dang_config) -> None:
        """'...-meshy-ai' must not silently become 'https://meshy.ai'."""
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            assert candidate.website is None or "dang.ai" not in candidate.website

    def test_provenance_records_that_the_official_url_is_unresolved(
        self, dang_config
    ) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            payload = candidate.raw_payload
            assert payload["outbound_link_present"] is False
            assert payload["official_url_source"] == "detail_page_required"

    def test_asset_and_internal_links_never_become_websites(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            website = candidate.website or ""
            assert not website.startswith("https://assets.dang.ai")
            assert "dang.ai" not in website


class TestRawSignals:
    def test_upvote_counter_stored_as_raw_string_observation(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        candidates = {c.name: c for c in source.discover(include_categories=False)}
        axiom = candidates["AXIOM"]
        assert axiom.raw_signals["directory_upvotes_raw"] == "8"
        # Raw means raw: not coerced to int, not renamed to a fact-like field.
        assert isinstance(axiom.raw_signals["directory_upvotes_raw"], str)

    def test_adult_content_badge_is_not_recorded_as_an_upvote_count(
        self, dang_config
    ) -> None:
        """Regression: the 'Adult content' badge renders the bare text '18'.

        A "first number on the card" fallback filed that 18 as this product's
        upvote count. The real counter for Bonbon AI is 47.
        """
        source = make_source({HOME: LISTING}, dang_config)
        bonbon = next(
            c for c in source.discover(include_categories=False) if c.name == "Bonbon AI"
        )
        assert bonbon.raw_signals["directory_upvotes_raw"] == "47"
        assert bonbon.raw_signals["directory_upvotes_raw"] != "18"

    def test_missing_counter_stays_missing(self, dang_config) -> None:
        """A card with no labelled counter must not acquire one."""
        source = make_source({HOME: MALFORMED}, dang_config)
        candidates = {c.name: c for c in source.discover(include_categories=False)}
        legit = candidates["Legit Tool"]
        assert legit.raw_signals["directory_upvotes_raw"] == "1,204"

    def test_featured_placement_flagged_as_paid_not_as_quality(
        self, dang_config
    ) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        candidates = list(source.discover(include_categories=False))
        featured = [
            c for c in candidates if c.raw_signals.get("directory_featured_placement")
        ]
        assert featured, "captured markup contains 'Pro featured on Dang' badges"
        for candidate in featured:
            # The flag lives in raw_signals only — it never sets a score,
            # a verification status or any fact-bearing field.
            assert candidate.raw_payload["verified"] is False
            assert candidate.raw_payload["evidence_level"] == "directory_listing"


class TestPagination:
    def test_page_two_yields_genuinely_new_products(self, dang_config) -> None:
        """Verified live: pages share zero tool slugs."""
        source = make_source({HOME: LISTING, PAGE2: LISTING2}, dang_config)
        candidates = list(source.discover(include_categories=False, max_pages=2))

        page_one = {c.name for c in candidates if c.raw_payload["page_number"] == 1}
        page_two = {c.name for c in candidates if c.raw_payload["page_number"] == 2}
        assert page_one and page_two
        assert not (page_one & page_two), "pages must not re-yield the same products"

    def test_page_numbers_are_recorded_in_provenance(self, dang_config) -> None:
        source = make_source({HOME: LISTING, PAGE2: LISTING2}, dang_config)
        candidates = list(source.discover(include_categories=False, max_pages=2))
        assert {c.raw_payload["page_number"] for c in candidates} == {1, 2}

    def test_out_of_range_page_ends_the_walk_without_fabricating(
        self, dang_config
    ) -> None:
        """Live page 500 returns HTTP 200 with zero cards."""
        source = make_source(
            {HOME: LISTING, PAGE2: LISTING2, PAGE3: EMPTY}, dang_config
        )
        candidates = list(source.discover(include_categories=False, max_pages=5))
        assert all(c.raw_payload["page_number"] in (1, 2) for c in candidates)
        assert source.stats.stop_reasons.get("empty_page")

    def test_nav_links_outside_cards_are_not_treated_as_inventory(
        self, dang_config
    ) -> None:
        """An empty results page still carries /tool/ nav links; ignore them."""
        source = make_source({HOME: EMPTY}, dang_config)
        assert list(source.discover(include_categories=False)) == []
        assert source.stats.candidates_emitted == 0

    def test_repeated_page_content_does_not_duplicate_candidates(
        self, dang_config
    ) -> None:
        source = make_source({HOME: LISTING, PAGE2: LISTING}, dang_config)
        walked = list(source.discover(include_categories=False, max_pages=3))
        baseline = len(
            list(
                make_source({HOME: LISTING}, dang_config).discover(
                    include_categories=False
                )
            )
        )
        assert len(walked) == baseline

    def test_limit_is_respected(self, dang_config) -> None:
        source = make_source({HOME: LISTING, PAGE2: LISTING2}, dang_config)
        assert len(list(source.discover(limit=3, include_categories=False))) == 3


class TestCategories:
    def test_category_slugs_discovered_from_the_index(self, dang_config) -> None:
        source = make_source({CATEGORIES: CATEGORY_INDEX}, dang_config)
        slugs = source.discover_category_slugs(CATEGORIES)
        assert "business-ai-tools" in slugs
        assert all("/" not in slug for slug in slugs)
        # A ?page=2 variant is the same category, not a second one.
        assert slugs.count("business-ai-tools") == 1
        # /tool/ and /login links are not categories.
        assert "some-tool" not in slugs
        assert "login" not in slugs

    def test_no_request_is_made_when_no_categories_would_be_walked(
        self, dang_config
    ) -> None:
        """max_categories=0 must not spend a fetch enumerating the index."""
        client = FakeHttpClient(responses={HOME: LISTING, CATEGORIES: CATEGORY_INDEX})
        source = DangSource(dang_config, client)  # type: ignore[arg-type]
        list(source.discover(max_categories=0))
        assert CATEGORIES not in client.calls

    def test_category_plans_are_built_when_requested(self, dang_config) -> None:
        source = make_source({CATEGORIES: CATEGORY_INDEX}, dang_config)
        plans = source.listing_plans(max_categories=2)
        labels = [plan.label for plan in plans]
        assert labels[0] == "all"
        assert any(label.startswith("category:") for label in labels)

    def test_category_enumeration_failure_is_graceful(self, dang_config) -> None:
        source = make_source({}, dang_config)
        assert source.discover_category_slugs(CATEGORIES) == []
        assert source.stats.errors


class TestRobustness:
    def test_malformed_cards_are_skipped_not_fatal(self, dang_config) -> None:
        source = make_source({HOME: MALFORMED}, dang_config)
        names = {c.name for c in source.discover(include_categories=False)}

        assert "Legit Tool" in names
        # An editorial teaser, an unlabelled anchor and a furniture-only block
        # are all unidentifiable and must be dropped.
        assert len(names) == 1
        assert source.stats.cards_unparsed >= 3

    def test_disallowed_paths_never_become_candidates(self, dang_config) -> None:
        source = make_source({HOME: MALFORMED}, dang_config)
        for candidate in source.discover(include_categories=False):
            for blocked in excluded_paths():
                assert not candidate.listing_url.endswith(f"/{blocked}")

    def test_excluded_paths_cover_every_robots_disallow_rule(self) -> None:
        """robots.txt (verified 2026-09-16) disallows exactly these paths."""
        disallowed = {"login", "dashboard", "account", "submit", "api"}
        assert disallowed.issubset(set(excluded_paths()))

    def test_unreachable_listing_yields_nothing(self, dang_config) -> None:
        source = make_source({}, dang_config)
        assert list(source.discover(include_categories=False)) == []
        assert source.stats.candidates_emitted == 0

    def test_only_allowed_urls_are_requested(self, dang_config) -> None:
        client = FakeHttpClient(responses={HOME: LISTING, PAGE2: LISTING2})
        source = DangSource(dang_config, client)  # type: ignore[arg-type]
        list(source.discover(include_categories=False, max_pages=2))
        for url in client.calls:
            assert url.startswith("https://dang.ai/")
            for blocked in ("/login", "/dashboard", "/account", "/submit", "/api"):
                assert blocked not in url


class TestProvenance:
    def test_every_candidate_carries_full_provenance(self, dang_config) -> None:
        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            payload = candidate.raw_payload
            assert payload["discovery_method"] == "html_listing"
            assert payload["source_key"] == "dang"
            assert payload["source_name"] == "Dang.ai"
            assert payload["source_tier"] == 2
            assert payload["page_url"]
            assert payload["retrieved_at"]
            # A directory listing is never evidence of quality or liveness.
            assert payload["verified"] is False
            assert payload["evidence_level"] == "directory_listing"

    def test_candidates_are_json_serialisable(self, dang_config) -> None:
        import json

        source = make_source({HOME: LISTING}, dang_config)
        for candidate in source.discover(include_categories=False):
            assert json.loads(json.dumps(candidate.to_dict()))["name"] == candidate.name

    def test_stats_dict_is_serialisable(self, dang_config) -> None:
        import json

        source = make_source({HOME: LISTING}, dang_config)
        list(source.discover(include_categories=False))
        assert json.loads(json.dumps(source.stats_dict()))["source"] == "dang"


class TestRunnerIntegration:
    def test_candidates_survive_the_merge_with_identity_preserved(
        self, dang_config
    ) -> None:
        """Website-less candidates must not collapse into one another.

        Every Dang candidate has ``website=None``, so if identity fell back to
        a shared value they would all merge into a single row.
        """
        from src.discovery.runner import merge_candidates

        source = make_source({HOME: LISTING}, dang_config)
        candidates = list(source.discover(include_categories=False))
        merged, duplicates = merge_candidates(candidates)

        assert duplicates == 0
        assert len(merged) == len(candidates)

    def test_merging_with_another_source_keeps_both(self, dang_config) -> None:
        from src.discovery.base import CandidateTool
        from src.discovery.runner import merge_candidates

        source = make_source({HOME: LISTING}, dang_config)
        dang_candidates = list(source.discover(include_categories=False))
        other = CandidateTool(
            name="Some Other Tool",
            source_key="creati",
            source_name="Creati.ai",
            website="https://example-other-tool.com/",
        )
        merged, _ = merge_candidates([*dang_candidates, other])
        assert len(merged) == len(dang_candidates) + 1
