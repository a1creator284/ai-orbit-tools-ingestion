"""Official-website verification.

Guideline §10: after discovery, open the official product website and verify
that the product exists, is accessible, what it does, its pricing, features,
status, developer and launch/update information. **If reliable sources
disagree, prefer the official source. If information cannot be verified, leave
the field blank — do not guess.**

This module provides:

* :class:`LivenessChecker` — is the official site reachable and not parked/dead?
* :class:`OfficialSiteVerifier` — orchestrates verification of one tool and
  records the outcome in :class:`~src.models.tool.VerificationRecord`;
* :func:`resolve_conflict` — the official-source-wins conflict rule.

Deep field extraction from the official page (pricing tables, feature lists) is
delivered by the extraction adapters in :mod:`src.extraction`; this module owns
the *policy*, so the policy is testable without any network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from src.core.http_client import FetchResult, HttpClient
from src.core.logging_setup import get_logger
from src.core.text import clean_text
from src.core.urls import extract_registrable_domain, normalize_url, same_site
from src.models.base import SourceRef
from src.models.enums import RejectionReason, ToolStatus, VerificationStatus
from src.models.tool import Tool

logger = get_logger("verification")

#: Phrases that indicate a dead, parked or shut-down product (guideline §4).
DEAD_SITE_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale",
    "parked domain", "account suspended", "website expired", "coming soon",
    "under construction", "site not found", "no longer available",
    "we have shut down", "has shut down", "shutting down", "service discontinued",
    "this project is no longer maintained", "sunset", "404 not found",
    "default web page", "welcome to nginx", "apache2 ubuntu default page",
    "future home of something quite cool",
)

#: Phrases that indicate an acquisition or a wind-down.
STATUS_MARKERS: tuple[tuple[str, ToolStatus], ...] = (
    ("has been acquired", ToolStatus.ACQUIRED),
    ("we've been acquired", ToolStatus.ACQUIRED),
    ("joining forces with", ToolStatus.ACQUIRED),
    ("no longer accepting new", ToolStatus.DEPRECATED),
    ("deprecated", ToolStatus.DEPRECATED),
    ("discontinued", ToolStatus.DISCONTINUED),
    ("join the waitlist", ToolStatus.WAITLIST),
    ("request early access", ToolStatus.WAITLIST),
    ("public beta", ToolStatus.BETA),
)

#: Minimum rendered text length for a page to count as a real product site.
MIN_CONTENT_CHARS = 400


@dataclass
class LivenessResult:
    """Outcome of an accessibility probe."""

    url: str
    is_accessible: bool
    http_status: int | None = None
    final_url: str | None = None
    redirected_off_domain: bool = False
    detected_status: ToolStatus | None = None
    notes: list[str] = field(default_factory=list)
    content_length: int = 0

    @property
    def looks_dead(self) -> bool:
        return not self.is_accessible or self.detected_status in (
            ToolStatus.DISCONTINUED,
            ToolStatus.INACCESSIBLE,
        )


class LivenessChecker:
    """Checks that an official website is currently accessible and real."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()

    def check(self, url: str | None) -> LivenessResult | None:
        """Probe ``url``; returns ``None`` when there is no URL to probe."""
        normalized = normalize_url(url)
        if not normalized:
            return None

        result = self.client.try_fetch(normalized, use_cache=True)
        if result is None:
            return LivenessResult(
                url=normalized,
                is_accessible=False,
                notes=["network request failed after retries"],
                detected_status=ToolStatus.INACCESSIBLE,
            )
        return self.evaluate(normalized, result)

    def evaluate(self, url: str, result: FetchResult) -> LivenessResult:
        """Pure evaluation of a fetched response (unit-testable, no network)."""
        notes: list[str] = []
        body = (result.text or "")
        lowered = body.lower()
        detected: ToolStatus | None = None

        accessible = bool(result.ok)
        if not accessible:
            notes.append(f"HTTP {result.status}")
            detected = ToolStatus.INACCESSIBLE

        stripped_length = len(clean_text(body) or "")
        if accessible and stripped_length < MIN_CONTENT_CHARS:
            accessible = False
            notes.append(f"page body too thin ({stripped_length} chars) to verify a product")

        for marker in DEAD_SITE_MARKERS:
            if marker in lowered:
                accessible = False
                detected = ToolStatus.DISCONTINUED if "shut" in marker else ToolStatus.INACCESSIBLE
                notes.append(f"dead-site marker detected: '{marker}'")
                break

        if accessible:
            for marker, status in STATUS_MARKERS:
                if marker in lowered:
                    detected = status
                    notes.append(f"status marker detected: '{marker}'")
                    break
            else:
                detected = ToolStatus.ACTIVE

        redirected_off_domain = bool(
            result.final_url and not same_site(url, result.final_url)
        )
        if redirected_off_domain:
            notes.append(
                f"redirects off-domain to {extract_registrable_domain(result.final_url)}"
                " — possible acquisition, rebrand or parked domain"
            )

        return LivenessResult(
            url=url,
            is_accessible=accessible,
            http_status=result.status,
            final_url=result.final_url,
            redirected_off_domain=redirected_off_domain,
            detected_status=detected,
            notes=notes,
            content_length=stripped_length,
        )


