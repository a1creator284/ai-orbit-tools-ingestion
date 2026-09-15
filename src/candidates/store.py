"""Reading persisted discovery candidates back into memory.

Discovery writes immutable raw artefacts (see :mod:`src.discovery.runner`)::

    data/raw/discovery/<source>.jsonl   one record per candidate, per source
    data/raw/discovery/report.json      per-source audit trail
    data/raw/candidates.jsonl           merged, exact-duplicate-free feed

Later stages must be able to resume from those files without re-crawling, so
this module rehydrates :class:`~src.discovery.base.CandidateTool` objects from
them.

Hard rules
----------
* **Resilience over strictness.** A malformed line, a missing field or a bad
  timestamp never aborts the load: the record is skipped (or the single field
  dropped) and the reason is recorded in :class:`CandidateLoadReport`.
* **Nothing is invented.** A record without a usable name, or with no pointer
  at all, is skipped rather than patched with a placeholder. Unparseable
  timestamps become ``None``, not "now".
* **Provenance is preserved verbatim.** ``raw_signals`` / ``raw_payload`` are
  copied through untouched so the audit trail survives a reload.
* **Discovery stays discovery.** Loading a candidate grants it no verified
  status of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from src.core.io import read_jsonl
from src.core.logging_setup import get_logger
from src.core.text import clean_text
from src.discovery.base import CandidateTool
from src.discovery.runner import RAW_SUBDIR

logger = get_logger("candidates.store")

__all__ = [
    "CandidateLoadReport",
    "SkipReason",
    "candidate_from_dict",
    "load_candidate_file",
    "load_discovery_dir",
    "iter_candidate_dicts",
]


class SkipReason:
    """Stable, machine-readable reasons a raw record was not loaded."""

    NOT_A_MAPPING = "not_a_mapping"
    NO_NAME = "no_name"
    NO_POINTER = "no_pointer"
    NO_SOURCE_KEY = "no_source_key"
    CONSTRUCTION_FAILED = "construction_failed"


@dataclass
class CandidateLoadReport:
    """Auditable summary of one load pass."""

    files_read: list[str] = field(default_factory=list)
    files_missing: list[str] = field(default_factory=list)
    records_seen: int = 0
    candidates_loaded: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    #: First few skipped records, for human debugging (never auto-repaired).
    skipped_examples: list[dict[str, Any]] = field(default_factory=list)
    field_repairs: dict[str, int] = field(default_factory=dict)
    max_examples: int = 10

    def note_skip(self, reason: str, record: Any = None) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        if len(self.skipped_examples) < self.max_examples:
            self.skipped_examples.append(
                {"reason": reason, "record": _preview(record)}
            )

    def note_repair(self, field_name: str) -> None:
        self.field_repairs[field_name] = self.field_repairs.get(field_name, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_read": self.files_read,
            "files_missing": self.files_missing,
            "records_seen": self.records_seen,
            "candidates_loaded": self.candidates_loaded,
            "skipped": self.skipped,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "skipped_examples": self.skipped_examples,
            "field_repairs": dict(sorted(self.field_repairs.items())),
        }


def _preview(record: Any, *, limit: int = 200) -> Any:
    """Small, safe representation of a bad record for the report."""
    if isinstance(record, Mapping):
        return {
            key: (str(value)[:limit] if value is not None else None)
            for key, value in list(record.items())[:6]
            if key in {"name", "website", "listing_url", "source_key", "source_name"}
        } or {"keys": sorted(str(k) for k in list(record.keys())[:6])}
    return str(record)[:limit]


def _parse_timestamp(value: Any, report: CandidateLoadReport | None = None) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when unparseable.

    Never substitutes "now": a fabricated observation time would corrupt the
    audit trail.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        if report is not None:
            report.note_repair("discovered_at_unparseable")
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Mapping):
        value = list(value.values())
    out: list[str] = []
    try:
        items = list(value)
    except TypeError:
        return []
    for item in items:
        cleaned = clean_text(item, max_length=80)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _dict_field(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def candidate_from_dict(
    record: Any,
    *,
    report: CandidateLoadReport | None = None,
    default_source_key: str | None = None,
) -> CandidateTool | None:
    """Rehydrate one persisted record, or return ``None`` if unusable.

    ``default_source_key`` is only used when the record itself omits the key
    and the filename tells us which source produced it — that is recovering a
    known fact, not inventing one.
    """
    if not isinstance(record, Mapping):
        if report is not None:
            report.note_skip(SkipReason.NOT_A_MAPPING, record)
        return None

    name = clean_text(record.get("name"), max_length=200)
    if not name:
        if report is not None:
            report.note_skip(SkipReason.NO_NAME, record)
        return None

    source_key = clean_text(record.get("source_key")) or default_source_key
    if not source_key:
        if report is not None:
            report.note_skip(SkipReason.NO_SOURCE_KEY, record)
        return None
    source_name = clean_text(record.get("source_name")) or source_key

    discovered_at = _parse_timestamp(record.get("discovered_at"), report)

    try:
        candidate = CandidateTool(
            name=name,
            source_key=source_key,
            source_name=source_name,
            listing_url=record.get("listing_url"),
            website=record.get("website"),
            tagline=record.get("tagline"),
            categories=_string_list(record.get("categories")),
            raw_signals=_dict_field(record.get("raw_signals")),
            raw_payload=_dict_field(record.get("raw_payload")),
            # ``CandidateTool`` defaults to now(); keep the real observation
            # time when we have one and fall back to the default otherwise,
            # recording that the original timestamp was lost.
            **({"discovered_at": discovered_at} if discovered_at else {}),
        )
    except Exception as exc:  # noqa: BLE001 - one bad record must not stop a load
        logger.warning("candidate rehydration failed", extra={"name": name, "error": str(exc)})
        if report is not None:
            report.note_skip(SkipReason.CONSTRUCTION_FAILED, record)
        return None

    if not candidate.is_usable:
        if report is not None:
            report.note_skip(SkipReason.NO_POINTER, record)
        return None

    if discovered_at is None and report is not None:
        report.note_repair("discovered_at_missing")
    return candidate


def iter_candidate_dicts(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream raw records from a JSONL artefact (malformed lines skipped)."""
    return read_jsonl(path)


