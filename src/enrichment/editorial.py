"""Deterministic editorial synthesis.

Guideline §9 asks for a short description, a detailed overview, pros, cons and
an "AI Orbit verdict". Those are *editorial* fields — but the guideline's other
rule is absolute: **never state anything that is not verified**.

This module resolves that tension by making editorial text a pure function of
the record's own verified facts:

* every pro cites a fact that is on the record (a published API, a documented
  free plan, an open-source licence…);
* every con is either a limitation the official page itself stated, or a
  *transparency* gap we can prove ("pricing is not published on the official
  site") — never a claim about quality, performance or popularity;
* the verdict names the primary task, the evidence we hold and the evidence we
  lack, and nothing else.

Consequences of that design:

* it is deterministic, so two runs produce identical text and the scorer stays
  reproducible;
* it needs no LLM and no API key, so the pipeline is fully operable offline;
* it cannot hallucinate, because it has no generative step at all. Adoption,
  funding, customers, performance and pricing superlatives are structurally
  unreachable from here.

An LLM-backed rewriter may later be layered on top (``llm.enabled`` in
``config/settings.yaml``), but it must consume these same grounded facts; it is
not permitted to add new ones.
"""

from __future__ import annotations

from typing import Any

from src.core.logging_setup import get_logger
from src.enrichment.base import Enricher, EnrichmentResult
from src.models.enums import OpenSourceStatus, SignupRequirement, VerificationStatus
from src.models.tool import Tool

logger = get_logger("enrichment.editorial")

__all__ = ["EditorialSynthesizer", "build_pros", "build_cons", "build_verdict"]

#: Caps keep the editorial block readable and comparable across records.
MAX_PROS = 6
MAX_CONS = 5


def _is_open_source(tool: Tool) -> bool:
    return tool.open_source_status in (
        OpenSourceStatus.OPEN_SOURCE,
        OpenSourceStatus.OPEN_SOURCE.value,
    )


def build_pros(tool: Tool) -> list[str]:
    """Pros, each one a restatement of a fact already on the record.

    Deliberately boring: a pro is only ever "this verified fact is a benefit",
    so there is no room for an invented advantage.
    """
    pros: list[str] = []

    if tool.pricing.has_free_plan:
        pros.append("Free plan available, so the tool can be evaluated at no cost")
    elif tool.pricing.has_free_trial:
        days = tool.pricing.free_trial_days
        pros.append(
            f"Free trial available ({days} days)" if days else "Free trial available"
        )

    if tool.signup_requirement in (SignupRequirement.NONE, SignupRequirement.NONE.value):
        pros.append("Usable without creating an account")

    if tool.has_api:
        pros.append(
            "Public API for programmatic use"
            + (" with published documentation" if tool.api_docs_url else "")
        )

    if _is_open_source(tool):
        pros.append(
            "Open source"
            + (" with a public repository" if tool.repository_url else "")
        )

    if len(tool.platforms) >= 2:
        pros.append(
            "Available on multiple platforms: "
            + ", ".join(str(p) for p in tool.platforms[:4])
        )

    if len(tool.integrations) >= 3:
        pros.append(
            "Integrates with existing tooling: "
            + ", ".join(tool.integrations[:4])
        )

    if len(tool.key_features) >= 4:
        pros.append(
            f"Documents {len(tool.key_features)} distinct capabilities on its own site"
        )

    if tool.pricing.model and tool.pricing.starting_price_raw:
        pros.append(
            f"Pricing is published openly (from {tool.pricing.starting_price_raw})"
        )

    return pros[:MAX_PROS]


def build_cons(tool: Tool) -> list[str]:
    """Cons: page-stated limitations first, then *provable* transparency gaps.

    A gap is phrased as a statement about the **evidence**, not about the
    product: "pricing is not published on the official website" is something we
    verified; "the product is expensive" is not.
    """
    cons: list[str] = []

    # 1. Limitations the vendor itself published.
    for limitation in tool.limitations[:3]:
        cons.append(limitation)

    # 2. Verifiable transparency gaps.
    if not tool.pricing.model and not tool.pricing.starting_price_raw:
        cons.append("Pricing is not published on the official website")
    if tool.signup_requirement in (
        SignupRequirement.REQUIRED,
        SignupRequirement.REQUIRED.value,
    ) and not (tool.pricing.has_free_plan or tool.pricing.has_free_trial):
        cons.append("An account is required and no free plan or trial is documented")
    if tool.signup_requirement in (
        SignupRequirement.WAITLIST,
        SignupRequirement.WAITLIST.value,
    ):
        cons.append("Access is gated behind a waitlist")
    if tool.has_api is None:
        cons.append("The official site does not state whether an API is available")
    if not tool.company:
        cons.append("The operating company is not identified on the official website")
    if tool.verification.status in (
        VerificationStatus.PARTIALLY_VERIFIED,
        VerificationStatus.PARTIALLY_VERIFIED.value,
    ):
        cons.append(
            "Only partially verifiable from the official website — some claims "
            "could not be confirmed"
        )

    # De-duplicate while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for item in cons:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:MAX_CONS]


