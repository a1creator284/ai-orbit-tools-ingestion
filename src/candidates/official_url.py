"""Official-URL resolution from a directory detail page.

Why this module exists
----------------------
Discovery leaves a large, *known* hole in the dataset. A directory listing card
sometimes publishes the product's outbound official URL (AIToolNet does) and
sometimes does not (Dang.ai does not, and some Creati rows do not). The
adapters deliberately refuse to guess a domain from a ``/tool/<slug>`` slug, so
those candidates reach the pipeline with ``website = None``.

That is honest, but it is also terminal: :mod:`src.verification.verifier`
fetches the **official URL and nothing else** — a listing URL is a discovery
pointer and is never fetched as a substitute. A candidate without an official
URL therefore fails verification with ``no_official_url`` forever, no matter how
real the product is. Measured on the committed production pool
(``data/raw/candidates.jsonl``, 3,957 rows): **1,763 candidates — 44.6% —
carry no website**, i.e. all 1,713 Dang rows and 50 Creati rows. Those records
can never be verified, scored on real evidence, or published.

This module closes that hole, and it is the only stage allowed to put a
*website* on a candidate that did not have one.

What counts as a resolved URL
-----------------------------
Only a URL the directory **itself published** for that product, read from one
of two page structures that actually exist on the live sites (both verified
against real fetches on 2026-09-16):

``json_ld_software_url``
    A ``schema.org`` ``SoftwareApplication``/``WebApplication``/``Product``
    node whose ``url``/``sameAs`` names the product's own site. This is the
    directory's machine-readable declaration, and it carries the product
    ``name`` too, which lets us cross-check identity before accepting it.
    (Dang.ai publishes exactly this.)

``outbound_visit_link``
    An explicit outbound "Visit"/"Website" anchor pointing off-site — the
    button a human would click to leave the directory for the product.
    (Creati.ai publishes exactly this, as ``rel="external"``.)

Hard rules encoded here
-----------------------
* **Nothing is guessed.** A slug is never turned into a domain, no TLD is ever
  tried, no search engine is consulted. If the page does not publish a URL, the
  result is a :class:`ResolutionFailure` code and the candidate keeps
  ``website = None``.
* **The URL is a directory claim, not verification.** Resolution never sets a
  verification status and never promotes a record out of ``unverified``. It
  hands :mod:`src.verification.verifier` something to *check*; the official
  page still has to prove itself.
* **Directory-internal and shared hosts are rejected.** A link back into the
  directory, or to ``github.com`` / ``play.google.com`` / a social profile, is
  not a product website (see
  :data:`~src.core.product_identity.SHARED_HOST_DOMAINS`).
* **Identity is cross-checked when the page gives us a name.** If the JSON-LD
  node names a *different* product than the candidate, the URL is refused
  (``identity_mismatch``) rather than attached to the wrong record.
* **Conflicts are surfaced, not silently picked.** When the page publishes two
  different registrable domains, the stronger evidence wins and the candidate
  is flagged for review.
* **Pure core.** :func:`extract_official_url_evidence` is a pure function over
  markup — no network, no clock — so every decision is reproducible from a
  fixture. Only :class:`OfficialUrlResolver` touches the network, and only to
  fetch the detail URL the candidate already carries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from src.core.http_client import FetchResult, HttpClient
from src.core.logging_setup import get_logger
from src.core.product_identity import SHARED_HOST_DOMAINS
from src.core.text import canonical_name, clean_text, name_tokens
from src.core.urls import extract_registrable_domain, normalize_url
from src.extraction.html import (
    absolutize,
    anchor_targets,
    best_label,
    is_asset_url,
    looks_like_challenge,
    parse_html,
)

logger = get_logger("candidates.official_url")

__all__ = [
    "ResolutionFailure",
    "ResolutionBasis",
    "UrlCandidate",
    "OfficialUrlEvidence",
    "ResolutionResult",
    "ResolutionReport",
    "extract_official_url_evidence",
    "OfficialUrlResolver",
]


class ResolutionFailure:
    """Stable reason codes for *why* no official URL was resolved.

    Every code means "we could not read a published URL", never "the product
    does not exist". A failure leaves ``website = None`` exactly as discovery
    left it, so no downstream stage can mistake a failure for a finding.
    """

    #: The candidate has no directory detail URL to read at all.
    NO_DETAIL_URL = "no_detail_url"
    #: The candidate already carries a website; resolution was not needed.
    ALREADY_RESOLVED = "already_resolved"
    #: Transport failed (timeout, DNS, retries exhausted).
    FETCH_FAILED = "fetch_failed"
    #: The detail page answered with a non-2xx status.
    HTTP_ERROR = "http_error"
    #: An anti-bot interstitial was served instead of the page.
    BOT_CHALLENGE = "bot_challenge"
    #: The response body was not HTML.
    NON_HTML_RESPONSE = "non_html_response"
    #: The response body was empty.
    EMPTY_RESPONSE = "empty_response"
    #: The body could not be parsed into a document.
    UNPARSEABLE_HTML = "unparseable_html"
    #: The page parsed fine but publishes no outbound product URL.
    NO_OUTBOUND_URL = "no_outbound_url"
    #: Every outbound URL pointed at the directory itself or a shared host.
    ONLY_SHARED_HOSTS = "only_shared_hosts"
    #: The page named a different product than the candidate.
    IDENTITY_MISMATCH = "identity_mismatch"
    #: The record was malformed enough that resolution could not be attempted.
    RESOLUTION_FAILED = "resolution_failed"


class ResolutionBasis:
    """How a resolved URL was obtained, strongest first."""

    #: ``schema.org`` software/product node published by the directory.
    JSON_LD = "json_ld_software_url"
    #: Explicit outbound "Visit site" anchor.
    VISIT_LINK = "outbound_visit_link"


#: Evidence strength per basis. A JSON-LD declaration is machine-readable and
#: carries the product name, so it outranks a button we had to recognise by its
#: label.
_BASIS_RANK = {ResolutionBasis.JSON_LD: 2, ResolutionBasis.VISIT_LINK: 1}

#: ``schema.org`` types that describe *the product itself*.
_PRODUCT_TYPES = (
    "softwareapplication",
    "webapplication",
    "mobileapplication",
    "product",
    "service",
)

#: Anchor labels/attributes that mark the directory's outbound product button.
#: Matched against the anchor's label, ``title``/``aria-label`` and class list —
#: never against the URL, so an arbitrary off-site link is not promoted just
#: because its path happens to contain a word.
_VISIT_LABEL_PATTERNS = (
    "visit",
    "official site",
    "official website",
    "go to site",
    "go to website",
    "open site",
    "open website",
    "try it",
    "try now",
    "website",
    "homepage",
)

#: ``rel`` tokens a directory uses to mark a link as leaving the site.
_OUTBOUND_REL_TOKENS = frozenset({"external", "nofollow", "sponsored"})

#: Hosts that are never a product's own website even though they are off-site.
#: Extends the shared-host list with pure social/analytics surfaces.
_NEVER_OFFICIAL = frozenset(
    {
        "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
        "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "discord.com",
        "discord.gg", "t.me", "telegram.me", "medium.com", "substack.com",
        "pinterest.com", "threads.net", "whatsapp.com", "mastodon.social",
        "google.com", "gstatic.com", "googleapis.com", "doubleclick.net",
        "bit.ly", "buymeacoffee.com", "patreon.com", "ko-fi.com",
        "crunchbase.com", "wikipedia.org", "trustpilot.com", "g2.com",
        "capterra.com", "producthunt.com", "apple.com", "microsoft.com",
    }
)

#: Content types we can read a page from.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml")

_WS_RE = re.compile(r"\s+")


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """``getattr`` that also survives an attribute which *raises*.

    ``getattr(obj, name, default)`` only swallows :class:`AttributeError`; a
    property that raises anything else propagates and would take down the whole
    pass. Candidate records are rehydrated from JSONL written by earlier runs,
    so one hostile record must never be able to do that.
    """
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 - deliberately broad: resilience boundary
        return default


# --------------------------------------------------------------------------- #
# evidence records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UrlCandidate:
    """One outbound URL the detail page published, with how it was found."""

    url: str
    basis: str
    #: Registrable domain of :attr:`url` — the identity unit we compare on.
    domain: str | None
    #: Product name the page attached to this URL, when it published one.
    declared_name: str | None = None
    #: Human-readable justification, e.g. the anchor label that was matched.
    evidence: str | None = None

    @property
    def rank(self) -> int:
        return _BASIS_RANK.get(self.basis, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "basis": self.basis,
            "domain": self.domain,
            "declared_name": self.declared_name,
            "evidence": self.evidence,
        }


@dataclass
class OfficialUrlEvidence:
    """Everything the detail page said about where the product lives.

    Transport facts and page-published URLs only. No decision is taken here —
    :meth:`OfficialUrlResolver.resolve_candidate` applies the policy.
    """

    detail_url: str
    status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    #: Registrable domain of the directory itself (used to reject self-links).
    directory_domain: str | None = None
    #: Outbound product URLs found on the page, strongest first.
    url_candidates: list[UrlCandidate] = field(default_factory=list)
    #: Off-site URLs that were found but rejected, with the reason.
    rejected: list[dict[str, str]] = field(default_factory=list)
    failure: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_candidate(self) -> bool:
        return bool(self.url_candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail_url": self.detail_url,
            "status": self.status,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "directory_domain": self.directory_domain,
            "url_candidates": [c.to_dict() for c in self.url_candidates],
            "rejected": list(self.rejected),
            "failure": self.failure,
            "notes": list(self.notes),
        }


@dataclass
class ResolutionResult:
    """Outcome of resolving one candidate's official URL.

    Immutable with respect to the candidate: the caller decides whether to
    apply :attr:`website`. Nothing here is a verification claim.
    """

    candidate_id: str
    name: str
    detail_url: str | None
    #: The resolved official URL, or ``None`` when nothing was published.
    website: str | None = None
    website_domain: str | None = None
    basis: str | None = None
    #: Product name the directory attached to the resolved URL.
    declared_name: str | None = None
    evidence: str | None = None
    failure: str | None = None
    needs_review: bool = False
    review_notes: list[str] = field(default_factory=list)
    #: True when a real HTTP request was attempted for this candidate.
    fetched: bool = False
    live: bool = False
    page_evidence: OfficialUrlEvidence | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.website)

    def add_note(self, note: str, *, review: bool = False) -> None:
        if note and note not in self.review_notes:
            self.review_notes.append(note)
        if review:
            self.needs_review = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "detail_url": self.detail_url,
            "website": self.website,
            "website_domain": self.website_domain,
            "basis": self.basis,
            "declared_name": self.declared_name,
            "evidence": self.evidence,
            "failure": self.failure,
            "resolved": self.resolved,
            "needs_review": self.needs_review,
            "review_notes": list(self.review_notes),
            "fetched": self.fetched,
            "live": self.live,
            "page_evidence": self.page_evidence.to_dict() if self.page_evidence else None,
        }


@dataclass
class ResolutionReport:
    """Auditable summary of one resolution pass."""

    considered: int = 0
    already_had_website: int = 0
    attempted: int = 0
    fetched: int = 0
    resolved: int = 0
    needs_review: int = 0
    live: bool = False
    failures: dict[str, int] = field(default_factory=dict)
    bases: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def record(self, result: ResolutionResult) -> None:
        self.considered += 1
        if result.fetched:
            self.fetched += 1
        if result.resolved:
            self.resolved += 1
            if result.basis:
                self.bases[result.basis] = self.bases.get(result.basis, 0) + 1
        if result.needs_review:
            self.needs_review += 1
        if result.failure:
            self.failures[result.failure] = self.failures.get(result.failure, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "already_had_website": self.already_had_website,
            "attempted": self.attempted,
            "fetched": self.fetched,
            "resolved": self.resolved,
            "needs_review": self.needs_review,
            "live": self.live,
            "failures": dict(sorted(self.failures.items())),
            "bases": dict(sorted(self.bases.items())),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# pure extraction
# --------------------------------------------------------------------------- #
def _json_ld_nodes(soup: Any) -> list[Mapping[str, Any]]:
    """Every JSON-LD object on the page, ``@graph`` containers flattened."""
    nodes: list[Mapping[str, Any]] = []
    try:
        scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    except Exception:  # noqa: BLE001 - malformed soup must not kill the run
        return nodes
    for script in scripts:
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - a broken block is skipped, not fatal
            continue
        stack: list[Any] = [payload]
        seen = 0
        while stack and seen < 200:
            item = stack.pop()
            seen += 1
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, Mapping):
                nodes.append(item)
                graph = item.get("@graph")
                if isinstance(graph, (list, Mapping)):
                    stack.append(graph)
    return nodes


def _is_product_node(node: Mapping[str, Any]) -> bool:
    raw_type = node.get("@type")
    types = raw_type if isinstance(raw_type, (list, tuple)) else [raw_type]
    for value in types:
        if isinstance(value, str) and value.strip().lower() in _PRODUCT_TYPES:
            return True
    return False


def _node_urls(node: Mapping[str, Any]) -> list[str]:
    """``url`` then ``sameAs`` values of a JSON-LD node, in that order."""
    out: list[str] = []
    for key in ("url", "sameAs"):
        value = node.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, Mapping):
                nested = item.get("url") or item.get("@id")
                if isinstance(nested, str) and nested.strip():
                    out.append(nested.strip())
    return out


def _anchor_is_outbound_button(anchor: Any) -> str | None:
    """Return the matched justification when ``anchor`` is a "visit" button."""
    haystacks: list[str] = []
    label = best_label(anchor, max_length=120)
    if label:
        haystacks.append(label)
    for attr in ("title", "aria-label"):
        try:
            value = anchor.get(attr)
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, str) and value.strip():
            haystacks.append(value.strip())
    for text in haystacks:
        lowered = _WS_RE.sub(" ", text).strip().lower()
        for pattern in _VISIT_LABEL_PATTERNS:
            if pattern in lowered:
                return f"anchor label {text[:80]!r} matched {pattern!r}"

    # A directory that marks the link as leaving the site is also an explicit
    # declaration, even when the button is an image with no text.
    try:
        rel = anchor.get("rel")
    except Exception:  # noqa: BLE001
        rel = None
    tokens = set()
    if isinstance(rel, str):
        tokens = {t.strip().lower() for t in rel.split()}
    elif isinstance(rel, (list, tuple)):
        tokens = {str(t).strip().lower() for t in rel}
    matched = tokens & _OUTBOUND_REL_TOKENS
    if "external" in matched:
        return 'anchor marked rel="external"'
    return None


def _classify_host(url: str, directory_domain: str | None) -> str | None:
    """Return a rejection reason for ``url``, or ``None`` when acceptable."""
    domain = extract_registrable_domain(url)
    if not domain:
        return "no_registrable_domain"
    if directory_domain and domain == directory_domain:
        return "directory_self_link"
    if domain in _NEVER_OFFICIAL:
        return "social_or_platform_host"
    if domain in SHARED_HOST_DOMAINS:
        return "shared_host"
    if is_asset_url(url):
        return "asset_url"
    host = urlsplit(url).netloc.lower()
    if not host or host.count(".") == 0:
        return "not_a_public_host"
    return None


def extract_official_url_evidence(
    detail_url: str,
    result: FetchResult | None,
    *,
    product_name: str | None = None,
) -> OfficialUrlEvidence:
    """Read every official-URL claim a directory detail page publishes.

    Pure: no network, no clock, no global state. ``result`` is whatever the
    HTTP client returned (``None`` when the fetch degraded gracefully), so the
    same page always produces the same evidence.

    ``product_name`` is used only to *order* equally-ranked candidates by
    name agreement; it never invents a URL.
    """
    evidence = OfficialUrlEvidence(detail_url=detail_url)
    evidence.directory_domain = extract_registrable_domain(detail_url)

    if result is None:
        evidence.failure = ResolutionFailure.FETCH_FAILED
        evidence.notes.append(
            "the directory detail page could not be fetched (timeout, DNS or "
            "retries exhausted); no URL claim could be read"
        )
        return evidence

    evidence.status = result.status
    evidence.final_url = result.final_url or result.url
    headers = result.headers if isinstance(result.headers, Mapping) else {}
    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
    evidence.content_type = content_type or None
    body = result.text if isinstance(result.text, str) else ""

    if looks_like_challenge(body, result.status):
        evidence.failure = ResolutionFailure.BOT_CHALLENGE
        evidence.notes.append(
            "an anti-bot interstitial was served instead of the detail page; "
            "challenge markup is never read as a product URL"
        )
        return evidence

    if not result.ok:
        evidence.failure = ResolutionFailure.HTTP_ERROR
        evidence.notes.append(f"detail page returned HTTP {result.status}")
        return evidence

    if content_type and not any(t in content_type.lower() for t in _HTML_CONTENT_TYPES):
        evidence.failure = ResolutionFailure.NON_HTML_RESPONSE
        evidence.notes.append(f"detail page content type is {content_type!r}, not HTML")
        return evidence

    if not body.strip():
        evidence.failure = ResolutionFailure.EMPTY_RESPONSE
        evidence.notes.append("detail page body was empty")
        return evidence

    soup = parse_html(body)
    if soup is None:
        evidence.failure = ResolutionFailure.UNPARSEABLE_HTML
        evidence.notes.append("detail page body could not be parsed as HTML")
        return evidence

    base = evidence.final_url or detail_url
    found: list[UrlCandidate] = []
    seen_urls: set[tuple[str, str]] = set()

    def _consider(raw: str, basis: str, declared: str | None, why: str) -> None:
        absolute = absolutize(raw, base)
        if not absolute:
            absolute = normalize_url(raw)
        if not absolute:
            evidence.rejected.append({"url": str(raw)[:200], "reason": "unusable_url"})
            return
        reason = _classify_host(absolute, evidence.directory_domain)
        if reason:
            evidence.rejected.append({"url": absolute, "reason": reason})
            return
        key = (absolute, basis)
        if key in seen_urls:
            return
        seen_urls.add(key)
        found.append(
            UrlCandidate(
                url=absolute,
                basis=basis,
                domain=extract_registrable_domain(absolute),
                declared_name=clean_text(declared) if declared else None,
                evidence=why,
            )
        )

    # -- strongest first: the directory's own structured declaration --------
    for node in _json_ld_nodes(soup):
        if not _is_product_node(node):
            continue
        declared = node.get("name")
        declared = declared if isinstance(declared, str) else None
        for raw in _node_urls(node):
            _consider(
                raw,
                ResolutionBasis.JSON_LD,
                declared,
                f"schema.org {node.get('@type')!r} node url",
            )

    # -- then the explicit outbound button ---------------------------------
    try:
        anchors = anchor_targets(soup, base)
    except Exception:  # noqa: BLE001
        anchors = []
    for anchor, url in anchors:
        # Skip the directory's own navigation before doing any label work: a
        # "Visit" link back into the directory is not a product pointer.
        domain = extract_registrable_domain(url)
        if domain and evidence.directory_domain and domain == evidence.directory_domain:
            continue
        why = _anchor_is_outbound_button(anchor)
        if not why:
            continue
        _consider(url, ResolutionBasis.VISIT_LINK, None, why)

    if not found:
        # Distinguish "nothing outbound at all" from "everything outbound was a
        # platform/social host" — those are different data problems.
        if evidence.rejected and all(
            r.get("reason") in {"shared_host", "social_or_platform_host", "directory_self_link"}
            for r in evidence.rejected
        ):
            evidence.failure = ResolutionFailure.ONLY_SHARED_HOSTS
            evidence.notes.append(
                "the page's only outbound links were shared hosts or social "
                "profiles, which are never a product's own website"
            )
        else:
            evidence.failure = ResolutionFailure.NO_OUTBOUND_URL
            evidence.notes.append(
                "the detail page publishes no outbound product URL; the website "
                "stays blank rather than being guessed from the slug"
            )
        return evidence

    wanted = canonical_name(product_name) if product_name else None

    def _sort_key(item: UrlCandidate) -> tuple[int, int, int]:
        declared = canonical_name(item.declared_name) if item.declared_name else None
        agrees = 0
        if wanted and declared:
            agrees = 1 if (declared == wanted or _names_overlap(declared, wanted)) else 0
        # Highest rank first, then name agreement, then stability by insertion.
        return (-item.rank, -agrees, found.index(item))

    evidence.url_candidates = sorted(found, key=_sort_key)
    return evidence


def _names_overlap(a: str | None, b: str | None) -> bool:
    """Conservative containment check between two canonical names."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    # Require a meaningful stem so "ai" does not match everything.
    if len(shorter) >= 4 and shorter in longer:
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta and tb and (ta & tb))


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
class OfficialUrlResolver:
    """Fetch directory detail pages and attach the URLs they publish.

    Only the candidate's *own* detail URL is fetched, and only when the
    candidate has no website yet. A candidate that already carries one is left
    completely untouched — discovery's observation is never overwritten.
    """

    def __init__(self, client: HttpClient | Any | None = None) -> None:
        self.client = client or HttpClient()

    # ------------------------------------------------------------- one record
    def resolve_candidate(self, candidate: Any, *, live: bool = False) -> ResolutionResult:
        """Resolve one prepared candidate's official URL.

        Never mutates ``candidate``; the caller applies the result explicitly.
        """
        name = str(_safe_attr(candidate, "name", "") or "") or "<unnamed>"
        candidate_id = str(_safe_attr(candidate, "candidate_id", "") or "")
        detail_url = _safe_attr(candidate, "listing_url")
        result = ResolutionResult(
            candidate_id=candidate_id,
            name=name,
            detail_url=detail_url,
            live=live,
        )

        existing = _safe_attr(candidate, "website")
        if existing:
            result.failure = ResolutionFailure.ALREADY_RESOLVED
            result.add_note(
                "candidate already carries an official website from discovery; "
                "resolution skipped so the observed value is never overwritten"
            )
            return result

        if not detail_url or not isinstance(detail_url, str):
            result.failure = ResolutionFailure.NO_DETAIL_URL
            result.add_note(
                "no directory detail URL to read, so no official URL can be "
                "published for this candidate",
                review=True,
            )
            return result

        try:
            fetched = self.client.try_fetch(detail_url)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop a pass
            logger.warning(
                "detail-page fetch raised; candidate left unresolved",
                extra={"url": detail_url, "error": str(exc)[:200]},
            )
            fetched = None
        result.fetched = True

        try:
            evidence = extract_official_url_evidence(
                detail_url, fetched, product_name=name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "official-URL extraction raised; candidate left unresolved",
                extra={"url": detail_url, "error": str(exc)[:200]},
            )
            result.failure = ResolutionFailure.RESOLUTION_FAILED
            result.add_note(f"resolution raised: {str(exc)[:200]}", review=True)
            return result

        result.page_evidence = evidence
        return self._decide(result, evidence, name)

    # ---------------------------------------------------------------- policy
    def _decide(
        self,
        result: ResolutionResult,
        evidence: OfficialUrlEvidence,
        name: str,
    ) -> ResolutionResult:
        """Apply the acceptance rules. One readable place for the policy."""
        if not evidence.has_candidate:
            result.failure = evidence.failure or ResolutionFailure.NO_OUTBOUND_URL
            for note in evidence.notes:
                result.add_note(note)
            return result

        best = evidence.url_candidates[0]

        # Identity cross-check: only possible when the page declared a name.
        wanted = canonical_name(name)
        declared = canonical_name(best.declared_name) if best.declared_name else None
        if wanted and declared and not _names_overlap(declared, wanted):
            result.failure = ResolutionFailure.IDENTITY_MISMATCH
            result.add_note(
                f"the detail page publishes {best.url} for {best.declared_name!r}, "
                f"which does not agree with the candidate name {name!r}; the URL "
                "is refused rather than attached to the wrong product",
                review=True,
            )
            return result

        result.website = best.url
        result.website_domain = best.domain
        result.basis = best.basis
        result.declared_name = best.declared_name
        result.evidence = best.evidence

        if declared and wanted and declared == wanted:
            pass  # exact agreement: nothing to flag
        elif not declared:
            result.add_note(
                "the directory published this URL without naming the product, so "
                "identity could not be cross-checked here; official-site "
                "verification still has to confirm it"
            )

        # A page publishing two different domains is a real ambiguity.
        domains = {c.domain for c in evidence.url_candidates if c.domain}
        if len(domains) > 1:
            others = sorted(d for d in domains if d != best.domain)
            result.add_note(
                f"the detail page published more than one off-site domain "
                f"({', '.join(others)}); the strongest evidence "
                f"({best.basis}) was used and the record is flagged for review",
                review=True,
            )
        return result

    # ------------------------------------------------------------ many records
    def resolve_many(
        self,
        candidates: Sequence[Any],
        *,
        live: bool = False,
        limit: int | None = None,
    ) -> tuple[list[ResolutionResult], ResolutionReport]:
        """Resolve a batch, skipping candidates that already have a website.

        ``limit`` caps how many candidates are *attempted* (i.e. fetched), so a
        pass can always be kept to a deliberate spot check rather than a bulk
        crawl.
        """
        report = ResolutionReport(live=live)
        results: list[ResolutionResult] = []
        attempted = 0

        for candidate in candidates:
            if _safe_attr(candidate, "website"):
                report.already_had_website += 1
                continue
            if limit is not None and attempted >= max(0, limit):
                break
            attempted += 1
            try:
                result = self.resolve_candidate(candidate, live=live)
            except Exception as exc:  # noqa: BLE001 - batch resilience
                label = _safe_attr(candidate, "name", "<unknown>") or "<unknown>"
                message = f"{label}: {str(exc)[:200]}"
                report.errors.append(message)
                logger.warning("resolution failed for one candidate", extra={"ctx": message})
                result = ResolutionResult(
                    candidate_id=str(_safe_attr(candidate, "candidate_id", "") or ""),
                    name=str(_safe_attr(candidate, "name", "") or "<unnamed>"),
                    detail_url=_safe_attr(candidate, "listing_url"),
                    failure=ResolutionFailure.RESOLUTION_FAILED,
                    live=live,
                )
                result.add_note(f"resolution raised: {str(exc)[:200]}", review=True)
            results.append(result)
            report.record(result)

        report.attempted = attempted
        return results, report


def apply_resolution(candidate: Any, result: ResolutionResult) -> bool:
    """Write a resolved URL onto ``candidate``, returning whether it changed.

    Kept separate from resolution on purpose: reading the directory's claim and
    deciding to trust it enough to store it are two different actions, and only
    this one mutates a record. The candidate stays ``unverified`` either way —
    a directory claim is not verification.
    """
    if not result.resolved or _safe_attr(candidate, "website"):
        return False
    candidate.website = result.website
    try:
        candidate.website_domain = result.website_domain
    except Exception:  # noqa: BLE001 - optional attribute on foreign records
        pass
    adder = _safe_attr(candidate, "add_issue")
    if callable(adder):
        adder(
            "official_url_resolved_from_directory",
            (
                f"official URL {result.website} was read from the directory "
                f"detail page ({result.basis}); it is a directory claim and "
                "still has to be verified against the product's own site"
            ),
            review=result.needs_review,
        )
    return True