def load_candidate_file(
    path: str | Path,
    *,
    report: CandidateLoadReport | None = None,
    default_source_key: str | None = None,
) -> list[CandidateTool]:
    """Load every usable candidate from one JSONL artefact."""
    path = Path(path)
    report = report if report is not None else CandidateLoadReport()
    if not path.exists():
        report.files_missing.append(str(path))
        logger.warning("candidate file missing", extra={"path": str(path)})
        return []

    report.files_read.append(str(path))
    if default_source_key is None:
        default_source_key = path.stem if path.stem != "candidates" else None

    loaded: list[CandidateTool] = []
    for record in iter_candidate_dicts(path):
        report.records_seen += 1
        candidate = candidate_from_dict(
            record, report=report, default_source_key=default_source_key
        )
        if candidate is not None:
            loaded.append(candidate)
    report.candidates_loaded += len(loaded)
    logger.info(
        "candidates loaded",
        extra={"path": str(path), "loaded": len(loaded), "skipped": report.skipped},
    )
    return loaded


def load_discovery_dir(
    raw_root: str | Path,
    *,
    source_keys: Iterable[str] | None = None,
    report: CandidateLoadReport | None = None,
) -> tuple[list[CandidateTool], CandidateLoadReport]:
    """Load per-source raw candidates from ``<raw_root>/discovery/``.

    Reads the immutable per-source artefacts rather than the merged feed, so
    every sighting is preserved and cross-source merging stays the explicit
    responsibility of :func:`src.discovery.runner.merge_candidates`.
    """
    report = report if report is not None else CandidateLoadReport()
    discovery_dir = Path(raw_root) / RAW_SUBDIR
    if not discovery_dir.is_dir():
        report.files_missing.append(str(discovery_dir))
        logger.warning("discovery directory missing", extra={"path": str(discovery_dir)})
        return [], report

    wanted = {str(key) for key in source_keys} if source_keys is not None else None
    candidates: list[CandidateTool] = []
    for path in sorted(discovery_dir.glob("*.jsonl")):
        key = path.stem
        if wanted is not None and key not in wanted:
            continue
        candidates.extend(load_candidate_file(path, report=report, default_source_key=key))
    return candidates, report


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
