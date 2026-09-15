"""Candidate preparation — the stage between discovery and verification.

A discovery candidate is a *sighting*, not a product record. Before the
pipeline is allowed to reason about it, it must be turned into something
stable, identifiable and auditable. That is this stage:

    data/raw/discovery/<source>.jsonl        raw sightings (immutable)
        → PreparedCandidate                  stable, normalized, unverified
        → data/interim/candidates_prepared.jsonl

What preparation *does*
-----------------------
* resolves a **deterministic candidate id** from the strongest identity signal
  available (official domain > product-URL domain > name+company > name), and
  records which basis was used plus its confidence;
* **normalizes** the fields a directory can legitimately supply: display name,
  slug, website, listing URL, categories, and the observed tagline;
* **validates** required candidate identity: a name plus at least one
  resolvable pointer, otherwise the candidate is rejected with a reason;
* **preserves provenance** — every contributing source is kept as a
  :class:`~src.models.base.SourceRef`, and raw observed signals travel through
  untouched;
* marks every output ``verification_status = "unverified"``.

What preparation must never do
------------------------------
* invent a launch date, price, feature, capability, user count or company;
* promote a directory claim to a verified fact (a listing is discovery only);
* drop a real product because its identity is weak — a weak identity is
  *flagged for review*, not discarded;
* merge two candidates. Exact-identity merging belongs to
  :func:`src.discovery.runner.merge_candidates`; fuzzy entity resolution
  belongs to :mod:`src.deduplication`. Preparation is 1-in → 1-out.

The output is deliberately **not** a :class:`~src.models.tool.Tool`: prepared
candidates are unverified and live under ``data/interim/``, so they can never
be confused with the verified records published to ``data/final/tools.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.core.ids import make_entity_id
from src.core.io import write_json, write_jsonl
from src.core.logging_setup import get_logger
from src.core.text import canonical_name, clean_text, slugify
from src.core.urls import extract_registrable_domain, normalize_url
from src.discovery.base import CandidateTool
from src.models.base import SourceRef

logger = get_logger("candidates.prepare")

__all__ = [
    "CandidateIssue",
    "PreparedCandidate",
    "PreparationReport",
    "CandidatePreparer",
    "RejectReason",
    "INTERIM_FILENAME",
]

#: Prepared candidates live here — never under ``data/final/``.
INTERIM_FILENAME = "candidates_prepared.jsonl"
INTERIM_REPORT_FILENAME = "candidates_prepared_report.json"

#: The only verification status a prepared candidate may carry.
UNVERIFIED = "unverified"

#: Minimum length before an observed tagline is treated as a description.
MIN_DESCRIPTION_LENGTH = 20

#: Longest tagline we keep (matches ``Tool.short_description``).
MAX_DESCRIPTION_LENGTH = 320


class RejectReason:
    """Stable reasons a candidate cannot be prepared."""

    NO_NAME = "no_name"
    NO_POINTER = "no_pointer"
    NO_IDENTITY = "no_identity"
    PREPARATION_FAILED = "preparation_failed"


class CandidateIssue:
    """Stable, non-fatal findings recorded on a prepared candidate."""

    WEAK_IDENTITY = "weak_identity_name_only"
    NO_OFFICIAL_URL = "no_official_website_observed"
    DESCRIPTION_MISSING = "no_description_observed"
    DESCRIPTION_TOO_SHORT = "observed_description_too_short"
    NO_CATEGORIES = "no_categories_observed"
    NAME_LOOKS_LIKE_TAGLINE = "name_may_contain_a_tagline"
    SPONSORED_PLACEMENT = "directory_sponsored_placement"
    MISSING_OBSERVATION_TIME = "no_discovered_at_recorded"


@dataclass
class PreparedCandidate:
    """A stable, normalized, **unverified** candidate record.

    Every field is either observed on a discovery source or derived
    deterministically from an observed field. Anything unobserved is ``None``
    or empty — never guessed.
    """

    candidate_id: str
    identity_key: str
    identity_basis: str
    identity_confidence: str
    name: str
    name_key: str | None
    slug: str | None
    website: str | None
    website_domain: str | None
    listing_url: str | None
    #: Cleaned tagline exactly as observed on the directory (never rewritten).
    observed_description: str | None
    categories: list[str] = field(default_factory=list)
    source_keys: list[str] = field(default_factory=list)
    discovery_sources: list[SourceRef] = field(default_factory=list)
    raw_signals: dict[str, Any] = field(default_factory=dict)
    discovered_at: str | None = None
    prepared_at: str | None = None
    #: Always ``"unverified"``: a directory listing is not verification.
    verification_status: str = UNVERIFIED
    issues: list[str] = field(default_factory=list)
    needs_review: bool = False
    #: Free-text explanations, one per issue, so review is explainable.
    review_notes: list[str] = field(default_factory=list)

    def add_issue(self, code: str, note: str, *, review: bool = False) -> None:
        if code not in self.issues:
            self.issues.append(code)
            self.review_notes.append(note)
        if review:
            self.needs_review = True

    @property
    def has_official_url(self) -> bool:
        return bool(self.website)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "identity_key": self.identity_key,
            "identity_basis": self.identity_basis,
            "identity_confidence": self.identity_confidence,
            "name": self.name,
            "name_key": self.name_key,
            "slug": self.slug,
            "website": self.website,
            "website_domain": self.website_domain,
            "listing_url": self.listing_url,
            "observed_description": self.observed_description,
            "categories": list(self.categories),
            "source_keys": list(self.source_keys),
            "discovery_sources": [s.to_dict() for s in self.discovery_sources],
            "raw_signals": dict(self.raw_signals),
            "discovered_at": self.discovered_at,
            "prepared_at": self.prepared_at,
            "verification_status": self.verification_status,
            "issues": list(self.issues),
            "needs_review": self.needs_review,
            "review_notes": list(self.review_notes),
        }


@dataclass
class RejectedCandidate:
    """A candidate that could not be prepared, with an explainable reason."""

    name: str | None
    source_key: str | None
    reason: str
    detail: str | None = None
    website: str | None = None
    listing_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_key": self.source_key,
            "reason": self.reason,
            "detail": self.detail,
            "website": self.website,
            "listing_url": self.listing_url,
        }


@dataclass
class PreparationReport:
    """Auditable summary of one preparation pass."""

    started_at: str
    finished_at: str | None = None
    input_count: int = 0
    prepared_count: int = 0
    rejected_count: int = 0
    needs_review_count: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    issue_counts: dict[str, int] = field(default_factory=dict)
    identity_bases: dict[str, int] = field(default_factory=dict)
    with_official_url: int = 0
    rejected_examples: list[dict[str, Any]] = field(default_factory=list)
    artefacts: dict[str, str] = field(default_factory=dict)
    max_examples: int = 20

    def note_reject(self, rejected: RejectedCandidate) -> None:
        self.rejected_count += 1
        self.reject_reasons[rejected.reason] = self.reject_reasons.get(rejected.reason, 0) + 1
        if len(self.rejected_examples) < self.max_examples:
            self.rejected_examples.append(rejected.to_dict())

    def note_prepared(self, prepared: PreparedCandidate) -> None:
        self.prepared_count += 1
        basis = prepared.identity_basis
        self.identity_bases[basis] = self.identity_bases.get(basis, 0) + 1
        if prepared.has_official_url:
            self.with_official_url += 1
        if prepared.needs_review:
            self.needs_review_count += 1
        for code in prepared.issues:
            self.issue_counts[code] = self.issue_counts.get(code, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input": self.input_count,
            "prepared": self.prepared_count,
            "rejected": self.rejected_count,
            "needs_review": self.needs_review_count,
            "with_official_url": self.with_official_url,
            "identity_bases": dict(sorted(self.identity_bases.items())),
            "reject_reasons": dict(sorted(self.reject_reasons.items())),
            "issue_counts": dict(sorted(self.issue_counts.items())),
            "rejected_examples": self.rejected_examples,
            "artefacts": self.artefacts,
        }


class CandidatePreparer:
    """Turns discovery candidates into stable, unverified candidate records.

    Deterministic and side-effect free (persistence is a separate, explicit
    call), so the same input always produces the same output — a requirement
    for reproducible runs and for diffing two discovery passes.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        #: Injectable clock so tests (and reruns) stay deterministic.
        self._now = now

    # ---------------------------------------------------------------- public
    def prepare(
        self, candidate: CandidateTool
    ) -> tuple[PreparedCandidate | None, RejectedCandidate | None]:
        """Prepare one candidate. Returns ``(prepared, rejected)``."""
        try:
            return self._prepare(candidate)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop a batch
            logger.warning(
                "candidate preparation failed",
                extra={"name": getattr(candidate, "name", None), "error": str(exc)},
            )
            return None, RejectedCandidate(
                name=getattr(candidate, "name", None),
                source_key=getattr(candidate, "source_key", None),
                reason=RejectReason.PREPARATION_FAILED,
                detail=str(exc)[:300],
            )

    def prepare_many(
        self, candidates: Iterable[CandidateTool]
    ) -> tuple[list[PreparedCandidate], list[RejectedCandidate], PreparationReport]:
        """Prepare a batch, collecting an auditable report."""
        report = PreparationReport(started_at=_iso(self._now or _utcnow()))
        prepared: list[PreparedCandidate] = []
        rejected: list[RejectedCandidate] = []

        for candidate in candidates:
            report.input_count += 1
            ok, bad = self.prepare(candidate)
            if ok is not None:
                prepared.append(ok)
                report.note_prepared(ok)
            if bad is not None:
                rejected.append(bad)
                report.note_reject(bad)

        report.finished_at = _iso(self._now or _utcnow())
        logger.info("candidate preparation complete", extra=report.to_dict()["reject_reasons"])
        return prepared, rejected, report

    def persist(
        self,
        prepared: Sequence[PreparedCandidate],
        report: PreparationReport,
        *,
        interim_dir: str | Path,
        rejected: Sequence[RejectedCandidate] = (),
    ) -> PreparationReport:
        """Write prepared candidates + report under ``interim_dir``.

        Prepared candidates are unverified, so they are persisted to
        ``data/interim/`` — deliberately not to ``data/final/``.
        """
        interim = Path(interim_dir)
        interim.mkdir(parents=True, exist_ok=True)
        artefacts = {
            "prepared_candidates": write_jsonl(
                interim / INTERIM_FILENAME, [p.to_dict() for p in prepared]
            ),
            "rejected_candidates": write_jsonl(
                interim / "candidates_rejected.jsonl", [r.to_dict() for r in rejected]
            ),
        }
        report.artefacts = {name: str(path) for name, path in artefacts.items()}
        report.artefacts["preparation_report"] = str(
            write_json(interim / INTERIM_REPORT_FILENAME, report.to_dict())
        )
        return report

    # ------------------------------------------------------------- internals
    def _prepare(
        self, candidate: CandidateTool
    ) -> tuple[PreparedCandidate | None, RejectedCandidate | None]:
        name = clean_text(candidate.name, max_length=200)
        if not name:
            return None, RejectedCandidate(
                name=None,
                source_key=candidate.source_key,
                reason=RejectReason.NO_NAME,
                detail="candidate has no usable name",
                website=candidate.website,
                listing_url=candidate.listing_url,
            )

        website = normalize_url(candidate.website) if candidate.website else None
        listing_url = normalize_url(candidate.listing_url) if candidate.listing_url else None
        if not website and not listing_url:
            return None, RejectedCandidate(
                name=name,
                source_key=candidate.source_key,
                reason=RejectReason.NO_POINTER,
                detail="no official website and no directory detail URL",
            )

        try:
            candidate_id, identity = make_entity_id(
                "tool",
                name=name,
                website=website,
                product_url=listing_url,
            )
        except ValueError as exc:
            return None, RejectedCandidate(
                name=name,
                source_key=candidate.source_key,
                reason=RejectReason.NO_IDENTITY,
                detail=str(exc)[:300],
                website=website,
                listing_url=listing_url,
            )

        prepared = PreparedCandidate(
            candidate_id=candidate_id,
            identity_key=identity.value,
            identity_basis=identity.basis,
            identity_confidence=identity.confidence,
            name=name,
            name_key=canonical_name(name),
            slug=slugify(name),
            website=website,
            website_domain=extract_registrable_domain(website),
            listing_url=listing_url,
            observed_description=_observed_description(candidate.tagline),
            categories=_normalize_categories(candidate.categories),
            source_keys=_source_keys(candidate),
            discovery_sources=_discovery_sources(candidate),
            raw_signals=dict(candidate.raw_signals or {}),
            discovered_at=_iso(candidate.discovered_at),
            prepared_at=_iso(self._now or _utcnow()),
        )
        self._flag_issues(prepared, candidate)
        return prepared, None

    @staticmethod
    def _flag_issues(prepared: PreparedCandidate, candidate: CandidateTool) -> None:
        """Record what is missing/uncertain — without repairing it."""
        if prepared.identity_basis == "name":
            prepared.add_issue(
                CandidateIssue.WEAK_IDENTITY,
                "identity derived from the name only; no domain resolved yet",
                review=True,
            )
        if not prepared.website:
            prepared.add_issue(
                CandidateIssue.NO_OFFICIAL_URL,
                "no outbound official URL on the listing; the directory detail "
                "page is the only pointer (verification must resolve the real site)",
                review=True,
            )

        description = prepared.observed_description
        if not description:
            prepared.add_issue(
                CandidateIssue.DESCRIPTION_MISSING,
                "no tagline observed; left blank rather than generated",
            )
        elif len(description) < MIN_DESCRIPTION_LENGTH:
            prepared.add_issue(
                CandidateIssue.DESCRIPTION_TOO_SHORT,
                f"observed tagline is shorter than {MIN_DESCRIPTION_LENGTH} characters",
            )

        if not prepared.categories:
            prepared.add_issue(
                CandidateIssue.NO_CATEGORIES,
                "no categories observed; primary task must be derived after verification",
            )

        if _looks_like_tagline(prepared.name):
            prepared.add_issue(
                CandidateIssue.NAME_LOOKS_LIKE_TAGLINE,
                "name still contains a separator and may include a directory tagline",
                review=True,
            )

        if (candidate.raw_signals or {}).get("directory_sponsored_placement"):
            prepared.add_issue(
                CandidateIssue.SPONSORED_PLACEMENT,
                "listed in a sponsored/promoted slot; placement is not a quality signal",
            )

        if prepared.discovered_at is None:
            prepared.add_issue(
                CandidateIssue.MISSING_OBSERVATION_TIME,
                "no discovery timestamp recorded for this sighting",
            )


