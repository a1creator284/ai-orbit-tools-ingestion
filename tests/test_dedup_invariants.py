"""Regression tests for the deduplication invariants (guideline §7).

These lock down the *hard* rules of entity resolution. They exist because each
one of them, when broken, either silently destroys real products (over-merging
on a shared company/domain) or floods the batch with duplicates (failing to
merge the same canonical URL seen in two directories).

Invariants protected here
-------------------------
1. same exact canonical product URL            => MERGE
2. same canonical product identity             => MERGE
3. same deterministic ID                       => MERGE
4. explicit canonical/alias identity           => MERGE
5. same company alone                          => NEVER merge
6. same registrable domain alone               => NEVER merge
7. similar names alone                          => NEVER merge
8. different product paths on one domain       => stay separate
9. distinct subdomain products                 => stay separate
10. listing/directory URL is not official identity
11. weak evidence                               => both records preserved / review
12. merge evidence + provenance are explainable
13. output is deterministic
14. no network access from pure dedup logic
"""

from __future__ import annotations

import socket

import pytest

from src.core.config import DedupConfig
from src.core.errors import ConfigError
from src.core.ids import make_entity_id
from src.deduplication.deduplicator import Deduplicator
from src.deduplication.matcher import MatchDecision, MatchTier, ToolMatcher
from src.models.enums import EntityType, VerificationStatus
from src.models.tool import DedupInfo, SourceRef, Tool


# --------------------------------------------------------------------- helpers
def tool(
    name: str,
    *,
    website: str | None = None,
    company: str | None = None,
    canonical_url: str | None = None,
    identity_key: str | None = None,
    product_key: str | None = None,
    identity_basis: str | None = None,
    aliases: list[str] | None = None,
    listing_url: str | None = None,
    source: str = "taaft",
    repository_url: str | None = None,
    verified_final_url: str | None = None,
    tool_id: str | None = None,
) -> Tool:
    """Build a Tool with a deterministic ID, exactly like the normalizer does."""
    if tool_id is None:
        tool_id, identity = make_entity_id(
            EntityType.TOOL.value,
            name=name,
            website=website,
            product_url=listing_url,
            company=company,
            canonical_url=canonical_url,
        )
        basis = identity_basis or identity.basis
        key = identity_key or identity.value
    else:
        basis = identity_basis
        key = identity_key

    record = Tool(
        id=tool_id,
        name=name,
        website=website,
        company=company,
        repository_url=repository_url,
        discovery_sources=[SourceRef(name=source, url=listing_url, kind="directory")],
        dedup=DedupInfo(
            identity_key=key,
            identity_basis=basis,
            product_key=product_key,
            canonical_url=canonical_url,
            canonical_name_key=name.strip().lower(),
            aliases=aliases or [name],
            listing_urls=[listing_url] if listing_url else [],
        ),
    )
    if verified_final_url:
        record.verification.status = VerificationStatus.VERIFIED
        record.verification.final_url = verified_final_url
        record.verification.official_url_checked = verified_final_url
    return record


@pytest.fixture()
def matcher() -> ToolMatcher:
    return ToolMatcher()


