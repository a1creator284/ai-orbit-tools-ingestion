"""Pipeline orchestration.

Stage order (technical spec §2 + Tools guideline §2 "Discovery → Official
website → Verify → Enrich → Deduplicate → Curate")::

    discovery
      → extraction
      → candidate preparation  (normalize + validate identity, still unverified)
      → verify_candidates   (official website is authoritative; opt-in network)
      → cleaning / normalization
      → verification        (official website is authoritative)
      → quality filtering   (drops records already rejected upstream)
      → deduplication / entity resolution
      → scoring             (100-point framework)
      → threshold filtering (reject / skip / selective / include, §5 bands)
      → enrichment
      → descriptions        (LLM, editorial only)
      → relationships
      → validation
      → selection           (best N, never the first N)

Every stage is independently runnable, records its own statistics, and persists
its artefact under ``data/``, so a later run can resume from any stage.
Stages are resilient: a failure on one record is logged and skipped rather than
aborting the batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src.candidates.prepare import CandidatePreparer, PreparedCandidate
from src.candidates.store import load_discovery_dir
from src.cleaning.normalizer import ToolNormalizer
from src.core.config import Settings, get_settings
from src.core.http_client import OfflineHttpClient
from src.core.io import write_json, write_jsonl
from src.core.logging_setup import get_logger, setup_logging
from src.deduplication.deduplicator import Deduplicator
from src.discovery.base import CandidateTool, DiscoverySource
from src.discovery.registry import SourceRegistry
from src.discovery.runner import DiscoveryRunner
from src.enrichment.base import EnrichmentPipeline
from src.enrichment.relationships import RelationshipExtractor
from src.models.base import Relationship
from src.models.tool import Tool
from src.scoring.filter import FilterDecision, FilterReport, QualityFilter
from src.scoring.scorer import QualityScorer, rank_and_select
from src.validation.validator import ToolValidator
from src.verification.store import persist_verification, verification_record
from src.verification.verifier import (
    OfficialSiteVerifier,
    VerificationReport,
    VerificationResult,
)

logger = get_logger("pipeline")

STAGES = (
    "discovery",
    "candidate_preparation",
    "verify_candidates",
    "normalization",
    "verification",
    "quality_filter",
    "deduplication",
    "scoring",
    "threshold_filter",
    "enrichment",
    "relationships",
    "validation",
    "selection",
)


@dataclass
class StageStats:
    """Per-stage counters for the run report."""

    name: str
    input_count: int = 0
    output_count: int = 0
    dropped_count: int = 0
    error_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "input": self.input_count,
            "output": self.output_count,
            "dropped": self.dropped_count,
            "errors": self.error_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": self.details,
        }


@dataclass
class RunReport:
    """Auditable summary of a full pipeline run."""

    started_at: str
    finished_at: str | None = None
    dry_run: bool = True
    batch_number: int = 1
    stages: list[StageStats] = field(default_factory=list)
    selected_count: int = 0
    rejected_count: int = 0
    review_count: int = 0
    relationship_count: int = 0
    artefacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def stage(self, name: str) -> StageStats:
        for stats in self.stages:
            if stats.name == name:
                return stats
        stats = StageStats(name=name)
        self.stages.append(stats)
        return stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "batch_number": self.batch_number,
            "selected_count": self.selected_count,
            "rejected_count": self.rejected_count,
            "review_count": self.review_count,
            "relationship_count": self.relationship_count,
            "stages": [s.to_dict() for s in self.stages],
            "artefacts": self.artefacts,
            "notes": self.notes,
        }


class ToolsPipeline:
    """Orchestrates the Tools ingestion pipeline.

    All collaborators are injected, so each stage can be unit-tested in
    isolation and a new discovery source needs no orchestrator change.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: SourceRegistry | None = None,
        normalizer: ToolNormalizer | None = None,
        verifier: OfficialSiteVerifier | None = None,
        deduplicator: Deduplicator | None = None,
        scorer: QualityScorer | None = None,
        quality_filter: QualityFilter | None = None,
        preparer: CandidatePreparer | None = None,
        enrichment: EnrichmentPipeline | None = None,
        relationships: RelationshipExtractor | None = None,
        validator: ToolValidator | None = None,
        describer: Callable[[Tool], Tool] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        setup_logging(self.settings.log_level)
        self.registry = registry if registry is not None else SourceRegistry()
        self.normalizer = normalizer or ToolNormalizer(
            batch_number=self.settings.batch.batch_number
        )
        self.verifier = verifier
        self.preparer = preparer or CandidatePreparer()
        self.deduplicator = deduplicator or Deduplicator()
        self.scorer = scorer or QualityScorer(self.settings.scoring)
        self.quality_filter = quality_filter or QualityFilter(self.settings.scoring)
        #: Decisions from the last threshold-filter pass (persisted in _persist).
        self._filter_decisions: list[FilterDecision] = []
        self.enrichment = enrichment or EnrichmentPipeline()
        self.relationships = relationships or RelationshipExtractor()
        self.validator = validator or ToolValidator()
        self.describer = describer
        self.report = RunReport(
            started_at=_now(),
            dry_run=self.settings.dry_run,
            batch_number=self.settings.batch.batch_number,
        )

    # =====================================================  individual stages
    def discover(
        self,
        *,
        limit_per_source: int | None = None,
        source_keys: Sequence[str] | None = None,
        persist_raw: bool = True,
        **discover_kwargs: Any,
    ) -> list[CandidateTool]:
        """Collect candidates from every enabled, implemented discovery source.

        Delegates to :class:`~src.discovery.runner.DiscoveryRunner` so that raw
        per-source candidates, provenance and the discovery audit trail are
        persisted separately from processed data — regardless of who triggers
        discovery (pipeline run or the standalone ``discover`` command).
        """
        stats = self.report.stage("discovery")
        stats.started_at = _now()

        sources: list[DiscoverySource] = list(self.registry.active(role="discovery"))
        if not sources and not source_keys:
            note = (
                "no discovery adapters are enabled/implemented; "
                f"configured sources: {len(self.registry.configs)}"
            )
            stats.details["warning"] = note
            self.report.notes.append(note)
            logger.warning(note)
            stats.finished_at = _now()
            return []

        runner = DiscoveryRunner(self.settings, registry=self.registry)
        candidates, discovery_report = runner.run(
            source_keys=source_keys,
            limit_per_source=limit_per_source,
            persist=persist_raw,
            **discover_kwargs,
        )

        usable = [c for c in candidates if c.is_usable]
        stats.dropped_count = len(candidates) - len(usable)
        stats.error_count = sum(
            1 for data in discovery_report.sources.values() if data.get("errors")
        )
        stats.output_count = len(usable)
        stats.details["by_source"] = discovery_report.sources
        stats.details["cross_source_duplicates"] = discovery_report.cross_source_duplicates
        stats.details["raw_artefacts"] = discovery_report.artefacts
        self.report.notes.extend(discovery_report.notes)
        stats.finished_at = _now()
        return usable

    def load_candidates(
        self, *, source_keys: Sequence[str] | None = None
    ) -> list[CandidateTool]:
        """Rehydrate candidates from ``data/raw/discovery/`` without re-crawling.

        Lets the pipeline resume from a previous discovery pass. Loading grants
        no verified status: these are still unverified sightings.
        """
        candidates, load_report = load_discovery_dir(
            self.settings.paths.resolve("raw"), source_keys=source_keys
        )
        stats = self.report.stage("discovery")
        stats.started_at = stats.started_at or _now()
        stats.details["loaded_from_disk"] = load_report.to_dict()
        stats.output_count = len(candidates)
        stats.finished_at = _now()
        return candidates

    def prepare_candidates(
        self, candidates: Sequence[CandidateTool], *, persist: bool = True
    ) -> list[tuple[CandidateTool, PreparedCandidate]]:
        """Normalize + identity-validate candidates (still unverified).

        Produces :class:`~src.candidates.prepare.PreparedCandidate` records,
        persisted under ``data/interim/`` so unverified candidates can never be
        confused with the verified records published to ``data/final/``.

        Returns ``(candidate, prepared)`` pairs: only candidates that passed
        required-identity validation continue to normalization, and each keeps
        an explicit link to the prepared record that admitted it.
        """
        stats = self.report.stage("candidate_preparation")
        stats.started_at = _now()
        stats.input_count = len(candidates)

        pairs, rejected, prep_report = self.preparer.prepare_batch(candidates)
        if persist:
            prep_report = self.preparer.persist(
                [prepared for _, prepared in pairs],
                prep_report,
                interim_dir=self.settings.paths.resolve("interim"),
                rejected=rejected,
            )

        stats.output_count = len(pairs)
        stats.dropped_count = len(rejected)
        stats.details = prep_report.to_dict()
        stats.finished_at = _now()

        if prep_report.needs_review_count:
            self.report.notes.append(
                f"candidate preparation flagged {prep_report.needs_review_count} "
                "candidate(s) for human review (weak identity or no official URL) — "
                "flagged, not dropped"
            )
        return pairs

    def verify_candidates(
        self,
        prepared: Sequence[Any],
        *,
        live: bool = False,
        limit: int | None = None,
        persist: bool = True,
    ) -> tuple[list[VerificationResult], VerificationReport]:
        """Verify prepared candidates against their **official** websites.

        Sits between candidate preparation and normalization: a candidate is a
        directory *sighting*, and this is the stage that goes and looks at the
        product's own site (guideline §10). It reuses
        :class:`~src.verification.verifier.OfficialSiteVerifier` verbatim — the
        decision rules are not re-implemented, extended or relaxed here.

        Network access is **opt-in**:

        * ``live=False`` (default) swaps in
          :class:`~src.core.http_client.OfflineHttpClient`, so not a single
          socket is opened. Every candidate still flows through the real
          verifier and comes back ``unreachable``/``failed`` — an honest "we
          learned nothing", never a fabricated pass;
        * ``live=True`` uses the injected/real :class:`HttpClient`.

        ``limit`` caps how many candidates are processed, so the first live
        pass can be a small spot check instead of a bulk crawl.

        Results are persisted to ``data/interim/`` with discovery provenance
        and official evidence kept in separate blocks. Nothing is written to
        ``data/final/``.
        """
        stats = self.report.stage("verify_candidates")
        stats.started_at = _now()
        stats.input_count = len(prepared)

        batch = list(prepared)[:limit] if limit is not None and limit >= 0 else list(prepared)
        stats.details["live"] = live
        stats.details["limit"] = limit
        stats.details["considered"] = len(batch)
        stats.details["skipped_by_limit"] = len(prepared) - len(batch)
        stats.details["with_official_url"] = sum(
            1 for candidate in batch if getattr(candidate, "website", None)
        )

        verifier = self._verifier_for(live=live)
        results, report = verifier.verify_candidates(batch)

        stats.output_count = len(results)
        stats.details["report"] = report.to_dict()
        if not live:
            note = (
                "verify_candidates ran offline (no --live): no network calls were "
                "made, so no candidate could be verified from real page evidence"
            )
            stats.details["offline"] = note
            self.report.notes.append(note)

        if persist:
            records = [
                verification_record(candidate, result, live=live)
                for candidate, result in zip(batch, results)
            ]
            artefacts = persist_verification(
                records,
                report,
                interim_dir=self.settings.paths.resolve("interim"),
                extra_report_fields={
                    "live": live,
                    "limit": limit,
                    "input_candidates": len(prepared),
                    "considered": len(batch),
                },
            )
            stats.details["artefacts"] = artefacts
            self.report.artefacts.update(artefacts)

        stats.finished_at = _now()
        return results, report

    def _verifier_for(self, *, live: bool) -> OfficialSiteVerifier:
        """The verifier to use for this pass — offline unless explicitly live.

        An injected verifier always wins (tests and callers stay in control);
        otherwise a live pass gets the real HTTP client and an offline pass gets
        a client that physically cannot reach the network.
        """
        if self.verifier is not None:
            return self.verifier
        if live:
            return OfficialSiteVerifier()
        return OfficialSiteVerifier(client=OfflineHttpClient())  # type: ignore[arg-type]

    def normalize(self, candidates: Sequence[CandidateTool]) -> list[Tool]:
        """Clean + normalize candidates into canonical records."""
        stats = self.report.stage("normalization")
        stats.started_at = _now()
        stats.input_count = len(candidates)
        tools: list[Tool] = []
        for candidate in candidates:
            try:
                tool = self.normalizer.from_candidate(candidate)
            except Exception as exc:  # noqa: BLE001
                stats.error_count += 1
                logger.warning(
                    "normalization failed", extra={"name": candidate.name, "error": str(exc)}
                )
                continue
            if tool is None:
                stats.dropped_count += 1
                continue
            tools.append(tool)
        stats.output_count = len(tools)
        stats.finished_at = _now()
        return tools

    def verify(self, tools: Sequence[Tool], *, extractors: Iterable[Any] = ()) -> list[Tool]:
        """Verify each tool against its official website (guideline §10)."""
        stats = self.report.stage("verification")
        stats.started_at = _now()
        stats.input_count = len(tools)

        if self.settings.dry_run:
            note = "dry_run enabled: official-website verification skipped"
            stats.details["skipped"] = note
            self.report.notes.append(note)
            stats.output_count = len(tools)
            stats.finished_at = _now()
            return list(tools)

        verifier = self.verifier or OfficialSiteVerifier()
        for tool in tools:
            try:
                verifier.verify(tool, extractors=extractors)
            except Exception as exc:  # noqa: BLE001
                stats.error_count += 1
                logger.warning("verification failed", extra={"tool": tool.name, "error": str(exc)})
        stats.output_count = len(tools)
        stats.details["verified"] = sum(1 for tool in tools if tool.is_verified)
        stats.finished_at = _now()
        return list(tools)

    def filter_quality(self, tools: Sequence[Tool]) -> list[Tool]:
        """Drop records already rejected by verification (guideline §4)."""
        stats = self.report.stage("quality_filter")
        stats.started_at = _now()
        stats.input_count = len(tools)
        kept = [tool for tool in tools if not tool.rejected]
        stats.dropped_count = len(tools) - len(kept)
        stats.output_count = len(kept)
        reasons: dict[str, int] = {}
        for tool in tools:
            for reason in tool.rejection_reasons:
                key = str(reason)
                reasons[key] = reasons.get(key, 0) + 1
        stats.details["rejection_reasons"] = reasons
        stats.finished_at = _now()
        return kept

    def deduplicate(self, tools: Sequence[Tool]) -> list[Tool]:
        """Collapse the same underlying product into one record (guideline §7)."""
        stats = self.report.stage("deduplication")
        stats.started_at = _now()
        stats.input_count = len(tools)
        canonical, dedup_report = self.deduplicator.deduplicate(tools)
        stats.output_count = len(canonical)
        stats.dropped_count = dedup_report.merged_count
        stats.details = dedup_report.to_dict()
        stats.finished_at = _now()
        return canonical

    def score(self, tools: Sequence[Tool]) -> list[Tool]:
        """Apply the 100-point framework (guideline §5).

        Scoring is pure and non-destructive: every record gets all eight
        rubric components plus a per-criterion breakdown, and **nothing is
        admitted or rejected here**. Threshold decisions are
        :meth:`apply_thresholds`'s job, so the score of a record never depends
        on what the batch needs.
        """
        stats = self.report.stage("scoring")
        stats.started_at = _now()
        stats.input_count = len(tools)
        bands: dict[str, int] = {}
        currency: dict[str, int] = {}
        unevidenced: dict[str, int] = {}
        scored = 0
        for tool in tools:
            try:
                self.scorer.apply(tool)
            except Exception as exc:  # noqa: BLE001
                stats.error_count += 1
                logger.warning("scoring failed", extra={"tool": tool.name, "error": str(exc)})
                continue
            scored += 1
            band = tool.band or "unscored"
            bands[band] = bands.get(band, 0) + 1
            breakdown = tool.quality
            if breakdown is not None:
                state = breakdown.currency_state or "unknown"
                currency[state] = currency.get(state, 0) + 1
                for component in breakdown.unevidenced_components:
                    unevidenced[component] = unevidenced.get(component, 0) + 1
        stats.details["bands"] = dict(sorted(bands.items()))
        stats.details["currency_states"] = dict(sorted(currency.items()))
        stats.details["unevidenced_components"] = dict(sorted(unevidenced.items()))
        stats.details["scored"] = scored
        stats.details["weights"] = dict(self.settings.scoring.weights)
        stats.output_count = len(tools)
        stats.finished_at = _now()
        return list(tools)

    def apply_thresholds(
        self, tools: Sequence[Tool]
    ) -> tuple[list[Tool], list[FilterDecision], FilterReport]:
        """Apply the guideline §5 score thresholds (reject/skip/selective/include).

        Returns ``(kept, decisions, report)``. Every input record produces a
        decision — including the dropped ones — so no record can leave the
        pipeline without a recorded reason. The batch is never padded here:
        the filter has no notion of a target count.
        """
        stats = self.report.stage("threshold_filter")
        stats.started_at = _now()
        stats.input_count = len(tools)

        kept, decisions, report = self.quality_filter.filter(tools)

        stats.output_count = len(kept)
        stats.dropped_count = len(tools) - len(kept)
        stats.details = report.to_dict()
        self.report.notes.extend(report.notes)
        stats.finished_at = _now()
        self._filter_decisions = list(decisions)
        return kept, decisions, report

    def enrich(self, tools: Sequence[Tool]) -> list[Tool]:
        """Fill remaining gaps from secondary sources (never overwriting official data)."""
        stats = self.report.stage("enrichment")
        stats.started_at = _now()
        stats.input_count = len(tools)
        if not self.enrichment.enrichers:
            stats.details["skipped"] = "no enrichers registered"
        else:
            for tool in tools:
                try:
                    self.enrichment.run(tool)
                except Exception as exc:  # noqa: BLE001
                    stats.error_count += 1
                    logger.warning("enrichment failed", extra={"tool": tool.name, "error": str(exc)})
        if self.describer is not None:
            for tool in tools:
                try:
                    self.describer(tool)
                except Exception as exc:  # noqa: BLE001
                    stats.error_count += 1
                    logger.warning("describer failed", extra={"tool": tool.name, "error": str(exc)})
        stats.output_count = len(tools)
        stats.finished_at = _now()
        return list(tools)

    def map_relationships(self, tools: Sequence[Tool]) -> list[Relationship]:
        """Build the relationship graph (technical spec §5)."""
        stats = self.report.stage("relationships")
        stats.started_at = _now()
        stats.input_count = len(tools)
        edges = self.relationships.extract_many(tools)
        by_type: dict[str, int] = {}
        for edge in edges:
            by_type[str(edge.relationship)] = by_type.get(str(edge.relationship), 0) + 1
        stats.details["by_type"] = by_type
        stats.output_count = len(edges)
        stats.finished_at = _now()
        self.report.relationship_count = len(edges)
        return edges

    def validate(self, tools: Sequence[Tool]) -> tuple[list[Tool], list[dict]]:
        """Run schema + business validation; returns ``(publishable, results)``."""
        stats = self.report.stage("validation")
        stats.started_at = _now()
        stats.input_count = len(tools)
        results, summary = self.validator.validate_many(tools)
        publishable_ids = {r.tool_id for r in results if r.is_publishable}
        publishable = [tool for tool in tools if tool.id in publishable_ids]
        stats.output_count = len(publishable)
        stats.dropped_count = len(tools) - len(publishable)
        stats.details = summary
        stats.finished_at = _now()
        return publishable, [r.to_dict() for r in results]

    def select(self, tools: Sequence[Tool]) -> tuple[list[Tool], list[Tool]]:
        """Select the *best* N tools for the batch (guideline §1, §5, §11)."""
        stats = self.report.stage("selection")
        stats.started_at = _now()
        stats.input_count = len(tools)
        selected, not_selected = rank_and_select(
            list(tools),
            target_size=self.settings.batch.target_size,
            max_per_primary_task=self.settings.batch.max_per_primary_task,
            min_score=self.settings.scoring.include_at_or_above,
        )
        stats.output_count = len(selected)
        stats.dropped_count = len(not_selected)
        stats.details["target_size"] = self.settings.batch.target_size
        if len(selected) < self.settings.batch.target_size:
            note = (
                f"only {len(selected)} tools met the quality threshold "
                f"(target {self.settings.batch.target_size}); the batch is NOT padded "
                "— guideline §11"
            )
            stats.details["under_target"] = note
            self.report.notes.append(note)
        stats.finished_at = _now()
        return selected, not_selected

    # ==========================================================  full run
    def run(
        self,
        *,
        limit_per_source: int | None = None,
        candidates: Sequence[CandidateTool] | None = None,
        persist: bool = True,
        verify_limit: int | None = None,
    ) -> RunReport:
        """Execute every stage end to end and persist the artefacts.

        ``verify_limit`` caps the candidate-verification stage, so a live run
        can be kept to a deliberate spot check rather than a bulk crawl.
        """
        logger.info(
            "pipeline run starting",
            extra={
                "dry_run": self.settings.dry_run,
                "batch": self.settings.batch.batch_number,
                "target": self.settings.batch.target_size,
            },
        )
        self.settings.paths.ensure()

        discovered = list(candidates) if candidates is not None else self.discover(
            limit_per_source=limit_per_source
        )
        if candidates is not None:
            stats = self.report.stage("discovery")
            stats.started_at = stats.started_at or _now()
            stats.output_count = len(discovered)
            stats.details["injected"] = True
            stats.finished_at = _now()

        # Candidate preparation is 1-in → 1-out and rejects only candidates
        # that fail required-identity validation, so normalization consumes
        # exactly the candidates that survived it.
        pairs = self.prepare_candidates(discovered, persist=persist)

        # Official-website verification of the *candidates*, before they become
        # Tool records. Network access follows the run's own dry_run setting:
        # a dry run stays strictly offline.
        self.verify_candidates(
            [prepared for _, prepared in pairs],
            live=not self.settings.dry_run,
            limit=verify_limit,
            persist=persist,
        )

        tools = self.normalize([candidate for candidate, _ in pairs])
        tools = self.verify(tools)
        tools = self.filter_quality(tools)
        tools = self.deduplicate(tools)
        scored = self.score(tools)
        # Threshold filtering: <60 reject, 60-69 skip, 70-79 selective, 80+
        # include. Records that do not pass keep their score and their reason
        # and stay available as rejected artefacts — they are not deleted.
        above_threshold, _decisions, _filter_report = self.apply_thresholds(scored)
        above_threshold = self.enrich(above_threshold)
        publishable, validation_results = self.validate(above_threshold)
        selected, not_selected = self.select(publishable)
        edges = self.map_relationships(selected)

        # Identity comparison (``id()``), not equality: two distinct records
        # can compare equal field-by-field and must not cancel each other out.
        kept_ids = {id(tool) for tool in above_threshold}
        dropped_by_threshold = [tool for tool in scored if id(tool) not in kept_ids]
        self.report.selected_count = len(selected)
        self.report.rejected_count = (
            len(not_selected)
            + len(dropped_by_threshold)
            + (len(above_threshold) - len(publishable))
        )
        self.report.review_count = sum(1 for tool in selected if tool.needs_human_review)
        self.report.finished_at = _now()

        if persist:
            self._persist(
                discovered,
                scored,
                selected,
                [*not_selected, *dropped_by_threshold],
                edges,
                validation_results,
            )

        logger.info("pipeline run complete", extra=self.report.to_dict()["stages"][-1])
        return self.report

    # ------------------------------------------------------------ persistence
    def _persist(
        self,
        candidates: Sequence[CandidateTool],
        normalized: Sequence[Tool],
        selected: Sequence[Tool],
        not_selected: Sequence[Tool],
        edges: Sequence[Relationship],
        validation_results: Sequence[dict],
    ) -> None:
        paths = self.settings.paths
        artefacts: dict[str, Path] = {
            "raw_candidates": write_jsonl(
                paths.resolve("raw") / "candidates.jsonl", [c.to_dict() for c in candidates]
            ),
            "processed_tools": write_jsonl(
                paths.resolve("processed") / "tools.jsonl",
                [t.to_dict() for t in normalized],
            ),
            "final_tools": write_json(
                paths.resolve("final") / "tools.json", [t.to_dict() for t in selected]
            ),
            "relationships": write_json(
                paths.resolve("final") / "relationships.json", [e.to_dict() for e in edges]
            ),
            "rejected": write_jsonl(
                paths.resolve("processed") / "rejected.jsonl",
                [
                    {
                        "id": tool.id,
                        "name": tool.name,
                        "website": tool.website,
                        "score": tool.score,
                        "band": tool.band,
                        "reasons": [str(r) for r in tool.rejection_reasons],
                        "notes": tool.rejection_notes,
                        # Provenance travels with the rejection: a reader can
                        # always tell which directory claimed the record.
                        "discovery_sources": [
                            source.to_dict() for source in tool.discovery_sources
                        ],
                    }
                    for tool in not_selected
                ],
            ),
            # One row per scored record, kept or dropped, with the band, the
            # reason and the failed evidence gates.
            "score_decisions": write_jsonl(
                paths.resolve("processed") / "score_decisions.jsonl",
                [decision.to_dict() for decision in self._filter_decisions],
            ),
            "review_queue": write_jsonl(
                paths.resolve("processed") / "review_queue.jsonl",
                [
                    {"id": tool.id, "name": tool.name, "notes": tool.review_notes}
                    for tool in [*selected, *not_selected]
                    if tool.needs_human_review
                ],
            ),
            "validation": write_json(
                paths.resolve("processed") / "validation.json", list(validation_results)
            ),
            "run_report": write_json(
                paths.resolve("final") / "run_report.json", self.report.to_dict()
            ),
        }
        # ``update``, not assignment: earlier stages (candidate verification)
        # already registered their interim artefacts and must not be dropped.
        self.report.artefacts.update(
            {name: str(path) for name, path in artefacts.items()}
        )
        # rewrite the report so it contains its own artefact list
        write_json(paths.resolve("final") / "run_report.json", self.report.to_dict())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