def resolve_conflict(
    field_name: str,
    official_value: Any,
    other_values: Mapping[str, Any],
) -> tuple[Any, list[str]]:
    """Apply "prefer the official source" (guideline §10).

    Returns ``(chosen_value, conflict_notes)``. When the official source has no
    value, a single agreed non-official value may be used; when non-official
    sources disagree among themselves the value is dropped (``None``) rather
    than guessed.
    """
    notes: list[str] = []
    cleaned_official = official_value if official_value not in ("", [], {}) else None

    distinct: dict[str, list[str]] = {}
    for source_name, value in other_values.items():
        if value in (None, "", [], {}):
            continue
        distinct.setdefault(str(value).strip().lower(), []).append(source_name)

    if cleaned_official is not None:
        conflicting = [
            key for key in distinct if key != str(cleaned_official).strip().lower()
        ]
        if conflicting:
            notes.append(
                f"{field_name}: official value kept; {len(conflicting)} non-official "
                f"value(s) discarded ({', '.join(sorted(conflicting))[:200]})"
            )
        return cleaned_official, notes

    if len(distinct) == 1:
        value_key, sources = next(iter(distinct.items()))
        notes.append(
            f"{field_name}: no official value; using agreed value from {', '.join(sources)}"
        )
        return other_values[sources[0]], notes

    if len(distinct) > 1:
        notes.append(
            f"{field_name}: sources disagree and no official value exists — left blank"
        )
    return None, notes


class OfficialSiteVerifier:
    """Verifies a tool against its official website and records the outcome."""

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        liveness: LivenessChecker | None = None,
    ) -> None:
        self.client = client or HttpClient()
        self.liveness = liveness or LivenessChecker(self.client)

    def verify(self, tool: Tool, *, extractors: Iterable[Any] = ()) -> Tool:
        """Verify ``tool`` in place. Never fabricates a value.

        ``extractors`` are optional callables ``(tool, FetchResult) -> dict`` that
        pull verified fields from the official page; they are injected so this
        stage stays testable and so new extraction logic needs no changes here.
        """
        record = tool.verification
        record.checked_at = datetime.now(timezone.utc)
        record.official_url_checked = tool.website

        if not tool.website:
            record.status = VerificationStatus.FAILED
            record.notes = [*record.notes, "no official website available to verify"]
            tool.reject(
                RejectionReason.FAKE_OR_UNVERIFIABLE,
                "no official website could be resolved from discovery sources",
            )
            return tool

        probe = self.liveness.check(tool.website)
        if probe is None:
            record.status = VerificationStatus.FAILED
            return tool

        record.http_status = probe.http_status
        record.final_url = probe.final_url
        record.is_accessible = probe.is_accessible
        record.notes = [*record.notes, *probe.notes]

        if not probe.is_accessible:
            record.status = VerificationStatus.UNREACHABLE
            reason = (
                RejectionReason.DEAD_OR_SHUTDOWN
                if probe.detected_status
                in (ToolStatus.DISCONTINUED, ToolStatus.DEPRECATED)
                else RejectionReason.WEBSITE_BROKEN
            )
            tool.reject(reason, "; ".join(probe.notes) or "official website not accessible")
            if probe.detected_status:
                tool.status = probe.detected_status
            return tool

        if probe.detected_status and not tool.status:
            tool.status = probe.detected_status
            record.verified_fields = sorted({*record.verified_fields, "status"})

        record.verification_sources = _merge_source_refs(
            record.verification_sources,
            SourceRef(
                name="Official product website",
                url=probe.final_url or tool.website,
                kind="official",
                retrieved_at=record.checked_at,
            ),
        )

        applied = self._apply_extractors(tool, extractors)
        record.verified_fields = sorted({*record.verified_fields, *applied})
        record.unverifiable_fields = sorted(self._unverifiable_fields(tool))
        record.status = (
            VerificationStatus.VERIFIED
            if len(record.verified_fields) >= 3
            else VerificationStatus.PARTIALLY_VERIFIED
        )
        tool.last_verified_date = record.checked_at.date()
        if probe.redirected_off_domain:
            tool.flag_for_review("official URL redirects off-domain; confirm ownership/rebrand")
        return tool

    # -------------------------------------------------------------- helpers
    def _apply_extractors(self, tool: Tool, extractors: Iterable[Any]) -> set[str]:
        """Run injected extractors, applying only non-empty verified values."""
        applied: set[str] = set()
        if not extractors:
            return applied
        result = self.client.try_fetch(tool.website) if tool.website else None
        if result is None or not result.ok:
            return applied
        for extractor in extractors:
            try:
                values = extractor(tool, result) or {}
            except Exception as exc:  # noqa: BLE001 - one bad extractor must not kill the run
                logger.warning(
                    "extractor failed",
                    extra={"tool": tool.name, "extractor": repr(extractor), "error": str(exc)},
                )
                continue
            for name, value in values.items():
                if value in (None, "", [], {}) or not hasattr(tool, name):
                    continue
                try:
                    setattr(tool, name, value)
                    applied.add(name)
                except Exception as exc:  # noqa: BLE001 - validation rejection is expected
                    logger.debug(
                        "extracted value rejected by schema",
                        extra={"tool": tool.name, "field": name, "error": str(exc)},
                    )
        return applied

    @staticmethod
    def _unverifiable_fields(tool: Tool) -> set[str]:
        """List guideline §8 fields deliberately left blank."""
        watched = (
            "company", "logo_url", "country", "version", "launch_date", "status",
            "detailed_overview", "primary_task", "has_api", "open_source_status",
            "signup_requirement", "aiorbit_summary",
        )
        blank = {name for name in watched if getattr(tool, name, None) in (None, "", [])}
        if tool.pricing.model is None:
            blank.add("pricing.model")
        if not tool.inputs:
            blank.add("inputs")
        if not tool.outputs:
            blank.add("outputs")
        if not tool.adoption.has_any_signal:
            blank.add("adoption")
        return blank


def _merge_source_refs(existing: list[SourceRef], new: SourceRef) -> list[SourceRef]:
    keys = {(ref.name.lower(), ref.url or "") for ref in existing}
    if (new.name.lower(), new.url or "") in keys:
        return existing
    return [*existing, new]
