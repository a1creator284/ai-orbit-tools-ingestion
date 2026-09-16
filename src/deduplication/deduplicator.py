"""Entity resolution / deduplication.

Guideline §7: the same tool will appear across many directories and must become
**one** AI Orbit record. Extra sightings become additional *discovery sources*
and extra *adoption signals* — never extra records.

Algorithm:

1. **Blocking** — group records by cheap keys (domain, canonical name, tokens)
   so we never do a full O(n²) comparison.
2. **Pairwise matching** — :class:`~src.deduplication.matcher.ToolMatcher`
   produces an explainable MERGE / REVIEW / DISTINCT decision.
3. **Union-find clustering** — MERGE edges form clusters of one real product.
4. **Merging** — one canonical record per cluster; field-level merge prefers
   officially verified values, then higher-trust sources, then completeness.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from src.core.logging_setup import get_logger
from src.core.text import canonical_name
from src.deduplication.matcher import MatchDecision, MatchResult, ToolMatcher
from src.models.enums import RejectionReason, VerificationStatus
from src.models.tool import Tool

logger = get_logger("dedup")


class UnionFind:
    """Minimal disjoint-set structure for clustering merge decisions."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a

    def clusters(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            groups[self.find(item)].append(item)
        return dict(groups)


@dataclass
class DedupReport:
    """Auditable summary of one deduplication run."""

    input_count: int = 0
    output_count: int = 0
    merged_count: int = 0
    review_pairs: list[tuple[str, str, float, str]] = field(default_factory=list)
    comparisons: int = 0
    clusters: dict[str, list[str]] = field(default_factory=dict)
    #: ``(blocking_key, size)`` for every block skipped as non-discriminative.
    skipped_blocks: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "input_count": self.input_count,
            "output_count": self.output_count,
            "merged_count": self.merged_count,
            "comparisons": self.comparisons,
            "skipped_block_count": len(self.skipped_blocks),
            "skipped_blocks": [
                {"key": key, "size": size} for key, size in sorted(self.skipped_blocks)
            ],
            "review_pair_count": len(self.review_pairs),
            "review_pairs": [
                {"left": a, "right": b, "confidence": round(c, 3), "explanation": e}
                for a, b, c, e in self.review_pairs
            ],
            "multi_record_clusters": {
                root: members for root, members in self.clusters.items() if len(members) > 1
            },
        }


