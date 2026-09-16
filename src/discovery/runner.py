"""Discovery orchestration + raw-candidate storage.

Separation of concerns required by the ingestion spec:

* ``data/raw/discovery/<source>.jsonl`` — one immutable record per candidate,
  exactly as observed, with full provenance. Never edited by later stages.
* ``data/raw/discovery/report.json`` — per-source audit trail (pages fetched,
  cards seen, duplicates, stop reasons, errors).
* ``data/raw/discovery/state.json`` — cumulative, resumable run state: which
  listing plans a source has already completed, and how many candidates are
  stored for it. A later batch reads this and skips finished work.
* ``data/raw/candidates.jsonl`` — the merged, cross-source de-duplicated feed
  consumed by the normalization/deduplication pipeline.

Production discovery is **batched and resumable**, not one long run:

* a source's artefact is checkpointed to disk *while* it is being walked
  (``checkpoint_every``), so an interrupted batch never loses collected work;
* a new batch *accumulates* into the stored artefact instead of replacing it
  (``accumulate``), so running one source today never deletes the candidates
  another source contributed yesterday;
* the merged feed is rebuilt from everything on disk, so it always reflects
  the full accumulated pool rather than only the most recent batch.

Cross-source de-duplication here is intentionally conservative: it only
collapses *exact* identity matches (same official domain, or same listing URL)
and records every source that contributed. Fuzzy entity resolution stays in
:mod:`src.deduplication`, which is the stage that owns it.

Nothing in this module invents data. A blocked or failing source contributes
zero candidates and an explicit reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.core.config import Settings, get_settings
from src.core.io import write_json, write_jsonl
from src.core.logging_setup import get_logger
from src.discovery.base import CandidateTool, DiscoverySource
from src.discovery.identity import candidate_key
from src.discovery.registry import SourceRegistry

logger = get_logger("discovery.runner")

RAW_SUBDIR = "discovery"
STATE_FILENAME = "state.json"
REPORT_FILENAME = "report.json"
MERGED_FILENAME = "candidates.jsonl"

#: Default number of new candidates between on-disk checkpoints.
DEFAULT_CHECKPOINT_EVERY = 25


@dataclass
class DiscoveryRunReport:
    """Auditable summary of one discovery pass."""

    started_at: str
    finished_at: str | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_emitted: int = 0
    total_unique: int = 0
    cross_source_duplicates: int = 0
    #: Size of the whole accumulated pool on disk (accumulate mode only).
    total_pool: int | None = None
    pool_cross_source_duplicates: int | None = None
    artefacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_emitted": self.total_emitted,
            "total_unique": self.total_unique,
            "cross_source_duplicates": self.cross_source_duplicates,
            "sources": self.sources,
            "artefacts": self.artefacts,
            "notes": self.notes,
        }
        if self.total_pool is not None:
            payload["total_pool"] = self.total_pool
        if self.pool_cross_source_duplicates is not None:
            payload["pool_cross_source_duplicates"] = self.pool_cross_source_duplicates
        return payload


def candidate_identity(candidate: CandidateTool) -> str:
    """Exact-match identity key used for cross-source merging.

    Deliberately identical to the per-source key (see
    :mod:`src.discovery.identity`): a candidate must not be treated as one
    product inside a source and a different product across sources. Fuzzy
    resolution stays in :mod:`src.deduplication`.
    """
    return candidate_key(
        website=candidate.website,
        listing_url=candidate.listing_url,
        name=candidate.name,
    )


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


def load_stored_candidates(path: Path) -> list[CandidateTool]:
    """Rehydrate an existing per-source artefact so a batch can extend it.

    Imported lazily: :mod:`src.candidates.store` imports this module for
    ``RAW_SUBDIR``, so a module-level import would be circular.
    """
    from src.candidates.store import load_candidate_file

    if not path.exists():
        return []
    return load_candidate_file(path, default_source_key=path.stem)


@dataclass
class DiscoveryState:
    """Cumulative, resumable state across discovery batches."""

    version: int = 1
    updated_at: str | None = None
    #: ``source_key -> {"completed_plans": [...], "stored_candidates": int, ...}``
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "DiscoveryState":
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("discovery state unreadable; starting fresh", extra={"path": str(path)})
            return cls()
        if not isinstance(payload, dict):
            return cls()
        state = cls(
            version=int(payload.get("version", 1)),
            updated_at=payload.get("updated_at"),
            sources=dict(payload.get("sources") or {}),
            batches=list(payload.get("batches") or []),
        )
        return state

    def source(self, key: str) -> dict[str, Any]:
        entry = self.sources.setdefault(
            key, {"completed_plans": [], "stored_candidates": 0, "batches": 0}
        )
        entry.setdefault("completed_plans", [])
        entry.setdefault("stored_candidates", 0)
        entry.setdefault("batches", 0)
        return entry

    def completed_plans(self, key: str) -> set[str]:
        return {str(label) for label in self.source(key).get("completed_plans", [])}

    def note_completed_plans(self, key: str, labels: Iterable[str]) -> None:
        entry = self.source(key)
        known = list(entry["completed_plans"])
        for label in labels:
            if label not in known:
                known.append(label)
        entry["completed_plans"] = known

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "sources": self.sources,
            # Keep the batch log bounded; it is an audit trail, not a dataset.
            "batches": self.batches[-50:],
        }


class DiscoveryRunner:
    """Runs discovery across sources and persists raw + merged artefacts.

    Parameters
    ----------
    accumulate:
        When ``True`` (production default) a batch is *added* to whatever is
        already stored for that source instead of replacing it, and the merged
        feed is rebuilt from every stored source. When ``False`` the run
        writes only what it just collected (the original single-shot
        behaviour, kept for tests and one-off re-crawls).
    checkpoint_every:
        Flush the in-progress source artefact to disk after this many new
        candidates. ``0`` disables intra-source checkpointing.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: SourceRegistry | None = None,
        accumulate: bool = False,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry if registry is not None else SourceRegistry()
        self.report = DiscoveryRunReport(started_at=_now())
        self.accumulate = accumulate
        self.checkpoint_every = max(0, int(checkpoint_every))
        self.state = DiscoveryState()

    # ------------------------------------------------------------------- run
    def run(
        self,
        *,
        source_keys: Sequence[str] | None = None,
        limit_per_source: int | None = None,
        persist: bool = True,
        **discover_kwargs: Any,
    ) -> tuple[list[CandidateTool], DiscoveryRunReport]:
        """Discover candidates from the requested (or all active) sources.

        In accumulate mode the per-source artefact is checkpointed to disk
        while the source is still being walked, so a batch that is interrupted
        (rate limit, timeout, credit budget) keeps everything collected up to
        that point.
        """
        sources = self._resolve_sources(source_keys)
        if not sources:
            note = "no discovery adapters are enabled/implemented"
            self.report.notes.append(note)
            logger.warning(note)

        discovery_dir = self.settings.paths.resolve("raw") / RAW_SUBDIR
        if self.accumulate:
            discovery_dir.mkdir(parents=True, exist_ok=True)
            self.state = DiscoveryState.load(discovery_dir / STATE_FILENAME)

        raw_per_source: dict[str, list[CandidateTool]] = {}
        all_candidates: list[CandidateTool] = []

        for source in sources:
            produced: list[CandidateTool] = []
            error: str | None = None
            checkpoints = 0
            try:
                for candidate in source.discover(limit=limit_per_source, **discover_kwargs):
                    produced.append(candidate)
                    if (
                        persist
                        and self.accumulate
                        and self.checkpoint_every
                        and len(produced) % self.checkpoint_every == 0
                    ):
                        self._checkpoint_source(discovery_dir, source.key, produced)
                        checkpoints += 1
            except Exception as exc:  # noqa: BLE001 - one dead source must not stop the run
                error = str(exc)
                logger.warning(
                    "discovery source failed", extra={"source": source.key, "error": error}
                )
            except BaseException:
                # Ctrl-C / budget kill: never discard what was already collected.
                if persist and self.accumulate and produced:
                    self._checkpoint_source(discovery_dir, source.key, produced)
                raise

            if persist and self.accumulate and produced:
                self._checkpoint_source(discovery_dir, source.key, produced)
                checkpoints += 1

            stats = (
                source.stats_dict()
                if hasattr(source, "stats_dict")
                else {"source": source.key}
            )
            if error:
                stats.setdefault("errors", []).append(error)
            stats["emitted"] = len(produced)
            if checkpoints:
                stats["checkpoints_written"] = checkpoints
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

    # ------------------------------------------------------- checkpointing
    def _checkpoint_source(
        self, discovery_dir: Path, key: str, produced: Sequence[CandidateTool]
    ) -> Path:
        """Write the source's artefact mid-walk (stored ∪ just-collected).

        Rewriting the whole file (atomically, via :func:`write_jsonl`) rather
        than appending keeps the artefact valid at every instant and keeps the
        exact-duplicate guarantee: a resumed batch that re-sees a candidate
        does not duplicate the stored row.
        """
        path = discovery_dir / f"{key}.jsonl"
        stored = load_stored_candidates(path)
        combined, _dupes = merge_candidates([*stored, *produced])
        write_jsonl(path, [c.to_dict() for c in combined])
        entry = self.state.source(key)
        entry["stored_candidates"] = len(combined)
        entry["last_checkpoint_at"] = _now()
        self._write_state(discovery_dir)
        logger.info(
            "discovery checkpoint written",
            extra={"source": key, "new": len(produced), "stored": len(combined)},
        )
        return path

    def _write_state(self, discovery_dir: Path) -> Path:
        self.state.updated_at = _now()
        return write_json(discovery_dir / STATE_FILENAME, self.state.to_dict())

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
            path = discovery_dir / f"{key}.jsonl"
            if self.accumulate:
                stored = load_stored_candidates(path)
                combined, _dupes = merge_candidates([*stored, *candidates])
                entry = self.state.source(key)
                entry["stored_candidates"] = len(combined)
                entry["batches"] = int(entry.get("batches", 0)) + 1
                entry["last_batch_at"] = _now()
                entry["last_batch_emitted"] = len(candidates)
            else:
                combined = list(candidates)
            artefacts[f"raw_{key}"] = write_jsonl(
                path, [c.to_dict() for c in combined]
            )

        if self.accumulate:
            # The merged feed must describe the whole accumulated pool, not
            # just this batch: running one source must never silently delete
            # another source's candidates from the pipeline's input.
            merged = self._merge_all_stored(discovery_dir)

        artefacts["merged_candidates"] = write_jsonl(
            raw_root / MERGED_FILENAME, [c.to_dict() for c in merged]
        )
        artefacts["discovery_report"] = write_json(
            discovery_dir / REPORT_FILENAME, self.report.to_dict()
        )
        if self.accumulate:
            self.state.batches.append(
                {
                    "started_at": self.report.started_at,
                    "finished_at": self.report.finished_at,
                    "sources": sorted(raw_per_source),
                    "emitted": self.report.total_emitted,
                    "pool_size": len(merged),
                }
            )
            artefacts["discovery_state"] = self._write_state(discovery_dir)
            self.report.total_pool = len(merged)

        self.report.artefacts = {name: str(path) for name, path in artefacts.items()}
        write_json(discovery_dir / REPORT_FILENAME, self.report.to_dict())

    def _merge_all_stored(self, discovery_dir: Path) -> list[CandidateTool]:
        """Rebuild the merged feed from every stored per-source artefact."""
        everything: list[CandidateTool] = []
        for path in sorted(discovery_dir.glob("*.jsonl")):
            everything.extend(load_stored_candidates(path))
        merged, duplicates = merge_candidates(everything)
        self.report.pool_cross_source_duplicates = duplicates
        return merged


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
