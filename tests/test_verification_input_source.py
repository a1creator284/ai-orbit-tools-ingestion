"""Regression tests for the *input* verification consumes in production.

Verification must check the **grounded official URLs** produced by the
official-URL resolution stage, i.e. ``data/interim/candidates_resolved.jsonl``
(see :mod:`src.candidates.resolution`). Before this contract existed, ``verify``
read ``candidates_prepared.jsonl`` and silently fell back to preparing the raw
discovery feed in memory, which meant it re-verified ungrounded directory
listing URLs.

What is pinned here:

* ``verify`` prefers ``candidates_resolved.jsonl`` over
  ``candidates_prepared.jsonl`` whenever the resolved artefact exists;
* the prepared feed is **not** consulted at all in that case;
* the resolved official URLs are the URLs that actually reach the verifier;
* resolution provenance travels with the candidate instead of being dropped;
* a present-but-empty resolved artefact fails loudly rather than falling back
  to raw/prepared data;
* with no resolved artefact the legacy prepared/in-memory paths still work;
* CLI + persisted report metadata name the input source honestly.

Entirely offline: ``tmp_path`` artefacts and a verifier that refuses to touch
the network. No live calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import run as cli
from src.candidates.prepare import INTERIM_FILENAME, CandidatePreparer
from src.candidates.resolution import PROVENANCE_KEY, RESOLVED_FILENAME
from src.core.config import Settings
from src.core.io import write_jsonl
from src.discovery.base import CandidateTool
from src.verification.store import (
    VERIFICATION_REPORT_FILENAME,
    load_prepared_candidates,
    load_resolved_candidates,
    load_verification_input,
)

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SEEN_AT = datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc)

#: The grounded URL the resolution stage read off a directory detail page.
RESOLVED_OFFICIAL = "https://jasper.ai/"
#: A different URL, only present in the older prepared artefact. If this ever
#: reaches the verifier, the wrong input was used.
PREPARED_OFFICIAL = "https://stale-prepared.example.com/"
LISTING = "https://theresanaiforthat.com/ai/jasper/"


# ----------------------------------------------------------------- helpers
def settings_for(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.dry_run = True
    settings.paths.ensure()
    return settings


def resolved_row(
    *,
    name: str = "Jasper",
    website: str | None = RESOLVED_OFFICIAL,
    listing_url: str | None = LISTING,
    source_key: str = "taaft",
    origin: str = "directory_detail_page",
    **extra: Any,
) -> dict[str, Any]:
    """One row shaped exactly like ``candidates_resolved.jsonl`` writes it."""
    row: dict[str, Any] = {
        "name": name,
        "source_key": source_key,
        "source_name": "There's An AI For That",
        "listing_url": listing_url,
        "website": website,
        "tagline": "AI writing assistant for marketing teams",
        "categories": ["AI Writing"],
        "raw_signals": {"directory_saves": 1200},
        "raw_payload": {"discovery_method": "html_listing"},
        "discovered_at": SEEN_AT.isoformat(),
        PROVENANCE_KEY: {
            "origin": origin,
            "resolved_at": "2026-09-16T04:14:22.272882+00:00",
            "directory_source_key": source_key,
            "directory_source_name": "There's An AI For That",
            "detail_url": listing_url,
            "basis": "json_ld_software_url",
            "website_domain": "jasper.ai",
            "fetched": True,
            "live": True,
            "verified": False,
            "verification_status": "unverified",
        },
    }
    row.update(extra)
    return row


def prepared_row(
    *, name: str = "Jasper", website: str | None = PREPARED_OFFICIAL
) -> dict[str, Any]:
    """One row shaped like ``candidates_prepared.jsonl`` writes it."""
    prepared, rejected = CandidatePreparer(now=FIXED_NOW).prepare(
        CandidateTool(
            name=name,
            source_key="taaft",
            source_name="There's An AI For That",
            listing_url=LISTING,
            website=website,
            tagline="AI writing assistant for marketing teams",
            categories=["AI Writing"],
            discovered_at=SEEN_AT,
        )
    )
    assert rejected is None and prepared is not None
    return prepared.to_dict()


def write_resolved(interim: Path, rows: list[dict[str, Any]]) -> Path:
    return Path(write_jsonl(interim / RESOLVED_FILENAME, rows))


def write_prepared(interim: Path, rows: list[dict[str, Any]]) -> Path:
    return Path(write_jsonl(interim / INTERIM_FILENAME, rows))


class NoNetworkVerifier:
    """Verifier stand-in that records every candidate handed to it.

    Both entry points are implemented, because the CLI uses whichever suits
    the pass: the in-memory ``--no-persist`` path verifies a whole batch, and
    the resumable production path verifies one candidate at a time so each
    result can be appended to disk immediately. The assertion this double
    exists to support — *which URL reached the verifier* — is identical
    either way.
    """

    def __init__(self) -> None:
        self.seen: list[Any] = []

    @staticmethod
    def _real() -> Any:
        # Delegate to the real verifier with an offline client so behaviour,
        # statuses and the report stay exactly what production produces.
        from src.core.http_client import OfflineHttpClient
        from src.verification.verifier import OfficialSiteVerifier

        return OfficialSiteVerifier(client=OfflineHttpClient(), now=FIXED_NOW)

    def verify_candidate(self, candidate: Any) -> Any:
        self.seen.append(candidate)
        return self._real().verify_candidate(candidate)

    def verify_candidates(self, candidates: Any) -> tuple[list[Any], Any]:
        batch = list(candidates)
        self.seen.extend(batch)
        return self._real().verify_candidates(batch)


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guarantee: constructing a live HTTP client is a test failure."""

    def explode(*_: Any, **__: Any) -> None:
        raise AssertionError("no network client may be built in these tests")

    monkeypatch.setattr("src.verification.verifier.HttpClient", explode)