class Deduplicator:
    """Clusters and merges duplicate tool records."""

    def __init__(self, matcher: ToolMatcher | None = None) -> None:
        self.matcher = matcher or ToolMatcher()

    # --------------------------------------------------------------- public
    def deduplicate(self, tools: Iterable[Tool]) -> tuple[list[Tool], DedupReport]:
        """Return ``(canonical_tools, report)``."""
        incoming = list(tools)
        report = DedupReport(input_count=len(incoming))

        # ---- 0. collapse records that already share a deterministic ID.
        # Two sightings of the same official domain produce the same UUIDv5, so
        # they must be MERGED (keeping every discovery source), never dropped.
        by_id: dict[str, list[Tool]] = defaultdict(list)
        for tool in incoming:
            by_id[tool.id].append(tool)
        records: dict[str, Tool] = {}
        for tool_id, group in by_id.items():
            if len(group) == 1:
                records[tool_id] = group[0]
            else:
                records[tool_id] = self.merge_cluster(group)
                report.merged_count += len(group) - 1

        if len(records) <= 1:
            report.output_count = len(records)
            return list(records.values()), report

        union = UnionFind()
        for tool_id in records:
            union.add(tool_id)

        # ---- 1. blocking
        blocks: dict[str, list[str]] = defaultdict(list)
        for tool in records.values():
            for key in self.matcher.blocking_keys(tool):
                blocks[key].append(tool.id)

        # ---- 2/3. pairwise matching within blocks + clustering
        compared: set[tuple[str, str]] = set()
        for key in sorted(blocks):  # deterministic block order
            members = blocks[key]
            limit = self.matcher.block_limit(key)
            if len(members) < 2 or len(members) > limit:
                # Oversized blocks are non-discriminative (e.g. a generic name
                # token shared by hundreds of records) and comparing them is
                # what turns dedup into an O(n²) sweep. Skipping loses no
                # *evidence-based* merge: identity-grade equality always also
                # lands the pair in a precise block.
                if len(members) > limit:
                    report.skipped_blocks.append((key, len(members)))
                    logger.debug(
                        "skipping oversized block",
                        extra={"key": key, "size": len(members), "limit": limit},
                    )
                continue
            for index, left_id in enumerate(members):
                for right_id in members[index + 1 :]:
                    pair = (left_id, right_id) if left_id < right_id else (right_id, left_id)
                    if pair in compared:
                        continue
                    compared.add(pair)
                    report.comparisons += 1
                    result = self.matcher.compare(records[pair[0]], records[pair[1]])
                    self._record_decision(union, records, report, pair, result)

        # ---- 4. merging
        clusters = union.clusters()
        report.clusters = clusters
        canonical: list[Tool] = []
        for members in clusters.values():
            group = [records[m] for m in members if m in records]
            if not group:
                continue
            if len(group) == 1:
                canonical.append(group[0])
                continue
            merged = self.merge_cluster(group)
            report.merged_count += len(group) - 1
            canonical.append(merged)

        report.output_count = len(canonical)
        logger.info(
            "deduplication complete",
            extra={
                "input": report.input_count,
                "output": report.output_count,
                "merged": report.merged_count,
                "comparisons": report.comparisons,
                "review_pairs": len(report.review_pairs),
            },
        )
        return canonical, report

    def merge_cluster(self, group: Sequence[Tool]) -> Tool:
        """Merge duplicate records into one canonical :class:`Tool`.

        Records in a cluster may legitimately share an ``id`` (the same official
        domain yields the same UUIDv5), so the primary is excluded by *object
        identity*, not by ID.
        """
        primary = max(group, key=self._primary_rank)
        others = [tool for tool in group if tool is not primary]

        for other in others:
            self._merge_into(primary, other)
            if other.id != primary.id:
                primary.dedup.merged_from_ids = sorted(
                    {*primary.dedup.merged_from_ids, other.id}
                )

        aliases = {primary.name, *primary.dedup.aliases}
        for other in others:
            aliases.update({other.name, *other.dedup.aliases})
        primary.dedup.aliases = sorted(a for a in aliases if a)

        # provenance: every identity key and listing URL folded in is kept, so a
        # reviewer can retrace each sighting that became this single record.
        identity_keys = {primary.dedup.identity_key, *primary.dedup.merged_identity_keys}
        listing_urls = list(primary.dedup.listing_urls)
        evidence = list(primary.dedup.merge_evidence)
        for other in others:
            identity_keys.update({other.dedup.identity_key, *other.dedup.merged_identity_keys})
            listing_urls = _merge_unique(listing_urls, other.dedup.listing_urls)
            evidence = _merge_unique(evidence, other.dedup.merge_evidence)
        primary.dedup.merged_identity_keys = sorted(
            key for key in identity_keys if key and key != primary.dedup.identity_key
        )
        primary.dedup.listing_urls = listing_urls
        primary.dedup.merge_evidence = evidence
        primary.dedup.merged_source_count = len(primary.discovery_sources)
        return primary

    # -------------------------------------------------------------- helpers
    def _record_decision(
        self,
        union: UnionFind,
        records: dict[str, Tool],
        report: DedupReport,
        pair: tuple[str, str],
        result: MatchResult,
    ) -> None:
        left, right = records[pair[0]], records[pair[1]]
        if result.decision is MatchDecision.MERGE:
            union.union(pair[0], pair[1])
            # Persist the audit trail on *both* sides so the surviving record
            # carries the evidence regardless of which one becomes canonical.
            evidence = f"{result.tier.value}: {result.reason}"
            for record in (left, right):
                record.dedup.merge_evidence = _merge_unique(
                    record.dedup.merge_evidence, [evidence]
                )
        elif result.decision is MatchDecision.REVIEW:
            report.review_pairs.append((pair[0], pair[1], result.confidence, result.explain()))
            left.flag_for_review(f"possible duplicate of {right.name} ({right.id})")
            right.flag_for_review(f"possible duplicate of {left.name} ({left.id})")
            left.dedup.review_candidates = sorted({*left.dedup.review_candidates, right.id})
            right.dedup.review_candidates = sorted({*right.dedup.review_candidates, left.id})
            left.dedup.review_reasons = _merge_unique(
                left.dedup.review_reasons, [f"{right.id}: {result.reason}"]
            )
            right.dedup.review_reasons = _merge_unique(
                right.dedup.review_reasons, [f"{left.id}: {result.reason}"]
            )

    @staticmethod
    def _primary_rank(tool: Tool) -> tuple:
        """Pick the best canonical record: verified > domain-identified > complete."""
        verified = tool.verification.status in (
            VerificationStatus.VERIFIED,
            VerificationStatus.VERIFIED.value,
        )
        domain_based = (tool.dedup.identity_basis or "").endswith("domain")
        return (
            1 if verified else 0,
            1 if domain_based else 0,
            1 if tool.website else 0,
            tool.completeness(),
            tool.score or 0.0,
            len(tool.discovery_sources),
        )

    def _merge_into(self, primary: Tool, other: Tool) -> None:
        """Copy every value the primary lacks; never overwrite verified data."""
        for source in other.discovery_sources:
            primary.add_discovery_source(source)

        scalar_fields = (
            "company", "company_id", "website", "logo_url", "country", "version",
            "launch_date", "launch_date_precision", "status", "short_description",
            "detailed_overview", "primary_task", "primary_task_id", "has_api",
            "api_docs_url", "open_source_status", "repository_url",
            "signup_requirement", "aiorbit_summary", "description", "url",
            "last_verified_date",
        )
        for name in scalar_fields:
            if getattr(primary, name, None) in (None, "") and getattr(other, name, None) not in (None, ""):
                setattr(primary, name, getattr(other, name))

        list_fields = (
            "categories", "tags", "key_features", "use_cases", "ai_capabilities",
            "inputs", "outputs", "platforms", "integrations", "pros", "cons",
            "limitations",
        )
        for name in list_fields:
            merged = _merge_unique(getattr(primary, name) or [], getattr(other, name) or [])
            setattr(primary, name, merged)

        # pricing: fill gaps only
        for name in (
            "model", "starting_price_amount", "starting_price_currency",
            "starting_price_period", "starting_price_raw", "has_free_plan",
            "has_free_trial", "free_trial_days", "pricing_url", "pricing_verified_at",
        ):
            if getattr(primary.pricing, name) is None and getattr(other.pricing, name) is not None:
                setattr(primary.pricing, name, getattr(other.pricing, name))
        primary.pricing.usage_limits = _merge_unique(
            primary.pricing.usage_limits, other.pricing.usage_limits
        )

        # adoption: keep the strongest verified signal from any source
        for name, value in other.adoption.model_dump(exclude={"signal_sources", "has_any_signal"}).items():
            current = getattr(primary.adoption, name, None)
            if value in (None, [], ""):
                continue
            if current in (None, [], ""):
                setattr(primary.adoption, name, value)
            elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
                setattr(primary.adoption, name, max(current, value))
        primary.adoption.signal_sources = _merge_refs(
            primary.adoption.signal_sources, other.adoption.signal_sources
        )

        # verification: never downgrade
        if not primary.is_verified and other.is_verified:
            primary.verification = other.verification

        # review + rejection flags are unioned so nothing is lost silently
        if other.needs_human_review:
            primary.needs_human_review = True
        primary.review_notes = _merge_unique(primary.review_notes, other.review_notes)
        if other.rejected and RejectionReason.DUPLICATE not in other.rejection_reasons:
            primary.rejection_reasons = _merge_unique(
                primary.rejection_reasons, other.rejection_reasons
            )
            primary.rejection_notes = _merge_unique(primary.rejection_notes, other.rejection_notes)
        if not primary.dedup.canonical_name_key:
            primary.dedup.canonical_name_key = canonical_name(primary.name)


def _merge_unique(left: Iterable, right: Iterable) -> list:
    out: list = []
    seen: set[str] = set()
    for item in [*left, *right]:
        key = str(item).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _merge_refs(left: Iterable, right: Iterable) -> list:
    out: list = []
    seen: set[tuple[str, str]] = set()
    for ref in [*left, *right]:
        key = (str(getattr(ref, "name", "")).lower(), str(getattr(ref, "url", "") or ""))
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out
