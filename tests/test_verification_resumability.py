"""Regression tests for **resumable** official-website verification.

A full production pass verifies thousands of candidates against thousands of
real websites and takes well over an hour of network I/O. The original
implementation held every result in memory and rewrote
``candidates_verified.jsonl`` once, at the very end, which meant an
interruption anywhere in that window discarded *all* completed verification
and the rerun had to start from zero.

What is pinned here:

* every result is appended to the artefact as it is produced, so an
  interrupted pass keeps the work it had already done;
* a resumed pass continues where the last one stopped and **never duplicates**
  a record;
* records that differ only by official URL / listing pointer (the same product
  seen by two directories) are *not* collapsed into one — the resume key is
  the identity triple, not ``candidate_id`` alone;
* ``--restart`` rebuilds the artefact instead of mixing two passes;
* the published report is recomputed from the rows on disk, so its counts
  describe the file across any number of resumed passes rather than a single
  pass's in-memory total;
* every status — verified, partially_verified, unverified, unreachable,
  failed, needs_review — survives persistence and resumption honestly;
* provenance (the separate discovery / verification blocks) survives resume;
* the progress state file records observed counts, not projections.

Entirely offline. No live calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import run as cli
from src.core.config import Settings
from src.core.io import read_jsonl, write_jsonl
from src.candidates.prepare import PreparedCandidate
from src.candidates.resolution import RESOLVED_FILENAME
from src.verification.runner import (
    VERIFICATION_STATE_FILENAME,
    CandidateVerificationRunner,
    completed_keys,
    summarize_verified_file,
    verification_key,
)
from src.verification.store import VERIFICATION_REPORT_FILENAME, VERIFIED_FILENAME
from src.verification.verifier import (
    VerificationResult,
    VerificationStatus,
)

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- helpers
def settings_for(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.dry_run = True
    settings.paths.ensure()
    return settings


def candidate(
    name: str,
    *,
    candidate_id: str | None = None,
    website: str | None = None,
    listing_url: str | None = None,
) -> PreparedCandidate:
    """A prepared candidate with just the fields verification reads."""
    slug = name.lower().replace(" ", "-")
    return PreparedCandidate(
        candidate_id=candidate_id or f"cand-{slug}",
        identity_key=f"key-{slug}",
        identity_basis="website_domain",
        identity_confidence="high",
        name=name,
        name_key=slug,
        observed_description=None,
        slug=slug,
        website=website if website is not None else f"https://{slug}.example.com/",
        website_domain=f"{slug}.example.com",
        listing_url=listing_url or f"https://directory.example.com/{slug}",
    )


class StatusVerifier:
    """Offline verifier stand-in returning a scripted status per candidate.

    Real decision rules are exercised elsewhere; this double exists so a test
    can assert *persistence* behaviour against a known mix of statuses,
    including the ones that must never be silently discarded.
    """

    def __init__(self, statuses: dict[str, VerificationStatus] | None = None) -> None:
        self.statuses = statuses or {}
        self.seen: list[str] = []
        self.calls = 0

    def verify_candidate(self, cand: Any) -> VerificationResult:
        self.calls += 1
        self.seen.append(cand.name)
        status = self.statuses.get(cand.name, VerificationStatus.VERIFIED)
        return VerificationResult(
            candidate_id=cand.candidate_id,
            name=cand.name,
            status=status,
            official_url=cand.website,
            checked_at=FIXED_NOW.isoformat(),
            fetched=status is not VerificationStatus.FAILED,
            needs_review=status
            in (VerificationStatus.UNVERIFIED, VerificationStatus.FAILED),
        )


class ExplodingVerifier(StatusVerifier):
    """Verifies ``fail_after`` candidates, then raises — simulates a crash."""

    def __init__(self, fail_after: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_after = fail_after

    def verify_candidate(self, cand: Any) -> VerificationResult:
        if self.calls >= self.fail_after:
            raise KeyboardInterrupt("simulated interruption")
        return super().verify_candidate(cand)


def rows_of(interim: Path) -> list[dict[str, Any]]:
    return [dict(row) for row in read_jsonl(interim / VERIFIED_FILENAME)]


# =============================================== append-as-you-go persistence
class TestIncrementalPersistence:
    def test_results_are_on_disk_before_the_pass_finishes(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        runner = CandidateVerificationRunner(
            verifier=StatusVerifier(), checkpoint_every=1
        )
        runner.run(
            [candidate(f"Tool {i}") for i in range(5)], interim_dir=interim, limit=3
        )
        # Only what was processed is persisted — nothing is written for the
        # candidates this pass never reached.
        assert len(rows_of(interim)) == 3

    def test_interruption_keeps_completed_verification(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        candidates = [candidate(f"Tool {i}") for i in range(10)]
        runner = CandidateVerificationRunner(
            verifier=ExplodingVerifier(fail_after=4), checkpoint_every=2
        )

        with pytest.raises(KeyboardInterrupt):
            runner.run(candidates, interim_dir=interim)

        # The whole point: the four finished before the crash survived it.
        assert len(rows_of(interim)) == 4

    def test_resume_completes_the_remainder_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        interim = tmp_path / "interim"
        candidates = [candidate(f"Tool {i}") for i in range(10)]

        with pytest.raises(KeyboardInterrupt):
            CandidateVerificationRunner(
                verifier=ExplodingVerifier(fail_after=4)
            ).run(candidates, interim_dir=interim)

        second = StatusVerifier()
        report = CandidateVerificationRunner(verifier=second).run(
            candidates, interim_dir=interim
        )

        rows = rows_of(interim)
        assert len(rows) == 10
        assert len({row["candidate_id"] for row in rows}) == 10
        # The resumed pass re-verified only what was missing: no wasted fetch.
        assert second.calls == 6
        assert report.input_count == 10

    def test_rerunning_a_finished_pass_is_a_no_op(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        candidates = [candidate(f"Tool {i}") for i in range(4)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            candidates, interim_dir=interim
        )

        again = StatusVerifier()
        CandidateVerificationRunner(verifier=again).run(candidates, interim_dir=interim)

        assert again.calls == 0
        assert len(rows_of(interim)) == 4

    def test_restart_rebuilds_instead_of_appending(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        candidates = [candidate(f"Tool {i}") for i in range(4)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            candidates, interim_dir=interim
        )

        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            candidates, interim_dir=interim, resume=False
        )

        assert len(rows_of(interim)) == 4


# ============================================================== resume key
class TestVerificationKey:
    def test_same_candidate_id_under_different_urls_is_not_collapsed(
        self, tmp_path: Path
    ) -> None:
        # The real resolved feed carries 3,441 rows under 3,317 distinct
        # candidate ids. Keying on the id alone would silently drop rows.
        interim = tmp_path / "interim"
        pair = [
            candidate("Dup", candidate_id="same-id", website="https://a.example.com/"),
            candidate("Dup", candidate_id="same-id", website="https://b.example.com/"),
        ]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            pair, interim_dir=interim
        )
        assert len(rows_of(interim)) == 2

    def test_a_truly_identical_row_is_written_once(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        same = candidate("Twin")
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            [same, same], interim_dir=interim
        )
        assert len(rows_of(interim)) == 1

    def test_persisted_row_and_candidate_agree_on_the_key(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cand = candidate("Jasper")
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            [cand], interim_dir=interim
        )
        assert verification_key(rows_of(interim)[0]) == verification_key(cand)

    def test_completed_keys_reads_the_artefact(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(f"Tool {i}") for i in range(3)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            cands, interim_dir=interim
        )
        assert completed_keys(interim / VERIFIED_FILENAME) == {
            verification_key(c) for c in cands
        }

    def test_missing_artefact_has_no_completed_keys(self, tmp_path: Path) -> None:
        assert completed_keys(tmp_path / "nope.jsonl") == set()


# ======================================================= honest status counts
class TestStatusesSurvive:
    STATUSES = {
        "A": VerificationStatus.VERIFIED,
        "B": VerificationStatus.PARTIALLY_VERIFIED,
        "C": VerificationStatus.UNVERIFIED,
        "D": VerificationStatus.UNREACHABLE,
        "E": VerificationStatus.FAILED,
    }

    def test_no_status_is_discarded(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(name) for name in self.STATUSES]
        report = CandidateVerificationRunner(
            verifier=StatusVerifier(self.STATUSES)
        ).run(cands, interim_dir=interim)

        assert report.status_counts == {
            "verified": 1,
            "partially_verified": 1,
            "unverified": 1,
            "unreachable": 1,
            "failed": 1,
        }
        assert report.needs_review_count == 2

    def test_counts_are_rebuilt_from_disk_across_resumed_passes(
        self, tmp_path: Path
    ) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(name) for name in self.STATUSES]

        first = CandidateVerificationRunner(verifier=StatusVerifier(self.STATUSES))
        first.run(cands, interim_dir=interim, limit=2)
        second = CandidateVerificationRunner(verifier=StatusVerifier(self.STATUSES))
        report = second.run(cands, interim_dir=interim)

        # The report describes the *file*, not the second pass alone.
        assert report.input_count == 5
        assert sum(report.status_counts.values()) == 5

    def test_summarize_matches_a_direct_count_of_the_file(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(name) for name in self.STATUSES]
        CandidateVerificationRunner(verifier=StatusVerifier(self.STATUSES)).run(
            cands, interim_dir=interim
        )

        report = summarize_verified_file(interim / VERIFIED_FILENAME)
        on_disk: dict[str, int] = {}
        for row in rows_of(interim):
            key = row["verification"]["verification_status"]
            on_disk[key] = on_disk.get(key, 0) + 1
        assert report.status_counts == on_disk

    def test_a_raising_candidate_is_persisted_as_unverified(
        self, tmp_path: Path
    ) -> None:
        class Boom(StatusVerifier):
            def verify_candidate(self, cand: Any) -> VerificationResult:
                if cand.name == "Bad":
                    raise RuntimeError("hostile record")
                return super().verify_candidate(cand)

        interim = tmp_path / "interim"
        CandidateVerificationRunner(verifier=Boom()).run(
            [candidate("Good"), candidate("Bad")], interim_dir=interim
        )

        rows = {row["name"]: row for row in rows_of(interim)}
        assert rows["Bad"]["verification_status"] == "unverified"
        assert rows["Bad"]["verification"]["needs_review"] is True
        # ...and it did not abort the pass.
        assert rows["Good"]["verification_status"] == "verified"


# ============================================================== provenance
class TestProvenanceSurvives:
    def test_discovery_and_verification_stay_separate_blocks(
        self, tmp_path: Path
    ) -> None:
        interim = tmp_path / "interim"
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            [candidate("Jasper")], interim_dir=interim
        )
        row = rows_of(interim)[0]
        assert row["discovery"]["listing_url"].startswith("https://directory.")
        assert row["verification"]["official_url"] == "https://jasper.example.com/"
        assert "verification_status" in row["verification"]

    def test_liveness_flag_is_recorded_per_row(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        CandidateVerificationRunner(verifier=StatusVerifier(), live=False).run(
            [candidate("Jasper")], interim_dir=interim
        )
        assert rows_of(interim)[0]["live"] is False


# ============================================================== state file
class TestProgressState:
    def test_state_records_observed_counts(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(f"Tool {i}") for i in range(6)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            cands, interim_dir=interim, limit=4
        )

        state = json.loads(
            (interim / VERIFICATION_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert state["records_persisted"] == 4
        assert state["records_persisted"] == len(rows_of(interim))
        assert state["pending"] == 2
        assert state["batches"][-1]["records_written"] == 4

    def test_each_pass_appends_a_batch_entry(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(f"Tool {i}") for i in range(6)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            cands, interim_dir=interim, limit=3
        )
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            cands, interim_dir=interim
        )

        state = json.loads(
            (interim / VERIFICATION_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert len(state["batches"]) == 2
        assert state["records_persisted"] == 6
        assert state["pending"] == 0

    def test_report_marks_completion_and_resume_metadata(self, tmp_path: Path) -> None:
        interim = tmp_path / "interim"
        cands = [candidate(f"Tool {i}") for i in range(4)]
        CandidateVerificationRunner(verifier=StatusVerifier()).run(
            cands, interim_dir=interim, limit=2, input_source_kind="resolved"
        )
        report = json.loads(
            (interim / VERIFICATION_REPORT_FILENAME).read_text(encoding="utf-8")
        )
        assert report["resumable"] is True
        assert report["processed_this_pass"] == 2
        assert report["input_source_kind"] == "resolved"


# ==================================================================== CLI
class TestVerifyCliResume:
    @staticmethod
    def _write_resolved(interim: Path, count: int) -> None:
        rows = [
            {
                "name": f"Tool {i}",
                "source_key": "taaft",
                "source_name": "There's An AI For That",
                "listing_url": f"https://directory.example.com/tool-{i}",
                "website": f"https://tool-{i}.example.com/",
                "discovered_at": FIXED_NOW.isoformat(),
            }
            for i in range(count)
        ]
        write_jsonl(interim / RESOLVED_FILENAME, rows)

    def test_cli_exposes_resume_controls(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["verify", "--restart", "--checkpoint-every", "5"])
        assert args.restart is True
        assert args.checkpoint_every == 5
        # Resuming is the default: a production rerun must never need a flag
        # to avoid re-verifying everything.
        assert parser.parse_args(["verify"]).restart is False

    def test_cli_pass_resumes_rather_than_restarting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        self._write_resolved(interim, 6)
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify", "--limit", "2"]) == 0
        capsys.readouterr()
        assert len(rows_of(interim)) == 2

        assert cli.main(["verify"]) == 0
        payload = json.loads(capsys.readouterr().out)

        rows = rows_of(interim)
        assert len(rows) == 6
        assert payload["report"]["input"] == 6
        assert payload["artefacts"]["verification_state"].endswith(
            VERIFICATION_STATE_FILENAME
        )

    def test_cli_no_persist_still_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        settings = settings_for(tmp_path)
        interim = settings.paths.resolve("interim")
        self._write_resolved(interim, 3)
        monkeypatch.setattr(cli, "get_settings", lambda **_: settings)

        assert cli.main(["verify", "--no-persist"]) == 0
        capsys.readouterr()
        assert not (interim / VERIFIED_FILENAME).exists()
