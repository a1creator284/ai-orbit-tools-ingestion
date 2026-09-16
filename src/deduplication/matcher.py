"""Deterministic entity resolution: does evidence prove two records are one product?

Guideline §7 defines a duplicate as **the same underlying product** listed
multiple times by different directories, URLs, categories, aliases or listings.
It is *not* a duplicate merely because two records share a company, a domain, a
category, a similar name or a similar description.

Evidence hierarchy (strongest first)
------------------------------------
Each tier is a deterministic rule, not a score. The first tier whose evidence
is present decides, and the tier is recorded on the result so every merge is
auditable.

======  ====================================================================
Tier    Evidence — and what it proves
======  ====================================================================
**A**   Identical canonical official/product URL (after full normalization:
        http/https, ``www``, trailing slash, fragment, tracking parameters,
        index files, locale prefix, generic marketing path). Same URL ⇒ same
        product. ``MERGE``.
**B**   Identical canonical **product identity** (host + product path) on the
        same official site — e.g. ``jasper.ai`` from one directory and
        ``www.jasper.ai/pricing?utm_source=x`` from another. ``MERGE``.
**C**   Explicit canonical/alias identity: one record's asserted canonical URL
        (redirect target / ``rel=canonical``) or identity key equals the
        other's product identity. ``MERGE``.
**D**   Strong combination: same registrable domain **and** compatible product
        paths **and** an equal canonical name (or a recorded alias match)
        **and** no company contradiction. ``MERGE``.
**E**   Weaker signals (similar-but-unequal names, shared company, shared
        domain with only a name hint, identical repository). ``REVIEW`` — the
        pair is flagged with a reason and **both records are preserved**.
        Never an automatic merge.
======  ====================================================================

Hard false-positive protections (these *veto* a merge)
------------------------------------------------------
* **Domain-only equality is never proof.** Tier D additionally requires path
  compatibility *and* name agreement; tier B requires the full product key.
* **Conflicting product paths on one host** — ``company.com/product-a`` vs
  ``company.com/product-b`` — are different products. Vetoed.
* **Different subdomains** — ``wabot.wadesk.io`` vs ``tg.wadesk.io`` — are
  different products unless tier A/B/C evidence says otherwise. Vetoed.
* **Different official domains** never merge on name similarity alone.
* **Shared-host directories/app stores** (``apps.apple.com``,
  ``producthunt.com``, ``vercel.app``, …) are not identity: their registrable
  domain is ignored and only the full product path can identify a product.
* **No official URL on either side** means only name+company evidence exists,
  which is review-grade at best — never an automatic merge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.core.config import DedupConfig, get_settings
from src.core.product_identity import ProductIdentity, product_identity
from src.core.text import canonical_name, name_tokens, similarity, token_set_ratio
from src.models.tool import Tool

__all__ = [
    "BROAD_BLOCK_PREFIXES",
    "MatchDecision",
    "MatchTier",
    "MatchSignal",
    "MatchResult",
    "ToolMatcher",
]

#: Blocking-key prefixes that are *recall helpers*, not identity claims.
#:
#: A ``product:``/``canonical:``/``identity:``/``repo:`` block groups records
#: that already claim the same identity, so it is precise and small by nature.
#: A ``prefix:``/``token:``/``host:``/``domain:`` block instead groups every
#: record that merely *might* be related — for 1,000 candidates a single shared
#: 6-character prefix would produce ~500,000 comparisons. Broad blocks are
#: therefore capped much harder (see ``DedupConfig.max_broad_block_size``).
BROAD_BLOCK_PREFIXES = ("prefix:", "token:", "host:", "domain:")


class MatchDecision(str, Enum):
    """Outcome of comparing two tool records."""

    MERGE = "merge"        # same underlying product — merge into one record
    REVIEW = "review"      # possible duplicate — flag, preserve BOTH records
    DISTINCT = "distinct"  # different products — keep both, no flag


class MatchTier(str, Enum):
    """Which rule in the evidence hierarchy produced the decision."""

    IDENTICAL_ID = "identical_id"
    A_CANONICAL_URL = "tier_a_canonical_url"
    B_PRODUCT_IDENTITY = "tier_b_product_identity"
    C_EXPLICIT_ALIAS = "tier_c_explicit_alias"
    D_STRONG_COMBINATION = "tier_d_strong_combination"
    E_REVIEW = "tier_e_review"
    NONE = "no_matching_evidence"


@dataclass(frozen=True)
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
    """Explainable comparison result.

    ``tier`` and ``reasons`` are the audit trail: a merge always records which
    rule fired and on what evidence, and a review always records why the
    identity is uncertain.
    """

    decision: MatchDecision
    confidence: float
    tier: MatchTier = MatchTier.NONE
    signals: list[MatchSignal] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: The concrete evidence string, e.g. the shared product key.
    evidence: str | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.decision is MatchDecision.MERGE

    @property
    def reason(self) -> str:
        """Single-line human reason, stable for a given pair."""
        return "; ".join(self.reasons) if self.reasons else self.tier.value

    def explain(self) -> str:
        parts = [f"{s.name}={s.value:+.2f}(w{s.weight:g})" for s in self.signals]
        return (
            f"{self.decision.value} [{self.tier.value}] conf={self.confidence:.2f} "
            f"{self.reason}"
            + (f" signals=[{'; '.join(parts)}]" if parts else "")
        )

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "tier": self.tier.value,
            "confidence": round(self.confidence, 4),
            "evidence": self.evidence,
            "reasons": list(self.reasons),
        }


class ToolMatcher:
    """Decides whether two :class:`Tool` records describe the same product.

    Pure and deterministic: the decision depends only on the two records, never
    on batch contents, iteration order or wall-clock time.
    """

    def __init__(self, config: DedupConfig | None = None) -> None:
        self.config = config or get_settings().dedup
        self._ignored = {d.lower() for d in self.config.ignore_domains}

    # --------------------------------------------------------------- public
    def compare(self, left: Tool, right: Tool) -> MatchResult:
        """Compare two tools and return an explainable, tiered decision."""
        if left.id == right.id:
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                tier=MatchTier.IDENTICAL_ID,
                signals=[MatchSignal("identical_id", 1.0, 1.0, left.id)],
                reasons=[f"identical deterministic ID ({left.id})"],
                evidence=left.id,
            )

        left_identity = self._product_identity(left)
        right_identity = self._product_identity(right)

        # ---- tier A: identical canonical official/product URL.
        canonical = self._canonical_url_match(left, right)
        if canonical:
            return self._merge(
                MatchTier.A_CANONICAL_URL,
                canonical,
                f"identical canonical product URL: {canonical}",
            )

        # ---- tier B: identical canonical product identity (host + product path).
        if left_identity and right_identity and left_identity.key == right_identity.key:
            return self._merge(
                MatchTier.B_PRODUCT_IDENTITY,
                left_identity.key,
                f"identical official product identity: {left_identity.key}",
            )

        # ---- tier C: an explicitly asserted canonical/alias identity agrees.
        explicit = self._explicit_identity_match(
            left, right, left_identity, right_identity
        )
        if explicit:
            return self._merge(
                MatchTier.C_EXPLICIT_ALIAS,
                explicit,
                f"explicit canonical/alias identity agrees: {explicit}",
            )

        signals = self._signals(left, right, left_identity, right_identity)
        confidence = self._confidence(signals)

        # ---- hard vetoes: evidence that these are *different* products.
        veto = self._veto(left, right, left_identity, right_identity)
        if veto:
            # A near-identical name under a veto is worth a human look, but the
            # records are always preserved.
            name_score = self._best_name_score(left, right)
            if name_score >= self.config.review_name_floor:
                return MatchResult(
                    decision=MatchDecision.REVIEW,
                    confidence=confidence,
                    tier=MatchTier.E_REVIEW,
                    signals=signals,
                    reasons=[veto, f"but names are very similar ({name_score:.2f})"],
                    evidence=veto,
                )
            return MatchResult(
                decision=MatchDecision.DISTINCT,
                confidence=confidence,
                tier=MatchTier.NONE,
                signals=signals,
                reasons=[veto],
                evidence=veto,
            )

        # ---- tier D: strong combination of independent identity signals.
        strong = self._strong_combination(left, right, left_identity, right_identity)
        if strong:
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                tier=MatchTier.D_STRONG_COMBINATION,
                signals=signals,
                reasons=[strong],
                evidence=strong,
            )

        # ---- tier E: everything else is review-or-distinct, never a merge.
        return self._review_or_distinct(left, right, signals, confidence)

    def blocking_keys(self, tool: Tool) -> list[str]:
        """Cheap keys used to avoid O(n²) comparison.

        Two records are only compared if they share a blocking key. Returned as
        a **sorted list** (not a set) so the comparison order — and therefore
        the report — is identical across runs and Python hash seeds.
        """
        keys: set[str] = set()

        identity = self._product_identity(tool)
        if identity:
            keys.add(f"product:{identity.key}")
            keys.add(f"host:{identity.host}")
            if not self._is_shared_host(identity.registrable_domain):
                keys.add(f"domain:{identity.registrable_domain}")

        for url in self._canonical_urls(tool):
            keys.add(f"canonical:{url}")
        if tool.dedup.identity_key:
            keys.add(f"identity:{tool.dedup.identity_key.lower()}")

        canonical = canonical_name(tool.name)
        if canonical:
            keys.add(f"name:{canonical}")
            # prefix block catches "Jasper" vs "Jasper AI Writer"
            keys.add(f"prefix:{canonical[:6]}")
        for alias in tool.dedup.aliases:
            alias_key = canonical_name(alias)
            if alias_key:
                keys.add(f"name:{alias_key}")
        for token in name_tokens(tool.name):
            if len(token) >= 5:
                keys.add(f"token:{token}")

        if tool.repository_url:
            keys.add(f"repo:{tool.repository_url.lower()}")
        return sorted(keys)

    def block_limit(self, key: str) -> int:
        """Max members to compare inside the block named ``key``.

        Broad keys (name prefix, shared token, host, domain) get the small cap
        so a generic token can never turn deduplication into a full pairwise
        sweep; precise identity keys get the larger one.
        """
        if key.startswith(BROAD_BLOCK_PREFIXES):
            return self.config.max_broad_block_size
        return self.config.max_block_size

    # ------------------------------------------------------ tier evaluation
    @staticmethod
    def _merge(tier: MatchTier, evidence: str, reason: str) -> MatchResult:
        return MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            tier=tier,
            signals=[MatchSignal(tier.value, 1.0, 1.0, evidence)],
            reasons=[reason],
            evidence=evidence,
        )

    def _canonical_url_match(self, left: Tool, right: Tool) -> str | None:
        """Tier A — the two records point at the very same canonical URL."""
        shared = self._canonical_urls(left) & self._canonical_urls(right)
        return min(shared) if shared else None

    def _canonical_urls(self, tool: Tool) -> set[str]:
        """Every URL that *asserts* this record's official product identity.

        Directory listing URLs are deliberately excluded: two directories
        listing the same product have different listing URLs, and one directory
        listing two products has two listing URLs under one host — so a listing
        URL is a provenance pointer, not a cross-source identity.

        A URL that cannot identify a single product is also excluded: two
        unrelated tools whose ``website`` was (wrongly) recorded as a directory
        or app-store **root** must not merge on tier A just because the strings
        are equal.
        """
        urls = {
            tool.dedup.canonical_url,
            tool.website,
            tool.verification.final_url if tool.is_verified else None,
            tool.verification.official_url_checked if tool.is_verified else None,
        }
        return {url for url in urls if url and self._identifies_a_product(url)}

    def _identifies_a_product(self, url: str | None) -> bool:
        """True when ``url`` can stand for exactly one product."""
        identity = product_identity(url)
        if identity is None:
            return False
        return not (identity.is_root and self._is_shared_host(identity.registrable_domain))

    def _explicit_identity_match(
        self,
        left: Tool,
        right: Tool,
        left_identity: ProductIdentity | None,
        right_identity: ProductIdentity | None,
    ) -> str | None:
        """Tier C — an explicit canonical URL or identity key resolves the pair.

        Covers company renames and rebrands *where evidence exists*: if the old
        site's verified redirect target (or a stored ``canonical_url``) is the
        new site's product identity, the two records are the same product.
        """
        left_keys = self._explicit_keys(left)
        right_keys = self._explicit_keys(right)

        if left_identity and left_identity.key in right_keys:
            return left_identity.key
        if right_identity and right_identity.key in left_keys:
            return right_identity.key
        shared = left_keys & right_keys
        return min(shared) if shared else None

    def _explicit_keys(self, tool: Tool) -> set[str]:
        """Product keys explicitly asserted for this record (not merely observed)."""
        keys: set[str] = set()
        for url in (
            tool.dedup.canonical_url,
            tool.verification.final_url if tool.is_verified else None,
        ):
            identity = product_identity(url)
            if identity and self._identifies_a_product(url):
                keys.add(identity.key)
        basis = tool.dedup.identity_basis or ""
        if tool.dedup.identity_key and basis in {
            "canonical_url", "website_product_url", "website_domain", "explicit"
        }:
            keys.add(tool.dedup.identity_key.lower())
        if tool.dedup.product_key:
            keys.add(tool.dedup.product_key.lower())
        return keys

    def _veto(
        self,
        left: Tool,
        right: Tool,
        left_identity: ProductIdentity | None,
        right_identity: ProductIdentity | None,
    ) -> str | None:
        """Evidence that the two records are *different* products.

        A veto blocks any automatic merge below tier C. It never deletes data.
        """
        if left_identity and right_identity:
            if left_identity.path_conflicts_with(right_identity):
                return (
                    "different product paths on the same host: "
                    f"{left_identity.key} != {right_identity.key}"
                )
            if left_identity.host != right_identity.host:
                if left_identity.shares_registrable_domain(right_identity):
                    return (
                        "different hosts under one domain are different products: "
                        f"{left_identity.host} != {right_identity.host}"
                    )
                return (
                    "different official domains: "
                    f"{left_identity.registrable_domain} != "
                    f"{right_identity.registrable_domain}"
                )
            # Same host, one at the root and one on a product path: a shared
            # host that serves several products (apiframe.ai/models/<vendor>)
            # cannot be collapsed into its landing page.
            if left_identity.is_root != right_identity.is_root:
                return (
                    "a host landing page and a product path on it are not "
                    f"proven to be one product: {left_identity.key} vs {right_identity.key}"
                )
        if self._company_conflicts(left, right):
            return f"different companies: {left.company} != {right.company}"
        return None

    def _strong_combination(
        self,
        left: Tool,
        right: Tool,
        left_identity: ProductIdentity | None,
        right_identity: ProductIdentity | None,
    ) -> str | None:
        """Tier D — same site *and* equal product name (or a recorded alias).

        Only reached when no veto applies, so the paths are already compatible
        (one is a prefix of the other) and the hosts are equal. Domain equality
        alone never gets here.
        """
        if not left_identity or not right_identity:
            return None
        if left_identity.host != right_identity.host:
            return None

        left_name, right_name = canonical_name(left.name), canonical_name(right.name)
        if left_name and right_name and left_name == right_name:
            return (
                f"same official site ({left_identity.host}) with compatible product "
                f"paths and the same normalized name '{left_name}'"
            )
        if self._alias_identity_match(left, right):
            return (
                f"same official site ({left_identity.host}) with compatible product "
                "paths and a recorded name alias in common"
            )
        return None

    def _review_or_distinct(
        self,
        left: Tool,
        right: Tool,
        signals: list[MatchSignal],
        confidence: float,
    ) -> MatchResult:
        """Tier E — uncertain identity is queued for review, never merged."""
        name_score = self._best_name_score(left, right)
        reasons: list[str] = []
        if name_score >= self.config.review_name_floor:
            reasons.append(f"similar names ({name_score:.2f}) but no official-identity proof")
        if self._same_company(left, right):
            reasons.append(f"same company '{left.company}' but distinct product identities")
        if left.repository_url and left.repository_url == right.repository_url:
            reasons.append(f"same repository {left.repository_url}")

        if reasons and confidence >= self.config.review_similarity_threshold:
            return MatchResult(
                decision=MatchDecision.REVIEW,
                confidence=confidence,
                tier=MatchTier.E_REVIEW,
                signals=signals,
                reasons=reasons,
                evidence="; ".join(reasons),
            )
        return MatchResult(
            decision=MatchDecision.DISTINCT,
            confidence=confidence,
            tier=MatchTier.NONE,
            signals=signals,
            reasons=reasons or [f"no shared identity evidence (confidence {confidence:.2f})"],
        )

    # -------------------------------------------------------------- signals
    def _signals(
        self,
        left: Tool,
        right: Tool,
        left_identity: ProductIdentity | None,
        right_identity: ProductIdentity | None,
    ) -> list[MatchSignal]:
        """Explanatory signals. They inform *review*, they do not cause merges."""
        signals: list[MatchSignal] = []
        if left_identity and right_identity:
            same_domain = left_identity.shares_registrable_domain(right_identity)
            signals.append(
                MatchSignal(
                    "registrable_domain",
                    1.0 if same_domain else -1.0,
                    0.4,
                    left_identity.registrable_domain
                    if same_domain
                    else f"{left_identity.registrable_domain} != "
                    f"{right_identity.registrable_domain}",
                )
            )
            signals.append(
                MatchSignal(
                    "product_path",
                    1.0 if left_identity.key == right_identity.key else -1.0,
                    0.4,
                    f"{left_identity.key} vs {right_identity.key}",
                )
            )
        signals.append(
            MatchSignal(
                "name_similarity",
                self._best_name_score(left, right),
                0.25,
                f"{left.name} ~ {right.name}",
            )
        )
        signals.append(
            MatchSignal("name_tokens", token_set_ratio(left.name, right.name), 0.1)
        )
        if left.company and right.company:
            signals.append(
                MatchSignal(
                    "company",
                    similarity(left.company, right.company),
                    0.1,
                    f"{left.company} ~ {right.company}",
                )
            )
        if left.repository_url and right.repository_url:
            same_repo = left.repository_url.lower() == right.repository_url.lower()
            signals.append(
                MatchSignal("repository", 1.0 if same_repo else -0.5, 0.2, left.repository_url)
            )
        return signals

    @staticmethod
    def _confidence(signals: list[MatchSignal]) -> float:
        total_weight = sum(abs(s.weight) for s in signals) or 1.0
        raw = sum(s.contribution for s in signals) / total_weight
        return max(0.0, min(1.0, raw))

    # -------------------------------------------------------------- helpers
    def _product_identity(self, tool: Tool) -> ProductIdentity | None:
        """Canonical product identity from the record's **official** URL.

        A URL on a shared host (an app store, a directory, a site builder) is
        only usable when it carries a product path: ``apps.apple.com`` is not an
        identity, but ``apps.apple.com/app/foo`` is.
        """
        for url in (tool.dedup.canonical_url, tool.website):
            identity = product_identity(url)
            if identity is None:
                continue
            # A shared-host *root* (a directory/app-store/site-builder landing
            # page) identifies nothing; a product path on it does.
            if identity.is_root and self._is_shared_host(identity.registrable_domain):
                continue
            return identity
        return None

    def _is_shared_host(self, registrable_domain: str | None) -> bool:
        return bool(registrable_domain) and registrable_domain.lower() in self._ignored

    @staticmethod
    def _names(tool: Tool) -> set[str]:
        keys = {canonical_name(tool.name), canonical_name(tool.dedup.canonical_name_key)}
        keys.update(canonical_name(alias) for alias in tool.dedup.aliases)
        return {key for key in keys if key}

    def _best_name_score(self, left: Tool, right: Tool) -> float:
        """Best similarity over both records' names and recorded aliases."""
        best = 0.0
        for a in self._names(left) or {canonical_name(left.name) or ""}:
            for b in self._names(right) or {canonical_name(right.name) or ""}:
                if a and b:
                    best = max(best, 1.0 if a == b else similarity(a, b))
        return best

    def _alias_identity_match(self, left: Tool, right: Tool) -> bool:
        """True when a *recorded* alias (not a fuzzy guess) is shared."""
        return bool(self._names(left) & self._names(right))

    @staticmethod
    def _same_company(left: Tool, right: Tool) -> bool:
        if not left.company or not right.company:
            return False
        a, b = canonical_name(left.company), canonical_name(right.company)
        return bool(a and b and a == b)

    @staticmethod
    def _company_conflicts(left: Tool, right: Tool) -> bool:
        """True when both companies are known and clearly different."""
        if not left.company or not right.company:
            return False
        a, b = canonical_name(left.company), canonical_name(right.company)
        if not a or not b or a == b:
            return False
        return similarity(a, b) < 0.9
