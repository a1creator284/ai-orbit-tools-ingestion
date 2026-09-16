"""Tests for the production official-URL resolution stage.

:mod:`src.candidates.official_url` is already covered by
``tests/test_official_url_resolution.py`` (how a URL is read off a page). This
suite covers the *stage* around it: reading the persisted raw feed, preserving
every discovered field, writing provenance, persisting incrementally, resuming
without duplicating, and deriving the report from the file on disk.

Everything is offline: real captured fixtures through ``FakeHttpClient``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.candidates.official_url import ResolutionBasis, ResolutionFailure
from src.candidates.resolution import (
    ORIGIN_DETAIL_PAGE,
    ORIGIN_DISCOVERY,
    ORIGIN_NONE,
    PROVENANCE_KEY,
    RESOLUTION_REPORT_FILENAME,
    RESOLUTION_STATE_FILENAME,
    RESOLVED_FILENAME,
    OfficialUrlResolutionRunner,
    enriched_record,
    record_key,
    summarize_resolved_file,
)
from tests.conftest import FakeHttpClient, load_fixture

DANG_URL = "https://dang.ai/tool/axiom-ai-work-assistant"
DANG_BONBON_URL = "https://dang.ai/tool/bonbon-ai-ai-character-chat"
CREATI_URL = "https://creati.ai/ai-tools/quantinor"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def raw(
    name: str,
    *,
    source_key: str = "dang",
    source_name: str = "Dang.ai",
    listing_url: str | None = None,
    website: str | None = None,
    **extra: object,
) -> dict[str, object]:
    """A raw discovery record shaped like a line of ``candidates.jsonl``."""
    record: dict[str, object] = {
        "name": name,
        "source_key": source_key,
        "source_name": source_name,
        "listing_url": listing_url,
        "website": website,
        "tagline": f"{name} tagline",
        "categories": ["Productivity"],
        "raw_signals": {"directory_like_counter_raw": "12"},
        "raw_payload": {
            "discovery_method": "html_listing",
            "page_url": "https://dang.ai/",
            "evidence_level": "directory_listing",
            "verified": False,
        },
        "discovered_at": "2026-09-16T04:14:22.272882+00:00",
    }
    record.update(extra)
    return record


def write_feed(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def dang_client() -> FakeHttpClient:
    return FakeHttpClient(
        responses={
            DANG_URL: load_fixture("dang_detail_axiom.html"),
            DANG_BONBON_URL: load_fixture("dang_detail_bonbon.html"),
            CREATI_URL: load_fixture("creati_detail_quantinor.html"),
        }
    )


def read_output(interim: Path) -> list[dict[str, object]]:
    lines = (interim / RESOLVED_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def run_stage(
    tmp_path: Path,
    records: list[dict[str, object]],
    *,
    client: FakeHttpClient | None = None,
    limit: int | None = None,
    resume: bool = True,
    sources: set[str] | None = None,
) -> tuple[list[dict[str, object]], object, Path]:
    feed = write_feed(tmp_path / "raw" / "candidates.jsonl", records)
    interim = tmp_path / "interim"
    runner = OfficialUrlResolutionRunner(client=client or dang_client(), live=True)
    report = runner.run(feed, interim_dir=interim, limit=limit, resume=resume, sources=sources)
    return read_output(interim), report, interim


# --------------------------------------------------------------------------- #
# field + provenance preservation
# --------------------------------------------------------------------------- #
def test_existing_official_url_is_preserved_and_never_fetched(tmp_path: Path) -> None:
    client = dang_client()
    out, report, _ = run_stage(
        tmp_path,
        [raw("Axiom", listing_url=DANG_URL, website="https://axiom.ai/")],
        client=client,
    )
    assert client.calls == []
    assert out[0]["website"] == "https://axiom.ai/"
    provenance = out[0][PROVENANCE_KEY]
    assert provenance["origin"] == ORIGIN_DISCOVERY
    assert provenance["failure"] == ResolutionFailure.ALREADY_RESOLVED
    assert provenance["fetched"] is False
    assert report.already_had_official_url == 1
    assert report.needed_resolution == 0


def test_every_discovered_field_survives_verbatim(tmp_path: Path) -> None:
    record = raw("Axiom AI Work Assistant", listing_url=DANG_URL)
    out, _, _ = run_stage(tmp_path, [record])
    enriched = out[0]
    for key, value in record.items():
        if key == "website":
            continue
        assert enriched[key] == value, key
    # Discovery provenance specifically must survive.
    assert enriched["raw_payload"]["evidence_level"] == "directory_listing"
    assert enriched["raw_signals"] == {"directory_like_counter_raw": "12"}
    assert enriched["source_key"] == "dang"
    assert enriched["listing_url"] == DANG_URL


def test_raw_feed_is_never_modified(tmp_path: Path) -> None:
    feed = write_feed(
        tmp_path / "raw" / "candidates.jsonl", [raw("Axiom", listing_url=DANG_URL)]
    )
    before = feed.read_bytes()
    runner = OfficialUrlResolutionRunner(client=dang_client(), live=True)
    runner.run(feed, interim_dir=tmp_path / "interim")
    assert feed.read_bytes() == before


# --------------------------------------------------------------------------- #
# per-source resolution
# --------------------------------------------------------------------------- #
def test_dang_json_ld_resolution_is_recorded_with_evidence(tmp_path: Path) -> None:
    out, report, _ = run_stage(tmp_path, [raw("Axiom AI Work Assistant", listing_url=DANG_URL)])
    provenance = out[0][PROVENANCE_KEY]
    assert out[0]["website"]
    assert provenance["origin"] == ORIGIN_DETAIL_PAGE
    assert provenance["basis"] == ResolutionBasis.JSON_LD
    assert provenance["detail_url"] == DANG_URL
    assert provenance["evidence"]
    assert provenance["resolved_at"]
    assert report.resolved == 1
    assert report.bases[ResolutionBasis.JSON_LD] == 1


def test_creati_outbound_visit_resolution_is_recorded(tmp_path: Path) -> None:
    out, report, _ = run_stage(
        tmp_path,
        [
            raw(
                "Quantinor",
                source_key="creati",
                source_name="Creati.ai",
                listing_url=CREATI_URL,
            )
        ],
    )
    assert out[0]["website"]
    assert out[0][PROVENANCE_KEY]["basis"] == ResolutionBasis.VISIT_LINK
    assert out[0][PROVENANCE_KEY]["directory_source_key"] == "creati"
    assert report.resolved == 1


def test_aitoolnet_records_keep_their_listing_card_website(tmp_path: Path) -> None:
    """AIToolNet publishes the website on the card, so nothing is re-read."""
    client = dang_client()
    out, report, _ = run_stage(
        tmp_path,
        [
            raw(
                "DeepSeek Harness",
                source_key="aitoolnet",
                source_name="AIToolNet",
                listing_url="https://aitoolnet.com/deepseek-harness",
                website="https://deepseek.com/harness",
            )
        ],
        client=client,
    )
    assert client.calls == []
    assert out[0]["website"] == "https://deepseek.com/harness"
    assert report.by_source["aitoolnet"]["already_had_official_url"] == 1


def test_source_breakdown_counts_each_directory_separately(tmp_path: Path) -> None:
    out, report, _ = run_stage(
        tmp_path,
        [
            raw("Axiom AI Work Assistant", listing_url=DANG_URL),
            raw("Quantinor", source_key="creati", source_name="Creati.ai", listing_url=CREATI_URL),
            raw(
                "DeepSeek Harness",
                source_key="aitoolnet",
                source_name="AIToolNet",
                listing_url="https://aitoolnet.com/deepseek-harness",
                website="https://deepseek.com/harness",
            ),
        ],
    )
    assert len(out) == 3
    assert report.by_source["dang"]["needed_resolution"] == 1
    assert report.by_source["creati"]["needed_resolution"] == 1
    assert report.by_source["aitoolnet"]["needed_resolution"] == 0


# --------------------------------------------------------------------------- #
# failure handling — nothing is ever guessed
# --------------------------------------------------------------------------- #
def test_missing_detail_url_is_skipped_not_invented(tmp_path: Path) -> None:
    client = dang_client()
    out, report, _ = run_stage(
        tmp_path, [raw("Nameless Tool", listing_url=None)], client=client
    )
    assert client.calls == []
    assert out[0]["website"] is None
    assert out[0][PROVENANCE_KEY]["failure"] == ResolutionFailure.NO_DETAIL_URL
    assert report.skipped_no_detail_url == 1
    assert report.unresolved == 1


def test_unresolvable_page_leaves_website_none(tmp_path: Path) -> None:
    client = FakeHttpClient(
        responses={DANG_URL: "<html><body><p>no links here</p></body></html>"}
    )
    out, report, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert out[0]["website"] is None
    assert out[0][PROVENANCE_KEY]["failure"] == ResolutionFailure.NO_OUTBOUND_URL
    assert report.no_outbound_url == 1


def test_malformed_html_does_not_produce_a_url(tmp_path: Path) -> None:
    client = FakeHttpClient(responses={DANG_URL: load_fixture("dang_malformed.html")})
    out, _, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert out[0]["website"] is None
    assert out[0][PROVENANCE_KEY]["failure"]


def test_malformed_json_ld_does_not_produce_a_url(tmp_path: Path) -> None:
    body = (
        "<html><head><script type='application/ld+json'>{not json,,</script>"
        "</head><body><p>nothing</p></body></html>"
    )
    client = FakeHttpClient(responses={DANG_URL: body})
    out, _, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert out[0]["website"] is None


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_statuses_are_recorded_and_survive(tmp_path: Path, status: int) -> None:
    client = FakeHttpClient(responses={DANG_URL: (status, "<html><body>blocked</body></html>")})
    out, report, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert len(out) == 1
    assert out[0]["website"] is None
    assert out[0][PROVENANCE_KEY]["detail_status"] == status
    assert report.unresolved == 1


def test_timeout_style_fetch_failure_is_recorded(tmp_path: Path) -> None:
    # An unmapped URL is the fake client's "fetch degraded" contract.
    client = FakeHttpClient(responses={})
    out, report, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert out[0]["website"] is None
    assert out[0][PROVENANCE_KEY]["failure"] == ResolutionFailure.FETCH_FAILED
    assert report.fetch_failures == 1


def test_one_hostile_record_does_not_stop_the_batch(tmp_path: Path) -> None:
    feed = tmp_path / "raw" / "candidates.jsonl"
    feed.parent.mkdir(parents=True)
    feed.write_text(
        "\n".join(
            [
                json.dumps(raw("Axiom AI Work Assistant", listing_url=DANG_URL)),
                "{ this is not json",
                json.dumps({"name": None, "source_key": "dang", "listing_url": 12345}),
                json.dumps(raw("Quantinor", source_key="creati", listing_url=CREATI_URL)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runner = OfficialUrlResolutionRunner(client=dang_client(), live=True)
    report = runner.run(feed, interim_dir=tmp_path / "interim")
    out = read_output(tmp_path / "interim")
    # The two good records resolved; the unusable one is persisted unresolved.
    assert sum(1 for r in out if r["website"]) == 2
    assert report.output_records == 3


def test_no_slug_or_search_engine_url_is_ever_written(tmp_path: Path) -> None:
    client = FakeHttpClient(
        responses={
            DANG_URL: (
                "<html><body>"
                "<a href='https://www.google.com/search?q=axiom'>Search</a>"
                "<a href='https://twitter.com/axiomai'>Twitter</a>"
                "<a href='https://apps.apple.com/app/id123'>App Store</a>"
                "<a href='https://play.google.com/store/apps/details?id=x'>Get it</a>"
                "<a href='https://g2.com/products/axiom'>Reviews</a>"
                "</body></html>"
            )
        }
    )
    out, _, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    assert out[0]["website"] is None
    assert "axiom.ai" not in json.dumps(out[0])


def test_resolved_url_is_normalized_and_tracking_params_are_dropped(tmp_path: Path) -> None:
    body = (
        "<html><body>"
        "<a rel='external' href='http://WWW.Axiom.ai/Home?utm_source=dang&ref=dang#top'>"
        "Visit site</a>"
        "</body></html>"
    )
    client = FakeHttpClient(responses={DANG_URL: body})
    out, _, _ = run_stage(tmp_path, [raw("Axiom", listing_url=DANG_URL)], client=client)
    website = out[0]["website"]
    assert website
    assert "utm_source" not in website
    assert "#" not in website
    assert website.startswith("https://")
    assert "AXIOM" not in website
    assert out[0][PROVENANCE_KEY]["website_domain"] == "axiom.ai"


# --------------------------------------------------------------------------- #
# provenance semantics
# --------------------------------------------------------------------------- #
def test_provenance_never_claims_verification(tmp_path: Path) -> None:
    out, report, _ = run_stage(
        tmp_path,
        [
            raw("Axiom AI Work Assistant", listing_url=DANG_URL),
            raw("Known", listing_url=DANG_BONBON_URL, website="https://known.example/"),
        ],
    )
    for record in out:
        provenance = record[PROVENANCE_KEY]
        assert provenance["verified"] is False
        assert provenance["verification_status"] == "unverified"
        assert "not official-site verification" in provenance["note"]
    assert "not official-site verification" in report.to_dict()["note"]


def test_provenance_distinguishes_discovered_from_resolved(tmp_path: Path) -> None:
    out, _, _ = run_stage(
        tmp_path,
        [
            raw("Known", listing_url=DANG_BONBON_URL, website="https://known.example/"),
            raw("Axiom AI Work Assistant", listing_url=DANG_URL),
        ],
    )
    assert out[0][PROVENANCE_KEY]["origin"] == ORIGIN_DISCOVERY
    assert out[1][PROVENANCE_KEY]["origin"] == ORIGIN_DETAIL_PAGE


def test_enriched_record_is_pure_and_does_not_mutate_input() -> None:
    record = raw("Axiom", listing_url=DANG_URL)
    snapshot = json.dumps(record, sort_keys=True)
    enriched_record(record, None, live=False)
    assert json.dumps(record, sort_keys=True) == snapshot


def test_unattempted_record_is_persisted_as_unresolved() -> None:
    out = enriched_record(raw("Axiom", listing_url=DANG_URL), None, live=True)
    assert out["website"] is None
    assert out[PROVENANCE_KEY]["origin"] == ORIGIN_NONE
    assert out[PROVENANCE_KEY]["needs_review"] is True


# --------------------------------------------------------------------------- #
# incremental persistence, resume, determinism
# --------------------------------------------------------------------------- #
def test_limit_leaves_the_rest_pending_and_persists_what_it_did(tmp_path: Path) -> None:
    records = [
        raw("Axiom AI Work Assistant", listing_url=DANG_URL),
        raw("BonBon AI", listing_url=DANG_BONBON_URL),
        raw("Quantinor", source_key="creati", listing_url=CREATI_URL),
    ]
    out, report, interim = run_stage(tmp_path, records, limit=1)
    assert len(out) == 1
    assert report.output_records == 1
    assert report.pending == 2
    assert (interim / RESOLUTION_STATE_FILENAME).exists()
    assert (interim / RESOLUTION_REPORT_FILENAME).exists()


def test_resume_continues_without_duplicating(tmp_path: Path) -> None:
    records = [
        raw("Axiom AI Work Assistant", listing_url=DANG_URL),
        raw("BonBon AI", listing_url=DANG_BONBON_URL),
        raw("Quantinor", source_key="creati", listing_url=CREATI_URL),
    ]
    feed = write_feed(tmp_path / "raw" / "candidates.jsonl", records)
    interim = tmp_path / "interim"

    first = OfficialUrlResolutionRunner(client=dang_client(), live=True)
    first.run(feed, interim_dir=interim, limit=2)
    after_first = read_output(interim)
    assert len(after_first) == 2

    second_client = dang_client()
    second = OfficialUrlResolutionRunner(client=second_client, live=True)
    report = second.run(feed, interim_dir=interim)
    after_second = read_output(interim)

    assert len(after_second) == 3
    # The already-persisted records were not re-fetched.
    assert second_client.calls == [CREATI_URL]
    # And the first pass's rows are untouched, in place.
    assert after_second[:2] == after_first
    assert report.output_records == 3
    assert report.pending == 0

    keys = [record_key(r) for r in after_second]
    assert len(keys) == len(set(keys))


def test_restart_replaces_the_artefact_instead_of_appending(tmp_path: Path) -> None:
    records = [raw("Axiom AI Work Assistant", listing_url=DANG_URL)]
    feed = write_feed(tmp_path / "raw" / "candidates.jsonl", records)
    interim = tmp_path / "interim"
    OfficialUrlResolutionRunner(client=dang_client(), live=True).run(feed, interim_dir=interim)
    OfficialUrlResolutionRunner(client=dang_client(), live=True).run(
        feed, interim_dir=interim, resume=False
    )
    assert len(read_output(interim)) == 1


def test_duplicate_input_lines_produce_one_output_record(tmp_path: Path) -> None:
    record = raw("Axiom AI Work Assistant", listing_url=DANG_URL)
    out, report, _ = run_stage(tmp_path, [record, dict(record)])
    assert len(out) == 1
    assert report.output_records == 1


def test_output_is_deterministic_and_in_input_order(tmp_path: Path) -> None:
    records = [
        raw("Axiom AI Work Assistant", listing_url=DANG_URL),
        raw("BonBon AI", listing_url=DANG_BONBON_URL),
        raw("Quantinor", source_key="creati", listing_url=CREATI_URL),
    ]
    first, _, _ = run_stage(tmp_path / "a", records)
    second, _, _ = run_stage(tmp_path / "b", records)
    assert [r["name"] for r in first] == ["Axiom AI Work Assistant", "BonBon AI", "Quantinor"]

    def strip(rows: list[dict[str, object]]) -> str:
        cleaned = []
        for row in rows:
            copy = dict(row)
            prov = dict(copy[PROVENANCE_KEY])  # type: ignore[arg-type]
            prov.pop("resolved_at", None)
            copy[PROVENANCE_KEY] = prov
            cleaned.append(copy)
        return json.dumps(cleaned, sort_keys=True)

    assert strip(first) == strip(second)


def test_source_filter_only_processes_the_requested_directory(tmp_path: Path) -> None:
    out, _, _ = run_stage(
        tmp_path,
        [
            raw("Axiom AI Work Assistant", listing_url=DANG_URL),
            raw("Quantinor", source_key="creati", listing_url=CREATI_URL),
        ],
        sources={"creati"},
    )
    assert [r["source_key"] for r in out] == ["creati"]


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_report_is_derived_from_the_persisted_file(tmp_path: Path) -> None:
    _, report, interim = run_stage(
        tmp_path,
        [
            raw("Axiom AI Work Assistant", listing_url=DANG_URL),
            raw("Nameless", listing_url=None),
            raw("Known", listing_url=DANG_BONBON_URL, website="https://known.example/"),
        ],
    )
    recomputed = summarize_resolved_file(interim / RESOLVED_FILENAME)
    assert recomputed.output_records == report.output_records
    assert recomputed.resolved == report.resolved
    assert recomputed.unresolved == report.unresolved
    assert recomputed.already_had_official_url == 1
    assert report.needed_resolution == 2
    assert report.coverage_rate == round(report.resolved / 2, 4)


def test_report_is_json_serialisable(tmp_path: Path) -> None:
    _, report, interim = run_stage(
        tmp_path, [raw("Axiom AI Work Assistant", listing_url=DANG_URL)]
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["input_records"] == 1
    on_disk = json.loads((interim / RESOLUTION_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["output_records"] == 1


# --------------------------------------------------------------------------- #
# checkpoint/report consistency
#
# Regression cover for the f42c8b7 production checkpoint, where the resolved
# dataset held 2,266 rows while the published report said 2,200. Nothing was
# wrong with the data; the report was simply the one written by the previous
# finished pass, because a report was only ever emitted at the *end* of a run
# and the third (live) pass was interrupted after 66 further rows.
# --------------------------------------------------------------------------- #
def test_interrupted_pass_still_leaves_a_report_matching_the_rows_on_disk(
    tmp_path: Path,
) -> None:
    """An interrupted pass must not leave the previous pass's report behind.

    The runner is stopped mid-pass exactly the way a killed production batch
    stops: the client raises once a number of detail pages have been read. The
    report on disk then has to describe the rows that were actually appended.
    """
    records = [raw(f"Tool {i}", listing_url=DANG_URL) for i in range(6)]
    feed = write_feed(tmp_path / "raw" / "candidates.jsonl", records)
    interim = tmp_path / "interim"

    class DyingClient(FakeHttpClient):
        def try_fetch(self, url: str, **kwargs: object):  # type: ignore[override]
            if len(self.calls) >= 4:
                raise KeyboardInterrupt("production batch killed")
            return super().try_fetch(url, **kwargs)

    client = DyingClient(responses={DANG_URL: load_fixture("dang_detail_axiom.html")})
    runner = OfficialUrlResolutionRunner(client=client, live=True, checkpoint_every=2)
    with pytest.raises(KeyboardInterrupt):
        runner.run(feed, interim_dir=interim)

    rows = read_output(interim)
    on_disk = json.loads((interim / RESOLUTION_REPORT_FILENAME).read_text(encoding="utf-8"))
    # The report describes the persisted file, not the pass that finished last.
    assert on_disk["output_records"] == len(rows) == 4
    # And it says out loud that the pass did not finish.
    assert on_disk["complete"] is False


def test_checkpoint_report_is_rewritten_during_the_pass(tmp_path: Path) -> None:
    """The report is refreshed at checkpoints, not only when a pass ends."""
    records = [raw(f"Tool {i}", listing_url=DANG_URL) for i in range(4)]
    feed = write_feed(tmp_path / "raw" / "candidates.jsonl", records)
    interim = tmp_path / "interim"
    seen: list[int] = []

    class WatchingClient(FakeHttpClient):
        def try_fetch(self, url: str, **kwargs: object):  # type: ignore[override]
            report_path = interim / RESOLUTION_REPORT_FILENAME
            if report_path.exists():
                seen.append(
                    json.loads(report_path.read_text(encoding="utf-8"))["output_records"]
                )
            return super().try_fetch(url, **kwargs)

    client = WatchingClient(responses={DANG_URL: load_fixture("dang_detail_axiom.html")})
    OfficialUrlResolutionRunner(client=client, live=True, checkpoint_every=2).run(
        feed, interim_dir=interim
    )
    # A report existed mid-pass and already counted the rows written by then.
    assert seen == [2, 2]


def test_completed_pass_reports_complete(tmp_path: Path) -> None:
    _, report, interim = run_stage(
        tmp_path, [raw("Axiom AI Work Assistant", listing_url=DANG_URL)]
    )
    assert report.complete is True
    on_disk = json.loads((interim / RESOLUTION_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["complete"] is True


def test_source_filtered_pass_reports_its_pending_scope(tmp_path: Path) -> None:
    """``pending`` is scoped to ``--source``, and the report says so.

    This is the second half of the f42c8b7 puzzle: a ``--source dang`` pass
    reported ``pending: 1710`` (the outstanding Dang.ai rows) which does not
    equal ``input_records - output_records`` across the whole feed. That is
    correct behaviour, so the scope has to be published with the number.
    """
    records = [
        raw("Axiom AI Work Assistant", listing_url=DANG_URL),
        raw("BonBon AI", listing_url=DANG_BONBON_URL),
        raw("Quantinor", source_key="creati", listing_url=CREATI_URL),
    ]
    _, report, interim = run_stage(tmp_path, records, sources={"dang"}, limit=1)

    # One Dang row written, one Dang row still pending; the Creati row is out
    # of scope and is therefore not counted as pending.
    assert report.output_records == 1
    assert report.pending == 1
    assert report.scope_sources == ["dang"]
    on_disk = json.loads((interim / RESOLUTION_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["pending_scope"] == "sources: dang"


def test_unscoped_pass_reports_the_whole_feed_as_its_scope(tmp_path: Path) -> None:
    _, report, interim = run_stage(
        tmp_path, [raw("Axiom AI Work Assistant", listing_url=DANG_URL)]
    )
    assert report.scope_sources is None
    on_disk = json.loads((interim / RESOLUTION_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["pending_scope"] == "whole feed"


def test_duplicate_raw_lines_make_output_records_smaller_than_input(
    tmp_path: Path,
) -> None:
    """One candidate spread over several raw lines collapses to one row.

    The production feed has 3,957 lines but only 3,946 distinct candidates, so
    ``output_records`` is legitimately below ``input_records`` even on a
    complete pass. The later line's website must win over the earlier blank
    one rather than the candidate being persisted twice.
    """
    first = raw("Quantinor", source_key="creati", listing_url=CREATI_URL)
    second = raw(
        "Quantinor",
        source_key="creati",
        listing_url=CREATI_URL,
        website="https://quantinor.example/",
    )
    out, report, _ = run_stage(tmp_path, [first, second])

    assert report.input_records == 2
    assert report.output_records == 1
    assert report.pending == 0
    assert len(out) == 1
    assert out[0]["website"]


def test_offline_runner_makes_no_network_call_and_resolves_nothing(tmp_path: Path) -> None:
    feed = write_feed(
        tmp_path / "raw" / "candidates.jsonl", [raw("Axiom", listing_url=DANG_URL)]
    )
    runner = OfficialUrlResolutionRunner(live=False)
    report = runner.run(feed, interim_dir=tmp_path / "interim")
    assert report.resolved == 0
    assert report.live is False
    # The offline client records what a live pass would have read.
    assert runner.client.attempts == [DANG_URL]


def test_record_key_is_stable_across_passes() -> None:
    record = raw("Axiom", listing_url=DANG_URL)
    assert record_key(record) == record_key(dict(record))
    assert record_key(record) != record_key(raw("Other", listing_url=DANG_URL))
    assert record_key({"candidate_id": "abc"}) == "abc"
