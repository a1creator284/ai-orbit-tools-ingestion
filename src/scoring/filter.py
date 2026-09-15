"""Threshold-based quality filtering.

Scoring says *how good* a record is; this module decides *what happens to it*,
using the guideline §5 bands verbatim::

    90-100  exceptional  -> include (strongly prioritize)
    80- 89  excellent    -> include
    70- 79  good         -> include **selectively**, based on evidence
    60- 69  average      -> normally skip
      < 60  reject       -> reject

Separating the two is deliberate: the scorer must stay a pure, deterministic
function of the evidence, while the admission policy is what a curator argues
about. Both are auditable — every decision carries the band, the total, the
reason and (for rejections) a :class:`~src.models.enums.RejectionReason`.

Rules that this stage must honour
---------------------------------
* **Never pad.** Nothing here can promote a record to reach a target count;
  the only way up is better evidence. Selection/target logic lives in
  :func:`src.scoring.scorer.rank_and_select` and is not touched here.
* **Never lower the bar.** Thresholds come from
  :mod:`src.scoring.rubric`/:class:`~src.core.config.ScoringConfig`, not from
  how many records happened to pass.
* **Selective means evidence-gated, not arbitrary.** A 70-79 record is
  admitted only when it clears :data:`SELECTIVE_REQUIREMENTS`: it must be
  currently alive, verified at least partially, have real provenance and not
  be a thin record (see :func:`selective_evidence`). Otherwise it is skipped
  with the specific gate named in the reason.
* **Preserve provenance and reasons.** Decisions are recorded on the record
  (``rejection_reasons``/``rejection_notes``/``review_notes``) *and* returned
  as :class:`FilterDecision` objects for the run report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from src.core.config import ScoringConfig, get_settings
from src.core.logging_setup import get_logger
from src.models.enums import RejectionReason
from src.models.tool import Tool
from src.scoring.rubric import INCLUDE_AT_OR_ABOVE, REJECT_BELOW, SKIP_BELOW, quantize

logger = get_logger("scoring.filter")

__all__ = [
    "Outcome",
    "SELECTIVE_REQUIREMENTS",
    "FilterDecision",
    "FilterReport",
    "QualityFilter",
    "selective_evidence",
    "rejection_rows",
    "MIN_SELECTIVE_EVIDENCE_CONFIDENCE",
]


class Outcome:
    """Stable outcome codes — persisted and counted, so they must not drift."""

    INCLUDE = "include"
    INCLUDE_SELECTIVE = "include_selective"
    SKIP = "skip"
    REJECT = "reject"
    #: Rejected before scoring (e.g. by verification) — score not the cause.
    PRE_REJECTED = "pre_rejected"
    #: No score at all: we refuse to admit an unscored record.
    UNSCORED = "unscored"


#: Evidence gates a 70-79 "include selectively" record must clear.
#: Each entry is ``(gate, human-readable requirement)``.
SELECTIVE_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("currently_alive", "current evidence that the product is still running"),
    ("verified", "at least partial official-website verification"),
    ("has_provenance", "a recorded discovery source"),
    ("describable", "a usable description or overview"),
    ("not_thin", "enough assessable evidence to stand behind the record"),
)

#: Minimum share of the 100 points that must have been *assessable* before a
#: borderline (70-79) record is admitted. A record scoring 72 out of 55
#: assessable points is a guess dressed as a score.
MIN_SELECTIVE_EVIDENCE_CONFIDENCE = 0.6


@dataclass
class FilterDecision:
    """What the filter decided about one record, and why."""

    tool_id: str | None
    name: str | None
    outcome: str
    score: float | None
    band: str | None
    reason: str
    rejection_reason: str | None = None
    failed_gates: list[str] = field(default_factory=list)
    currency_state: str | None = None
    evidence_confidence: float | None = None
    unevidenced_components: list[str] = field(default_factory=list)

    @property
    def included(self) -> bool:
        return self.outcome in (Outcome.INCLUDE, Outcome.INCLUDE_SELECTIVE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "outcome": self.outcome,
            "score": self.score,
            "band": self.band,
            "reason": self.reason,
            "rejection_reason": self.rejection_reason,
            "failed_gates": list(self.failed_gates),
            "currency_state": self.currency_state,
            "evidence_confidence": self.evidence_confidence,
            "unevidenced_components": list(self.unevidenced_components),
        }


@dataclass
class FilterReport:
    """Auditable summary of one filtering pass."""

    input_count: int = 0
    kept_count: int = 0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    band_counts: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    failed_gate_counts: dict[str, int] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    score_sum: float = 0.0
    scored_count: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, decision: FilterDecision) -> None:
        self.input_count += 1
        if decision.included:
            self.kept_count += 1
        self.outcome_counts[decision.outcome] = (
            self.outcome_counts.get(decision.outcome, 0) + 1
        )
        if decision.band:
            self.band_counts[decision.band] = self.band_counts.get(decision.band, 0) + 1
        if decision.rejection_reason:
            self.rejection_reasons[decision.rejection_reason] = (
                self.rejection_reasons.get(decision.rejection_reason, 0) + 1
            )
        for gate in decision.failed_gates:
            self.failed_gate_counts[gate] = self.failed_gate_counts.get(gate, 0) + 1
        if decision.score is not None:
            self.score_sum += decision.score
            self.scored_count += 1

    @property
    def mean_score(self) -> float | None:
        if not self.scored_count:
            return None
        return quantize(self.score_sum / self.scored_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_count,
            "kept": self.kept_count,
            "dropped": self.input_count - self.kept_count,
            "mean_score": self.mean_score,
            "thresholds": dict(self.thresholds),
            "outcomes": dict(sorted(self.outcome_counts.items())),
            "bands": dict(sorted(self.band_counts.items())),
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "failed_selective_gates": dict(sorted(self.failed_gate_counts.items())),
            "notes": list(self.notes),
        }


def selective_evidence(tool: Tool) -> dict[str, bool]:
    """Evaluate the :data:`SELECTIVE_REQUIREMENTS` gates for one record.

    Every gate is answered from evidence already on the record — nothing is
    fetched, inferred or assumed. An unknown is ``False``: a borderline record
    has to *prove* it belongs, not merely fail to disprove it.
    """
    quality = tool.quality
    currency = getattr(quality, "currency_state", None) if quality else None
    confidence = getattr(quality, "evidence_confidence", None) if quality else None
    description = tool.detailed_overview or tool.short_description or ""
    return {
        "currently_alive": currency in ("current", "accessible_stale"),
        "verified": bool(tool.is_verified),
        "has_provenance": bool(tool.discovery_sources),
        "describable": len(description.strip()) >= 20,
        "not_thin": (confidence or 0.0) >= MIN_SELECTIVE_EVIDENCE_CONFIDENCE,
    }


class QualityFilter:
    """Applies the guideline §5 thresholds to scored records.

    Thresholds are read from :class:`~src.core.config.ScoringConfig` and
    default to the rubric, so a caller cannot accidentally relax them by
    omitting one.
    """

    def __init__(
        self,
        config: ScoringConfig | None = None,
        *,
        reject_below: float | None = None,
        skip_below: float | None = None,
        include_at_or_above: float | None = None,
    ) -> None:
        self.config = config or get_settings().scoring
        self.reject_below = float(
            reject_below
            if reject_below is not None
            else getattr(self.config, "reject_below", REJECT_BELOW)
        )
        self.skip_below = float(
            skip_below if skip_below is not None else getattr(self.config, "skip_below", SKIP_BELOW)
        )
        self.include_at_or_above = float(
            include_at_or_above
            if include_at_or_above is not None
            else getattr(self.config, "include_at_or_above", INCLUDE_AT_OR_ABOVE)
        )
        if not self.reject_below <= self.skip_below <= self.include_at_or_above:
            raise ValueError(
                "thresholds must be ordered reject_below <= skip_below <= "
                f"include_at_or_above, got {self.reject_below}/{self.skip_below}/"
                f"{self.include_at_or_above}"
            )

    # ------------------------------------------------------------- decisions
    def decide(self, tool: Tool) -> FilterDecision:
        """Decide one record's fate. Mutates only the record's own audit trail."""
        quality = tool.quality
        band = str(quality.band) if quality else None
        total = quality.total if quality else None
        base = dict(
            tool_id=tool.id,
            name=tool.name,
            score=total,
            band=band,
            currency_state=getattr(quality, "currency_state", None) if quality else None,
            evidence_confidence=(
                getattr(quality, "evidence_confidence", None) if quality else None
            ),
            unevidenced_components=(
                list(getattr(quality, "unevidenced_components", []) or []) if quality else []
            ),
        )

        # An unscored record can never be included: we would be publishing
        # something we never assessed.
        if quality is None or total is None:
            reason = "record has no quality score — cannot be admitted"
            tool.reject(RejectionReason.UNVERIFIABLE_INFORMATION, reason)
            return FilterDecision(
                outcome=Outcome.UNSCORED,
                reason=reason,
                rejection_reason=str(RejectionReason.UNVERIFIABLE_INFORMATION),
                **base,
            )

        # Already rejected upstream (verification, dedup, …). The earlier
        # reason is authoritative and is preserved untouched.
        if tool.rejected:
            existing = [str(r) for r in tool.rejection_reasons]
            return FilterDecision(
                outcome=Outcome.PRE_REJECTED,
                reason=(
                    "rejected before quality filtering: "
                    + (", ".join(existing) if existing else "reason not recorded")
                ),
                rejection_reason=existing[0] if existing else None,
                **base,
            )

        if total < self.reject_below:
            reason = (
                f"score {total:.2f} is below the reject threshold "
                f"{self.reject_below:g} (guideline §5: <60 → reject)"
            )
            tool.reject(RejectionReason.BELOW_SCORE_THRESHOLD, reason)
            return FilterDecision(
                outcome=Outcome.REJECT,
                reason=reason,
                rejection_reason=str(RejectionReason.BELOW_SCORE_THRESHOLD),
                **base,
            )

        if total < self.skip_below:
            reason = (
                f"score {total:.2f} is in the {self.reject_below:g}-"
                f"{self.skip_below - 1:g} 'average / usually skip' band"
            )
            tool.reject(RejectionReason.VERY_LOW_QUALITY, reason)
            tool.flag_for_review(
                f"{reason} — a curator may re-admit it only on stronger evidence"
            )
            return FilterDecision(
                outcome=Outcome.SKIP,
                reason=reason,
                rejection_reason=str(RejectionReason.VERY_LOW_QUALITY),
                **base,
            )

        if total < self.include_at_or_above:
            gates = selective_evidence(tool)
            failed = [gate for gate, _ in SELECTIVE_REQUIREMENTS if not gates.get(gate)]
            if failed:
                missing = ", ".join(
                    requirement
                    for gate, requirement in SELECTIVE_REQUIREMENTS
                    if gate in failed
                )
                reason = (
                    f"score {total:.2f} is in the 'good / include selectively' band "
                    f"but the evidence gates were not met: {missing}"
                )
                tool.reject(RejectionReason.UNVERIFIABLE_INFORMATION, reason)
                tool.flag_for_review(reason)
                return FilterDecision(
                    outcome=Outcome.SKIP,
                    reason=reason,
                    rejection_reason=str(RejectionReason.UNVERIFIABLE_INFORMATION),
                    failed_gates=failed,
                    **base,
                )
            reason = (
                f"score {total:.2f} is in the 'good / include selectively' band and "
                "clears every evidence gate"
            )
            tool.flag_for_review(
                f"{reason} — selective inclusion, confirm before publishing"
            )
            return FilterDecision(outcome=Outcome.INCLUDE_SELECTIVE, reason=reason, **base)

        reason = (
            f"score {total:.2f} is at or above the include threshold "
            f"{self.include_at_or_above:g} (band '{band}')"
        )
        return FilterDecision(outcome=Outcome.INCLUDE, reason=reason, **base)

    def filter(
        self, tools: Iterable[Tool]
    ) -> tuple[list[Tool], list[FilterDecision], FilterReport]:
        """Filter a batch.

        Returns ``(kept, decisions, report)``. ``decisions`` has one entry per
        input record in input order — including the dropped ones, so nothing
        disappears without a recorded reason.
        """
        report = FilterReport(
            thresholds={
                "reject_below": self.reject_below,
                "skip_below": self.skip_below,
                "include_at_or_above": self.include_at_or_above,
            }
        )
        kept: list[Tool] = []
        decisions: list[FilterDecision] = []
        for tool in tools:
            try:
                decision = self.decide(tool)
            except Exception as exc:  # noqa: BLE001 - one bad record, not the batch
                logger.warning(
                    "quality filtering failed",
                    extra={"tool": getattr(tool, "name", None), "error": str(exc)},
                )
                decision = FilterDecision(
                    tool_id=getattr(tool, "id", None),
                    name=getattr(tool, "name", None),
                    outcome=Outcome.UNSCORED,
                    score=None,
                    band=None,
                    reason=f"quality filtering raised: {exc}",
                )
            decisions.append(decision)
            report.note(decision)
            if decision.included:
                kept.append(tool)

        if report.input_count and not report.kept_count:
            report.notes.append(
                "no record met the inclusion thresholds; the batch is reported "
                "empty rather than padded (guideline §11)"
            )
        logger.info("quality filtering complete", extra=report.to_dict())
        return kept, decisions, report


def rejection_rows(decisions: Sequence[FilterDecision]) -> list[dict[str, Any]]:
    """Persistable rows for every non-included decision (reasons preserved)."""
    return [decision.to_dict() for decision in decisions if not decision.included]