def build_verdict(tool: Tool) -> str | None:
    """The AI Orbit verdict: what we verified, and what we could not.

    Returns ``None`` when there is nothing verified to say — an empty verdict
    is more honest than a generic one, and the scorer treats a missing verdict
    as missing evidence rather than as a neutral default.
    """
    if not tool.is_verified:
        return None

    sentences: list[str] = []
    name = tool.name or "This tool"

    task = tool.primary_task
    if task:
        sentences.append(f"{name} is an AI tool for {task.lower()}.")
    elif tool.ai_capabilities:
        caps = ", ".join(str(c).replace("_", " ") for c in tool.ai_capabilities[:3])
        sentences.append(f"{name} provides {caps}.")
    else:
        sentences.append(f"{name} was verified against its official website.")

    # What the official site actually evidences.
    evidenced: list[str] = []
    if tool.key_features:
        evidenced.append(f"{len(tool.key_features)} documented feature(s)")
    if tool.use_cases:
        evidenced.append(f"{len(tool.use_cases)} stated use case(s)")
    if tool.inputs and tool.outputs:
        evidenced.append(
            f"{len(tool.inputs)} input and {len(tool.outputs)} output format(s)"
        )
    if tool.platforms:
        evidenced.append(f"{len(tool.platforms)} platform(s)")
    if tool.integrations:
        evidenced.append(f"{len(tool.integrations)} integration(s)")
    if evidenced:
        sentences.append(
            "Its official site documents " + ", ".join(evidenced) + "."
        )

    # Access and pricing, stated factually.
    if tool.pricing.has_free_plan and tool.pricing.starting_price_raw:
        sentences.append(
            f"There is a free plan, with paid tiers from "
            f"{tool.pricing.starting_price_raw}."
        )
    elif tool.pricing.has_free_plan:
        sentences.append("A free plan is documented.")
    elif tool.pricing.starting_price_raw:
        sentences.append(f"Published pricing starts at {tool.pricing.starting_price_raw}.")
    elif not tool.pricing.model:
        sentences.append("Pricing is not published on the official site.")

    # The honest caveat, so the verdict never over-claims.
    gaps: list[str] = []
    if not tool.adoption.has_any_signal:
        gaps.append("no independent usage or adoption data was verifiable")
    if tool.launch_date is None:
        gaps.append("no launch or release date is published")
    if gaps:
        sentences.append(
            ("Note that " + " and ".join(gaps) + ".").capitalize()
            if not sentences
            else "Note that " + " and ".join(gaps) + "."
        )

    verdict = " ".join(sentences).strip()
    return verdict[:1200] or None


class EditorialSynthesizer(Enricher):
    """Fills the editorial fields from verified facts only.

    Runs as a normal :class:`~src.enrichment.base.Enricher`, so the pipeline's
    additive-only contract applies: it can fill a gap, but it can never
    overwrite a field verified from the official website.
    """

    source_name = "AI Orbit editorial synthesis (deterministic)"
    provides = ("pros", "cons", "aiorbit_summary", "detailed_overview")

    def applies_to(self, tool: Tool) -> bool:
        """Only verified records get editorial text.

        Writing a verdict about an unverified record would dress up an
        unverified record as a reviewed one.
        """
        if not tool.is_verified:
            return False
        return any(
            getattr(tool, name, None) in (None, "", [], {}) for name in self.provides
        )

    def enrich(self, tool: Tool) -> EnrichmentResult:
        values: dict[str, Any] = {}

        if not tool.pros:
            pros = build_pros(tool)
            if pros:
                values["pros"] = pros
        if not tool.cons:
            cons = build_cons(tool)
            if cons:
                values["cons"] = cons
        if not tool.aiorbit_summary:
            verdict = build_verdict(tool)
            if verdict:
                values["aiorbit_summary"] = verdict

        return EnrichmentResult(
            source_name=self.source_name,
            values=values,
            # Grounded in already-verified facts, but *not itself* an official
            # page reading: it must not be recorded as a verified field.
            verified=False,
        )