# ============================================================ MUST MERGE
class TestMustMerge:
    def test_identical_canonical_product_url_merges(self, matcher: ToolMatcher) -> None:
        """Invariant 1 — the very same canonical URL is the same product."""
        left = tool("Jasper", website="https://jasper.ai/", source="taaft")
        right = tool("Jasper AI", website="http://www.jasper.ai", source="creati")

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier in (MatchTier.IDENTICAL_ID, MatchTier.A_CANONICAL_URL)
        assert result.evidence

    def test_same_product_identity_across_marketing_paths_merges(
        self, matcher: ToolMatcher
    ) -> None:
        """Invariant 2 — ``/pricing?utm=...`` is a page of the same product."""
        left = tool("Jasper", website="https://jasper.ai/")
        right = tool("Jasper", website="https://www.jasper.ai/pricing?utm_source=dir")

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier in (MatchTier.IDENTICAL_ID, MatchTier.B_PRODUCT_IDENTITY)

    def test_same_deterministic_id_merges(self, matcher: ToolMatcher) -> None:
        """Invariant 3 — equal UUIDv5 means equal identity key by construction."""
        left = tool("Runway", website="https://runwayml.com/")
        right = tool("Runway ML", website="https://runwayml.com/")
        assert left.id == right.id

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier is MatchTier.IDENTICAL_ID

    def test_explicit_canonical_url_resolves_a_rebrand(self, matcher: ToolMatcher) -> None:
        """Invariant 4 — an asserted canonical URL is the strongest evidence."""
        new_site = tool("Writesonic", website="https://writesonic.com/")
        old_site = tool(
            "Writesonic (old)",
            website="https://old-writesonic.io/",
            canonical_url="https://writesonic.com/",
        )

        # An asserted canonical URL already drives the deterministic ID, so the
        # two sightings share an ID *and* tier-A/C evidence — both are merges.
        result = matcher.compare(new_site, old_site)

        assert result.decision is MatchDecision.MERGE
        assert result.tier in (
            MatchTier.IDENTICAL_ID,
            MatchTier.A_CANONICAL_URL,
            MatchTier.C_EXPLICIT_ALIAS,
        )
        assert result.evidence

        # ...and the same holds when the old record has no ID-level agreement.
        renamed = tool(
            "Writesonic Legacy",
            website="https://old-writesonic.io/",
            identity_basis="website_domain",
            canonical_url="https://writesonic.com/",
            tool_id="11111111-1111-5111-8111-111111111111",
            identity_key="old-writesonic.io",
        )
        second = matcher.compare(new_site, renamed)
        assert second.decision is MatchDecision.MERGE
        assert second.tier in (MatchTier.A_CANONICAL_URL, MatchTier.C_EXPLICIT_ALIAS)
        assert "writesonic.com" in (second.evidence or "")

    def test_verified_redirect_target_resolves_identity(self, matcher: ToolMatcher) -> None:
        """Invariant 4 — a *verified* redirect target counts as explicit evidence."""
        left = tool("Jasper", website="https://jasper.ai/")
        right = tool(
            "Jarvis AI",
            website="https://jarvis.ai/",
            verified_final_url="https://jasper.ai/",
        )

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier in (MatchTier.A_CANONICAL_URL, MatchTier.C_EXPLICIT_ALIAS)

    def test_same_site_same_name_on_compatible_paths_merges(
        self, matcher: ToolMatcher
    ) -> None:
        """Tier D — same host, prefix-compatible paths and an equal name."""
        left = tool("Suno API", website="https://apiframe.ai/models/suno")
        right = tool("Suno API", website="https://apiframe.ai/models/suno/docs")

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier in (MatchTier.B_PRODUCT_IDENTITY, MatchTier.D_STRONG_COMBINATION)


