"""Pipeline orchestration.

Stage order (technical spec §2 + Tools guideline §2 "Discovery → Official
website → Verify → Enrich → Deduplicate → Curate")::

    discovery
      → extraction
      → cleaning / normalization
      → verification        (official website is authoritative)
      → quality filtering
      → deduplication / entity resolution
      → scoring             (100-point framework)
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

from src.cleaning.normalizer import ToolNormalizer
from src.core.config import Settings, get_settings
from src.core.io import write_json, write_jsonl
from src.core.logging_setup import get_logger, setup_logging
from src.deduplication.deduplicator import Deduplicator
from src.discovery.base import CandidateTool, DiscoverySource
from src.discovery.registry import SourceRegistry
from src.enrichment.base import EnrichmentPipeline
from src.enrichment.relationships import RelationshipExtractor
from src.models.base import Relationship
from src.models.tool import Tool
from src.scoring.scorer import QualityScorer, rank_and_select
from src.validation.validator import ToolValidator
from src.verification.verifier import OfficialSiteVerifier

logger = get_logger("pipeline")

STAGES = (
    "discovery",
    "normalization",
    "verification",
    "quality_filter",
    "deduplication",
    "scoring",
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
        self.deduplicator = deduplicator or Deduplicator()
        self.scorer = scorer or QualityScorer(self.settings.scoring)
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
    def discover(self, *, limit_per_source: int | None = None) -> list[CandidateTool]:
        """Collect candidates from every enabled, implemented discovery source."""
        stats = self.report.stage("discovery")
        stats.started_at = _now()
        candidates: list[CandidateTool] = []

        sources: list[DiscoverySource] = list(self.registry.active(role="discovery"))
        if not sources:
            note = (
                "no discovery adapters are enabled/implemented; "
                f"configured sources: {len(self.registry.configs)}"
            )
            stats.details["warning"] = note
            self.report.notes.append(note)
            logger.warning(note)

        for source in sources:
            try:
                produced = 0
                for candidate in source.discover(limit=limit_per_source):
                    if candidate.is_usable:
                        candidates.append(candidate)
                        produced += 1
                    else:
                        stats.dropped_count += 1
                stats.details[source.key] = produced
            except Exception as exc:  # noqa: BLE001 - one dead source must not stop the run
                stats.error_count += 1
                logger.warning(
                    "discovery source failed",
                    extra={"source": source.key, "error": str(exc)},
                )

        stats.output_count = len(candidates)
        stats.finished_at = _now()
        return candidates

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
        """Apply the 100-point framework (guideline §5)."""
        stats = self.report.stage("scoring")
        stats.started_at = _now()
        stats.input_count = len(tools)
        bands: dict[str, int] = {}
        for tool in tools:
            try:
                self.scorer.apply(tool)
            except Exception as exc:  # noqa: BLE001
                stats.error_count += 1
                logger.warning("scoring failed", extra={"tool": tool.name, "error": str(exc)})
                continue
            band = tool.band or "unscored"
            bands[band] = bands.get(band, 0) + 1
        stats.details["bands"] = bands
        stats.output_count = len(tools)
        stats.finished_at = _now()
        return list(tools)

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
    ) -> RunReport:
        """Execute every stage end to end and persist the artefacts."""
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

        tools = self.normalize(discovered)
        tools = self.verify(tools)
        tools = self.filter_quality(tools)
        tools = self.deduplicate(tools)
        tools = self.score(tools)
        tools = self.enrich(tools)
        publishable, validation_results = self.validate(tools)
        selected, not_selected = self.select(publishable)
        edges = self.map_relationships(selected)

        self.report.selected_count = len(selected)
        self.report.rejected_count = len(not_selected) + (len(tools) - len(publishable))
        self.report.review_count = sum(1 for tool in selected if tool.needs_human_review)
        self.report.finished_at = _now()

        if persist:
            self._persist(discovered, tools, selected, not_selected, edges, validation_results)

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
                        "reasons": [str(r) for r in tool.rejection_reasons],
                        "notes": tool.rejection_notes,
                    }
                    for tool in not_selected
                ],
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
        self.report.artefacts = {name: str(path) for name, path in artefacts.items()}
        # rewrite the report so it contains its own artefact list
        write_json(paths.resolve("final") / "run_report.json", self.report.to_dict())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
