"""Official-website verification.

Guideline §10: after discovery, open the **official product website** and verify
that the product really exists, is accessible and is the product the directory
claimed it was. **If information cannot be verified, leave the field blank — do
not guess.**

Why this stage exists
---------------------
Discovery produces *sightings*. A TAAFT/Creati listing — or any other
directory row — is a pointer, never proof: the listing can be stale, wrong,
SEO-generated or point at a parked domain. Verification is therefore the only
stage allowed to promote a candidate out of ``unverified``, and it may only do
so from evidence **actually present on the fetched official page**.

Module layout
-------------
* :func:`extract_official_evidence` — *pure* function: turns one
  :class:`~src.core.http_client.FetchResult` into an
  :class:`OfficialPageEvidence` record (identity signals, product-existence
  signals, dead/challenge/non-HTML detection). No network, fully testable.
* :class:`LivenessChecker` — "is the official site reachable and real?", a thin
  policy view over the same evidence engine.
* :class:`OfficialSiteVerifier` — orchestration:
  :meth:`~OfficialSiteVerifier.verify_candidate` for
  :class:`~src.candidates.prepare.PreparedCandidate` records (returns an
  immutable :class:`VerificationResult`, leaving discovery provenance
  untouched) and :meth:`~OfficialSiteVerifier.verify` for
  :class:`~src.models.tool.Tool` records (records the outcome in
  :class:`~src.models.tool.VerificationRecord`).
* :func:`resolve_conflict` — the official-source-wins conflict rule.

Hard rules encoded here
-----------------------
* the official URL is the *only* URL that is fetched — a directory/listing URL
  is never fetched as a substitute and never counts as verification;
* no official URL → nothing is fetched at all;
* a bot challenge, a non-HTML body, an empty body or a thin body means
  "could not verify" (``unverified`` + review), never "verified";
* identity must be confirmed from a page-level brand slot before any
  ``verified``/``partially_verified`` status is set;
* launch date, pricing, features, usage, company, integrations and
  capabilities are **never** inferred here; only injected extractors may set
  fields, and only from the fetched official page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from src.core.http_client import FetchResult, HttpClient
from src.core.logging_setup import get_logger
from src.core.text import canonical_name, clean_text, name_tokens
from src.core.urls import extract_registrable_domain, normalize_url, same_site
from src.extraction.html import looks_like_challenge, node_text, parse_html
from src.models.base import SourceRef
from src.models.enums import RejectionReason, ToolStatus, VerificationStatus
from src.models.tool import Tool

logger = get_logger("verification")

__all__ = [
    "DEAD_SITE_MARKERS",
    "STATUS_MARKERS",
    "MIN_CONTENT_CHARS",
    "VerificationFailure",
    "OfficialPageEvidence",
    "VerificationResult",
    "VerificationReport",
    "extract_official_evidence",
    "LivenessResult",
    "LivenessChecker",
    "OfficialSiteVerifier",
    "resolve_conflict",
]

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

#: Content types we can extract page evidence from.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml")

#: Product-existence signals, as ``(signal, url/text fragments)``.
#: Each fragment must appear in a link's path or its label — so the signal is
#: only ever recorded when the official page really offers that affordance.
_PRODUCT_SIGNAL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pricing_link", ("pricing", "plans", "/price", "subscription")),
    ("signup_link", ("sign-up", "signup", "register", "create-account",
                     "get-started", "start-free", "start-for-free")),
    ("login_link", ("login", "log-in", "sign-in", "signin")),
    ("docs_link", ("docs", "documentation", "/api", "developers", "developer")),
    ("download_link", ("download", "install", "add-to-chrome")),
    ("demo_link", ("demo", "playground", "try-it", "try-for-free", "free-trial")),
    ("app_link", ("/app", "dashboard", "console", "/studio", "workspace")),
)

#: Page slots a product is expected to brand itself in (identity evidence).
_BRAND_SLOTS = ("title", "og:title", "og:site_name", "twitter:title", "h1", "logo_alt")


class VerificationFailure:
    """Stable reason codes for *why* verification did not succeed.

    Codes are part of the artefact contract (they are persisted and counted),
    so they must stay stable even when the surrounding heuristics change.
    """

    NO_OFFICIAL_URL = "no_official_url"
    INVALID_OFFICIAL_URL = "invalid_official_url"
    FETCH_FAILED = "fetch_failed"
    HTTP_ERROR = "http_error"
    BOT_CHALLENGE = "bot_challenge"
    NON_HTML_RESPONSE = "non_html_response"
    EMPTY_RESPONSE = "empty_response"
    UNPARSEABLE_HTML = "unparseable_html"
    THIN_CONTENT = "thin_content"
    DEAD_SITE_MARKER = "dead_site_marker"
    IDENTITY_NOT_CONFIRMED = "identity_not_confirmed"
    OFF_DOMAIN_REDIRECT = "off_domain_redirect"
    NO_PRODUCT_SIGNAL = "no_product_signal"


# =========================================================== page evidence
@dataclass
class OfficialPageEvidence:
    """Everything verification learned from **one fetch of the official URL**.

    Every field is either transport metadata (status, final URL, content type)
    or a value literally read off the fetched page. Nothing is inferred from a
    directory listing, and nothing is defaulted to a plausible value.
    """

    official_url: str
    checked_at: datetime
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    fetched: bool = False
    is_html: bool = False
    is_challenge: bool = False
    text_length: int = 0
    title: str | None = None
    site_name: str | None = None
    meta_description: str | None = None
    headings: list[str] = field(default_factory=list)
    identity_signals: list[str] = field(default_factory=list)
    product_signals: list[str] = field(default_factory=list)
    dead_markers: list[str] = field(default_factory=list)
    detected_status: ToolStatus | None = None
    redirected: bool = False
    redirected_off_domain: bool = False
    failures: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ predicates
    @property
    def http_ok(self) -> bool:
        return self.http_status is not None and 200 <= self.http_status < 300

    @property
    def is_accessible(self) -> bool:
        """Reachable, HTML, not a challenge page and not visibly dead."""
        return (
            self.fetched
            and self.http_ok
            and self.is_html
            and not self.is_challenge
            and not self.dead_markers
            and self.text_length >= MIN_CONTENT_CHARS
        )

    @property
    def identity_confirmed(self) -> bool:
        """True only when the product brands itself on the fetched page."""
        return any(signal.startswith("name_in_") for signal in self.identity_signals)

    @property
    def has_product_signal(self) -> bool:
        return bool(self.product_signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "official_url": self.official_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "checked_at": self.checked_at.isoformat(),
            "fetched": self.fetched,
            "is_html": self.is_html,
            "is_challenge": self.is_challenge,
            "text_length": self.text_length,
            "title": self.title,
            "site_name": self.site_name,
            "meta_description": self.meta_description,
            "headings": list(self.headings),
            "identity_signals": list(self.identity_signals),
            "product_signals": list(self.product_signals),
            "dead_markers": list(self.dead_markers),
            "detected_status": str(self.detected_status) if self.detected_status else None,
            "redirected": self.redirected,
            "redirected_off_domain": self.redirected_off_domain,
            "failures": list(self.failures),
        }


def extract_official_evidence(
    official_url: str | None,
    result: FetchResult | None,
    *,
    product_name: str | None = None,
    now: datetime | None = None,
) -> OfficialPageEvidence:
    """Turn one official-site fetch into auditable evidence. Pure/offline.

    ``result is None`` models the HTTP client's graceful-degradation contract
    (timeout / connection error / retries exhausted) and yields
    :attr:`VerificationFailure.FETCH_FAILED` — *not* a rejection of the
    product, because nothing about the product was actually learned.
    """
    checked_at = now or _utcnow()
    url = normalize_url(official_url) if official_url else None
    evidence = OfficialPageEvidence(
        official_url=url or (official_url or ""),
        checked_at=checked_at,
    )

    if not url:
        evidence.failures.append(
            VerificationFailure.INVALID_OFFICIAL_URL
            if official_url
            else VerificationFailure.NO_OFFICIAL_URL
        )
        return evidence

    if result is None:
        evidence.failures.append(VerificationFailure.FETCH_FAILED)
        return evidence

    evidence.fetched = True
    evidence.http_status = result.status
    evidence.final_url = normalize_url(result.final_url) if result.final_url else None
    evidence.content_type = (result.headers or {}).get("content-type")
    evidence.redirected = bool(evidence.final_url and evidence.final_url != url)
    evidence.redirected_off_domain = bool(
        evidence.final_url and not same_site(url, evidence.final_url)
    )
    if evidence.redirected_off_domain:
        evidence.failures.append(VerificationFailure.OFF_DOMAIN_REDIRECT)

    body = result.text or ""

    # A bot challenge / WAF interstitial is *not* product content. Detect it
    # before anything else so its markup can never become "evidence".
    if looks_like_challenge(body, result.status):
        evidence.is_challenge = True
        evidence.failures.append(VerificationFailure.BOT_CHALLENGE)
        return evidence

    if not evidence.http_ok:
        evidence.failures.append(VerificationFailure.HTTP_ERROR)
        evidence.detected_status = ToolStatus.INACCESSIBLE
        return evidence

    if not body.strip():
        evidence.failures.append(VerificationFailure.EMPTY_RESPONSE)
        return evidence

    evidence.is_html = _is_html(evidence.content_type, body)
    if not evidence.is_html:
        evidence.failures.append(VerificationFailure.NON_HTML_RESPONSE)
        return evidence

    soup = parse_html(body)
    if soup is None:
        evidence.failures.append(VerificationFailure.UNPARSEABLE_HTML)
        return evidence

    text = clean_text(soup.get_text(" ", strip=True)) or ""
    lowered_text = text.casefold()
    evidence.text_length = len(text)

    evidence.title = clean_text(node_text(soup.title), max_length=300)
    evidence.site_name = _meta_content(soup, ("og:site_name",))
    evidence.meta_description = clean_text(
        _meta_content(soup, ("description", "og:description")), max_length=400
    )
    evidence.headings = _headings(soup)

    for marker in DEAD_SITE_MARKERS:
        if marker in lowered_text:
            evidence.dead_markers.append(marker)
            evidence.detected_status = (
                ToolStatus.DISCONTINUED if "shut" in marker else ToolStatus.INACCESSIBLE
            )
            break
    if evidence.dead_markers:
        evidence.failures.append(VerificationFailure.DEAD_SITE_MARKER)

    if evidence.text_length < MIN_CONTENT_CHARS:
        evidence.failures.append(VerificationFailure.THIN_CONTENT)

    base_url = evidence.final_url or url
    evidence.product_signals = _product_signals(soup, base_url)
    if not evidence.product_signals:
        evidence.failures.append(VerificationFailure.NO_PRODUCT_SIGNAL)

    evidence.identity_signals = _identity_signals(
        soup,
        product_name=product_name,
        official_url=url,
        final_url=evidence.final_url,
        title=evidence.title,
        site_name=evidence.site_name,
        meta_description=evidence.meta_description,
        headings=evidence.headings,
        page_text=lowered_text,
    )
    if not evidence.identity_confirmed:
        evidence.failures.append(VerificationFailure.IDENTITY_NOT_CONFIRMED)

    if evidence.detected_status is None and not evidence.dead_markers:
        for marker, status in STATUS_MARKERS:
            if marker in lowered_text:
                evidence.detected_status = status
                break
        else:
            if evidence.is_accessible:
                evidence.detected_status = ToolStatus.ACTIVE

    return evidence


# -------------------------------------------------------- evidence helpers
def _is_html(content_type: str | None, body: str) -> bool:
    """HTML per ``Content-Type``; falls back to sniffing when the header lies."""
    if content_type:
        lowered = content_type.lower()
        if any(kind in lowered for kind in _HTML_CONTENT_TYPES):
            return True
        # An explicit non-HTML content type is authoritative (PDF, JSON, image).
        return False
    head = body[:2000].lstrip().lower()
    return head.startswith("<!doctype html") or "<html" in head


def _meta_content(soup: Any, keys: Sequence[str]) -> str | None:
    """First non-empty ``<meta>`` value for any of ``keys`` (name or property)."""
    for key in keys:
        for attr in ("property", "name"):
            try:
                node = soup.find("meta", attrs={attr: key})
            except Exception:  # noqa: BLE001 - malformed markup must not abort
                node = None
            if node is None:
                continue
            value = clean_text(node.get("content"))
            if value:
                return value
    return None


def _headings(soup: Any, *, limit: int = 8) -> list[str]:
    """Visible ``h1``/``h2`` text, in document order (evidence, not analysis)."""
    out: list[str] = []
    for tag in ("h1", "h2"):
        try:
            nodes = soup.find_all(tag)
        except Exception:  # noqa: BLE001
            nodes = []
        for node in nodes:
            value = node_text(node, max_length=200)
            if value and value not in out:
                out.append(value)
            if len(out) >= limit:
                return out
    return out


def _product_signals(soup: Any, base_url: str | None) -> list[str]:
    """Affordances that show the page fronts a usable product.

    Only *same-site* links count: a link to the vendor's Twitter page says
    nothing about the product existing, while ``/pricing``, ``/signup`` or
    ``app.<domain>`` do.
    """
    found: list[str] = []
    domain = extract_registrable_domain(base_url)

    try:
        anchors = soup.find_all("a", href=True)
    except Exception:  # noqa: BLE001
        anchors = []

    haystacks: list[str] = []
    for anchor in anchors:
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        parts = _safe_split(href)
        if parts is None:
            continue
        if parts.netloc:
            link_domain = extract_registrable_domain(href)
            if domain and link_domain and link_domain != domain:
                continue  # off-site link: not evidence about this product
        label = (node_text(anchor) or "").casefold().replace(" ", "-")
        path = f"{parts.netloc}{parts.path}".casefold()
        haystacks.append(f"{path} {label}")

    for signal, fragments in _PRODUCT_SIGNAL_PATTERNS:
        if any(fragment in haystack for haystack in haystacks for fragment in fragments):
            found.append(signal)

    if _has_credential_form(soup):
        found.append("signup_or_login_form")
    return found


def _has_credential_form(soup: Any) -> bool:
    """True when the page hosts an email/password form (a real entry point)."""
    try:
        inputs = soup.find_all("input")
    except Exception:  # noqa: BLE001
        return False
    for node in inputs:
        kind = str(node.get("type") or "").strip().lower()
        if kind in ("email", "password"):
            return True
    return False


def _identity_signals(
    soup: Any,
    *,
    product_name: str | None,
    official_url: str,
    final_url: str | None,
    title: str | None,
    site_name: str | None,
    meta_description: str | None,
    headings: Sequence[str],
    page_text: str,
) -> list[str]:
    """Where (if anywhere) the fetched page brands itself with ``product_name``.

    ``name_in_*`` signals are page-level brand slots and are the *only* thing
    that can confirm identity. ``domain_matches_name`` and ``name_in_body_text``
    are recorded as supporting context; on their own they are not enough, and
    an unconfirmed identity keeps the record unverified rather than guessing.
    """
    signals: list[str] = []
    cleaned_name = clean_text(product_name)
    if not cleaned_name:
        return signals

    slots: list[tuple[str, str | None]] = [
        ("title", title),
        ("site_name", site_name),
        ("og_title", _meta_content(soup, ("og:title", "twitter:title"))),
        ("main_heading", headings[0] if headings else None),
        ("logo_alt", _logo_alt(soup)),
        ("meta_description", meta_description),
    ]
    for slot, value in slots:
        if value and _name_matches(cleaned_name, value):
            signals.append(f"name_in_{slot}")

    if cleaned_name.casefold() in page_text:
        signals.append("name_in_body_text")

    if _domain_matches_name(cleaned_name, final_url or official_url):
        signals.append("domain_matches_name")

    # A domain that carries the product name plus the name in the body text is
    # treated as a brand slot: the site is literally named after the product.
    if (
        not any(s.startswith("name_in_") and s != "name_in_body_text" for s in signals)
        and "domain_matches_name" in signals
        and "name_in_body_text" in signals
    ):
        signals.append("name_in_official_domain")
    return signals


def _logo_alt(soup: Any) -> str | None:
    """``alt`` text of the first image that looks like the site logo."""
    try:
        images = soup.find_all("img", limit=25)
    except Exception:  # noqa: BLE001
        return None
    for node in images:
        alt = clean_text(node.get("alt"))
        if not alt:
            continue
        haystack = " ".join(
            str(node.get(attr) or "")
            for attr in ("alt", "class", "id", "src")
        ).casefold()
        if "logo" in haystack or "brand" in haystack:
            return alt
    return None


def _name_matches(product_name: str, value: str) -> bool:
    """Conservative brand comparison on canonicalized names/tokens."""
    needle = canonical_name(product_name)
    haystack = canonical_name(value, drop_noise=False)
    if not needle or not haystack:
        return False
    if len(needle) >= 4:
        return needle in haystack
    # Very short names ("Hume", "Pi") would match almost anything by substring,
    # so require a whole-token hit instead.
    return needle in {canonical_name(token) for token in name_tokens(value)}


def _domain_matches_name(product_name: str, url: str | None) -> bool:
    """True when the registrable domain is named after the product."""
    domain = extract_registrable_domain(url)
    if not domain:
        return False
    label = domain.split(".")[0]
    needle, haystack = canonical_name(product_name), canonical_name(label)
    if not needle or not haystack:
        return False
    return needle == haystack or (len(needle) >= 4 and needle in haystack)


def _safe_split(href: str) -> Any:
    try:
        return urlsplit(href)
    except ValueError:
        return None


# ========================================================== liveness view
@dataclass
class LivenessResult:
    """Outcome of an accessibility probe (policy view over the evidence)."""

    url: str
    is_accessible: bool
    http_status: int | None = None
    final_url: str | None = None
    redirected_off_domain: bool = False
    detected_status: ToolStatus | None = None
    notes: list[str] = field(default_factory=list)
    content_length: int = 0
    is_challenge: bool = False
    is_html: bool = False
    failures: list[str] = field(default_factory=list)
    evidence: OfficialPageEvidence | None = None

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

    def check(
        self,
        url: str | None,
        *,
        product_name: str | None = None,
        now: datetime | None = None,
    ) -> LivenessResult | None:
        """Probe ``url``; returns ``None`` when there is no URL to probe."""
        normalized = normalize_url(url)
        if not normalized:
            return None

        result = self.client.try_fetch(normalized, use_cache=True)
        return self.evaluate(normalized, result, product_name=product_name, now=now)

    def evaluate(
        self,
        url: str,
        result: FetchResult | None,
        *,
        product_name: str | None = None,
        now: datetime | None = None,
    ) -> LivenessResult:
        """Pure evaluation of a fetched response (unit-testable, no network)."""
        evidence = extract_official_evidence(
            url, result, product_name=product_name, now=now
        )
        return self.from_evidence(evidence)

    @staticmethod
    def from_evidence(evidence: OfficialPageEvidence) -> LivenessResult:
        return LivenessResult(
            url=evidence.official_url,
            is_accessible=evidence.is_accessible,
            http_status=evidence.http_status,
            final_url=evidence.final_url,
            redirected_off_domain=evidence.redirected_off_domain,
            detected_status=evidence.detected_status,
            notes=_evidence_notes(evidence),
            content_length=evidence.text_length,
            is_challenge=evidence.is_challenge,
            is_html=evidence.is_html,
            failures=list(evidence.failures),
            evidence=evidence,
        )


#: Human-readable note per failure code — used for the audit trail.
_FAILURE_NOTES: dict[str, str] = {
    VerificationFailure.NO_OFFICIAL_URL: "no official website URL to verify",
    VerificationFailure.INVALID_OFFICIAL_URL: "official website URL is not resolvable",
    VerificationFailure.FETCH_FAILED: "official website could not be fetched (timeout/network)",
    VerificationFailure.HTTP_ERROR: "official website returned an error status",
    VerificationFailure.BOT_CHALLENGE: "official website served a bot/WAF challenge page",
    VerificationFailure.NON_HTML_RESPONSE: "official URL did not return an HTML page",
    VerificationFailure.EMPTY_RESPONSE: "official website returned an empty body",
    VerificationFailure.UNPARSEABLE_HTML: "official page markup could not be parsed",
    VerificationFailure.THIN_CONTENT: "official page has too little content to verify a product",
    VerificationFailure.DEAD_SITE_MARKER: "official page shows a dead/parked/shut-down marker",
    VerificationFailure.IDENTITY_NOT_CONFIRMED: (
        "product name was not found in any brand slot on the official page"
    ),
    VerificationFailure.OFF_DOMAIN_REDIRECT: (
        "official URL redirects to another domain — confirm rebrand/acquisition"
    ),
    VerificationFailure.NO_PRODUCT_SIGNAL: (
        "no product affordance (pricing/signup/login/docs/app) found on the official page"
    ),
}


def _evidence_notes(evidence: OfficialPageEvidence) -> list[str]:
    """Concise, ordered, human-readable notes describing the evidence."""
    notes: list[str] = []
    if evidence.http_status is not None:
        notes.append(f"HTTP {evidence.http_status}")
    if evidence.redirected and evidence.final_url:
        notes.append(f"redirected to {evidence.final_url}")
    for code in evidence.failures:
        note = _FAILURE_NOTES.get(code, code)
        if code == VerificationFailure.DEAD_SITE_MARKER and evidence.dead_markers:
            note = f"{note}: '{evidence.dead_markers[0]}'"
        if code == VerificationFailure.THIN_CONTENT:
            note = f"{note} ({evidence.text_length} chars)"
        notes.append(note)
    if evidence.identity_signals:
        notes.append("identity evidence: " + ", ".join(evidence.identity_signals))
    if evidence.product_signals:
        notes.append("product evidence: " + ", ".join(evidence.product_signals))
    return notes


# ======================================================= candidate results
@dataclass
class VerificationResult:
    """Verification outcome for one prepared candidate.

    Deliberately a *separate* record from the candidate: discovery provenance
    (which directory saw it) and official verification evidence (what the
    product's own site proves) must never be mixed up.
    """

    candidate_id: str | None
    name: str | None
    status: VerificationStatus
    official_url: str | None
    final_url: str | None = None
    http_status: int | None = None
    checked_at: str | None = None
    fetched: bool = False
    content_type: str | None = None
    identity_confirmed: bool = False
    product_signals: list[str] = field(default_factory=list)
    identity_signals: list[str] = field(default_factory=list)
    evidence_notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reason: str | None = None
    needs_review: bool = False
    detected_status: str | None = None
    verification_source: SourceRef | None = None

    @property
    def verified(self) -> bool:
        return self.status in (
            VerificationStatus.VERIFIED,
            VerificationStatus.PARTIALLY_VERIFIED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "verification_status": str(self.status),
            "verified": self.verified,
            "official_url": self.official_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "checked_at": self.checked_at,
            "fetched": self.fetched,
            "content_type": self.content_type,
            "identity_confirmed": self.identity_confirmed,
            "identity_signals": list(self.identity_signals),
            "product_signals": list(self.product_signals),
            "evidence_notes": list(self.evidence_notes),
            "failures": list(self.failures),
            "reason": self.reason,
            "needs_review": self.needs_review,
            "detected_status": self.detected_status,
            "verification_source": (
                self.verification_source.to_dict() if self.verification_source else None
            ),
        }


@dataclass
class VerificationReport:
    """Auditable summary of one verification pass."""

    started_at: str
    finished_at: str | None = None
    input_count: int = 0
    fetched_count: int = 0
    verified_count: int = 0
    partially_verified_count: int = 0
    unverified_count: int = 0
    unreachable_count: int = 0
    failed_count: int = 0
    needs_review_count: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)

    def note(self, result: VerificationResult) -> None:
        self.input_count += 1
        if result.fetched:
            self.fetched_count += 1
        if result.needs_review:
            self.needs_review_count += 1
        key = str(result.status)
        self.status_counts[key] = self.status_counts.get(key, 0) + 1
        for code in result.failures:
            self.failure_counts[code] = self.failure_counts.get(code, 0) + 1
        if result.status == VerificationStatus.VERIFIED:
            self.verified_count += 1
        elif result.status == VerificationStatus.PARTIALLY_VERIFIED:
            self.partially_verified_count += 1
        elif result.status == VerificationStatus.UNREACHABLE:
            self.unreachable_count += 1
        elif result.status == VerificationStatus.FAILED:
            self.failed_count += 1
        else:
            self.unverified_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input": self.input_count,
            "fetched": self.fetched_count,
            "verified": self.verified_count,
            "partially_verified": self.partially_verified_count,
            "unverified": self.unverified_count,
            "unreachable": self.unreachable_count,
            "failed": self.failed_count,
            "needs_review": self.needs_review_count,
            "status_counts": dict(sorted(self.status_counts.items())),
            "failure_counts": dict(sorted(self.failure_counts.items())),
        }


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
    """Verifies candidates/tools against their official website."""

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        liveness: LivenessChecker | None = None,
        now: datetime | None = None,
    ) -> None:
        self.client = client or HttpClient()
        self.liveness = liveness or LivenessChecker(self.client)
        #: Injectable clock so verification artefacts are reproducible in tests.
        self._now = now

    # ------------------------------------------------------ prepared candidates
    def verify_candidate(self, candidate: Any) -> VerificationResult:
        """Verify one :class:`PreparedCandidate`-shaped record.

        The candidate is **not** mutated: the caller decides what to do with the
        outcome, and the candidate's discovery provenance stays pristine. Only
        ``candidate.website`` is ever fetched — a listing URL is a discovery
        pointer, never verification evidence.
        """
        checked_at = self._now or _utcnow()
        official_url = normalize_url(getattr(candidate, "website", None) or None)
        name = clean_text(getattr(candidate, "name", None))
        candidate_id = getattr(candidate, "candidate_id", None)

        if not official_url:
            return VerificationResult(
                candidate_id=candidate_id,
                name=name,
                status=VerificationStatus.FAILED,
                official_url=None,
                checked_at=checked_at.isoformat(),
                failures=[VerificationFailure.NO_OFFICIAL_URL],
                evidence_notes=[_FAILURE_NOTES[VerificationFailure.NO_OFFICIAL_URL]],
                reason=(
                    "no official website was observed during discovery; a directory "
                    "listing is not verification, so nothing was fetched"
                ),
                needs_review=True,
            )

        fetched = self.client.try_fetch(official_url, use_cache=True)
        evidence = extract_official_evidence(
            official_url, fetched, product_name=name, now=checked_at
        )
        return self._result_from_evidence(candidate_id, name, evidence)

    def verify_candidates(
        self, candidates: Iterable[Any]
    ) -> tuple[list[VerificationResult], VerificationReport]:
        """Verify a batch; one bad record never aborts the pass."""
        report = VerificationReport(started_at=(self._now or _utcnow()).isoformat())
        results: list[VerificationResult] = []
        for candidate in candidates:
            try:
                result = self.verify_candidate(candidate)
            except Exception as exc:  # noqa: BLE001 - resilience over completeness
                logger.warning(
                    "verification raised",
                    extra={
                        "candidate": _safe_attr(candidate, "name"),
                        "error": str(exc),
                    },
                )
                result = VerificationResult(
                    candidate_id=_safe_attr(candidate, "candidate_id"),
                    name=clean_text(_safe_attr(candidate, "name")),
                    status=VerificationStatus.UNVERIFIED,
                    official_url=normalize_url(_safe_attr(candidate, "website") or None),
                    checked_at=(self._now or _utcnow()).isoformat(),
                    failures=[VerificationFailure.FETCH_FAILED],
                    reason=f"verification error: {str(exc)[:200]}",
                    needs_review=True,
                )
            results.append(result)
            report.note(result)
        report.finished_at = (self._now or _utcnow()).isoformat()
        logger.info("official-website verification complete", extra=report.to_dict())
        return results, report

    # ------------------------------------------------------------------ tools
    def verify(self, tool: Tool, *, extractors: Iterable[Any] = ()) -> Tool:
        """Verify ``tool`` in place. Never fabricates a value.

        ``extractors`` are optional callables ``(tool, FetchResult) -> dict``
        that pull verified fields from the official page. They receive the *same*
        fetched response verification used, so no field can be sourced from a
        page that was not actually verified.
        """
        record = tool.verification
        checked_at = self._now or _utcnow()
        record.checked_at = checked_at
        record.official_url_checked = tool.website

        if not tool.website:
            record.status = VerificationStatus.FAILED
            record.notes = [
                *record.notes,
                _FAILURE_NOTES[VerificationFailure.NO_OFFICIAL_URL],
            ]
            tool.reject(
                RejectionReason.FAKE_OR_UNVERIFIABLE,
                "no official website could be resolved from discovery sources",
            )
            return tool

        fetched = self.client.try_fetch(tool.website, use_cache=True)
        evidence = extract_official_evidence(
            tool.website, fetched, product_name=tool.name, now=checked_at
        )
        result = self._result_from_evidence(tool.id, tool.name, evidence)

        record.http_status = evidence.http_status
        record.final_url = evidence.final_url
        record.is_accessible = evidence.is_accessible
        record.notes = [*record.notes, *result.evidence_notes]
        record.status = result.status

        if result.status == VerificationStatus.UNREACHABLE:
            reason = (
                RejectionReason.DEAD_OR_SHUTDOWN
                if evidence.detected_status
                in (ToolStatus.DISCONTINUED, ToolStatus.DEPRECATED)
                else RejectionReason.WEBSITE_BROKEN
            )
            tool.reject(reason, result.reason or "official website not accessible")
            if evidence.detected_status:
                tool.status = evidence.detected_status
            return tool

        if not result.verified:
            # Reachable but unproven: stay unverified and ask for a human.
            tool.flag_for_review(result.reason or "official website evidence insufficient")
            return tool

        if evidence.detected_status and not tool.status:
            tool.status = evidence.detected_status
            record.verified_fields = sorted({*record.verified_fields, "status"})

        if result.verification_source is not None:
            record.verification_sources = _merge_source_refs(
                record.verification_sources, result.verification_source
            )

        applied = self._apply_extractors(tool, fetched, extractors)
        record.verified_fields = sorted(
            {*record.verified_fields, *applied, "name", "website"}
        )
        record.unverifiable_fields = sorted(self._unverifiable_fields(tool))
        tool.last_verified_date = checked_at.date()
        if result.needs_review and result.reason:
            tool.flag_for_review(result.reason)
        return tool

    # -------------------------------------------------------------- helpers
    def _result_from_evidence(
        self,
        candidate_id: str | None,
        name: str | None,
        evidence: OfficialPageEvidence,
    ) -> VerificationResult:
        """Map evidence to a status. Status is only granted, never assumed."""
        notes = _evidence_notes(evidence)
        status, reason, needs_review = _decide(evidence)
        source = None
        if evidence.is_accessible and status in (
            VerificationStatus.VERIFIED,
            VerificationStatus.PARTIALLY_VERIFIED,
        ):
            source = SourceRef(
                name="Official product website",
                url=evidence.final_url or evidence.official_url,
                kind="official",
                retrieved_at=evidence.checked_at,
            )
        return VerificationResult(
            candidate_id=candidate_id,
            name=name,
            status=status,
            official_url=evidence.official_url or None,
            final_url=evidence.final_url,
            http_status=evidence.http_status,
            checked_at=evidence.checked_at.isoformat(),
            fetched=evidence.fetched,
            content_type=evidence.content_type,
            identity_confirmed=evidence.identity_confirmed,
            identity_signals=list(evidence.identity_signals),
            product_signals=list(evidence.product_signals),
            evidence_notes=notes,
            failures=list(evidence.failures),
            reason=reason,
            needs_review=needs_review,
            detected_status=(
                str(evidence.detected_status) if evidence.detected_status else None
            ),
            verification_source=source,
        )

    def _apply_extractors(
        self, tool: Tool, fetched: FetchResult | None, extractors: Iterable[Any]
    ) -> set[str]:
        """Run injected extractors, applying only non-empty verified values."""
        applied: set[str] = set()
        if not extractors or fetched is None or not fetched.ok:
            return applied
        for extractor in extractors:
            try:
                values = extractor(tool, fetched) or {}
            except Exception as exc:  # noqa: BLE001 - one bad extractor must not kill the run
                logger.warning(
                    "extractor failed",
                    extra={
                        "tool": tool.name,
                        "extractor": repr(extractor),
                        "error": str(exc),
                    },
                )
                continue
            for field_name, value in values.items():
                if value in (None, "", [], {}) or not hasattr(tool, field_name):
                    continue
                try:
                    setattr(tool, field_name, value)
                    applied.add(field_name)
                except Exception as exc:  # noqa: BLE001 - validation rejection is expected
                    logger.debug(
                        "extracted value rejected by schema",
                        extra={"tool": tool.name, "field": field_name, "error": str(exc)},
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


def _decide(
    evidence: OfficialPageEvidence,
) -> tuple[VerificationStatus, str | None, bool]:
    """The verification policy, isolated so it can be reasoned about at a glance.

    Returns ``(status, concise_reason, needs_review)``.
    """
    codes = set(evidence.failures)

    if VerificationFailure.NO_OFFICIAL_URL in codes:
        return (
            VerificationStatus.FAILED,
            "no official website URL was available, so nothing was fetched",
            True,
        )
    if VerificationFailure.INVALID_OFFICIAL_URL in codes:
        return (
            VerificationStatus.FAILED,
            "official website URL could not be resolved",
            True,
        )

    # --- could not look at the product at all: unverified, never rejected ---
    if VerificationFailure.FETCH_FAILED in codes:
        return (
            VerificationStatus.UNREACHABLE,
            "official website did not respond (timeout/network failure after retries)",
            True,
        )
    if VerificationFailure.BOT_CHALLENGE in codes:
        return (
            VerificationStatus.UNVERIFIED,
            "official website is behind a bot/WAF challenge; no product evidence "
            "could be read, so nothing was verified",
            True,
        )
    if VerificationFailure.DEAD_SITE_MARKER in codes:
        marker = evidence.dead_markers[0] if evidence.dead_markers else "dead-site marker"
        return (
            VerificationStatus.UNREACHABLE,
            f"official website shows a dead/parked/shut-down marker: '{marker}'",
            False,
        )
    if VerificationFailure.HTTP_ERROR in codes:
        return (
            VerificationStatus.UNREACHABLE,
            f"official website returned HTTP {evidence.http_status}",
            False,
        )
    if VerificationFailure.NON_HTML_RESPONSE in codes:
        return (
            VerificationStatus.UNVERIFIED,
            f"official URL returned {evidence.content_type or 'a non-HTML body'}; "
            "no page evidence could be extracted",
            True,
        )
    if VerificationFailure.EMPTY_RESPONSE in codes:
        return (
            VerificationStatus.UNVERIFIED,
            "official website returned an empty body",
            True,
        )
    if VerificationFailure.UNPARSEABLE_HTML in codes:
        return (
            VerificationStatus.UNVERIFIED,
            "official page markup could not be parsed",
            True,
        )
    if VerificationFailure.THIN_CONTENT in codes:
        return (
            VerificationStatus.UNVERIFIED,
            f"official page carries only {evidence.text_length} characters of text — "
            "too little to verify that a product exists",
            True,
        )

    # --- page was readable: now demand real evidence -----------------------
    if not evidence.identity_confirmed:
        return (
            VerificationStatus.UNVERIFIED,
            "official page does not name the product in its title, headings, "
            "logo or metadata, so identity could not be confirmed",
            True,
        )
    if VerificationFailure.OFF_DOMAIN_REDIRECT in codes:
        return (
            VerificationStatus.PARTIALLY_VERIFIED,
            f"official URL redirects off-domain to {evidence.final_url}; identity "
            "matched but ownership/rebrand needs confirmation",
            True,
        )
    if not evidence.has_product_signal:
        return (
            VerificationStatus.PARTIALLY_VERIFIED,
            "product identity confirmed on the official page, but no usable "
            "product affordance (pricing/signup/login/docs/app) was found",
            True,
        )
    return (
        VerificationStatus.VERIFIED,
        "product identity and a working product entry point were both found on "
        "the official website",
        False,
    )


def _merge_source_refs(existing: list[SourceRef], new: SourceRef) -> list[SourceRef]:
    keys = {(ref.name.lower(), ref.url or "") for ref in existing}
    if (new.name.lower(), new.url or "") in keys:
        return existing
    return [*existing, new]


def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` that survives a record whose property itself raises.

    Real candidate records come from JSONL, but the batch API accepts any
    candidate-shaped object; a broken one must be reported, not crash the pass.
    """
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a hostile record is data, not control flow
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