# =================================================== loader: resolved feed
class TestLoadResolvedCandidates:
    def test_resolved_rows_become_prepared_candidates(self, tmp_path: Path) -> None:
        path = write_resolved(tmp_path, [resolved_row()])

        loaded, source = load_resolved_candidates(path)

        assert source == str(path)
        assert len(loaded) == 1
        candidate = loaded[0]
        assert candidate.name == "Jasper"
        assert candidate.website == RESOLVED_OFFICIAL
        assert candidate.website_domain == "jasper.ai"
        assert candidate.candidate_id, "identity must be derived, never blank"
        assert candidate.verification_status == "unverified"

    def test_identity_matches_the_normal_preparation_path(self, tmp_path: Path) -> None:
        """Rehydration reuses the real preparer, so ids cannot drift."""
        path = write_resolved(tmp_path, [resolved_row()])
        loaded, _ = load_resolved_candidates(path)

        expected, rejected = CandidatePreparer().prepare(
            CandidateTool(
                name="Jasper",
                source_key="taaft",
                source_name="There's An AI For That",
                listing_url=LISTING,
                website=RESOLVED_OFFICIAL,
                discovered_at=SEEN_AT,
            )
        )
        assert rejected is None and expected is not None
        assert loaded[0].candidate_id == expected.candidate_id
        assert loaded[0].identity_basis == expected.identity_basis

    def test_resolution_provenance_is_carried_through(self, tmp_path: Path) -> None:
        path = write_resolved(tmp_path, [resolved_row()])
        loaded, _ = load_resolved_candidates(path)

        provenance = loaded[0].raw_signals[PROVENANCE_KEY]
        assert provenance["origin"] == "directory_detail_page"
        assert provenance["basis"] == "json_ld_software_url"
        # A resolved URL is still only a directory claim.
        assert provenance["verified"] is False
        assert provenance["verification_status"] == "unverified"

    def test_observed_signals_are_not_lost(self, tmp_path: Path) -> None:
        path = write_resolved(tmp_path, [resolved_row()])
        loaded, _ = load_resolved_candidates(path)
        assert loaded[0].raw_signals["directory_saves"] == 1200

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        loaded, source = load_resolved_candidates(tmp_path / "absent.jsonl")
        assert loaded == []
        assert source.endswith("absent.jsonl")

    def test_unusable_rows_are_skipped_not_patched(self, tmp_path: Path) -> None:
        path = tmp_path / RESOLVED_FILENAME
        path.write_text(
            "\n".join(
                [
                    json.dumps({"source_key": "taaft"}),  # no name
                    json.dumps({"name": "No pointer", "source_key": "taaft"}),
                    "{ not json",
                    json.dumps(resolved_row(name="Kept")),
                ]
            ),
            encoding="utf-8",
        )

        loaded, _ = load_resolved_candidates(path)

        assert [c.name for c in loaded] == ["Kept"]

    def test_row_without_resolved_url_keeps_its_listing_pointer(
        self, tmp_path: Path
    ) -> None:
        """Unresolved records are kept and flagged, never dropped or invented."""
        path = write_resolved(
            tmp_path,
            [resolved_row(name="Unresolved", website=None, origin="none")],
        )

        loaded, _ = load_resolved_candidates(path)

        assert len(loaded) == 1
        assert loaded[0].website is None
        assert loaded[0].listing_url is not None
        assert "no_official_website_observed" in loaded[0].issues