# ----------------------------------------------------------------- helpers
#: Separators that usually glue a tagline onto a product name.
_TAGLINE_MARKERS = (" | ", " — ", " – ", " :: ", " • ", " · ")


def _looks_like_tagline(name: str) -> bool:
    return any(marker in name for marker in _TAGLINE_MARKERS)


def _observed_description(tagline: str | None) -> str | None:
    """Clean an observed tagline; ``None`` when there is nothing to keep."""
    return clean_text(tagline, max_length=MAX_DESCRIPTION_LENGTH)


def _normalize_categories(values: Iterable[str] | None) -> list[str]:
    """Clean, de-duplicate (case-insensitively) and order categories stably."""
    seen: dict[str, str] = {}
    for value in values or []:
        cleaned = clean_text(value, max_length=80)
        if not cleaned:
            continue
        seen.setdefault(cleaned.casefold(), cleaned)
    return list(seen.values())


def _source_keys(candidate: CandidateTool) -> list[str]:
    """Every source key that contributed to this candidate, first seen first."""
    keys: list[str] = []
    if candidate.source_key:
        keys.append(candidate.source_key)
    for entry in (candidate.raw_payload or {}).get("contributing_sources") or []:
        if not isinstance(entry, dict):
            continue
        key = clean_text(entry.get("source_key"))
        if key and key not in keys:
            keys.append(key)
    return keys