# ============================================================ MUST NOT MERGE
class TestMustNotMerge:
    def test_same_company_alone_never_merges(self, matcher: ToolMatcher) -> None:
        """Invariant 5 — one company ships many products."""
        left = tool("ChatGPT", website="https://chatgpt.com/", company="OpenAI")
        right = tool("Sora", website="https://sora.com/", company="OpenAI")

        result = matcher.compare(left, right)

        assert result.decision is not MatchDecision.MERGE

    def test_same_registrable_domain_alone_never_merges(self, matcher: ToolMatcher) -> None:
        """Invariant 6 — domain equality is a *site* signal, not identity."""
        left = tool("Product A", website="https://company.com/product-a")
        right = tool("Product B", website="https://company.com/product-b")

        result = matcher.compare(left, right)

        assert result.decision is not MatchDecision.MERGE
        assert "different product paths" in result.reason

    def test_similar_names_alone_never_merge(self, matcher: ToolMatcher) -> None:
        """Invariant 7 — name similarity is review-grade at best."""
        left = tool("Copy AI", website="https://copy.ai/")
        right = tool("Copyly AI", website="https://copyly.io/")

        result = matcher.compare(left, right)

        assert result.decision is not MatchDecision.MERGE

    def test_identical_names_on_different_domains_never_merge(
        self, matcher: ToolMatcher
    ) -> None:
        """Two unrelated vendors may legitimately pick the same product name."""
        left = tool("Halo", website="https://halo-ai.com/")
        right = tool("Halo", website="https://gethalo.io/")

        result = matcher.compare(left, right)

        assert result.decision is not MatchDecision.MERGE
        assert result.decision is MatchDecision.REVIEW  # preserved + flagged

    def test_distinct_product_paths_on_one_domain_stay_separate(self) -> None:
        """Invariant 8 — end-to-end through the deduplicator, not just the matcher."""
        tools = [
            tool("Suno AI API", website="https://apiframe.ai/models/suno"),
            tool("Midjourney AI API", website="https://apiframe.ai/models/midjourney"),
            tool("Apiframe AI", website="https://apiframe.ai/"),
        ]
        canonical, report = Deduplicator().deduplicate(tools)

        assert len(canonical) == 3
        assert report.merged_count == 0

    def test_distinct_subdomain_products_stay_separate(self) -> None:
        """Invariant 9 — ``wabot.`` / ``tg.`` / ``link.`` are three products."""
        tools = [
            tool("Wabot", website="https://wabot.wadesk.io/"),
            tool("TG Desk", website="https://tg.wadesk.io/"),
            tool("Link Desk", website="https://link.wadesk.io/"),
        ]
        canonical, report = Deduplicator().deduplicate(tools)

        assert len(canonical) == 3
        assert report.merged_count == 0

    def test_subdomain_merges_only_on_strong_evidence(self, matcher: ToolMatcher) -> None:
        """Invariant 9 — an explicit canonical URL *does* override the veto."""
        left = tool("Wabot", website="https://wabot.wadesk.io/")
        right = tool(
            "Wabot",
            website="https://tg.wadesk.io/",
            canonical_url="https://wabot.wadesk.io/",
        )

        assert matcher.compare(left, right).decision is MatchDecision.MERGE

    def test_listing_url_does_not_become_official_identity(self) -> None:
        """Invariant 10 — two directory listings are not one product.

        ``theresanaiforthat.com/ai/<slug>`` looks like a product path, but the
        host is a directory: distinct slugs must stay distinct records and must
        never be merged with each other or adopted as an official site.
        """
        left = tool(
            "Tool One",
            listing_url="https://theresanaiforthat.com/ai/tool-one/",
            source="taaft",
        )
        right = tool(
            "Tool Two",
            listing_url="https://theresanaiforthat.com/ai/tool-two/",
            source="taaft",
        )

        assert left.id != right.id
        assert left.website is None and right.website is None
        assert left.dedup.identity_basis == "listing_product_url"

        canonical, report = Deduplicator().deduplicate([left, right])
        assert len(canonical) == 2
        assert report.merged_count == 0

    def test_directory_root_is_never_a_product_identity(self, matcher: ToolMatcher) -> None:
        """A shared-host *root* URL carries no identity at all.

        Regression: keying on ``producthunt.com`` gave every product whose
        ``website`` was recorded as the directory root the same UUIDv5, which
        collapsed unrelated tools into one record.
        """
        left = tool("Thing One", website="https://producthunt.com/")
        right = tool("Thing Two", website="https://producthunt.com/")

        assert left.id != right.id
        assert left.dedup.identity_basis == "name"
        assert matcher._product_identity(left) is None
        assert matcher.compare(left, right).decision is not MatchDecision.MERGE

    def test_app_store_product_path_is_still_an_identity(self) -> None:
        """A *path* on a shared host does identify one product."""
        left = tool("Foo App", website="https://apps.apple.com/app/foo")
        right = tool("Foo App", website="https://apps.apple.com/app/bar")

        assert left.id != right.id
        assert left.dedup.identity_basis == "website_product_url"

        canonical, report = Deduplicator().deduplicate([left, right])
        assert len(canonical) == 2
        assert report.merged_count == 0

    def test_company_conflict_vetoes_a_merge(self, matcher: ToolMatcher) -> None:
        left = tool("Nova", website="https://nova-a.com/", company="Alpha Labs")
        right = tool("Nova", website="https://nova-b.com/", company="Beta Corp")

        result = matcher.compare(left, right)

        assert result.decision is not MatchDecision.MERGE

    def test_same_repository_alone_never_merges(self, matcher: ToolMatcher) -> None:
        """Two products can ship from one monorepo — review, never merge."""
        repo = "https://github.com/acme/monorepo"
        left = tool("Acme Writer", website="https://writer.acme.dev/", repository_url=repo)
        right = tool("Acme Coder", website="https://coder.acme.dev/", repository_url=repo)

        assert matcher.compare(left, right).decision is not MatchDecision.MERGE


# ============================================================ weak evidence
class TestWeakEvidencePreservesRecords:
    def test_review_preserves_both_records_and_records_a_reason(self) -> None:
        """Invariant 11 — a REVIEW decision never drops data."""
        left = tool("Halo", website="https://halo-ai.com/")
        right = tool("Halo", website="https://gethalo.io/")

        canonical, report = Deduplicator().deduplicate([left, right])

        assert len(canonical) == 2
        assert report.merged_count == 0
        assert len(report.review_pairs) == 1
        assert all(record.needs_human_review for record in canonical)
        assert all(record.dedup.review_candidates for record in canonical)

    def test_review_name_floor_is_configurable_not_hardcoded(self) -> None:
        """The tier-E floor comes from configuration, not from the matcher.

        ``Copy AI`` ~ ``Copyly AI`` scores ~0.80, so it sits *between* the two
        configured floors: the same pair must flip from DISTINCT to REVIEW when
        only the configuration changes. Neither floor can ever produce a MERGE.
        """
        left = tool("Copy AI", website="https://copy-ai-one.com/")
        right = tool("Copyly AI", website="https://copyly-two.com/")

        strict = ToolMatcher(DedupConfig(review_name_floor=0.88))
        lenient = ToolMatcher(DedupConfig(review_name_floor=0.5))

        assert strict.compare(left, right).decision is MatchDecision.DISTINCT
        assert lenient.compare(left, right).decision is MatchDecision.REVIEW

    def test_no_configuration_can_turn_name_similarity_into_a_merge(self) -> None:
        """Safety: lowering every threshold must still never merge on names."""
        left = tool("Copy AI", website="https://copy-ai-one.com/")
        right = tool("Copyly AI", website="https://copyly-two.com/")
        reckless = ToolMatcher(
            DedupConfig(
                name_similarity_threshold=0.0,
                review_similarity_threshold=0.0,
                review_name_floor=0.0,
            )
        )

        assert reckless.compare(left, right).decision is not MatchDecision.MERGE


