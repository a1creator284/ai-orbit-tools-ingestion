"""Candidate store tests: resilient rehydration of raw discovery artefacts.

All offline. Covers the contract that matters for the pipeline: a reload must
never fabricate data, never crash on malformed input, and must preserve the
discovery provenance verbatim.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.candidates.store import (
    CandidateLoadReport,
    SkipReason,
    candidate_from_dict,
    load_candidate_file,
    load_discovery_dir,
)


def write_jsonl_raw(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def record(**overrides) -> dict:
    base = {
        "name": "Jasper",
        "source_key": "taaft",
        "source_name": "There's An AI For That",
        "listing_url": "https://theresanaiforthat.com/ai/jasper/",
        "website": "https://www.jasper.ai/",
        "tagline": "AI writing assistant",
        "categories": ["AI Writing"],
        "raw_signals": {"directory_saves": 1200},
        "raw_payload": {"discovery_method": "html_listing", "plan": "all"},
        "discovered_at": "2026-09-15T10:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestCandidateFromDict:
    def test_round_trips_a_persisted_candidate(self) -> None:
        candidate = candidate_from_dict(record())
        assert candidate is not None
        assert candidate.name == "Jasper"
        assert candidate.website == "https://jasper.ai/"
        assert candidate.categories == ["AI Writing"]
        assert candidate.source_key == "taaft"

    def test_preserves_provenance_verbatim(self) -> None:
        candidate = candidate_from_dict(record())
        assert candidate is not None
        assert candidate.raw_signals == {"directory_saves": 1200}
        assert candidate.raw_payload["discovery_method"] == "html_listing"
        assert candidate.raw_payload["plan"] == "all"

    def test_keeps_the_original_observation_time(self) -> None:
        candidate = candidate_from_dict(record())
        assert candidate is not None
        assert candidate.discovered_at == datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)

    def test_unparseable_timestamp_is_reported_not_guessed(self) -> None:
        report = CandidateLoadReport()
        candidate = candidate_from_dict(record(discovered_at="last tuesday"), report=report)
        assert candidate is not None
        assert "discovered_at_unparseable" in report.field_repairs

    def test_missing_name_is_skipped(self) -> None:
        report = CandidateLoadReport()
        assert candidate_from_dict(record(name="  "), report=report) is None
        assert report.skip_reasons[SkipReason.NO_NAME] == 1

    def test_record_without_any_pointer_is_skipped(self) -> None:
        report = CandidateLoadReport()
        assert (
            candidate_from_dict(record(website=None, listing_url=None), report=report) is None
        )
        assert report.skip_reasons[SkipReason.NO_POINTER] == 1

    def test_non_mapping_is_skipped(self) -> None:
        report = CandidateLoadReport()
        assert candidate_from_dict(["not", "a", "record"], report=report) is None
        assert report.skip_reasons[SkipReason.NOT_A_MAPPING] == 1

    def test_source_key_is_recovered_from_the_filename_only(self) -> None:
        report = CandidateLoadReport()
        assert candidate_from_dict(record(source_key=None), report=report) is None
        assert report.skip_reasons[SkipReason.NO_SOURCE_KEY] == 1

        recovered = candidate_from_dict(record(source_key=None), default_source_key="creati")
        assert recovered is not None
        assert recovered.source_key == "creati"

    def test_malformed_field_types_do_not_raise(self) -> None:
        candidate = candidate_from_dict(
            record(categories={"a": "AI Writing", "b": None}, raw_signals="nope", raw_payload=7)
        )
        assert candidate is not None
        assert candidate.categories == ["AI Writing"]
        assert candidate.raw_signals == {}
        assert candidate.raw_payload == {}

    def test_no_verified_status_is_granted_by_loading(self) -> None:
        candidate = candidate_from_dict(record())
        assert candidate is not None
        # a candidate carries no verification field at all — verification is a
        # later stage working against the official website
        assert not hasattr(candidate, "verification")


class TestLoadCandidateFile:
    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        path = write_jsonl_raw(
            tmp_path / "taaft.jsonl",
            [
                json.dumps(record()),
                "{ this is not json",
                json.dumps(record(name="Copy.ai", website="https://copy.ai/")),
            ],
        )
        report = CandidateLoadReport()
        candidates = load_candidate_file(path, report=report)
        assert [c.name for c in candidates] == ["Jasper", "Copy.ai"]
        assert report.records_seen == 2
        assert report.candidates_loaded == 2

    def test_missing_file_is_reported_not_fatal(self, tmp_path: Path) -> None:
        report = CandidateLoadReport()
        assert load_candidate_file(tmp_path / "absent.jsonl", report=report) == []
        assert report.files_missing == [str(tmp_path / "absent.jsonl")]

    def test_empty_file_yields_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        candidates = load_candidate_file(path)
        assert candidates == []


class TestLoadDiscoveryDir:
    def _seed(self, tmp_path: Path) -> Path:
        discovery = tmp_path / "discovery"
        write_jsonl_raw(discovery / "taaft.jsonl", [json.dumps(record())])
        write_jsonl_raw(
            discovery / "creati.jsonl",
            [
                json.dumps(
                    record(
                        name="Jasper AI",
                        source_key="creati",
                        source_name="Creati.ai",
                        website="https://jasper.ai/pricing",
                        listing_url=None,
                    )
                ),
                json.dumps(record(name="Broken", website=None, listing_url=None)),
            ],
        )
        return tmp_path

    def test_loads_every_source_and_preserves_both_sightings(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        candidates, report = load_discovery_dir(root)
        # the same product seen twice is NOT merged here — merging is explicit
        assert len(candidates) == 2
        assert {c.source_key for c in candidates} == {"taaft", "creati"}
        assert report.skip_reasons[SkipReason.NO_POINTER] == 1
        assert report.records_seen == 3

    def test_can_restrict_to_specific_sources(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        candidates, _ = load_discovery_dir(root, source_keys=["taaft"])
        assert [c.source_key for c in candidates] == ["taaft"]

    def test_the_merged_feed_is_not_read_as_a_source(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        candidates, _ = load_discovery_dir(root, source_keys=["taaft", "creati"])
        assert all(c.source_key in {"taaft", "creati"} for c in candidates)

    def test_missing_directory_is_reported_not_fatal(self, tmp_path: Path) -> None:
        candidates, report = load_discovery_dir(tmp_path / "nowhere")
        assert candidates == []
        assert report.files_missing

    def test_report_is_json_serialisable(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        _, report = load_discovery_dir(root)
        assert json.loads(json.dumps(report.to_dict()))["records_seen"] == 3
