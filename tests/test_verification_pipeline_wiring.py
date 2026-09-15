"""Tests for wiring official-site verification into the production pipeline.

This suite covers the *integration* contract, not the verification rules
(those are owned by ``test_verification_official_site.py`` and are deliberately
not re-asserted here):

* ``ToolsPipeline.verify_candidates`` sits between candidate preparation and
  normalization and consumes **prepared candidates**;
* discovery provenance and official verification evidence are persisted as two
  separate blocks and never merged;
* artefacts land in ``data/interim/`` and **never** in ``data/final/``;
* ``--live`` gates network access: without it, not one request is made;
* ``--limit`` caps how many candidates are processed;
* a candidate without an official URL is not fetched at all — no substitute
  directory request is ever issued;
* the CLI reports honestly and never fabricates input.

Entirely offline: a fake HTTP client, in-memory candidates and ``tmp_path``
artefacts. No live network calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import run as cli
from src.candidates.prepare import CandidatePreparer, PreparedCandidate
from src.core.config import Settings
from src.core.errors import VerificationError
from src.core.http_client import FetchResult, OfflineHttpClient
from src.discovery.base import CandidateTool
from src.models.enums import VerificationStatus
from src.pipeline import STAGES, ToolsPipeline
from src.verification.store import (
    VERIFICATION_REPORT_FILENAME,
    VERIFIED_FILENAME,
    discovery_provenance,
    load_prepared_candidates,
    persist_verification,
    verification_record,
)
from src.verification.verifier import OfficialSiteVerifier, VerificationFailure

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SEEN_AT = datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc)

OFFICIAL = "https://jasper.ai/"
LISTING = "https://theresanaiforthat.com/ai/jasper/"
#: What ``normalize_url`` makes of ``LISTING`` (a trailing slash is dropped on
#: non-root paths), i.e. the form preparation actually persists.
LISTING_NORMALIZED = "https://theresanaiforthat.com/ai/jasper"


# ----------------------------------------------------------------- fixtures
def _filler() -> str:
    sentence = (
        "Jasper helps marketing teams draft, edit and repurpose campaign copy "
        "with brand voice controls and collaborative review. "
    )
    return sentence * 8


OFFICIAL_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <title>Jasper — AI copilot for marketing teams</title>
  <meta property="og:site_name" content="Jasper" />
</head>
<body>
  <nav>
    <a href="/pricing">Pricing</a>
    <a href="/signup">Get started free</a>
    <a href="/login">Log in</a>
  </nav>
  <h1>Jasper builds on-brand marketing content</h1>
  <p>{_filler()}</p>
</body>
</html>"""


