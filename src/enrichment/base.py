"""Enrichment interface.

An *enricher* adds verified metadata to an already-verified tool (adoption
signals, GitHub activity, company details). Enrichers are additive-only:

* they must never overwrite a value verified from the official website;
* they must never invent a value — returning ``None`` is always acceptable;
* a failing enricher degrades gracefully and is logged.

LLM enrichment is restricted to *editorial* text (``aiorbit_summary``,
``detailed_overview``) generated **from already-verified facts**, per the
guideline's prohibition on unverifiable data.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.core.logging_setup import get_logger
from src.models.tool import Tool

logger = get_logger("enrichment")


@dataclass
class EnrichmentResult:
    """Fields an enricher wants to add, with provenance."""

    source_name: str
    values: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    verified: bool = False

    def is_empty(self) -> bool:
        return not self.values


class Enricher(abc.ABC):
    """Base class for all enrichers."""

    #: Fields this enricher may populate. Anything else is ignored.
    provides: tuple[str, ...] = ()
    #: Human-readable provenance name recorded on the record.
    source_name: str = "unknown"

    @abc.abstractmethod
    def enrich(self, tool: Tool) -> EnrichmentResult:
        """Return additional verified values for ``tool``."""

    def applies_to(self, tool: Tool) -> bool:
        """Only run when at least one provided field is still blank."""
        return any(getattr(tool, name, None) in (None, "", [], {}) for name in self.provides)


class EnrichmentPipeline:
    """Runs a chain of enrichers, filling gaps only."""

    def __init__(self, enrichers: Iterable[Enricher] | None = None) -> None:
        self.enrichers = list(enrichers or [])

    def run(self, tool: Tool) -> Tool:
        for enricher in self.enrichers:
            if not enricher.applies_to(tool):
                continue
            try:
                result = enricher.enrich(tool)
            except Exception as exc:  # noqa: BLE001 - graceful degradation
                logger.warning(
                    "enricher failed",
                    extra={"enricher": type(enricher).__name__, "tool": tool.name, "error": str(exc)},
                )
                continue
            if result.is_empty():
                continue
            self._apply(tool, enricher, result)
        return tool

    @staticmethod
    def _apply(tool: Tool, enricher: Enricher, result: EnrichmentResult) -> None:
        verified_fields = set(tool.verification.verified_fields)
        for name, value in result.values.items():
            if name not in enricher.provides:
                continue  # enricher overstepped its declared scope
            if name in verified_fields:
                continue  # never overwrite an officially verified value
            if value in (None, "", [], {}):
                continue
            current = getattr(tool, name, None)
            if current not in (None, "", [], {}):
                continue  # gap-filling only
            try:
                setattr(tool, name, value)
            except Exception as exc:  # noqa: BLE001 - schema rejection is expected
                logger.debug(
                    "enrichment value rejected",
                    extra={"tool": tool.name, "field": name, "error": str(exc)},
                )
        for note in result.notes:
            if note not in tool.review_notes:
                tool.review_notes = [*tool.review_notes, note]
