"""Candidate preparation tests.

Contract under test:

* normalization is stable and deterministic;
* required candidate identity is validated (name + at least one pointer);
* provenance survives preparation;
* nothing is invented — missing values stay ``None``/empty and are *flagged*;
* prepared candidates are explicitly unverified and never land in
  ``data/final/``.

Entirely offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.candidates.prepare import (
    INTERIM_FILENAME,
    UNVERIFIED,
    CandidateIssue,
    CandidatePreparer,
    RejectReason,
)
from src.discovery.base import CandidateTool

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SEEN_AT = datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc)


def candidate(**overrides) -> CandidateTool:
    kwargs = {
        "name": "Jasper",
        "source_key": "taaft",
        "source_name": "There's An AI For That",
        "listing_url": "https://theresanaiforthat.com/ai/jasper/",
        "website": "https://www.jasper.ai/pricing",
        "tagline": "AI writing assistant for marketing teams",
        "categories": ["AI Writing", "ai writing", "Copywriting"],
        "raw_signals": {"directory_saves": 1200},
        "raw_payload": {},
        "discovered_at": SEEN_AT,
    }
    kwargs.update(overrides)
    return CandidateTool(**kwargs)


def preparer() -> CandidatePreparer:
    return CandidatePreparer(now=FIXED_NOW)


class TestNormalization:
    def test_normalizes_identity_and_urls(self) -> None:
        prepared, rejected = preparer().prepare(candidate())
        assert rejected is None
        assert prepared is not None
        assert prepared.name == "Jasper"
        assert prepared.website == "https://jasper.ai/pricing"
        assert prepared.website_domain == "jasper.ai"
        assert prepared.identity_basis == "website_domain"
        assert prepared.identity_confidence == "high"
        assert prepared.slug == "jasper"
        assert prepared.name_key == "jasper"

    def test_categories_are_deduplicated_case_insensitively_in_order(self) -> None:
        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        assert prepared.categories == ["AI Writing", "Copywriting"]

    def test_is_deterministic_for_url_variants(self) -> None:
        a, _ = preparer().prepare(candidate(name="Jasper", website="https://www.jasper.ai/"))
        b, _ = preparer().prepare(candidate(name="Jasper AI", website="http://jasper.ai/pricing"))
        assert a is not None and b is not None
        assert a.candidate_id == b.candidate_id
        assert a.identity_key == b.identity_key == "jasper.ai"

    def test_repeated_preparation_gives_an_identical_record(self) -> None:
        first, _ = preparer().prepare(candidate())
        second, _ = preparer().prepare(candidate())
        assert first is not None and second is not None
        assert first.to_dict() == second.to_dict()

    def test_observed_description_is_kept_verbatim_not_rewritten(self) -> None:
        prepared, _ = preparer().prepare(
            candidate(tagline="  AI writing   assistant for marketing teams ")
        )
        assert prepared is not None
        assert prepared.observed_description == "AI writing assistant for marketing teams"


class TestRequiredIdentity:
    def test_name_is_required(self) -> None:
        prepared, rejected = preparer().prepare(candidate(name="   "))
        assert prepared is None
        assert rejected is not None
        assert rejected.reason == RejectReason.NO_NAME

    def test_at_least_one_pointer_is_required(self) -> None:
        prepared, rejected = preparer().prepare(candidate(website=None, listing_url=None))
        assert prepared is None
        assert rejected is not None
        assert rejected.reason == RejectReason.NO_POINTER

    def test_listing_only_candidate_is_kept_but_flagged(self) -> None:
        prepared, rejected = preparer().prepare(candidate(website=None))
        assert rejected is None
        assert prepared is not None
        # discovery prefers under-merging / not losing real products
        assert prepared.website is None
        # The directory detail URL identifies the product only *inside* that
        # directory, so the basis is listing-scoped and only medium confidence.
        assert prepared.identity_basis == "listing_product_url"
        assert prepared.identity_confidence == "medium"
        assert CandidateIssue.NO_OFFICIAL_URL in prepared.issues
        assert prepared.needs_review is True
        assert prepared.review_notes


class TestNoFabrication:
    def test_missing_description_is_flagged_not_generated(self) -> None:
        prepared, _ = preparer().prepare(candidate(tagline=None))
        assert prepared is not None
        assert prepared.observed_description is None
        assert CandidateIssue.DESCRIPTION_MISSING in prepared.issues

    def test_short_description_is_flagged_not_padded(self) -> None:
        prepared, _ = preparer().prepare(candidate(tagline="Writes copy"))
        assert prepared is not None
        assert prepared.observed_description == "Writes copy"
        assert CandidateIssue.DESCRIPTION_TOO_SHORT in prepared.issues

    def test_missing_categories_are_flagged_not_invented(self) -> None:
        prepared, _ = preparer().prepare(candidate(categories=[]))
        assert prepared is not None
        assert prepared.categories == []
        assert CandidateIssue.NO_CATEGORIES in prepared.issues

    def test_no_pricing_or_launch_date_fields_exist_at_this_stage(self) -> None:
        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        payload = prepared.to_dict()
        for forbidden in ("pricing", "launch_date", "status", "key_features", "monthly_visits"):
            assert forbidden not in payload

    def test_sponsored_placement_is_recorded_as_a_caveat(self) -> None:
        prepared, _ = preparer().prepare(
            candidate(raw_signals={"directory_sponsored_placement": True})
        )
        assert prepared is not None
        assert CandidateIssue.SPONSORED_PLACEMENT in prepared.issues

    def test_name_with_a_tagline_separator_is_flagged_for_review(self) -> None:
        prepared, _ = preparer().prepare(candidate(name="Jasper | AI Writing Assistant"))
        assert prepared is not None
        assert CandidateIssue.NAME_LOOKS_LIKE_TAGLINE in prepared.issues
        assert prepared.needs_review is True


class TestUnverifiedSeparation:
    def test_every_prepared_candidate_is_unverified(self) -> None:
        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        assert prepared.verification_status == UNVERIFIED

    def test_a_directory_listing_never_marks_a_candidate_verified(self) -> None:
        prepared, _ = preparer().prepare(
            candidate(raw_signals={"directory_saves": 99999, "directory_rank": 1})
        )
        assert prepared is not None
        assert prepared.verification_status == UNVERIFIED

    def test_prepared_records_are_not_tool_records(self) -> None:
        from src.models.tool import Tool

        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        assert not isinstance(prepared, Tool)


class TestProvenance:
    def test_single_source_provenance_is_preserved(self) -> None:
        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        assert prepared.source_keys == ["taaft"]
        assert len(prepared.discovery_sources) == 1
        ref = prepared.discovery_sources[0]
        assert ref.name == "There's An AI For That"
        assert ref.url == "https://theresanaiforthat.com/ai/jasper"
        assert ref.kind == "directory"

    def test_merged_candidate_keeps_every_contributing_source(self) -> None:
        merged = candidate(
            raw_payload={
                "contributing_sources": [
                    {
                        "source_key": "taaft",
                        "source_name": "There's An AI For That",
                        "listing_url": "https://theresanaiforthat.com/ai/jasper/",
                        "discovered_at": "2026-09-15T10:30:00+00:00",
                    },
                    {
                        "source_key": "creati",
                        "source_name": "Creati.ai",
                        "listing_url": "https://creati.ai/ai-tools/jasper/",
                        "discovered_at": "2026-09-15T10:31:00+00:00",
                    },
                ]
            }
        )
        prepared, _ = preparer().prepare(merged)
        assert prepared is not None
        assert prepared.source_keys == ["taaft", "creati"]
        assert [s.name for s in prepared.discovery_sources] == [
            "There's An AI For That",
            "Creati.ai",
        ]

    def test_raw_signals_travel_through_untouched(self) -> None:
        prepared, _ = preparer().prepare(candidate(raw_signals={"directory_saves": 1200}))
        assert prepared is not None
        assert prepared.raw_signals == {"directory_saves": 1200}

    def test_observation_time_is_preserved(self) -> None:
        prepared, _ = preparer().prepare(candidate())
        assert prepared is not None
        assert prepared.discovered_at == SEEN_AT.isoformat()
        assert prepared.prepared_at == FIXED_NOW.isoformat()

    def test_malformed_contributing_sources_are_ignored_safely(self) -> None:
        prepared, _ = preparer().prepare(
            candidate(raw_payload={"contributing_sources": ["nope", {"source_key": None}, 7]})
        )
        assert prepared is not None
        # falls back to the candidate's own source rather than losing provenance
        assert [s.name for s in prepared.discovery_sources] == ["There's An AI For That"]


class TestBatchAndReport:
    def test_batch_separates_prepared_from_rejected(self) -> None:
        prepared, rejected, report = preparer().prepare_many(
            [
                candidate(),
                candidate(name="Copy.ai", website="https://copy.ai/"),
                candidate(name="", website=None, listing_url=None),
            ]
        )
        assert len(prepared) == 2
        assert len(rejected) == 1
        assert report.input_count == 3
        assert report.prepared_count == 2
        assert report.rejected_count == 1
        assert report.reject_reasons[RejectReason.NO_NAME] == 1

    def test_report_counts_identity_bases_and_issues(self) -> None:
        _, _, report = preparer().prepare_many(
            [candidate(), candidate(name="NoSite", website=None, tagline=None)]
        )
        assert report.identity_bases["website_domain"] == 1
        assert report.identity_bases["listing_product_url"] == 1
        assert report.with_official_url == 1
        assert report.issue_counts[CandidateIssue.NO_OFFICIAL_URL] == 1
        assert report.needs_review_count == 1

    def test_report_is_json_serialisable(self) -> None:
        _, _, report = preparer().prepare_many([candidate()])
        assert json.loads(json.dumps(report.to_dict()))["prepared"] == 1

    def test_one_broken_candidate_does_not_abort_the_batch(self) -> None:
        class Exploding:
            name = "Boom"
            source_key = "taaft"

            def __getattr__(self, item):  # noqa: ANN001
                raise RuntimeError("corrupt candidate")

        prepared, rejected, report = preparer().prepare_many([Exploding(), candidate()])
        assert len(prepared) == 1
        assert rejected[0].reason == RejectReason.PREPARATION_FAILED
        assert report.prepared_count == 1


class TestPersistence:
    def test_writes_to_interim_never_to_final(self, tmp_path: Path) -> None:
        prep = preparer()
        prepared, rejected, report = prep.prepare_many(
            [candidate(), candidate(name="", website=None, listing_url=None)]
        )
        interim = tmp_path / "interim"
        report = prep.persist(prepared, report, interim_dir=interim, rejected=rejected)

        prepared_path = interim / INTERIM_FILENAME
        assert prepared_path.exists()
        assert "final" not in str(prepared_path)

        rows = [json.loads(line) for line in prepared_path.read_text().splitlines() if line]
        assert len(rows) == 1
        assert rows[0]["verification_status"] == UNVERIFIED
        assert rows[0]["candidate_id"]

        assert (interim / "candidates_rejected.jsonl").exists()
        assert Path(report.artefacts["preparation_report"]).exists()

    def test_persisted_report_matches_the_run(self, tmp_path: Path) -> None:
        prep = preparer()
        prepared, rejected, report = prep.prepare_many([candidate()])
        prep.persist(prepared, report, interim_dir=tmp_path, rejected=rejected)
        saved = json.loads((tmp_path / "candidates_prepared_report.json").read_text())
        assert saved["prepared"] == 1
        assert saved["rejected"] == 0
