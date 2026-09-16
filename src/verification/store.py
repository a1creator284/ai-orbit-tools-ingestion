"""Persistence for official-website verification results.

Verification produces two kinds of fact about one candidate, and they must
never be allowed to blur into each other:

``discovery``
    Where the candidate was *seen*: which directory listed it, under which
    listing URL, when, and with what observed signals. A sighting.

``verification``
    What the product's *own* website proved when it was actually fetched:
    HTTP status, final URL, identity signals, product affordances, failures and
    the resulting :class:`~src.verification.verifier.VerificationStatus`.

Each persisted record therefore keeps those two blocks side by side but
strictly separate, so a reader can always answer "who claimed this?" and "what
did we confirm ourselves?" independently. Nothing from the discovery block can
be mistaken for evidence.

Layout::

    data/interim/candidates_verified.jsonl            one record per candidate
    data/interim/candidates_verification_report.json  auditable pass summary

Input
-----
Production verification consumes the *official-URL resolution* artefact
``data/interim/candidates_resolved.jsonl`` (see
:mod:`src.candidates.resolution`) via :func:`load_resolved_candidates`, because
that dataset carries the grounded official URLs read off directory detail
pages. :func:`load_prepared_candidates` remains the fallback reader for the
older ``candidates_prepared.jsonl`` artefact. Neither reader promotes a
directory claim to a verified fact.

Hard rule: verification artefacts are written under ``data/interim/`` only.
``data/final/`` is reserved for curated, published records, so
:func:`persist_verification` refuses to write there — see
:data:`FORBIDDEN_DIR_NAMES`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.candidates.prepare import CandidatePreparer, PreparedCandidate
from src.candidates.resolution import PROVENANCE_KEY, RESOLVED_FILENAME
from src.candidates.store import candidate_from_dict
from src.core.errors import VerificationError
from src.core.io import read_jsonl, write_json, write_jsonl
from src.core.logging_setup import get_logger
from src.models.base import SourceRef
from src.verification.verifier import VerificationReport, VerificationResult

logger = get_logger("verification.store")

__all__ = [
    "VERIFIED_FILENAME",
    "VERIFICATION_REPORT_FILENAME",
    "RESOLVED_FILENAME",
    "FORBIDDEN_DIR_NAMES",
    "discovery_provenance",
    "load_prepared_candidates",
    "load_resolved_candidates",
    "load_verification_input",
    "verification_record",
    "persist_verification",
]

#: Verified *candidates* — still not published tools.
VERIFIED_FILENAME = "candidates_verified.jsonl"

#: Auditable summary of the verification pass.
VERIFICATION_REPORT_FILENAME = "candidates_verification_report.json"

#: Directories verification results may never be written to.
FORBIDDEN_DIR_NAMES = ("final",)


def discovery_provenance(candidate: Any) -> dict[str, Any]:
    """Discovery-side provenance for one prepared candidate.

    Read defensively (``getattr``) so any candidate-shaped record works, and
    copied verbatim — this block is a record of what a directory *claimed*, so
    it is never cleaned up, re-derived or merged with page evidence.
    """
    sources = _safe(candidate, "discovery_sources") or []
    return {
        "candidate_id": _safe(candidate, "candidate_id"),
        "name": _safe(candidate, "name"),
        "website": _safe(candidate, "website"),
        "website_domain": _safe(candidate, "website_domain"),
        "listing_url": _safe(candidate, "listing_url"),
        "source_keys": list(_safe(candidate, "source_keys") or []),
        "discovery_sources": [_as_dict(ref) for ref in sources],
        "discovered_at": _safe(candidate, "discovered_at"),
        "prepared_at": _safe(candidate, "prepared_at"),
        "identity_basis": _safe(candidate, "identity_basis"),
        "identity_confidence": _safe(candidate, "identity_confidence"),
        "issues": list(_safe(candidate, "issues") or []),
        "needs_review": bool(_safe(candidate, "needs_review") or False),
    }


def load_prepared_candidates(
    path: str | Path,
) -> tuple[list[PreparedCandidate], str]:
    """Rehydrate ``candidates_prepared.jsonl`` into prepared candidates.

    Returns ``(candidates, source_label)``. Resilient by design: a malformed
    line or a record without the identity fields preparation guarantees is
    skipped rather than patched with a placeholder, because a fabricated
    candidate id would corrupt the verification audit trail.
    """
    path = Path(path)
    if not path.exists():
        return [], str(path)

    loaded: list[PreparedCandidate] = []
    for record in read_jsonl(path):
        candidate = _prepared_from_dict(record)
        if candidate is not None:
            loaded.append(candidate)
    logger.info(
        "prepared candidates loaded for verification",
        extra={"path": str(path), "loaded": len(loaded)},
    )
    return loaded, str(path)


def load_resolved_candidates(
    path: str | Path,
) -> tuple[list[PreparedCandidate], str]:
    """Rehydrate ``candidates_resolved.jsonl`` into prepared candidates.

    This is the **production** verification input: the resolution stage wrote
    a grounded official URL onto each record (``website``) plus an
    ``official_url_provenance`` block saying where that URL came from. Those
    URLs are what verification must go and check.

    A resolved row is a raw discovery record plus provenance, not a prepared
    record, so it is rehydrated through the *existing* stages verbatim —
    :func:`src.candidates.store.candidate_from_dict` then
    :class:`~src.candidates.prepare.CandidatePreparer` — rather than a second,
    parallel normalizer. Identity, normalization and issue flagging therefore
    stay byte-for-byte the same as ``python run.py prepare`` produces.

    The resolution provenance is carried through on ``raw_signals`` under
    :data:`~src.candidates.resolution.PROVENANCE_KEY` so the persisted
    discovery block still records that the URL is a *directory claim* awaiting
    verification. Nothing is invented: a row without a usable name/pointer is
    skipped, exactly as elsewhere.

    Returns ``(candidates, source_label)``.
    """
    path = Path(path)
    if not path.exists():
        return [], str(path)

    preparer = CandidatePreparer()
    loaded: list[PreparedCandidate] = []
    skipped = 0
    for record in read_jsonl(path):
        if not isinstance(record, Mapping):
            skipped += 1
            continue
        candidate = candidate_from_dict(record)
        if candidate is None:
            skipped += 1
            continue
        prepared, _rejected = preparer.prepare(candidate)
        if prepared is None:
            skipped += 1
            continue
        provenance = record.get(PROVENANCE_KEY)
        if isinstance(provenance, Mapping):
            # A claim, kept verbatim next to the other observed signals.
            prepared.raw_signals[PROVENANCE_KEY] = dict(provenance)
        loaded.append(prepared)

    logger.info(
        "resolved candidates loaded for verification",
        extra={"path": str(path), "loaded": len(loaded), "skipped": skipped},
    )
    return loaded, str(path)


def load_verification_input(
    interim_dir: str | Path,
) -> tuple[list[PreparedCandidate], str, str]:
    """Pick the verification input, preferring the resolved candidate feed.

    Returns ``(candidates, source_label, source_kind)`` where ``source_kind``
    is one of ``"resolved"``, ``"prepared"`` or ``"none"``.

    Precedence is deliberate and **not** a silent fallback chain: when
    ``candidates_resolved.jsonl`` exists it is the only input considered, even
    if it turns out to be empty or unreadable. Falling back to the prepared /
    raw discovery feed in that case would quietly verify ungrounded directory
    URLs instead of the resolved official ones.
    """
    from src.candidates.prepare import INTERIM_FILENAME

    interim = Path(interim_dir)
    resolved_path = interim / RESOLVED_FILENAME
    if resolved_path.exists():
        candidates, source = load_resolved_candidates(resolved_path)
        return candidates, source, "resolved"

    prepared_path = interim / INTERIM_FILENAME
    if prepared_path.exists():
        candidates, source = load_prepared_candidates(prepared_path)
        return candidates, source, "prepared"

    return [], str(resolved_path), "none"


def _prepared_from_dict(record: Any) -> PreparedCandidate | None:
    """Build one :class:`PreparedCandidate` from a persisted row."""
    if not isinstance(record, Mapping):
        return None
    candidate_id = record.get("candidate_id")
    name = record.get("name")
    if not candidate_id or not name:
        return None
    try:
        return PreparedCandidate(
            candidate_id=str(candidate_id),
            identity_key=str(record.get("identity_key") or ""),
            identity_basis=str(record.get("identity_basis") or ""),
            identity_confidence=str(record.get("identity_confidence") or ""),
            name=str(name),
            name_key=record.get("name_key"),
            slug=record.get("slug"),
            website=record.get("website"),
            website_domain=record.get("website_domain"),
            listing_url=record.get("listing_url"),
            observed_description=record.get("observed_description"),
            categories=list(record.get("categories") or []),
            source_keys=list(record.get("source_keys") or []),
            discovery_sources=_source_refs(record.get("discovery_sources")),
            raw_signals=dict(record.get("raw_signals") or {}),
            discovered_at=record.get("discovered_at"),
            prepared_at=record.get("prepared_at"),
            verification_status=str(record.get("verification_status") or "unverified"),
            issues=list(record.get("issues") or []),
            needs_review=bool(record.get("needs_review") or False),
            review_notes=list(record.get("review_notes") or []),
        )
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the load
        logger.warning(
            "prepared candidate rehydration failed",
            extra={"candidate_id": str(candidate_id)[:80], "error": str(exc)},
        )
        return None


def _source_refs(value: Any) -> list[SourceRef]:
    """Rebuild discovery provenance, dropping only rows that cannot be built."""
    refs: list[SourceRef] = []
    for entry in value or []:
        if not isinstance(entry, Mapping):
            continue
        try:
            refs.append(
                SourceRef(
                    name=entry.get("name"),
                    url=entry.get("url"),
                    kind=entry.get("kind"),
                    retrieved_at=entry.get("retrieved_at"),
                )
            )
        except (TypeError, ValueError):
            continue
    return refs


def verification_record(
    candidate: Any, result: VerificationResult, *, live: bool
) -> dict[str, Any]:
    """One persisted row: separated discovery provenance + official evidence.

    ``live`` is recorded on the row itself so an offline pass can never be
    mistaken for a real verification: an offline record is an explicit
    statement that *no* network evidence was gathered.
    """
    return {
        "candidate_id": result.candidate_id or _safe(candidate, "candidate_id"),
        "name": result.name or _safe(candidate, "name"),
        # --- what a directory claimed (never evidence) --------------------
        "discovery": discovery_provenance(candidate),
        # --- what the official website actually proved ---------------------
        "verification": result.to_dict(),
        "verification_status": str(result.status),
        "verified": result.verified,
        "needs_review": result.needs_review,
        "live": live,
    }


def persist_verification(
    records: Iterable[Mapping[str, Any]],
    report: VerificationReport | None = None,
    *,
    interim_dir: str | Path,
    extra_report_fields: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Write verification rows + report under ``interim_dir``.

    Raises :class:`~src.core.errors.VerificationError` when asked to write into
    ``data/final/``: unverified and freshly verified candidates are interim
    artefacts, and the published dataset must stay a curation decision.
    """
    interim = Path(interim_dir)
    _guard_destination(interim)
    interim.mkdir(parents=True, exist_ok=True)

    rows = [dict(record) for record in records]
    artefacts = {
        "verified_candidates": str(write_jsonl(interim / VERIFIED_FILENAME, rows)),
    }

    payload: dict[str, Any] = dict(report.to_dict()) if report is not None else {}
    if extra_report_fields:
        payload.update(dict(extra_report_fields))
    payload["artefacts"] = dict(artefacts)
    artefacts["verification_report"] = str(
        write_json(interim / VERIFICATION_REPORT_FILENAME, payload)
    )

    logger.info(
        "verification artefacts written",
        extra={"records": len(rows), "dir": str(interim)},
    )
    return artefacts


def _guard_destination(directory: Path) -> None:
    parts = {part.casefold() for part in directory.resolve().parts}
    for forbidden in FORBIDDEN_DIR_NAMES:
        if forbidden in parts:
            raise VerificationError(
                f"refusing to write verification artefacts into '{forbidden}/': "
                f"{directory} — verified candidates are interim data, and "
                "data/final/ is reserved for curated, published records"
            )


def _safe(obj: Any, name: str) -> Any:
    """``getattr`` that survives a record whose property itself raises."""
    try:
        value = getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a hostile record is data, not control flow
        return None
    return value


def _as_dict(value: Any) -> Any:
    """Best-effort JSON view of a provenance object, without losing it."""
    for method in ("to_dict", "model_dump"):
        func = getattr(value, method, None)
        if callable(func):
            try:
                return func()
            except Exception:  # noqa: BLE001
                continue
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_as_dict(item) for item in value]
    return value
