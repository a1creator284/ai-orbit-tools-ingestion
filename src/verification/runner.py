"""Resumable, checkpointed driver for official-website verification.

Why this module exists
----------------------
:meth:`src.pipeline.ToolsPipeline.verify_candidates` verifies a batch in
memory and hands the whole list to :func:`~src.verification.store.
persist_verification`, which rewrites ``candidates_verified.jsonl`` from
scratch. That is correct for a spot check of ten candidates, but it has one
property that makes a full production pass unsafe: **nothing reaches disk
until the last candidate has been verified**. A live pass over the 3,441
resolved candidates takes well over an hour of real network I/O, and any
interruption in that window — a dropped connection, a killed process, a
timeout on a dead host — discards every completed verification.

This runner keeps the decision logic exactly where it already lives and only
changes *when bytes hit the disk*:

* every verified candidate is **appended immediately** to the same
  ``candidates_verified.jsonl`` artefact, in the same row shape produced by
  :func:`~src.verification.store.verification_record`;
* a state file records progress so an interrupted pass can be resumed;
* the report is **recomputed from the file on disk** at every checkpoint
  (:func:`summarize_verified_file`), so the published counts always describe
  the rows that actually exist — across any number of resumed passes — and can
  never drift into a fabricated total.

What this module deliberately does **not** do
---------------------------------------------
* it does not re-implement, extend or relax any verification rule —
  :class:`~src.verification.verifier.OfficialSiteVerifier` is called verbatim,
  one candidate at a time, exactly as the batch path calls it;
* it does not touch discovery or official-URL resolution: the resolved feed
  ``candidates_resolved.jsonl`` is read-only input;
* it does not invent a result. A candidate that was never processed is absent
  from the output; a candidate that failed is persisted with its honest
  ``unverified`` / ``unreachable`` / ``failed`` status.

Artefacts (all under ``data/interim/``)::

    candidates_verified.jsonl             one row per verified candidate
    candidates_verification_report.json   auditable summary, rebuilt from file
    candidates_verification_state.json    resume marker + per-batch history
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core.ids import content_hash
from src.core.io import append_jsonl, read_jsonl, write_json
from src.core.logging_setup import get_logger
from src.verification.store import (
    VERIFICATION_REPORT_FILENAME,
    VERIFIED_FILENAME,
    verification_record,
)
from src.verification.verifier import (
    OfficialSiteVerifier,
    VerificationFailure,
    VerificationReport,
    VerificationResult,
    VerificationStatus,
)

logger = get_logger("verification.runner")

__all__ = [
    "VERIFICATION_STATE_FILENAME",
    "DEFAULT_CHECKPOINT_EVERY",
    "verification_key",
    "completed_keys",
    "summarize_verified_file",
    "CandidateVerificationRunner",
]

#: Resume marker + batch history for the verification stage.
VERIFICATION_STATE_FILENAME = "candidates_verification_state.json"

#: How often the state/report are refreshed. Rows are appended immediately
#: regardless, so this only affects the progress marker, never data safety.
DEFAULT_CHECKPOINT_EVERY = 25


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def verification_key(value: Any) -> str:
    """Stable identity for one candidate, used only to resume safely.

    ``candidate_id`` alone is **not** sufficient: the resolved feed carries
    3,441 records under 3,317 distinct candidate ids (the same product listed
    by more than one directory, or under more than one official URL). Keying
    on the id alone would silently drop those rows on a resumed pass and make
    the final dataset smaller than the input for no stated reason.

    The key therefore hashes the identity triple that verification itself
    cannot change — id, the official URL being checked, and the listing
    pointer it came from — so a row keeps the same key whether it is read back
    off ``candidates_verified.jsonl`` or built fresh from the resolved feed.
    """
    if isinstance(value, Mapping):
        candidate_id = value.get("candidate_id")
        discovery = value.get("discovery")
        verification = value.get("verification")
        website = None
        listing = None
        if isinstance(discovery, Mapping):
            candidate_id = candidate_id or discovery.get("candidate_id")
            website = discovery.get("website")
            listing = discovery.get("listing_url")
        if not website and isinstance(verification, Mapping):
            # A row persisted before/without a discovery block still names the
            # URL that was actually checked.
            website = verification.get("official_url")
        if not website:
            website = value.get("website")
        if not listing:
            listing = value.get("listing_url")
    else:
        candidate_id = getattr(value, "candidate_id", None)
        website = getattr(value, "website", None)
        listing = getattr(value, "listing_url", None)

    return content_hash(
        str(candidate_id or ""),
        str(website or ""),
        str(listing or ""),
    )


def completed_keys(output_path: str | Path) -> set[str]:
    """Keys already persisted in ``candidates_verified.jsonl``."""
    done: set[str] = set()
    for row in read_jsonl(output_path):
        if isinstance(row, Mapping):
            done.add(verification_key(row))
    return done


def _status_of(row: Mapping[str, Any]) -> str:
    verification = row.get("verification")
    if isinstance(verification, Mapping):
        status = verification.get("verification_status")
        if status:
            return str(status)
    return str(row.get("verification_status") or VerificationStatus.UNVERIFIED)


def summarize_verified_file(path: str | Path) -> VerificationReport:
    """Rebuild the verification report from the rows actually on disk.

    Counted by reading the artefact rather than by accumulating in memory, so
    the published summary describes the file after *any* number of resumed
    passes and cannot drift from it. Only statuses that were really persisted
    are counted — nothing is inferred for a candidate that was never processed.
    """
    path = Path(path)
    report = VerificationReport(started_at=_utcnow())
    for row in read_jsonl(path):
        if not isinstance(row, Mapping):
            continue
        verification = row.get("verification")
        verification = verification if isinstance(verification, Mapping) else {}
        result = VerificationResult(
            candidate_id=row.get("candidate_id"),
            name=row.get("name"),
            status=VerificationStatus(_status_of(row)),
            official_url=verification.get("official_url"),
            fetched=bool(verification.get("fetched")),
            failures=[str(code) for code in (verification.get("failures") or [])],
            needs_review=bool(
                verification.get("needs_review", row.get("needs_review", False))
            ),
        )
        report.note(result)
    report.finished_at = _utcnow()
    return report


class CandidateVerificationRunner:
    """Verify a candidate feed incrementally, appending every result.

    Parameters
    ----------
    verifier:
        The verifier to use. Defaults to the real
        :class:`~src.verification.verifier.OfficialSiteVerifier` for a live
        pass and an offline-client verifier otherwise, so nothing reaches the
        network unless ``live`` was explicitly requested.
    checkpoint_every:
        How often the state file and report are refreshed.
    """

    def __init__(
        self,
        *,
        verifier: OfficialSiteVerifier | None = None,
        live: bool = False,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    ) -> None:
        self.live = bool(live)
        if verifier is None:
            if self.live:
                verifier = OfficialSiteVerifier()
            else:
                from src.core.http_client import OfflineHttpClient

                verifier = OfficialSiteVerifier(client=OfflineHttpClient())  # type: ignore[arg-type]
        self.verifier = verifier
        self.checkpoint_every = max(1, int(checkpoint_every))

    # ------------------------------------------------------------------ run
    def run(
        self,
        candidates: Sequence[Any],
        *,
        interim_dir: str | Path,
        limit: int | None = None,
        resume: bool = True,
        input_source: str | None = None,
        input_source_kind: str | None = None,
    ) -> VerificationReport:
        """Verify ``candidates``, persisting each result as it is produced.

        ``limit`` caps how many candidates this pass *processes*, which is what
        makes a deliberate partial production batch safe: whatever it completes
        is already on disk and the next pass continues from there.

        ``resume=True`` (default) skips candidates already present in the
        output artefact, so a resumed pass never duplicates a record.
        ``resume=False`` replaces the artefact rather than mixing two passes in
        one file — only ever done on explicit request.
        """
        interim = Path(interim_dir)
        interim.mkdir(parents=True, exist_ok=True)
        output_path = interim / VERIFIED_FILENAME
        report_path = interim / VERIFICATION_REPORT_FILENAME
        state_path = interim / VERIFICATION_STATE_FILENAME

        if not resume and output_path.exists():
            output_path.unlink()

        done = completed_keys(output_path) if resume else set()
        resumed_from = len(done)
        seen: set[str] = set()
        processed = 0
        skipped_done = 0
        skipped_duplicate = 0
        started_at = _utcnow()

        for candidate in candidates:
            key = verification_key(candidate)
            if key in done:
                skipped_done += 1
                continue
            if key in seen:
                # Two identical rows in the input: persist the candidate once.
                skipped_duplicate += 1
                continue
            if limit is not None and processed >= max(0, limit):
                break

            result = self._verify_one(candidate)
            row = verification_record(candidate, result, live=self.live)
            append_jsonl(output_path, row)
            seen.add(key)
            processed += 1

            if processed % self.checkpoint_every == 0:
                self._checkpoint(
                    output_path,
                    report_path,
                    state_path,
                    started_at=started_at,
                    processed=processed,
                    resumed_from=resumed_from,
                    limit=limit,
                    input_candidates=len(candidates),
                    input_source=input_source,
                    input_source_kind=input_source_kind,
                    finished=False,
                )
                logger.info(
                    "verification checkpoint",
                    extra={"processed": processed, "resumed_from": resumed_from},
                )

        report = self._checkpoint(
            output_path,
            report_path,
            state_path,
            started_at=started_at,
            processed=processed,
            resumed_from=resumed_from,
            limit=limit,
            input_candidates=len(candidates),
            input_source=input_source,
            input_source_kind=input_source_kind,
            finished=True,
            pending=max(
                0, len(candidates) - (resumed_from + processed + skipped_duplicate)
            ),
        )
        logger.info(
            "verification pass complete",
            extra={
                "processed": processed,
                "resumed_from": resumed_from,
                "already_done": skipped_done,
                "duplicates": skipped_duplicate,
            },
        )
        return report

    # -------------------------------------------------------------- helpers
    def _verify_one(self, candidate: Any) -> VerificationResult:
        """Verify one candidate; a raising record never aborts the pass.

        Mirrors :meth:`OfficialSiteVerifier.verify_candidates` verbatim — same
        rules, same honest ``unverified`` fallback — so per-record resilience
        does not differ between the batch path and the resumable path.
        """
        from src.core.text import clean_text
        from src.core.urls import normalize_url

        try:
            return self.verifier.verify_candidate(candidate)
        except Exception as exc:  # noqa: BLE001 - resilience over completeness
            logger.warning(
                "verification raised",
                extra={
                    "candidate": str(getattr(candidate, "name", None))[:80],
                    "error": str(exc)[:200],
                },
            )
            return VerificationResult(
                candidate_id=getattr(candidate, "candidate_id", None),
                name=clean_text(getattr(candidate, "name", None)),
                status=VerificationStatus.UNVERIFIED,
                official_url=normalize_url(getattr(candidate, "website", None) or None),
                checked_at=_utcnow(),
                failures=[VerificationFailure.FETCH_FAILED],
                reason=f"verification error: {str(exc)[:200]}",
                needs_review=True,
            )

    def _checkpoint(
        self,
        output_path: Path,
        report_path: Path,
        state_path: Path,
        *,
        started_at: str,
        processed: int,
        resumed_from: int,
        limit: int | None,
        input_candidates: int,
        input_source: str | None,
        input_source_kind: str | None,
        finished: bool,
        pending: int | None = None,
    ) -> VerificationReport:
        """Recount the artefact and refresh both report and state."""
        report = summarize_verified_file(output_path)
        payload: dict[str, Any] = dict(report.to_dict())
        payload.update(
            {
                "live": self.live,
                "limit": limit,
                "input_candidates": input_candidates,
                "considered": report.input_count,
                "input_source": input_source,
                "input_source_kind": input_source_kind,
                "resumable": True,
                "resumed_from_records": resumed_from,
                "processed_this_pass": processed,
                "complete": finished,
                "artefacts": {
                    "verified_candidates": str(output_path),
                    "verification_state": str(state_path),
                },
            }
        )
        if pending is not None:
            payload["pending"] = pending
        write_json(report_path, payload)
        self._write_state(
            state_path,
            output_path=output_path,
            started_at=started_at,
            processed=processed,
            resumed_from=resumed_from,
            report=report,
            input_candidates=input_candidates,
            input_source=input_source,
            input_source_kind=input_source_kind,
            finished=finished,
            pending=pending,
        )
        return report

    def _write_state(
        self,
        state_path: Path,
        *,
        output_path: Path,
        started_at: str,
        processed: int,
        resumed_from: int,
        report: VerificationReport,
        input_candidates: int,
        input_source: str | None,
        input_source_kind: str | None,
        finished: bool,
        pending: int | None,
    ) -> None:
        """Append/refresh this pass's entry in the batch history.

        The history is *observed*, never projected: each entry records what a
        pass actually wrote. ``records_persisted`` is the count of rows in the
        artefact, so the state can be checked against the file at any time.
        """
        existing: Any = {}
        if state_path.exists():
            try:
                existing = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        batches = list(existing.get("batches") or []) if isinstance(existing, Mapping) else []

        entry = {
            "started_at": started_at,
            "updated_at": _utcnow(),
            "live": self.live,
            "resumed_from_records": resumed_from,
            "records_written": processed,
            "finished": finished,
        }
        if batches and isinstance(batches[-1], Mapping) and batches[-1].get("started_at") == started_at:
            batches[-1] = entry
        else:
            batches.append(entry)

        write_json(
            state_path,
            {
                "version": 1,
                "updated_at": _utcnow(),
                "input_source": input_source,
                "input_source_kind": input_source_kind,
                "input_candidates": input_candidates,
                "output_path": str(output_path),
                "records_persisted": report.input_count,
                "pending": pending,
                "status_counts": dict(sorted(report.status_counts.items())),
                "batches": batches,
            },
        )
