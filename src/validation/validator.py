"""Validation framework.

Two layers:

1. **Schema validation** — pydantic enforces types, enums and lengths.
2. **Business-rule validation** — the guideline's editorial rules, expressed as
   independent :class:`Rule` objects so new rules can be added without touching
   the pipeline.

Severities:

* ``ERROR``   — record must not be published;
* ``WARNING`` — record is publishable but flagged for human review;
* ``INFO``    — observation recorded for the run report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Iterable

from pydantic import ValidationError as PydanticValidationError

from src.cleaning.normalizer import is_vague
from src.core.logging_setup import get_logger
from src.core.urls import extract_registrable_domain
from src.models.enums import RejectionReason, ToolStatus, VerificationStatus
from src.models.tool import Tool

logger = get_logger("validation")


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    """A single validation finding."""

    rule: str
    severity: Severity
    message: str
    field: str | None = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
        }


@dataclass
class ValidationResult:
    """Validation outcome for one record."""

    tool_id: str
    tool_name: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def is_publishable(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "publishable": self.is_publishable,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class Rule:
    """A named business rule."""

    name: str
    severity: Severity
    check: Callable[[Tool], str | None]
    field: str | None = None
    description: str = ""

    def run(self, tool: Tool) -> Issue | None:
        try:
            message = self.check(tool)
        except Exception as exc:  # noqa: BLE001 - a broken rule must not kill the run
            logger.warning("rule raised", extra={"rule": self.name, "error": str(exc)})
            return Issue(self.name, Severity.WARNING, f"rule failed to execute: {exc}", self.field)
        if message:
            return Issue(self.name, self.severity, message, self.field)
        return None


# ---------------------------------------------------------------- rule set
def _require_name(tool: Tool) -> str | None:
    return None if tool.name and len(tool.name) >= 2 else "tool name is missing or too short"


def _require_identity(tool: Tool) -> str | None:
    if tool.website or tool.dedup.canonical_domain:
        return None
    return "no official website/domain: the record cannot be verified (guideline §10)"


def _require_deterministic_id(tool: Tool) -> str | None:
    from src.core.ids import make_entity_id

    try:
        expected, _ = make_entity_id(
            "tool",
            name=tool.name,
            website=tool.website,
            company=tool.company,
            identity_key=tool.dedup.identity_key,
        )
    except ValueError as exc:
        return f"cannot recompute deterministic ID: {exc}"
    return None if expected == tool.id else (
        f"ID is not deterministic for its identity key (expected {expected})"
    )


def _require_description(tool: Tool) -> str | None:
    if tool.short_description and len(tool.short_description) >= 20:
        return None
    return "short description missing or shorter than 20 characters"


def _require_discovery_source(tool: Tool) -> str | None:
    return None if tool.discovery_sources else "no discovery source recorded (provenance required)"


def _require_verification(tool: Tool) -> str | None:
    status = tool.verification.status
    if status in (
        VerificationStatus.VERIFIED, VerificationStatus.VERIFIED.value,
        VerificationStatus.PARTIALLY_VERIFIED, VerificationStatus.PARTIALLY_VERIFIED.value,
    ):
        return None
    return f"record not verified against the official website (status '{status}')"


def _require_specific_io(tool: Tool) -> str | None:
    vague = [str(v) for v in [*tool.inputs, *tool.outputs] if is_vague(str(v))]
    if vague:
        return f"vague I/O values present (guideline §9): {vague}"
    return None


def _warn_missing_io(tool: Tool) -> str | None:
    if tool.inputs and tool.outputs:
        return None
    missing = [name for name in ("inputs", "outputs") if not getattr(tool, name)]
    return f"{'/'.join(missing)} not verified — left blank rather than guessed"


def _reject_dead_status(tool: Tool) -> str | None:
    status = ToolStatus.coerce(tool.status)
    if status in (ToolStatus.DISCONTINUED, ToolStatus.DEPRECATED, ToolStatus.INACCESSIBLE):
        return f"tool status '{status.value}' is excluded by guideline §4"
    return None


def _require_score(tool: Tool) -> str | None:
    if tool.quality is None:
        return "record has not been scored"
    if tool.quality.total < 60:
        return f"score {tool.quality.total:.1f} is below the reject threshold of 60"
    return None


def _warn_low_band(tool: Tool) -> str | None:
    if tool.quality and 60 <= tool.quality.total < 70:
        return f"score {tool.quality.total:.1f} is in the 60-69 'usually skip' band"
    return None


def _no_future_dates(tool: Tool) -> str | None:
    today = datetime.now(timezone.utc).date()
    for name in ("launch_date", "last_verified_date"):
        value = getattr(tool, name, None)
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date) and value > today:
            return f"{name} {value.isoformat()} is in the future"
    return None


def _logo_on_plausible_host(tool: Tool) -> str | None:
    """A fabricated logo URL is worse than no logo at all."""
    if not tool.logo_url:
        return None
    logo_domain = extract_registrable_domain(tool.logo_url)
    if not logo_domain:
        return "logo_url is not a valid URL"
    return None


def _warn_stale_verification(tool: Tool) -> str | None:
    if not tool.last_verified_date:
        return "no last_verified_date recorded"
    value = tool.last_verified_date
    if isinstance(value, datetime):
        value = value.date()
    age = (datetime.now(timezone.utc).date() - value).days
    return f"verification is {age} days old (re-verify before publishing)" if age > 90 else None


def _warn_duplicate_flag(tool: Tool) -> str | None:
    if tool.dedup.duplicate_of:
        return f"record is marked as a duplicate of {tool.dedup.duplicate_of}"
    if tool.dedup.review_candidates:
        return f"possible duplicates pending review: {tool.dedup.review_candidates}"
    return None


def _warn_no_adoption(tool: Tool) -> str | None:
    if tool.adoption.has_any_signal:
        return None
    return "no verified usage/adoption signal (guideline §6 treats this as important)"


def default_rules() -> list[Rule]:
    """The standard rule set derived from the two project documents."""
    return [
        Rule("name_present", Severity.ERROR, _require_name, "name"),
        Rule("identity_present", Severity.ERROR, _require_identity, "website"),
        Rule("id_deterministic", Severity.ERROR, _require_deterministic_id, "id"),
        Rule("discovery_source_present", Severity.ERROR, _require_discovery_source, "discovery_sources"),
        Rule("io_specific", Severity.ERROR, _require_specific_io, "inputs/outputs"),
        Rule("status_not_dead", Severity.ERROR, _reject_dead_status, "status"),
        Rule("score_present", Severity.ERROR, _require_score, "quality"),
        Rule("no_future_dates", Severity.ERROR, _no_future_dates, "launch_date"),
        Rule("logo_url_valid", Severity.ERROR, _logo_on_plausible_host, "logo_url"),
        Rule("verified_officially", Severity.WARNING, _require_verification, "verification"),
        Rule("description_present", Severity.WARNING, _require_description, "short_description"),
        Rule("io_completeness", Severity.WARNING, _warn_missing_io, "inputs/outputs"),
        Rule("score_band", Severity.WARNING, _warn_low_band, "quality"),
        Rule("verification_fresh", Severity.WARNING, _warn_stale_verification, "last_verified_date"),
        Rule("dedup_clean", Severity.WARNING, _warn_duplicate_flag, "dedup"),
        Rule("adoption_signal", Severity.INFO, _warn_no_adoption, "adoption"),
    ]


class ToolValidator:
    """Runs schema + business validation over tool records."""

    def __init__(self, rules: Iterable[Rule] | None = None) -> None:
        self.rules = list(rules) if rules is not None else default_rules()

    def validate(self, tool: Tool) -> ValidationResult:
        result = ValidationResult(tool_id=tool.id, tool_name=tool.name)
        for rule in self.rules:
            issue = rule.run(tool)
            if issue:
                result.issues.append(issue)
        if result.warnings:
            for issue in result.warnings:
                tool.flag_for_review(f"{issue.rule}: {issue.message}")
        if result.errors and not tool.rejected:
            tool.reject(
                RejectionReason.MISSING_REQUIRED_FIELDS,
                "; ".join(issue.message for issue in result.errors)[:500],
            )
        return result

    def validate_many(self, tools: Iterable[Tool]) -> tuple[list[ValidationResult], dict]:
        results = [self.validate(tool) for tool in tools]
        publishable = [r for r in results if r.is_publishable]
        rule_counts: dict[str, int] = {}
        for result in results:
            for issue in result.issues:
                rule_counts[issue.rule] = rule_counts.get(issue.rule, 0) + 1
        summary = {
            "total": len(results),
            "publishable": len(publishable),
            "blocked": len(results) - len(publishable),
            "issues_by_rule": dict(sorted(rule_counts.items(), key=lambda kv: -kv[1])),
        }
        logger.info("validation complete", extra=summary)
        return results, summary

    @staticmethod
    def validate_schema(payload: dict) -> tuple[Tool | None, str | None]:
        """Schema-only validation of a raw dict (round-trip check)."""
        try:
            return Tool.model_validate(payload), None
        except PydanticValidationError as exc:
            return None, str(exc)