@dataclass
class RecordingClient:
    """Fake HTTP client that records every URL it is asked to fetch."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def try_fetch(self, url: str, **_: Any) -> FetchResult | None:
        self.calls.append(url)
        text = self.responses.get(url)
        if text is None:
            return None
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            text=text,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        result = self.try_fetch(url, **kwargs)
        if result is None:
            from src.core.errors import FetchError

            raise FetchError(f"no stub for {url}", url=url)
        return result

    def head(self, url: str, **kwargs: Any) -> FetchResult | None:
        return self.try_fetch(url, **kwargs)

    def close(self) -> None:  # pragma: no cover - parity with the real client
        return None


def settings_for(tmp_path: Path) -> Settings:
    """Settings whose whole ``data/`` tree lives inside ``tmp_path``."""
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.dry_run = True
    settings.paths.ensure()
    return settings


def prepared_candidate(**overrides: Any) -> PreparedCandidate:
    """A real ``PreparedCandidate``, built by the real preparer."""
    kwargs: dict[str, Any] = {
        "name": "Jasper",
        "source_key": "taaft",
        "source_name": "There's An AI For That",
        "listing_url": LISTING,
        "website": OFFICIAL,
        "tagline": "AI writing assistant for marketing teams",
        "categories": ["AI Writing"],
        "raw_signals": {"directory_saves": 1200},
        "raw_payload": {},
        "discovered_at": SEEN_AT,
    }
    kwargs.update(overrides)
    prepared, rejected = CandidatePreparer(now=FIXED_NOW).prepare(CandidateTool(**kwargs))
    assert rejected is None and prepared is not None
    return prepared


def pipeline_with(
    tmp_path: Path, client: RecordingClient | None = None
) -> tuple[ToolsPipeline, RecordingClient | None]:
    settings = settings_for(tmp_path)
    verifier = (
        OfficialSiteVerifier(client=client, now=FIXED_NOW)  # type: ignore[arg-type]
        if client is not None
        else None
    )
    return ToolsPipeline(settings, verifier=verifier), client


# ========================================================== stage placement
class TestStagePlacement:
    def test_stage_sits_between_preparation_and_normalization(self) -> None:
        assert STAGES.index("candidate_preparation") < STAGES.index("verify_candidates")
        assert STAGES.index("verify_candidates") < STAGES.index("normalization")

    def test_stage_is_recorded_in_the_run_report(self, tmp_path: Path) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        pipeline.verify_candidates([prepared_candidate()], live=True)

        stage = pipeline.report.stage("verify_candidates")
        assert stage.input_count == 1
        assert stage.output_count == 1
        assert stage.started_at and stage.finished_at
        assert stage.details["report"]["verified"] == 1

    def test_full_run_executes_the_stage_offline(self, tmp_path: Path) -> None:
        """A dry run wires the stage in without making a single request."""
        pipeline, _ = pipeline_with(tmp_path)
        candidate = CandidateTool(
            name="Jasper",
            source_key="taaft",
            source_name="There's An AI For That",
            listing_url=LISTING,
            website=OFFICIAL,
            discovered_at=SEEN_AT,
        )
        report = pipeline.run(candidates=[candidate], persist=True)

        names = [stats.name for stats in report.stages]
        assert names.index("verify_candidates") < names.index("normalization")
        stage = pipeline.report.stage("verify_candidates")
        assert stage.details["live"] is False
        assert stage.details["report"]["fetched"] == 0
        assert (tmp_path / "data" / "interim" / VERIFIED_FILENAME).exists()

    def test_full_run_verify_limit_is_honoured(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        candidates = [
            CandidateTool(
                name=f"Tool {index}",
                source_key="creati",
                source_name="Creati.ai",
                website=f"https://tool{index}.example.com/",
                discovered_at=SEEN_AT,
            )
            for index in range(5)
        ]
        pipeline.run(candidates=candidates, persist=True, verify_limit=2)

        stage = pipeline.report.stage("verify_candidates")
        assert stage.input_count == 5
        assert stage.details["considered"] == 2
        assert stage.details["skipped_by_limit"] == 3


# ============================================================ --live gating
class TestLiveGating:
    def test_offline_pass_makes_no_network_calls(self, tmp_path: Path) -> None:
        """Without ``live=True`` the client cannot reach the network at all."""
        pipeline, _ = pipeline_with(tmp_path)
        verifier = pipeline._verifier_for(live=False)

        assert isinstance(verifier.client, OfflineHttpClient)

        results, report = pipeline.verify_candidates([prepared_candidate()], live=False)

        assert report.fetched_count == 0
        assert results[0].fetched is False
        assert results[0].verified is False
        assert results[0].status == VerificationStatus.UNREACHABLE
        assert VerificationFailure.FETCH_FAILED in results[0].failures
        # The URL a live run *would* have fetched is still recorded.
        assert verifier.client.attempts == []  # this instance was never used

    def test_offline_client_records_attempts_but_returns_nothing(self) -> None:
        client = OfflineHttpClient()
        assert client.try_fetch(OFFICIAL) is None
        assert client.attempts == [OFFICIAL]

    def test_offline_client_fetch_raises_rather_than_connecting(self) -> None:
        from src.core.errors import FetchError

        client = OfflineHttpClient()
        with pytest.raises(FetchError):
            client.fetch(OFFICIAL)

    def test_offline_pass_is_flagged_in_the_report(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        pipeline.verify_candidates([prepared_candidate()], live=False)

        stage = pipeline.report.stage("verify_candidates")
        assert "offline" in stage.details
        assert any("offline" in note for note in pipeline.report.notes)

    def test_live_pass_uses_the_real_client_by_default(self, tmp_path: Path) -> None:
        """``live=True`` must not silently stay offline."""
        from src.core.http_client import HttpClient

        pipeline, _ = pipeline_with(tmp_path)
        verifier = pipeline._verifier_for(live=True)

        assert isinstance(verifier.client, HttpClient)
        assert not isinstance(verifier.client, OfflineHttpClient)

    def test_live_pass_verifies_from_page_evidence(self, tmp_path: Path) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        results, report = pipeline.verify_candidates([prepared_candidate()], live=True)

        assert client.calls == [OFFICIAL]
        assert results[0].status == VerificationStatus.VERIFIED
        assert report.verified_count == 1


# ================================================================== --limit
class TestLimit:
    def _batch(self, count: int) -> list[PreparedCandidate]:
        return [
            prepared_candidate(
                name=f"Tool {index}",
                website=f"https://tool{index}.example.com/",
                listing_url=None,
            )
            for index in range(count)
        ]

    def test_limit_caps_processed_candidates(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        results, report = pipeline.verify_candidates(self._batch(10), limit=3)

        assert len(results) == 3
        assert report.input_count == 3
        stage = pipeline.report.stage("verify_candidates")
        assert stage.input_count == 10
        assert stage.details["considered"] == 3
        assert stage.details["skipped_by_limit"] == 7

    def test_limit_caps_network_calls_too(self, tmp_path: Path) -> None:
        """The limit must bound real requests, not just reported rows."""
        client = RecordingClient()
        pipeline, _ = pipeline_with(tmp_path, client)
        pipeline.verify_candidates(self._batch(10), live=True, limit=4)

        assert len(client.calls) == 4

    def test_no_limit_processes_everything(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        results, _ = pipeline.verify_candidates(self._batch(6), limit=None)

        assert len(results) == 6

    def test_limit_larger_than_batch_is_harmless(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        results, _ = pipeline.verify_candidates(self._batch(2), limit=50)

        assert len(results) == 2


# ================================================== missing official URL
class TestMissingOfficialUrl:
    def test_candidate_without_official_url_is_never_fetched(self, tmp_path: Path) -> None:
        """No substitute directory request may be issued (guideline §10)."""
        client = RecordingClient(responses={LISTING: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        candidate = prepared_candidate(website=None, listing_url=LISTING)
        assert candidate.website is None

        results, report = pipeline.verify_candidates([candidate], live=True)

        assert client.calls == [], "nothing may be fetched without an official URL"
        assert LISTING not in client.calls
        assert results[0].status == VerificationStatus.FAILED
        assert VerificationFailure.NO_OFFICIAL_URL in results[0].failures
        assert report.fetched_count == 0

    def test_mixed_batch_only_fetches_candidates_with_official_urls(
        self, tmp_path: Path
    ) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        batch = [
            prepared_candidate(),
            prepared_candidate(name="NoSite", website=None, listing_url=LISTING),
        ]
        results, _ = pipeline.verify_candidates(batch, live=True)

        assert client.calls == [OFFICIAL]
        stage = pipeline.report.stage("verify_candidates")
        assert stage.details["with_official_url"] == 1
        assert results[0].verified is True
        assert results[1].verified is False


# ============================================================= persistence
class TestPersistence:
    def test_artefacts_are_written_under_interim(self, tmp_path: Path) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        pipeline.verify_candidates([prepared_candidate()], live=True, persist=True)

        interim = tmp_path / "data" / "interim"
        assert (interim / VERIFIED_FILENAME).exists()
        assert (interim / VERIFICATION_REPORT_FILENAME).exists()

    def test_nothing_is_written_to_final(self, tmp_path: Path) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        pipeline.verify_candidates([prepared_candidate()], live=True, persist=True)

        final = tmp_path / "data" / "final"
        assert list(final.glob("*candidates_verified*")) == []
        assert not (final / VERIFIED_FILENAME).exists()
        assert not (final / VERIFICATION_REPORT_FILENAME).exists()

    def test_persisting_into_final_is_refused(self, tmp_path: Path) -> None:
        """``data/final/`` is reserved for curated, published records."""
        with pytest.raises(VerificationError, match="final"):
            persist_verification([], None, interim_dir=tmp_path / "data" / "final")

    def test_persisted_row_separates_discovery_from_verification(
        self, tmp_path: Path
    ) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        pipeline, _ = pipeline_with(tmp_path, client)
        pipeline.verify_candidates([prepared_candidate()], live=True, persist=True)

        rows = [
            json.loads(line)
            for line in (tmp_path / "data" / "interim" / VERIFIED_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        row = rows[0]

        # two distinct blocks, neither one bleeding into the other
        assert set(row["discovery"]) >= {
            "listing_url",
            "source_keys",
            "discovery_sources",
            "discovered_at",
        }
        assert row["discovery"]["listing_url"] == LISTING_NORMALIZED
        assert row["discovery"]["discovery_sources"][0]["kind"] == "directory"

        assert set(row["verification"]) >= {
            "official_url",
            "http_status",
            "identity_signals",
            "product_signals",
            "failures",
        }
        assert row["verification"]["official_url"] == OFFICIAL
        assert row["verification"]["verification_source"]["kind"] == "official"
        # the discovery block carries no page evidence
        assert "identity_signals" not in row["discovery"]
        assert "product_signals" not in row["discovery"]
        # and the verification block carries no directory claim
        assert "listing_url" not in row["verification"]
        assert "discovery_sources" not in row["verification"]

    def test_persisted_row_records_whether_the_pass_was_live(
        self, tmp_path: Path
    ) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        pipeline.verify_candidates([prepared_candidate()], live=False, persist=True)

        row = json.loads(
            (tmp_path / "data" / "interim" / VERIFIED_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        assert row["live"] is False
        assert row["verified"] is False

    def test_report_records_the_run_parameters(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        pipeline.verify_candidates(
            [prepared_candidate(), prepared_candidate(name="Other")],
            live=False,
            limit=1,
            persist=True,
        )

        payload = json.loads(
            (tmp_path / "data" / "interim" / VERIFICATION_REPORT_FILENAME)
            .read_text(encoding="utf-8")
        )
        assert payload["live"] is False
        assert payload["limit"] == 1
        assert payload["input_candidates"] == 2
        assert payload["considered"] == 1
        assert payload["input"] == 1
        assert "status_counts" in payload
        assert payload["artefacts"]["verified_candidates"].endswith(VERIFIED_FILENAME)

    def test_persist_can_be_disabled(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        pipeline.verify_candidates([prepared_candidate()], persist=False)

        assert not (tmp_path / "data" / "interim" / VERIFIED_FILENAME).exists()

    def test_artefacts_are_registered_on_the_run_report(self, tmp_path: Path) -> None:
        pipeline, _ = pipeline_with(tmp_path)
        pipeline.verify_candidates([prepared_candidate()], persist=True)

        assert "verified_candidates" in pipeline.report.artefacts

    def test_full_run_keeps_verification_artefacts_in_the_report(
        self, tmp_path: Path
    ) -> None:
        """The later ``_persist`` must not clobber earlier artefact entries."""
        pipeline, _ = pipeline_with(tmp_path)
        candidate = CandidateTool(
            name="Jasper",
            source_key="taaft",
            source_name="There's An AI For That",
            website=OFFICIAL,
            discovered_at=SEEN_AT,
        )
        report = pipeline.run(candidates=[candidate], persist=True)

        assert "verified_candidates" in report.artefacts
        assert "final_tools" in report.artefacts


# =================================================== provenance + records
class TestProvenanceRecords:
    def test_discovery_provenance_is_copied_verbatim(self) -> None:
        candidate = prepared_candidate()
        block = discovery_provenance(candidate)

        assert block["candidate_id"] == candidate.candidate_id
        assert block["website"] == OFFICIAL
        assert block["listing_url"] == LISTING_NORMALIZED
        assert block["source_keys"] == ["taaft"]
        assert block["identity_basis"] == candidate.identity_basis
        assert block["discovered_at"] == candidate.discovered_at

    def test_provenance_survives_a_hostile_record(self) -> None:
        class Exploding:
            candidate_id = "boom"

            @property
            def name(self) -> str:
                raise RuntimeError("bad record")

        block = discovery_provenance(Exploding())
        assert block["candidate_id"] == "boom"
        assert block["name"] is None

    def test_record_is_json_serialisable(self, tmp_path: Path) -> None:
        client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
        verifier = OfficialSiteVerifier(client=client, now=FIXED_NOW)  # type: ignore[arg-type]
        candidate = prepared_candidate()
        result = verifier.verify_candidate(candidate)

        payload = json.loads(
            json.dumps(verification_record(candidate, result, live=True), default=str)
        )
        assert payload["verification_status"] == "verified"
        assert payload["live"] is True

    def test_verification_is_deterministic(self, tmp_path: Path) -> None:
        def once() -> dict[str, Any]:
            client = RecordingClient(responses={OFFICIAL: OFFICIAL_PAGE})
            verifier = OfficialSiteVerifier(client=client, now=FIXED_NOW)  # type: ignore[arg-type]
            candidate = prepared_candidate()
            result = verifier.verify_candidate(candidate)
            return verification_record(candidate, result, live=True)

        assert once() == once()


# ======================================================= prepared reload
class TestPreparedReload:
    def test_round_trips_prepared_candidates(self, tmp_path: Path) -> None:
        from src.core.io import write_jsonl

        original = prepared_candidate()
        path = tmp_path / "candidates_prepared.jsonl"
        write_jsonl(path, [original.to_dict()])

        loaded, source = load_prepared_candidates(path)

        assert source == str(path)
        assert len(loaded) == 1
        assert loaded[0].candidate_id == original.candidate_id
        assert loaded[0].website == OFFICIAL
        assert loaded[0].listing_url == LISTING_NORMALIZED
        assert loaded[0].discovery_sources[0].kind == "directory"

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        loaded, _ = load_prepared_candidates(tmp_path / "absent.jsonl")
        assert loaded == []

    def test_unusable_rows_are_skipped_not_patched(self, tmp_path: Path) -> None:
        path = tmp_path / "candidates_prepared.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"name": "No id"}),
                    json.dumps({"candidate_id": "x"}),
                    "{ not json",
                    json.dumps({"candidate_id": "ok", "name": "Jasper"}),
                ]
            ),
            encoding="utf-8",
        )
        loaded, _ = load_prepared_candidates(path)

        assert [c.candidate_id for c in loaded] == ["ok"]


# ===================================================================== CLI
class TestCli:
    def test_verify_subcommand_exists_with_live_and_limit(self) -> None:
        args = cli.build_parser().parse_args(["verify"])
        assert args.func is cli.cmd_verify
        assert args.live is False, "network access must be opt-in"
        assert args.limit is None

    def test_live_and_limit_flags_parse(self) -> None:
        args = cli.build_parser().parse_args(["verify", "--live", "--limit", "10"])
        assert args.live is True
        assert args.limit == 10

    def test_cli_offline_run_makes_no_network_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The default CLI path must never open a socket."""
        from src.core.io import write_jsonl

        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_jsonl(
            interim / "candidates_prepared.jsonl",
            [prepared_candidate().to_dict()],
        )
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        def explode(*_: Any, **__: Any) -> None:
            raise AssertionError("HttpClient must not be constructed without --live")

        monkeypatch.setattr("src.verification.verifier.HttpClient", explode)

        exit_code = cli.main(["verify"])
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert payload["live"] is False
        assert payload["report"]["fetched"] == 0
        assert payload["report"]["verified"] == 0
        assert (interim / VERIFIED_FILENAME).exists()

    def test_cli_limit_is_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from src.core.io import write_jsonl

        settings = settings_for(tmp_path)
        write_jsonl(
            settings.paths.resolve("interim") / "candidates_prepared.jsonl",
            [
                prepared_candidate(
                    name=f"Tool {index}",
                    website=f"https://tool{index}.example.com/",
                    listing_url=None,
                ).to_dict()
                for index in range(8)
            ],
        )
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify", "--limit", "2"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["prepared_candidates"] == 8
        assert payload["considered"] == 2
        assert payload["report"]["input"] == 2

    def test_cli_reports_honestly_when_there_is_no_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        settings = settings_for(tmp_path)
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        exit_code = cli.main(["verify"])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert json.loads(captured.out)["prepared_candidates"] == 0
        assert "nothing is fabricated" in captured.err

    def test_cli_no_persist_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from src.core.io import write_jsonl

        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_jsonl(interim / "candidates_prepared.jsonl", [prepared_candidate().to_dict()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify", "--no-persist"]) == 0
        capsys.readouterr()

        assert not (interim / VERIFIED_FILENAME).exists()
