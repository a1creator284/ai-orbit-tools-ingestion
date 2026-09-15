"""Discovery runner tests: merging, provenance preservation, storage, failures."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.config import Settings
from src.discovery.base import CandidateTool, SourceConfig
from src.discovery.registry import SourceRegistry, load_adapters
from src.discovery.runner import DiscoveryRunner, candidate_identity, merge_candidates
from src.discovery.sources.creati import CreatiSource
from src.discovery.sources.taaft import TaaftSource
from tests.conftest import FakeHttpClient, load_fixture

CREATI_URL = "https://creati.ai/ai-tools"
TAAFT_URL = "https://theresanaiforthat.com/tools"


def candidate(name: str, *, website=None, listing=None, source="taaft", **signals) -> CandidateTool:
    return CandidateTool(
        name=name,
        source_key=source,
        source_name=source.upper(),
        website=website,
        listing_url=listing,
        raw_signals=dict(signals),
    )


class TestIdentity:
    def test_domain_is_the_primary_identity(self) -> None:
        a = candidate("Jasper", website="https://www.jasper.ai/pricing")
        b = candidate("Jasper AI", website="http://jasper.ai/")
        assert candidate_identity(a) == candidate_identity(b)

    def test_listing_url_used_when_no_website(self) -> None:
        c = candidate("X", listing="https://theresanaiforthat.com/ai/x/")
        assert candidate_identity(c).startswith("listing:")

    def test_name_is_last_resort(self) -> None:
        c = CandidateTool(name="Solo", source_key="k", source_name="K")
        assert candidate_identity(c) == "name:solo"


class TestMerge:
    def test_duplicates_collapse_and_record_all_sources(self) -> None:
        a = candidate("Jasper", website="https://jasper.ai/", source="taaft")
        b = candidate("Jasper AI", website="https://www.jasper.ai/pricing", source="creati")
        merged, duplicates = merge_candidates([a, b])
        assert len(merged) == 1
        assert duplicates == 1
        contributors = {e["source_key"] for e in merged[0].raw_payload["contributing_sources"]}
        assert contributors == {"taaft", "creati"}

    def test_first_value_never_overwritten(self) -> None:
        a = candidate("Jasper", website="https://jasper.ai/")
        a.tagline = "First tagline"
        b = candidate("Jasper", website="https://jasper.ai/")
        b.tagline = "Second tagline"
        merged, _ = merge_candidates([a, b])
        assert merged[0].tagline == "First tagline"

    def test_gaps_are_filled_from_duplicate(self) -> None:
        a = candidate("Jasper", website="https://jasper.ai/")
        b = candidate("Jasper", website="https://jasper.ai/", listing="https://d.example/jasper/")
        b.tagline = "Filled in"
        merged, _ = merge_candidates([a, b])
        assert merged[0].tagline == "Filled in"
        assert merged[0].listing_url == "https://d.example/jasper"

    def test_signals_namespaced_per_source(self) -> None:
        a = candidate("Jasper", website="https://jasper.ai/", source="taaft", directory_saves_raw="10")
        b = candidate("Jasper", website="https://jasper.ai/", source="creati", directory_saves_raw="99")
        merged, _ = merge_candidates([a, b])
        assert merged[0].raw_signals["directory_saves_raw"] == "10"
        assert merged[0].raw_signals["creati.directory_saves_raw"] == "99"

    def test_distinct_tools_are_kept(self) -> None:
        merged, duplicates = merge_candidates(
            [candidate("A", website="https://a.example/"), candidate("B", website="https://b.example/")]
        )
        assert len(merged) == 2 and duplicates == 0


class FakeRegistry(SourceRegistry):
    """Registry backed by fixture-driven adapters (no network, no YAML)."""

    def __init__(self, sources) -> None:  # noqa: D107
        self._sources = list(sources)
        self.configs = {s.key: s.config for s in self._sources}

    def active(self, *, role: str = "discovery"):  # noqa: D102
        return iter(self._sources)

    def get(self, key: str):  # noqa: D102
        for source in self._sources:
            if source.key == key:
                return source
        raise KeyError(key)


def build_runner(tmp_path: Path, sources) -> DiscoveryRunner:
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.paths.ensure()
    return DiscoveryRunner(settings, registry=FakeRegistry(sources))


def creati_source(config, responses):
    return CreatiSource(config, FakeHttpClient(responses=responses))


def taaft_source(config, responses):
    return TaaftSource(config, FakeHttpClient(responses=responses))


class TestRunner:
    def test_persists_raw_per_source_and_merged_feed(
        self, tmp_path, creati_config, taaft_config
    ) -> None:
        sources = [
            creati_source(creati_config, {CREATI_URL: load_fixture("creati_listing_page1.html")}),
            taaft_source(taaft_config, {TAAFT_URL: load_fixture("taaft_listing_page1.html")}),
        ]
        runner = build_runner(tmp_path, sources)
        merged, report = runner.run(include_categories=False, max_pages=1)

        raw_dir = tmp_path / "data" / "raw" / "discovery"
        assert (raw_dir / "creati.jsonl").exists()
        assert (raw_dir / "taaft.jsonl").exists()
        assert (tmp_path / "data" / "raw" / "candidates.jsonl").exists()

        report_data = json.loads((raw_dir / "report.json").read_text(encoding="utf-8"))
        assert report_data["total_unique"] == len(merged)
        assert set(report_data["sources"]) == {"creati", "taaft"}
        assert report_data["sources"]["taaft"]["emitted"] >= 1

    def test_raw_records_keep_full_provenance(self, tmp_path, taaft_config) -> None:
        runner = build_runner(
            tmp_path, [taaft_source(taaft_config, {TAAFT_URL: load_fixture("taaft_listing_page1.html")})]
        )
        runner.run(max_pages=1)
        lines = (tmp_path / "data" / "raw" / "discovery" / "taaft.jsonl").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        assert lines
        record = json.loads(lines[0])
        assert record["source_key"] == "taaft"
        assert record["raw_payload"]["page_url"] == TAAFT_URL
        assert record["raw_payload"]["verified"] is False
        assert record["discovered_at"]

    def test_blocked_source_reported_not_fabricated(self, tmp_path, taaft_config) -> None:
        runner = build_runner(
            tmp_path,
            [taaft_source(taaft_config, {TAAFT_URL: (403, load_fixture("cloudflare_challenge.html"))})],
        )
        merged, report = runner.run(max_pages=2)
        assert merged == []
        assert report.sources["taaft"]["outcome"] == "blocked_by_bot_challenge"
        assert any("bot challenge" in note for note in report.notes)

    def test_source_exception_does_not_abort_run(self, tmp_path, creati_config, taaft_config) -> None:
        class Exploding(TaaftSource):
            def listing_plans(self, **kwargs):  # noqa: D102
                raise RuntimeError("boom")

        sources = [
            Exploding(taaft_config, FakeHttpClient()),
            creati_source(creati_config, {CREATI_URL: load_fixture("creati_listing_page1.html")}),
        ]
        runner = build_runner(tmp_path, sources)
        merged, report = runner.run(include_categories=False, max_pages=1)
        assert merged, "the healthy source still produced candidates"
        assert report.sources["taaft"]["errors"]

    def test_cross_source_duplicates_counted(self, tmp_path, creati_config, taaft_config) -> None:
        shared = "<html><body><div class='ai'>" \
            "<a class='taaft_title' href='/ai/shared/' title='Shared Tool'>Shared Tool</a>" \
            "<a class='ai_link' rel='nofollow' href='https://shared.example/'>Visit</a>" \
            "</div></body></html>"
        shared_creati = "<html><body><div class='cardContainer'>" \
            "<h3><a href='https://shared.example/?utm_source=creati.ai' title='Shared Tool'>Shared Tool</a></h3>" \
            "</div></body></html>"
        sources = [
            taaft_source(taaft_config, {TAAFT_URL: shared}),
            creati_source(creati_config, {CREATI_URL: shared_creati}),
        ]
        runner = build_runner(tmp_path, sources)
        merged, report = runner.run(include_categories=False, max_pages=1)
        assert len(merged) == 1
        assert report.cross_source_duplicates == 1
        assert report.total_emitted == 2


class TestRegistryWiring:
    def test_both_tier1_adapters_are_registered(self) -> None:
        adapters = load_adapters()
        assert "creati" in adapters
        assert "taaft" in adapters

    def test_real_registry_resolves_enabled_tier1_sources(self) -> None:
        registry = SourceRegistry()
        assert "creati" in registry.implemented()
        assert "taaft" in registry.implemented()
        assert isinstance(registry.get("creati"), CreatiSource)

    def test_configured_but_unimplemented_sources_are_reported(self) -> None:
        registry = SourceRegistry()
        assert registry.not_implemented(), "roadmap sources must stay visible"
