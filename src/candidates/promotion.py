"""Promote verified candidates into normalized ``Tool`` records.

Why this module exists
----------------------
Official-website verification produces
``data/interim/candidates_verified.jsonl``: one row per candidate, carrying a
``discovery`` block (what a directory *claimed*) and a ``verification`` block
(what the product's own website actually *proved*). Scoring, threshold
filtering and validation, however, all consume :class:`~src.models.tool.Tool`
records and read their evidence off ``tool.verification`` — so without this
stage the live verification results never reach the scorer, every record looks
"never verified", and the published dataset would be scored on directory
claims alone.

:meth:`src.pipeline.ToolsPipeline.verify` is *not* the way to close that gap
here. It re-fetches every official website to gather the same evidence we have
already paid for, which would mean a second full live crawl of thousands of
hosts. This module instead **replays the persisted evidence** onto the
normalized record.

The one rule that matters
-------------------------
**No fact is invented, and no status is upgraded.** Every value written here
was read off the product's own website during the verification pass and
persisted in the row. The mapping mirrors
:meth:`src.verification.verifier.OfficialSiteVerifier.verify` field for field
(status, HTTP status, final URL, accessibility, notes, official ``SourceRef``,
``last_verified_date``, detected product status, rejections and review flags),
so a record ends up in exactly the state a live re-verification would have
left it in — just without re-fetching the page.

Specifically:

* ``verified`` / ``partially_verified`` rows keep that status and gain the
  official ``SourceRef`` and ``last_verified_date`` the page evidenced;
* ``unreachable`` rows are **rejected** (``WEBSITE_BROKEN``, or
  ``DEAD_OR_SHUTDOWN`` when the page itself showed a shutdown marker), exactly
  as the live path rejects them;
* ``unverified`` / ``failed`` rows stay unverified and are flagged for human
  review with the verifier's own stated reason;
* a candidate with no persisted evidence is **absent**, never defaulted.

Descriptive fields (tagline, categories, directory counters) come from the
resolved feed and stay marked as discovery-side claims: they are normalized by
the existing :class:`~src.cleaning.normalizer.ToolNormalizer`, not re-derived
here, so identity, ids and cleaning behave byte-for-byte as elsewhere.

Join key
--------
Rows are joined to the resolved feed with
:func:`src.verification.runner.verification_key` — the same key the
verification runner uses to resume — because ``candidate_id`` alone is not
unique in this dataset (one product listed by two directories legitimately
appears twice under different listing URLs).

That key includes the listing URL, and the earliest verified rows were written
before the listing pointer was carried onto the row, so a handful of rows
cannot match on it. Those fall back to ``(candidate_id, official URL)`` and
then to ``candidate_id`` alone. The fallback only ever recovers the
*descriptive* discovery fields (tagline, categories, directory counters) for a
row we already verified; it can neither create a record nor alter a
verification status, so a wrong fallback match costs a description, never a
fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.cleaning.normalizer import ToolNormalizer
from src.core.io import read_jsonl
from src.core.logging_setup import get_logger
from src.models.base import SourceRef
from src.models.enums import RejectionReason, ToolStatus, VerificationStatus
from src.models.tool import Tool
from src.verification.runner import verification_key
from src.verification.store import load_resolved_candidates

logger = get_logger("candidates.promotion")

__all__ = [
    "PromotionReport",
    "promote_verified_rows",
    "load_verified_rows",
]

#: Statuses that carry real official-page evidence of the product existing.
_VERIFIED_STATUSES = (
    str(VerificationStatus.VERIFIED),
    str(VerificationStatus.PARTIALLY_VERIFIED),
)

#: Page-evidenced product states that mean "this product is gone".
_DEAD_STATUSES = (str(ToolStatus.DISCONTINUED), str(ToolStatus.DEPRECATED))


@dataclass
class PromotionReport:
    """Auditable summary of one promotion pass."""

    verified_rows: int = 0
    resolved_rows: int = 0
    matched_to_resolved: int = 0
    unmatched_rows: int = 0
    promoted: int = 0
    dropped_unidentifiable: int = 0
    errors: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    rejected_on_promotion: int = 0
    flagged_for_review: int = 0
    with_official_source: int = 0
    offline_rows_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified_rows": self.verified_rows,
            "resolved_rows": self.resolved_rows,
            "matched_to_resolved": self.matched_to_resolved,
            "unmatched_rows": self.unmatched_rows,
            "promoted": self.promoted,
            "dropped_unidentifiable": self.dropped_unidentifiable,
            "errors": self.errors,
            "status_counts": dict(sorted(self.status_counts.items())),
            "rejected_on_promotion": self.rejected_on_promotion,
            "flagged_for_review": self.flagged_for_review,
            "with_official_source": self.with_official_source,
            "offline_rows_skipped": self.offline_rows_skipped,
        }


def load_verified_rows(path: str | Path) -> list[dict[str, Any]]:
    """Read ``candidates_verified.jsonl``, skipping unusable rows."""
    rows: list[dict[str, Any]] = []
    for row in read_jsonl(path):
        if isinstance(row, Mapping) and row.get("name"):
            rows.append(dict(row))
    return rows


def _descriptive_payload(prepared: Any) -> dict[str, Any]:
    """Discovery-side descriptive fields, copied verbatim (still claims)."""
    if prepared is None:
        return {}
    return {
        "short_description": getattr(prepared, "observed_description", None),
        "categories": list(getattr(prepared, "categories", None) or []),
        "raw_signals": dict(getattr(prepared, "raw_signals", None) or {}),
    }


def _source_refs(entries: Any) -> list[SourceRef]:
    """Rebuild discovery provenance from persisted dicts."""
    refs: list[SourceRef] = []
    for entry in entries or []:
        if not isinstance(entry, Mapping) or not entry.get("name"):
            continue
        try:
            refs.append(
                SourceRef(
                    **{
                        key: value
                        for key, value in entry.items()
                        if key in SourceRef.model_fields
                    }
                )
            )
        except (TypeError, ValueError):
            continue
    return refs


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    return value.date()


def apply_persisted_evidence(tool: Tool, verification: Mapping[str, Any]) -> None:
    """Replay one persisted verification block onto ``tool``.

    Mirrors :meth:`OfficialSiteVerifier.verify` exactly, reading the evidence
    from the artefact instead of re-fetching the page. Nothing is upgraded: a
    status is only ever the one the page produced.
    """
    record = tool.verification
    status_text = str(verification.get("verification_status") or "").strip()

    record.official_url_checked = verification.get("official_url") or tool.website
    record.final_url = verification.get("final_url")
    record.http_status = verification.get("http_status")
    checked_at = _parse_dt(verification.get("checked_at"))
    record.checked_at = checked_at

    notes = [str(note) for note in (verification.get("evidence_notes") or [])]
    if notes:
        record.notes = [*record.notes, *notes]

    try:
        record.status = VerificationStatus(status_text)
    except ValueError:
        record.status = VerificationStatus.UNVERIFIED

    detected = verification.get("detected_status")
    reason = verification.get("reason") or None

    # --- not reachable: reject, exactly as the live path does ---------------
    if status_text == str(VerificationStatus.UNREACHABLE):
        record.is_accessible = False
        rejection = (
            RejectionReason.DEAD_OR_SHUTDOWN
            if detected and str(detected) in _DEAD_STATUSES
            else RejectionReason.WEBSITE_BROKEN
        )
        tool.reject(rejection, reason or "official website not accessible")
        if detected:
            coerced = ToolStatus.coerce(detected)
            if coerced:
                tool.status = coerced
        return

    # --- reachable but unproven: stay unverified, ask for a human ----------
    if status_text not in _VERIFIED_STATUSES:
        record.is_accessible = bool(verification.get("fetched"))
        tool.flag_for_review(reason or "official website evidence insufficient")
        return

    # --- proven: record what the page actually showed ----------------------
    record.is_accessible = True
    if detected and not tool.status:
        coerced = ToolStatus.coerce(detected)
        if coerced:
            tool.status = coerced
            record.verified_fields = sorted({*record.verified_fields, "status"})

    source = verification.get("verification_source")
    if isinstance(source, Mapping) and source.get("name"):
        for ref in _source_refs([source]):
            record.verification_sources = [*record.verification_sources, ref]

    record.verified_fields = sorted({*record.verified_fields, "name", "website"})
    verified_date = _as_date(checked_at)
    if verified_date:
        tool.last_verified_date = verified_date

    if verification.get("needs_review") and reason:
        tool.flag_for_review(reason)


def promote_verified_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    resolved_path: str | Path,
    normalizer: ToolNormalizer | None = None,
    require_live: bool = True,
) -> tuple[list[Tool], PromotionReport]:
    """Turn persisted verification rows into scoreable ``Tool`` records.

    ``require_live=True`` (the default) skips rows recorded by an *offline*
    pass: those rows explicitly state that no network evidence was gathered,
    so treating them as verification input would launder a non-result into the
    dataset.

    Returns ``(tools, report)``. A row that cannot be identified is dropped and
    counted — never patched with a placeholder.
    """
    report = PromotionReport()
    normalizer = normalizer or ToolNormalizer()

    prepared_records, _source = load_resolved_candidates(resolved_path)
    report.resolved_rows = len(prepared_records)

    # Three indexes, consulted most-specific first. The extra two only exist
    # to recover descriptive fields for early rows written before the listing
    # pointer was carried onto the persisted row; they never affect evidence.
    by_key: dict[str, Any] = {}
    by_id_url: dict[tuple[str, str], Any] = {}
    by_id: dict[str, Any] = {}
    for prepared in prepared_records:
        by_key.setdefault(verification_key(prepared), prepared)
        candidate_id = str(getattr(prepared, "candidate_id", "") or "")
        website = str(getattr(prepared, "website", "") or "")
        if candidate_id:
            by_id_url.setdefault((candidate_id, website), prepared)
            by_id.setdefault(candidate_id, prepared)

    def _match(row: Mapping[str, Any], discovery: Mapping[str, Any]) -> Any:
        found = by_key.get(verification_key(row))
        if found is not None:
            return found
        candidate_id = str(row.get("candidate_id") or discovery.get("candidate_id") or "")
        if not candidate_id:
            return None
        verification_block = row.get("verification")
        verification_block = (
            verification_block if isinstance(verification_block, Mapping) else {}
        )
        for url in (
            discovery.get("website"),
            verification_block.get("official_url"),
        ):
            found = by_id_url.get((candidate_id, str(url or "")))
            if found is not None:
                return found
        return by_id.get(candidate_id)

    tools: list[Tool] = []
    for row in rows:
        report.verified_rows += 1

        if require_live and not row.get("live"):
            report.offline_rows_skipped += 1
            continue

        discovery = row.get("discovery")
        discovery = discovery if isinstance(discovery, Mapping) else {}
        verification = row.get("verification")
        verification = verification if isinstance(verification, Mapping) else {}

        status_text = str(
            verification.get("verification_status")
            or row.get("verification_status")
            or ""
        )
        report.status_counts[status_text] = report.status_counts.get(status_text, 0) + 1

        prepared = _match(row, discovery)
        if prepared is not None:
            report.matched_to_resolved += 1
        else:
            report.unmatched_rows += 1

        payload: dict[str, Any] = {
            "name": row.get("name") or discovery.get("name"),
            # The URL that was actually checked is the product's own site.
            "website": (
                verification.get("final_url")
                or verification.get("official_url")
                or discovery.get("website")
            ),
            "listing_url": discovery.get("listing_url"),
            "discovery_sources": _source_refs(discovery.get("discovery_sources")),
            **_descriptive_payload(prepared),
        }

        try:
            tool = normalizer.from_raw(payload)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pass
            report.errors += 1
            logger.warning(
                "promotion failed",
                extra={"name": str(payload.get("name"))[:80], "error": str(exc)},
            )
            continue

        if tool is None:
            report.dropped_unidentifiable += 1
            continue

        # Carry the candidate's own review findings across rather than
        # re-deriving them: they are part of the audit trail.
        for note in discovery.get("issues") or []:
            if str(note) == "directory_sponsored_placement":
                tool.flag_for_review(
                    "listed in a sponsored/promoted slot; placement is not a "
                    "quality signal"
                )

        apply_persisted_evidence(tool, verification)

        if tool.rejected:
            report.rejected_on_promotion += 1
        if tool.needs_human_review:
            report.flagged_for_review += 1
        if tool.verification.verification_sources:
            report.with_official_source += 1

        tools.append(tool)
        report.promoted += 1

    logger.info("verified candidates promoted", extra=report.to_dict())
    return tools, report


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