def _discovery_sources(candidate: CandidateTool) -> list[SourceRef]:
    """Provenance as ``SourceRef`` objects, one per contributing sighting."""
    refs: list[SourceRef] = []
    seen: set[tuple[str, str]] = set()

    def add(name: Any, url: Any, retrieved_at: Any) -> None:
        cleaned_name = clean_text(name)
        if not cleaned_name:
            return
        normalized = normalize_url(url) if url else None
        key = (cleaned_name.casefold(), normalized or "")
        if key in seen:
            return
        seen.add(key)
        try:
            refs.append(
                SourceRef(
                    name=cleaned_name,
                    url=normalized,
                    kind="directory",
                    retrieved_at=retrieved_at,
                )
            )
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return

    contributions = (candidate.raw_payload or {}).get("contributing_sources") or []
    for entry in contributions:
        if not isinstance(entry, dict):
            continue
        add(
            entry.get("source_name") or entry.get("source_key"),
            entry.get("listing_url") or entry.get("page_url"),
            _parse_iso(entry.get("discovered_at")),
        )

    if not refs:
        add(
            candidate.source_name or candidate.source_key,
            candidate.listing_url or (candidate.raw_payload or {}).get("page_url"),
            candidate.discovered_at,
        )
    return refs


def _parse_iso(value: Any) -> datetime | None:
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
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
