"""Match-signal computation for entity resolution.

Implements the deduplication signals listed in the Tools guideline §7:
official website/domain, company/developer, product name, product URL, same
underlying product, and "slightly different name" variations.

The design is explicitly pluggable: :class:`MatchSignal` computations are
independent, and :class:`ToolMatcher` combines them into a decision. Extra
signals (embedding similarity, WHOIS, favicon hashing, ...) can be added later
without changing the deduplicator or the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.core.config import DedupConfig, get_settings
from src.core.text import canonical_name, name_tokens, similarity, token_set_ratio
from src.core.urls import extract_registrable_domain
from src.models.tool import Tool


class MatchDecision(str, Enum):
    """Outcome of comparing two tool records."""

    MERGE = "merge"        # same underlying product — merge into one record
    REVIEW = "review"      # probable duplicate — flag for human review
    DISTINCT = "distinct"  # different products — keep both


@dataclass
class MatchSignal:
    """One piece of evidence for/against two records being the same tool."""

    name: str
    value: float          # -1.0 (contradicts) .. 1.0 (supports)
    weight: float
    detail: str | None = None

    @property
    def contribution(self) -> float:
        return self.value * self.weight


@dataclass
class MatchResult:
    """Explainable comparison result."""

    decision: MatchDecision
    confidence: float
    signals: list[MatchSignal] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return self.decision is MatchDecision.MERGE

    def explain(self) -> str:
        parts = [f"{s.name}={s.value:+.2f}(w{s.weight:g})" for s in self.signals]
        return f"{self.decision.value} conf={self.confidence:.2f} [{'; '.join(parts)}]"


class ToolMatcher:
    """Computes whether two :class:`Tool` records describe the same product."""

    def __init__(self, config: DedupConfig | None = None) -> None:
        self.config = config or get_settings().dedup
        self._ignored = {d.lower() for d in self.config.ignore_domains}

    # --------------------------------------------------------------- public
    def compare(self, left: Tool, right: Tool) -> MatchResult:
        """Compare two tools and return an explainable decision."""
        if left.id == right.id:
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                signals=[MatchSignal("identical_id", 1.0, 1.0, left.id)],
                reasons=["identical deterministic ID"],
            )

        signals: list[MatchSignal] = []
        reasons: list[str] = []

        domain_signal = self._domain_signal(left, right)
        if domain_signal:
            signals.append(domain_signal)

        # An authoritative shared official domain is decisive: the same site
        # cannot be two different products in AI Orbit terms.
        if (
            self.config.domain_match_is_authoritative
            and domain_signal
            and domain_signal.value >= 1.0
        ):
            reasons.append(f"shared official domain: {domain_signal.detail}")
            return MatchResult(MatchDecision.MERGE, 1.0, signals, reasons)

        signals.append(self._name_signal(left, right))
        signals.append(self._token_signal(left, right))
        company_signal = self._company_signal(left, right)
        if company_signal:
            signals.append(company_signal)
        repo_signal = self._repository_signal(left, right)
        if repo_signal:
            signals.append(repo_signal)

        total_weight = sum(abs(s.weight) for s in signals) or 1.0
        confidence = max(0.0, min(1.0, sum(s.contribution for s in signals) / total_weight))

        name_score = max(
            similarity(left.name, right.name), token_set_ratio(left.name, right.name)
        )
        distinct_domains = (
            domain_signal is not None and domain_signal.value < 0
        )

        if distinct_domains and name_score < 1.0:
            reasons.append("different official domains: treated as distinct products")
            decision = MatchDecision.REVIEW if name_score >= 0.95 else MatchDecision.DISTINCT
            return MatchResult(decision, confidence, signals, reasons)

        if confidence >= self.config.name_similarity_threshold:
            decision = MatchDecision.MERGE
            reasons.append(f"strong combined match (confidence {confidence:.2f})")
        elif confidence >= self.config.review_similarity_threshold:
            decision = MatchDecision.REVIEW
            reasons.append(f"borderline match (confidence {confidence:.2f}) — needs human review")
        else:
            decision = MatchDecision.DISTINCT
            reasons.append(f"weak match (confidence {confidence:.2f})")

        return MatchResult(decision, confidence, signals, reasons)

    def blocking_keys(self, tool: Tool) -> set[str]:
        """Cheap keys used to avoid O(n²) comparison.

        Two records are only compared if they share a blocking key.
        """
        keys: set[str] = set()
        domain = self._official_domain(tool)
        if domain:
            keys.add(f"domain:{domain}")
        canonical = canonical_name(tool.name)
        if canonical:
            keys.add(f"name:{canonical}")
            # prefix block catches "Jasper" vs "Jasper AI Writer"
            keys.add(f"prefix:{canonical[:6]}")
        for token in name_tokens(tool.name):
            if len(token) >= 5:
                keys.add(f"token:{token}")
        repo = extract_registrable_domain(tool.repository_url)
        if tool.repository_url and repo:
            keys.add(f"repo:{tool.repository_url.lower()}")
        return keys

    # -------------------------------------------------------------- signals
    def _official_domain(self, tool: Tool) -> str | None:
        """Registrable domain of the official site, ignoring shared hosts.

        A tool hosted on ``vercel.app`` or listed on ``producthunt.com`` does
        not share identity with every other tool on that host.
        """
        domain = extract_registrable_domain(tool.website) or tool.dedup.canonical_domain
        if not domain or domain.lower() in self._ignored:
            return None
        return domain.lower()

    def _domain_signal(self, left: Tool, right: Tool) -> MatchSignal | None:
        left_domain, right_domain = self._official_domain(left), self._official_domain(right)
        if not left_domain or not right_domain:
            return None
        if left_domain == right_domain:
            return MatchSignal("official_domain", 1.0, 0.6, left_domain)
        return MatchSignal(
            "official_domain", -1.0, 0.6, f"{left_domain} != {right_domain}"
        )

    @staticmethod
    def _name_signal(left: Tool, right: Tool) -> MatchSignal:
        score = similarity(left.name, right.name)
        # also consider recorded aliases (directories rename products freely)
        for alias in [*left.dedup.aliases, left.name]:
            for other in [*right.dedup.aliases, right.name]:
                score = max(score, similarity(alias, other))
        return MatchSignal("name_similarity", score, 0.25, f"{left.name} ~ {right.name}")

    @staticmethod
    def _token_signal(left: Tool, right: Tool) -> MatchSignal:
        score = token_set_ratio(left.name, right.name)
        return MatchSignal("name_tokens", score, 0.1)

    @staticmethod
    def _company_signal(left: Tool, right: Tool) -> MatchSignal | None:
        if not left.company or not right.company:
            return None
        score = similarity(left.company, right.company)
        # Same company + similar name is strong; same company alone is weak
        # (one company ships many distinct tools), hence the small weight.
        return MatchSignal("company", score, 0.1, f"{left.company} ~ {right.company}")

    @staticmethod
    def _repository_signal(left: Tool, right: Tool) -> MatchSignal | None:
        if not left.repository_url or not right.repository_url:
            return None
        same = left.repository_url.lower() == right.repository_url.lower()
        return MatchSignal("repository", 1.0 if same else -0.5, 0.2, left.repository_url)