# ============================================================ explainability
class TestExplainability:
    def test_every_merge_records_tier_and_evidence(self, matcher: ToolMatcher) -> None:
        """Invariant 12 — a merge is always auditable."""
        left = tool("Jasper", website="https://jasper.ai/")
        right = tool("Jasper", website="https://www.jasper.ai/pricing")

        result = matcher.compare(left, right)

        assert result.decision is MatchDecision.MERGE
        assert result.tier is not MatchTier.NONE
        assert result.evidence
        assert result.reason
        payload = result.to_dict()
        assert payload["tier"] == result.tier.value
        assert payload["evidence"] == result.evidence

    def test_merged_record_keeps_every_discovery_source(self) -> None:
        """Extra sightings become extra sources, never extra records."""
        left = tool("Jasper", website="https://jasper.ai/", source="taaft")
        right = tool("Jasper AI", website="https://www.jasper.ai/pricing", source="creati")

        canonical, report = Deduplicator().deduplicate([left, right])

        assert len(canonical) == 1
        assert report.merged_count == 1
        names = {ref.name for ref in canonical[0].discovery_sources}
        assert names == {"taaft", "creati"}
        assert "Jasper AI" in canonical[0].dedup.aliases


# ============================================================ determinism
class TestDeterminism:
    def _population(self) -> list[Tool]:
        return [
            tool("Jasper", website="https://jasper.ai/", source="taaft"),
            tool("Jasper AI", website="https://www.jasper.ai/pricing", source="creati"),
            tool("Suno AI API", website="https://apiframe.ai/models/suno"),
            tool("Midjourney AI API", website="https://apiframe.ai/models/midjourney"),
            tool("Wabot", website="https://wabot.wadesk.io/"),
            tool("TG Desk", website="https://tg.wadesk.io/"),
            tool("Halo", website="https://halo-ai.com/"),
            tool("Halo", website="https://gethalo.io/"),
        ]

    def test_output_is_independent_of_input_order(self) -> None:
        """Invariant 13 — shuffling the input never changes the result set."""
        forward, report_a = Deduplicator().deduplicate(self._population())
        reverse, report_b = Deduplicator().deduplicate(list(reversed(self._population())))

        assert {t.id for t in forward} == {t.id for t in reverse}
        assert report_a.merged_count == report_b.merged_count
        assert report_a.output_count == report_b.output_count

    def test_blocking_keys_are_sorted_and_stable(self, matcher: ToolMatcher) -> None:
        record = tool("Jasper AI Writer", website="https://jasper.ai/", company="Jasper")
        keys = matcher.blocking_keys(record)

        assert keys == sorted(keys)
        assert keys == matcher.blocking_keys(record)

    def test_blocking_limits_comparisons(self) -> None:
        """Invariant — no uncontrolled O(n²) sweep over the whole pool."""
        population = [
            tool(f"Distinct Tool {index}", website=f"https://tool-{index}.example.com/")
            for index in range(120)
        ]
        _, report = Deduplicator().deduplicate(population)

        assert report.comparisons < (len(population) * (len(population) - 1)) // 2


# ============================================================ purity
class TestPurity:
    def test_dedup_makes_no_network_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invariant 14 — pure logic must not open a socket."""

        def blocked(*args: object, **kwargs: object) -> None:
            raise AssertionError("deduplication attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", blocked)
        monkeypatch.setattr(socket, "create_connection", blocked)

        canonical, _ = Deduplicator().deduplicate(
            [
                tool("Jasper", website="https://jasper.ai/"),
                tool("Jasper AI", website="https://www.jasper.ai/pricing"),
            ]
        )
        assert len(canonical) == 1


# ============================================================ configuration
class TestDedupConfig:
    def test_review_name_floor_has_a_default(self) -> None:
        assert 0.0 <= DedupConfig().review_name_floor <= 1.0

    def test_review_name_floor_is_loaded_from_settings_yaml(self) -> None:
        from src.core.config import Settings

        settings = Settings.load()
        assert settings.dedup.review_name_floor == pytest.approx(0.82)

    def test_out_of_range_floor_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DedupConfig(review_name_floor=1.5)

    def test_floor_above_merge_threshold_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DedupConfig(review_name_floor=0.95, name_similarity_threshold=0.88)
