"""Production official-URL resolution over the persisted raw candidate feed.

What this stage is
------------------
The one step between *raw discovery* and anything that needs a product's own
site::

    data/raw/candidates.jsonl                 (immutable discovery output)
        -> official-URL resolution (this module)
        -> data/interim/candidates_resolved.jsonl
           data/interim/candidates_resolution_report.json
           data/interim/candidates_resolution_state.json

:mod:`src.candidates.official_url` already knows *how* to read a URL a
directory published on a detail page. It does not know how to do that for
thousands of persisted records without losing work, re-fetching pages it
already read, or dropping the provenance discovery collected. That is this
module's only job:

* **the raw feed is read, never rewritten** — ``data/raw/candidates.jsonl``
  stays byte-identical; the enriched dataset is a new artefact;
* **every original field survives verbatim** (including ``raw_payload`` /
  ``raw_signals``, i.e. the full discovery audit trail), and the only key that
  can change is ``website`` — and only when it was empty;
* **work is persisted as it happens.** Each record is appended to the output
  the moment it is decided, so an interrupted pass keeps everything it had
  already done. A resumed pass reads the output back, skips the records that
  are already there, and appends only the rest;
* **the report is derived from the persisted dataset**, not from in-memory
  counters, so the published statistics can never disagree with the file they
  describe — and cannot be fabricated. It is rewritten at every checkpoint,
  not only when a pass finishes, so an *interrupted* pass also leaves a report
  that matches the rows on disk (``complete: false`` marks it as mid-pass, and
  ``pending`` is ``null`` there because the outstanding set is not yet known).
* **``pending`` is scoped.** With ``--source`` the pass only considers that
  directory, so ``pending`` counts what is outstanding *within that scope* and
  the report names the scope in ``scope_sources`` / ``pending_scope``. It is
  deliberately not ``input_records - output_records``: the raw feed can contain
  several lines for one candidate, and those collapse to a single output row.

Hard rules
----------
* **Resolution is not verification.** A resolved URL is a *directory claim*.
  Every enriched record keeps ``verification_status: "unverified"`` and its
  provenance block says so explicitly. Promoting it is
  :mod:`src.verification.verifier`'s job, later, against the product's own
  site.
* **Nothing is guessed.** This module adds no URL logic of its own; it
  delegates every decision to :func:`extract_official_url_evidence` /
  :class:`OfficialUrlResolver`, which refuse slugs, search engines, social
  profiles, app stores and review directories.
* **Only detail pages are fetched, and only when needed.** A candidate that
  already carries a website is never fetched. The resolved official site is
  *never* fetched here — this stage does not crawl products.
* **Offline unless asked.** Without ``live=True`` the runner uses
  :class:`~src.core.http_client.OfflineHttpClient`, so a dry run proves which
  URLs a live pass *would* read without opening a socket.
* **One bad record never stops the batch.** Malformed JSON lines, hostile
  field types, missing names, transport failures and parse failures all end as
  a reason code on that record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.candidates.official_url import (
    OfficialUrlResolver,
    ResolutionFailure,
    ResolutionResult,
)
from src.core.io import append_jsonl, read_jsonl, write_json
from src.core.ids import content_hash
from src.core.logging_setup import get_logger
from src.core.urls import extract_registrable_domain, normalize_url

logger = get_logger("candidates.resolution")

__all__ = [
    "RESOLVED_FILENAME",
    "RESOLUTION_REPORT_FILENAME",
    "RESOLUTION_STATE_FILENAME",
    "PROVENANCE_KEY",
    "UrlProvenance",
    "ResolutionRunReport",
    "OfficialUrlResolutionRunner",
    "record_key",
    "enriched_record",
    "summarize_resolved_file",
]

#: Enriched candidates: raw discovery + resolved official URLs.
RESOLVED_FILENAME = "candidates_resolved.jsonl"
#: Machine-readable statistics for the pass, derived from the output file.
RESOLUTION_REPORT_FILENAME = "candidates_resolution_report.json"
#: Resumable progress/audit state.
RESOLUTION_STATE_FILENAME = "candidates_resolution_state.json"

#: Where the official-URL provenance block lives on an enriched record.
PROVENANCE_KEY = "official_url_provenance"

#: How the record's official URL was obtained.
ORIGIN_DISCOVERY = "discovery_listing"
ORIGIN_DETAIL_PAGE = "directory_detail_page"
ORIGIN_NONE = "none"

#: Written on every enriched record so no downstream stage can mistake a
#: directory claim for a checked fact.
NOT_VERIFIED_NOTE = (
    "official URL resolution is not official-site verification: this URL is a "
    "claim published by a directory and still has to be verified against the "
    "product's own website"
)

DEFAULT_CHECKPOINT_EVERY = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# record identity
# --------------------------------------------------------------------------- #
def record_key(record: Mapping[str, Any]) -> str:
    """Stable key for one raw candidate record.

    Used only to recognise a record we have already resolved when resuming, so
    it has to be derived from fields that discovery actually wrote and that
    **this stage cannot change**. ``candidate_id`` is preferred when a record
    carries one (prepared feeds do); otherwise the discovery identity triple
    (source, name, detail URL) is hashed.

    ``website`` is deliberately *not* part of the key: resolution's whole job
    is to fill it in, so including it would give an enriched record a different
    key than the raw record it came from and a resumed pass would re-resolve —
    and duplicate — everything it had already written.
    """
    for key in ("candidate_id", "identity_key"):
        value = record.get(key) if isinstance(record, Mapping) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    name = str(record.get("name") or "") if isinstance(record, Mapping) else ""
    source_key = str(record.get("source_key") or "") if isinstance(record, Mapping) else ""
    listing = str(record.get("listing_url") or "") if isinstance(record, Mapping) else ""
    return content_hash(source_key, name, listing)


@dataclass
class _RecordView:
    """Attribute view of a raw record for :class:`OfficialUrlResolver`.

    The resolver reads ``name`` / ``candidate_id`` / ``listing_url`` /
    ``website`` off an object. Raw JSONL records are mappings, and rehydrating
    them into ``CandidateTool`` would silently drop the fields that class does
    not model. A view keeps the record itself untouched and authoritative.
    """

    name: str
    candidate_id: str
    listing_url: str | None
    website: str | None

    @classmethod
    def of(cls, record: Mapping[str, Any]) -> "_RecordView":
        def _text(value: Any) -> str | None:
            return value.strip() if isinstance(value, str) and value.strip() else None

        return cls(
            name=_text(record.get("name")) or "",
            candidate_id=record_key(record),
            listing_url=_text(record.get("listing_url")),
            website=_text(record.get("website")),
        )


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
@dataclass
class UrlProvenance:
    """Where a candidate's official URL came from, and on what evidence.

    Deliberately explicit about the difference between *discovery published a
    website on the listing card* and *we read one off the detail page* — those
    are different evidence levels, and verification wants to know which it is.
    """

    origin: str
    resolved_at: str | None = None
    #: Directory that produced the claim (source key + display name).
    directory_source_key: str | None = None
    directory_source_name: str | None = None
    #: Detail page that was read (or would have been read).
    detail_url: str | None = None
    detail_final_url: str | None = None
    detail_status: int | None = None
    #: Evidence type: ``json_ld_software_url`` / ``outbound_visit_link``.
    basis: str | None = None
    evidence: str | None = None
    declared_name: str | None = None
    website_domain: str | None = None
    failure: str | None = None
    fetched: bool = False
    live: bool = False
    needs_review: bool = False
    review_notes: list[str] = field(default_factory=list)
    #: Always false here. Resolution never verifies anything.
    verified: bool = False
    verification_status: str = "unverified"
    note: str = NOT_VERIFIED_NOTE

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "resolved_at": self.resolved_at,
            "directory_source_key": self.directory_source_key,
            "directory_source_name": self.directory_source_name,
            "detail_url": self.detail_url,
            "detail_final_url": self.detail_final_url,
            "detail_status": self.detail_status,
            "basis": self.basis,
            "evidence": self.evidence,
            "declared_name": self.declared_name,
            "website_domain": self.website_domain,
            "failure": self.failure,
            "fetched": self.fetched,
            "live": self.live,
            "needs_review": self.needs_review,
            "review_notes": list(self.review_notes),
            "verified": self.verified,
            "verification_status": self.verification_status,
            "note": self.note,
        }


def enriched_record(
    record: Mapping[str, Any],
    result: ResolutionResult | None,
    *,
    live: bool = False,
    resolved_at: str | None = None,
) -> dict[str, Any]:
    """Build the enriched output record for one raw candidate.

    Pure (apart from the timestamp the caller passes in), so the same record +
    result always produces the same output line.

    Every original key is copied through unchanged. ``website`` is only written
    when the record had none *and* the detail page published one.
    """
    out = dict(record)
    source_key = record.get("source_key")
    source_name = record.get("source_name")

    existing = record.get("website")
    existing = existing.strip() if isinstance(existing, str) else None

    if existing:
        provenance = UrlProvenance(
            origin=ORIGIN_DISCOVERY,
            directory_source_key=source_key if isinstance(source_key, str) else None,
            directory_source_name=source_name if isinstance(source_name, str) else None,
            detail_url=record.get("listing_url") if isinstance(record.get("listing_url"), str) else None,
            website_domain=extract_registrable_domain(existing),
            failure=ResolutionFailure.ALREADY_RESOLVED,
            live=live,
        )
        # Keep the observed value exactly as discovery stored it.
        out["website"] = existing
        out[PROVENANCE_KEY] = provenance.to_dict()
        return out

    if result is None:
        # Nothing was attempted for this record (e.g. it was unreadable).
        provenance = UrlProvenance(
            origin=ORIGIN_NONE,
            directory_source_key=source_key if isinstance(source_key, str) else None,
            directory_source_name=source_name if isinstance(source_name, str) else None,
            detail_url=record.get("listing_url") if isinstance(record.get("listing_url"), str) else None,
            failure=ResolutionFailure.RESOLUTION_FAILED,
            live=live,
            needs_review=True,
            review_notes=["the record could not be read well enough to attempt resolution"],
        )
        out["website"] = None
        out[PROVENANCE_KEY] = provenance.to_dict()
        return out

    evidence = result.page_evidence
    provenance = UrlProvenance(
        origin=ORIGIN_DETAIL_PAGE if result.resolved else ORIGIN_NONE,
        resolved_at=(resolved_at or _now()) if result.fetched else None,
        directory_source_key=source_key if isinstance(source_key, str) else None,
        directory_source_name=source_name if isinstance(source_name, str) else None,
        detail_url=result.detail_url,
        detail_final_url=evidence.final_url if evidence else None,
        detail_status=evidence.status if evidence else None,
        basis=result.basis,
        evidence=result.evidence,
        declared_name=result.declared_name,
        website_domain=result.website_domain,
        failure=result.failure,
        fetched=result.fetched,
        live=live,
        needs_review=result.needs_review,
        review_notes=list(result.review_notes),
    )

    if result.resolved:
        # Normalize through the project's single URL normalizer so a resolved
        # URL is shaped exactly like a discovered one (tracking params dropped,
        # host lower-cased, fragment removed).
        normalized = normalize_url(result.website) or result.website
        out["website"] = normalized
        provenance.website_domain = extract_registrable_domain(normalized) or result.website_domain
    else:
        out["website"] = None

    out[PROVENANCE_KEY] = provenance.to_dict()
    return out


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
@dataclass
class ResolutionRunReport:
    """Statistics for the enriched dataset, derived from the dataset itself."""

    input_path: str | None = None
    output_path: str | None = None
    generated_at: str | None = None
    live: bool = False
    input_records: int = 0
    input_unreadable_lines: int = 0
    output_records: int = 0
    already_had_official_url: int = 0
    needed_resolution: int = 0
    attempted: int = 0
    resolved: int = 0
    unresolved: int = 0
    skipped_no_detail_url: int = 0
    fetch_failures: int = 0
    http_errors: int = 0
    bot_challenges: int = 0
    parse_failures: int = 0
    identity_mismatches: int = 0
    no_outbound_url: int = 0
    needs_review: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    bases: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    #: ``None`` while a pass is still running. Pending is the set difference
    #: between the in-scope input keys and what is persisted, so it is only
    #: knowable once the whole input has been streamed. Reporting the
    #: part-way value would publish a confident ``0`` mid-pass.
    pending: int | None = 0
    #: ``None`` when the pass covered the whole feed, otherwise the sorted
    #: ``--source`` keys it was restricted to. ``pending`` is counted *within
    #: this scope*, so without it a source-filtered report's ``pending`` looks
    #: inconsistent with ``input_records - output_records``.
    scope_sources: list[str] | None = None
    #: False while a pass is still running (the report is refreshed at every
    #: checkpoint so an interrupted pass still describes the file on disk).
    complete: bool = False

    @property
    def resolution_rate(self) -> float | None:
        """Share of *attempted* candidates that yielded a published URL."""
        if not self.attempted:
            return None
        return round(self.resolved / self.attempted, 4)

    @property
    def coverage_rate(self) -> float | None:
        """Share of candidates lacking a website that now have one."""
        if not self.needed_resolution:
            return None
        return round(self.resolved / self.needed_resolution, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_path": self.input_path,
            "output_path": self.output_path,
            "generated_at": self.generated_at,
            "live": self.live,
            "complete": self.complete,
            "scope_sources": list(self.scope_sources) if self.scope_sources else None,
            "input_records": self.input_records,
            "input_unreadable_lines": self.input_unreadable_lines,
            "output_records": self.output_records,
            "pending": self.pending,
            "pending_scope": (
                "whole feed"
                if not self.scope_sources
                else "sources: " + ", ".join(self.scope_sources)
            ),
            "pending_known": self.pending is not None,
            "already_had_official_url": self.already_had_official_url,
            "needed_resolution": self.needed_resolution,
            "attempted": self.attempted,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "skipped_no_detail_url": self.skipped_no_detail_url,
            "fetch_failures": self.fetch_failures,
            "http_errors": self.http_errors,
            "bot_challenges": self.bot_challenges,
            "parse_failures": self.parse_failures,
            "identity_mismatches": self.identity_mismatches,
            "no_outbound_url": self.no_outbound_url,
            "needs_review": self.needs_review,
            "resolution_rate_of_attempted": self.resolution_rate,
            "resolution_rate_of_missing_official_url": self.coverage_rate,
            "failures": dict(sorted(self.failures.items())),
            "evidence_types": dict(sorted(self.bases.items())),
            "by_source": {k: dict(sorted(v.items())) for k, v in sorted(self.by_source.items())},
            "note": NOT_VERIFIED_NOTE,
        }


_PARSE_FAILURE_CODES = frozenset(
    {
        ResolutionFailure.UNPARSEABLE_HTML,
        ResolutionFailure.NON_HTML_RESPONSE,
        ResolutionFailure.EMPTY_RESPONSE,
        ResolutionFailure.RESOLUTION_FAILED,
    }
)


def summarize_resolved_file(path: str | Path) -> ResolutionRunReport:
    """Recompute the report by reading the persisted enriched dataset.

    The report is therefore always a description of what is actually on disk,
    including after a resumed or partial pass. No counter can drift out of
    sync, and no statistic can be invented.
    """
    report = ResolutionRunReport(output_path=str(path))
    for record in read_jsonl(path):
        if not isinstance(record, Mapping):
            continue
        report.output_records += 1
        provenance = record.get(PROVENANCE_KEY)
        provenance = provenance if isinstance(provenance, Mapping) else {}
        source = str(record.get("source_key") or "unknown")
        bucket = report.by_source.setdefault(
            source,
            {
                "records": 0,
                "already_had_official_url": 0,
                "needed_resolution": 0,
                "attempted": 0,
                "resolved": 0,
                "unresolved": 0,
                "skipped_no_detail_url": 0,
            },
        )
        bucket["records"] += 1

        origin = provenance.get("origin")
        failure = provenance.get("failure")
        if origin == ORIGIN_DISCOVERY:
            report.already_had_official_url += 1
            bucket["already_had_official_url"] += 1
            continue

        report.needed_resolution += 1
        bucket["needed_resolution"] += 1
        if provenance.get("fetched"):
            report.attempted += 1
            bucket["attempted"] += 1
        if provenance.get("needs_review"):
            report.needs_review += 1

        if record.get("website"):
            report.resolved += 1
            bucket["resolved"] += 1
            basis = provenance.get("basis")
            if isinstance(basis, str) and basis:
                report.bases[basis] = report.bases.get(basis, 0) + 1
        else:
            report.unresolved += 1
            bucket["unresolved"] += 1

        if isinstance(failure, str) and failure:
            report.failures[failure] = report.failures.get(failure, 0) + 1
            if failure == ResolutionFailure.NO_DETAIL_URL:
                report.skipped_no_detail_url += 1
                bucket["skipped_no_detail_url"] += 1
            elif failure == ResolutionFailure.FETCH_FAILED:
                report.fetch_failures += 1
            elif failure == ResolutionFailure.HTTP_ERROR:
                report.http_errors += 1
            elif failure == ResolutionFailure.BOT_CHALLENGE:
                report.bot_challenges += 1
            elif failure == ResolutionFailure.IDENTITY_MISMATCH:
                report.identity_mismatches += 1
            elif failure in (
                ResolutionFailure.NO_OUTBOUND_URL,
                ResolutionFailure.ONLY_SHARED_HOSTS,
            ):
                report.no_outbound_url += 1
            elif failure in _PARSE_FAILURE_CODES:
                report.parse_failures += 1
    return report


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class OfficialUrlResolutionRunner:
    """Resolve official URLs across a persisted candidate feed, resumably.

    Parameters
    ----------
    client:
        HTTP client used for detail pages. Defaults to
        :class:`~src.core.http_client.OfflineHttpClient` unless ``live`` is
        set, so nothing reaches the network by accident.
    checkpoint_every:
        How often the state file is refreshed. Output records themselves are
        appended immediately regardless, so this only affects the progress
        marker, never data safety.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        live: bool = False,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
        host_rates: Mapping[str, float] | None = None,
    ) -> None:
        self.live = bool(live)
        if client is None:
            if self.live:
                from src.core.http_client import HttpClient

                client = HttpClient()
            else:
                from src.core.http_client import OfflineHttpClient

                client = OfflineHttpClient()
        self.client = client
        self.resolver = OfficialUrlResolver(client)
        self.checkpoint_every = max(1, int(checkpoint_every))
        if host_rates:
            self._apply_host_rates(host_rates)

    def _apply_host_rates(self, host_rates: Mapping[str, float]) -> None:
        """Honour the per-source politeness configured in ``sources.yaml``."""
        limiter = getattr(self.client, "limiter", None)
        setter = getattr(limiter, "set_host_rate", None)
        if not callable(setter):
            return
        for host, rps in host_rates.items():
            try:
                setter(str(host), float(rps))
            except Exception:  # noqa: BLE001 - politeness config must not crash a run
                logger.debug("could not apply host rate", extra={"host": host})

    # ------------------------------------------------------------- resuming
    @staticmethod
    def completed_keys(output_path: str | Path) -> set[str]:
        """Keys already present in the enriched output (resume marker)."""
        done: set[str] = set()
        for record in read_jsonl(output_path):
            if isinstance(record, Mapping):
                done.add(record_key(record))
        return done

    # ------------------------------------------------------------------ run
    def run(
        self,
        input_path: str | Path,
        *,
        interim_dir: str | Path,
        limit: int | None = None,
        resume: bool = True,
        sources: set[str] | None = None,
    ) -> ResolutionRunReport:
        """Resolve official URLs for every record in ``input_path``.

        ``limit`` caps how many candidates are *fetched* in this pass (records
        that need no fetch are still written), which is what makes a deliberate
        partial production batch possible: whatever it completes is on disk and
        the next pass continues from there.
        """
        input_path = Path(input_path)
        interim = Path(interim_dir)
        interim.mkdir(parents=True, exist_ok=True)
        output_path = interim / RESOLVED_FILENAME
        state_path = interim / RESOLUTION_STATE_FILENAME

        done = self.completed_keys(output_path) if resume else set()
        if not resume and output_path.exists():
            # An explicit restart replaces the artefact rather than mixing two
            # passes in one file. Only ever done on request.
            output_path.unlink()

        seen_keys: set[str] = set()
        in_scope_keys: set[str] = set()
        input_records = 0
        unreadable = 0
        fetches = 0
        written = 0
        budget_spent = False
        started_at = _now()

        for record in self._iter_input(input_path):
            input_records += 1
            if not isinstance(record, Mapping):
                unreadable += 1
                continue
            source_key = record.get("source_key")
            if sources is not None and str(source_key or "") not in sources:
                continue
            key = record_key(record)
            in_scope_keys.add(key)
            if key in done or key in seen_keys:
                # Already persisted (resume) or a duplicate line for the same
                # candidate: never write the same candidate twice.
                continue

            needs_fetch = not record.get("website") and bool(record.get("listing_url"))
            if needs_fetch and limit is not None and fetches >= max(0, limit):
                # Budget spent: keep reading the input so the pending count is
                # exact, but write nothing we did not actually attempt.
                budget_spent = True
                continue

            result: ResolutionResult | None
            try:
                result = self.resolver.resolve_candidate(_RecordView.of(record), live=self.live)
            except Exception as exc:  # noqa: BLE001 - one record never stops a batch
                logger.warning(
                    "resolution raised; record persisted as unresolved",
                    extra={"key": key, "error": str(exc)[:200]},
                )
                result = None

            if result is not None and result.fetched:
                fetches += 1

            try:
                out = enriched_record(record, result, live=self.live)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "enrichment raised; record skipped",
                    extra={"key": key, "error": str(exc)[:200]},
                )
                continue

            append_jsonl(output_path, out)
            seen_keys.add(key)
            written += 1

            if written % self.checkpoint_every == 0:
                # The report is refreshed together with the state, not only at
                # the end of the pass: an interrupted run must still leave a
                # report that describes the rows actually on disk. Counting is
                # a cheap linear read of the output file, so doing it per
                # checkpoint costs far less than publishing a stale report.
                report = self._build_report(
                    output_path,
                    input_path=input_path,
                    input_records=input_records,
                    unreadable=unreadable,
                    # Not yet knowable: the input has only been streamed as
                    # far as this record, so the outstanding set is still
                    # being discovered. Publishing the part-way difference
                    # would claim a confident "0 pending" mid-pass.
                    pending=None,
                    sources=sources,
                    complete=False,
                )
                write_json(interim / RESOLUTION_REPORT_FILENAME, report.to_dict())
                self._write_state(
                    state_path,
                    input_path=input_path,
                    output_path=output_path,
                    started_at=started_at,
                    written=written,
                    fetches=fetches,
                    resumed_from=len(done),
                    finished=False,
                    report=report,
                )
                logger.info(
                    "resolution checkpoint",
                    extra={"written": written, "fetched": fetches},
                )

        # Pending is measured against the input, not inferred from a count
        # difference: exactly the in-scope candidates that are neither already
        # persisted nor written by this pass.
        report = self._build_report(
            output_path,
            input_path=input_path,
            input_records=input_records,
            unreadable=unreadable,
            pending=len(in_scope_keys - done - seen_keys),
            sources=sources,
            complete=True,
        )
        write_json(interim / RESOLUTION_REPORT_FILENAME, report.to_dict())
        self._write_state(
            state_path,
            input_path=input_path,
            output_path=output_path,
            started_at=started_at,
            written=written,
            fetches=fetches,
            resumed_from=len(done),
            finished=True,
            report=report,
        )
        return report

    # --------------------------------------------------------------- helpers
    def _build_report(
        self,
        output_path: Path,
        *,
        input_path: Path,
        input_records: int,
        unreadable: int,
        pending: int | None,
        sources: set[str] | None,
        complete: bool,
    ) -> ResolutionRunReport:
        """Recompute the report from the persisted dataset and stamp the pass.

        Single place where a report is produced, so a checkpoint report and a
        final report cannot describe the same file differently.
        """
        report = summarize_resolved_file(output_path)
        report.input_path = str(input_path)
        report.input_records = input_records
        report.input_unreadable_lines = unreadable
        report.live = self.live
        report.generated_at = _now()
        report.pending = pending
        report.scope_sources = sorted(sources) if sources else None
        report.complete = complete
        return report

    @staticmethod
    def _iter_input(path: Path) -> Iterator[Any]:
        """Stream raw records in file order (deterministic, malformed skipped)."""
        return read_jsonl(path)

    def _write_state(
        self,
        state_path: Path,
        *,
        input_path: Path,
        output_path: Path,
        started_at: str,
        written: int,
        fetches: int,
        resumed_from: int,
        finished: bool,
        report: ResolutionRunReport | None = None,
    ) -> None:
        """Refresh the resumable state file (append-only batch log)."""
        previous: dict[str, Any] = {}
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                previous = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                logger.warning("resolution state unreadable; starting fresh")
                previous = {}

        batches = previous.get("batches")
        batches = list(batches) if isinstance(batches, list) else []
        entry = {
            "started_at": started_at,
            "updated_at": _now(),
            "live": self.live,
            "resumed_from_records": resumed_from,
            "records_written": written,
            "detail_pages_fetched": fetches,
            "finished": finished,
        }
        if batches and batches[-1].get("started_at") == started_at:
            batches[-1] = entry
        else:
            batches.append(entry)

        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": _now(),
            "input_path": str(input_path),
            "output_path": str(output_path),
            "records_persisted": (
                report.output_records if report is not None else resumed_from + written
            ),
            # Bounded audit trail, like discovery state.
            "batches": batches[-50:],
        }
        if report is not None:
            payload["last_report"] = {
                "input_records": report.input_records,
                "output_records": report.output_records,
                "pending": report.pending,
                "already_had_official_url": report.already_had_official_url,
                "needed_resolution": report.needed_resolution,
                "attempted": report.attempted,
                "resolved": report.resolved,
                "unresolved": report.unresolved,
            }
        write_json(state_path, payload)
