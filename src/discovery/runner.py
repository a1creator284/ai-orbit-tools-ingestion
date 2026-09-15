"""Discovery orchestration + raw-candidate storage.

Separation of concerns required by the ingestion spec:

* ``data/raw/discovery/<source>.jsonl`` — one immutable record per candidate,
  exactly as observed, with full provenance. Never edited by later stages.
* ``data/raw/discovery/report.json`` — per-source audit trail (pages fetched,
  cards seen, duplicates, stop reasons, errors).
* ``data/raw/candidates.jsonl`` — the merged, cross-source de-duplicated feed
  consumed by the normalization/deduplication pipeline.

Cross-source de-duplication here is intentionally conservative: it only
collapses *exact* identity matches (same official domain, or same listing URL)
and records every source that contributed. Fuzzy entity resolution stays in
:mod:`src.deduplication`, which is the stage that owns it.

Nothing in this module invents data. A blocked or failing source contributes
zero candidates and an explicit reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.core.config import Settings, get_settings
from src.core.io import write_json, write_jsonl
from src.core.logging_setup import get_logger
from src.core.urls import extract_registrable_domain
from src.discovery.base import CandidateTool, DiscoverySource
from src.discovery.registry import SourceRegistry

logger = get_logger("discovery.runner")

RAW_SUBDIR = "discovery"


@dataclass
class DiscoveryRunReport:
    """Auditable summary of one discovery pass."""

    started_at: str
    finished_at: str | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_emitted: int = 0
    total_unique: int = 0
    cross_source_duplicates: int = 0
    artefacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_emitted": self.total_emitted,
            "total_unique": self.total_unique,
            "cross_source_duplicates": self.cross_source_duplicates,
            "sources": self.sources,
            "artefacts": self.artefacts,
            "notes": self.notes,
        }


def candidate_identity(candidate: CandidateTool) -> str:
    """Exact-match identity key used for cross-source merging."""
    domain = extract_registrable_domain(candidate.website)
    if domain:
        return f"domain:{domain}"
    if candidate.listing_url:
        return f"listing:{candidate.listing_url}"
    return f"name:{candidate.name.strip().lower()}"


def merge_candidates(candidates: Iterable[CandidateTool]) -> tuple[list[CandidateTool], int]:
    """Collapse exact-identity duplicates, preserving every contributing source.

    The first occurrence wins for scalar fields (it is never overwritten with a
    competing directory's value); missing scalars may be *filled* from a later
    duplicate, and categories/signals/provenance are unioned.
    """
    merged: dict[str, CandidateTool] = {}
    duplicates = 0

    for candidate in candidates:
        key = candidate_identity(candidate)
        existing = merged.get(key)
        if existing is None:
            candidate.raw_payload.setdefault("contributing_sources", [])
            _record_contribution(candidate, candidate)
            merged[key] = candidate
            continue

        duplicates += 1
        # Fill gaps only — never overwrite an already-observed value.
        if not existing.website and candidate.website:
            existing.website = candidate.website
        if not existing.listing_url and candidate.listing_url:
            existing.listing_url = candidate.listing_url
        if not existing.tagline and candidate.tagline:
            existing.tagline = candidate.tagline
        for category in candidate.categories:
            if category not in existing.categories:
                existing.categories.append(category)
        for signal_key, value in candidate.raw_signals.items():
            existing.raw_signals.setdefault(f"{candidate.source_key}.{signal_key}", value)
        _record_contribution(existing, candidate)

    return list(merged.values()), duplicates


def _record_contribution(target: CandidateTool, contributor: CandidateTool) -> None:
    entries = target.raw_payload.setdefault("contributing_sources", [])
    entry = {
        "source_key": contributor.source_key,
        "source_name": contributor.source_name,
        "listing_url": contributor.listing_url,
        "website": contributor.website,
        "discovered_at": contributor.discovered_at.isoformat(),
        "page_url": contributor.raw_payload.get("page_url"),
        "plan": contributor.raw_payload.get("plan"),
    }
    if entry not in entries:
        entries.append(entry)


class DiscoveryRunner:
    """Runs discovery across sources and persists raw + merged artefacts."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: SourceRegistry | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry if registry is not None else SourceRegistry()
        self.report = DiscoveryRunReport(started_at=_now())

    # ------------------------------------------------------------------- run
    def run(
        self,
        *,
        source_keys: Sequence[str] | None = None,
        limit_per_source: int | None = None,
        persist: bool = True,
        **discover_kwargs: Any,
    ) -> tuple[list[CandidateTool], DiscoveryRunReport]:
        """Discover candidates from the requested (or all active) sources."""
        sources = self._resolve_sources(source_keys)
        if not sources:
            note = "no discovery adapters are enabled/implemented"
            self.report.notes.append(note)
            logger.warning(note)

        raw_per_source: dict[str, list[CandidateTool]] = {}
        all_candidates: list[CandidateTool] = []

        for source in sources:
            produced: list[CandidateTool] = []
            error: str | None = None
            try:
                for candidate in source.discover(limit=limit_per_source, **discover_kwargs):
                    produced.append(candidate)
            except Exception as exc:  # noqa: BLE001 - one dead source must not stop the run
                error = str(exc)
                logger.warning(
                    "discovery source failed", extra={"source": source.key, "error": error}
                )

            stats = (
                source.stats_dict()
                if hasattr(source, "stats_dict")
                else {"source": source.key}
            )
            if error:
                stats.setdefault("errors", []).append(error)
            stats["emitted"] = len(produced)
            if not produced:
                stats["outcome"] = "no_candidates"
                reasons = stats.get("stop_reasons") or {}
                if "bot_challenge" in reasons:
                    stats["outcome"] = "blocked_by_bot_challenge"
                    self.report.notes.append(
                        f"{source.key}: blocked by bot challenge — zero candidates emitted "
                        "(no data fabricated)"
                    )
            self.report.sources[source.key] = stats
            raw_per_source[source.key] = produced
            all_candidates.extend(produced)

        self.report.total_emitted = len(all_candidates)
        unique, duplicates = merge_candidates(all_candidates)
        self.report.total_unique = len(unique)
        self.report.cross_source_duplicates = duplicates
        self.report.finished_at = _now()

        if persist:
            self._persist(raw_per_source, unique)

        return unique, self.report

    # ------------------------------------------------------------- internals
    def _resolve_sources(self, source_keys: Sequence[str] | None) -> list[DiscoverySource]:
        if not source_keys:
            return list(self.registry.active(role="discovery"))
        sources: list[DiscoverySource] = []
        for key in source_keys:
            try:
                sources.append(self.registry.get(key))
            except Exception as exc:  # noqa: BLE001
                self.report.notes.append(f"{key}: {exc}")
                logger.warning("source unavailable", extra={"source": key, "error": str(exc)})
        return sources

    def _persist(
        self,
        raw_per_source: dict[str, list[CandidateTool]],
        merged: Sequence[CandidateTool],
    ) -> None:
        raw_root: Path = self.settings.paths.resolve("raw")
        discovery_dir = raw_root / RAW_SUBDIR
        discovery_dir.mkdir(parents=True, exist_ok=True)

        artefacts: dict[str, Path] = {}
        for key, candidates in raw_per_source.items():
            artefacts[f"raw_{key}"] = write_jsonl(
                discovery_dir / f"{key}.jsonl", [c.to_dict() for c in candidates]
            )
        artefacts["merged_candidates"] = write_jsonl(
            raw_root / "candidates.jsonl", [c.to_dict() for c in merged]
        )
        artefacts["discovery_report"] = write_json(
            discovery_dir / "report.json", self.report.to_dict()
        )
        self.report.artefacts = {name: str(path) for name, path in artefacts.items()}
        write_json(discovery_dir / "report.json", self.report.to_dict())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