# ================================================ input source selection
class TestLoadVerificationInput:
    def test_prefers_resolved_over_prepared(self, tmp_path: Path) -> None:
        write_resolved(tmp_path, [resolved_row()])
        write_prepared(tmp_path, [prepared_row()])

        loaded, source, kind = load_verification_input(tmp_path)

        assert kind == "resolved"
        assert source.endswith(RESOLVED_FILENAME)
        assert [c.website for c in loaded] == [RESOLVED_OFFICIAL]
        assert PREPARED_OFFICIAL not in {c.website for c in loaded}

    def test_falls_back_to_prepared_only_when_resolved_is_absent(
        self, tmp_path: Path
    ) -> None:
        write_prepared(tmp_path, [prepared_row()])

        loaded, source, kind = load_verification_input(tmp_path)

        assert kind == "prepared"
        assert source.endswith(INTERIM_FILENAME)
        assert [c.website for c in loaded] == [PREPARED_OFFICIAL]

    def test_empty_resolved_artefact_does_not_fall_back(self, tmp_path: Path) -> None:
        """A present resolved artefact is the only input considered."""
        write_resolved(tmp_path, [])
        write_prepared(tmp_path, [prepared_row()])

        loaded, source, kind = load_verification_input(tmp_path)

        assert loaded == []
        assert kind == "resolved"
        assert source.endswith(RESOLVED_FILENAME)

    def test_no_artefacts_reports_none(self, tmp_path: Path) -> None:
        loaded, source, kind = load_verification_input(tmp_path)

        assert loaded == []
        assert kind == "none"
        assert source.endswith(RESOLVED_FILENAME)

    def test_prepared_loader_is_still_intact(self, tmp_path: Path) -> None:
        path = write_prepared(tmp_path, [prepared_row()])
        loaded, source = load_prepared_candidates(path)
        assert source == str(path)
        assert [c.website for c in loaded] == [PREPARED_OFFICIAL]


# ==================================================================== CLI
class TestVerifyCliInputWiring:
    def test_cli_uses_the_resolved_feed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_resolved(interim, [resolved_row()])
        write_prepared(interim, [prepared_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        exit_code = cli.main(["verify"])
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert payload["input_source_kind"] == "resolved"
        assert payload["input_source"].endswith(RESOLVED_FILENAME)
        assert payload["prepared_candidates"] == 1
        assert payload["live"] is False

    def test_resolved_official_url_reaches_the_verifier(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_resolved(interim, [resolved_row()])
        write_prepared(interim, [prepared_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        spy = NoNetworkVerifier()
        monkeypatch.setattr(
            cli.ToolsPipeline, "_verifier_for", lambda self, *, live: spy
        )

        assert cli.main(["verify"]) == 0
        capsys.readouterr()

        assert [c.website for c in spy.seen] == [RESOLVED_OFFICIAL]
        assert PREPARED_OFFICIAL not in {c.website for c in spy.seen}

    def test_persisted_report_names_the_input_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_resolved(interim, [resolved_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify"]) == 0
        capsys.readouterr()

        report = json.loads(
            (interim / VERIFICATION_REPORT_FILENAME).read_text(encoding="utf-8")
        )
        assert report["input_source_kind"] == "resolved"
        assert report["input_source"].endswith(RESOLVED_FILENAME)

    def test_empty_resolved_artefact_fails_loudly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_resolved(interim, [])
        write_prepared(interim, [prepared_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        exit_code = cli.main(["verify"])
        captured = capsys.readouterr()

        assert exit_code == 1
        payload = json.loads(captured.out)
        assert payload["input_source_kind"] == "resolved"
        assert payload["prepared_candidates"] == 0
        assert "Refusing to fall back" in captured.err
        assert "resolve-urls" in captured.err

    def test_missing_resolved_artefact_still_uses_prepared(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_prepared(interim, [prepared_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["input_source_kind"] == "prepared"
        assert payload["input_source"].endswith(INTERIM_FILENAME)

    def test_no_input_at_all_reports_honestly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        exit_code = cli.main(["verify"])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert json.loads(captured.out)["prepared_candidates"] == 0
        assert "nothing is fabricated" in captured.err

    def test_limit_still_applies_to_the_resolved_feed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        write_resolved(
            interim,
            [
                resolved_row(
                    name=f"Tool {index}",
                    website=f"https://tool{index}.example.com/",
                    listing_url=None,
                )
                for index in range(8)
            ],
        )
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify", "--limit", "2"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["prepared_candidates"] == 8
        assert payload["considered"] == 2
        assert payload["report"]["input"] == 2

    def test_offline_resolved_pass_verifies_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        no_network: None,
    ) -> None:
        """Offline mode keeps working: no calls, no fabricated verifications."""
        settings = settings_for(tmp_path)
        write_resolved(settings.paths.resolve("interim"), [resolved_row()])
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify"]) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["report"]["fetched"] == 0
        assert payload["report"]["verified"] == 0
        assert "Offline pass" in captured.err
