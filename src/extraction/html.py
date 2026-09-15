"""HTML parsing primitives shared by every discovery adapter.

Pure/offline helpers (no network) so they are deterministic and unit-testable.
Network work lives in :mod:`src.core.http_client`.

Design rules:

* never raise on malformed markup — return ``None``/``[]`` instead;
* never invent a value: an absent element yields ``None``;
* bot-challenge / interstitial pages are *detected*, not parsed, so a blocked
  source degrades to "no candidates" rather than to garbage candidates.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from src.core.logging_setup import get_logger
from src.core.text import clean_text
from src.core.urls import extract_registrable_domain, normalize_url

logger = get_logger("extraction.html")

__all__ = [
    "parse_html",
    "absolutize",
    "node_text",
    "best_label",
    "anchor_targets",
    "looks_like_challenge",
    "page_fingerprint",
    "is_asset_url",
    "is_internal",
]

#: Signatures of anti-bot interstitials (Cloudflare, Akamai, generic WAF).
_CHALLENGE_MARKERS = (
    "just a moment...",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "cf-browser-verification",
    "cf_chl_opt",
    "attention required! | cloudflare",
    "ddos protection by",
    "access denied",
    "request unsuccessful. incapsula",
    "please verify you are a human",
)

_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".avif",
    ".css", ".js", ".mp4", ".webm", ".pdf", ".zip", ".woff", ".woff2",
)

_ASSET_HOST_HINTS = ("cdn-image.", "cdn.", "images.", "img.", "static.", "assets.")


def parse_html(markup: str | None) -> BeautifulSoup | None:
    """Parse ``markup`` with lxml, falling back to the stdlib parser.

    Returns ``None`` for empty input or unparseable markup.
    """
    if not markup or not markup.strip():
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(markup, parser)
        except Exception as exc:  # noqa: BLE001 - malformed markup must not abort a run
            logger.debug("parser %s failed: %s", parser, exc)
    return None


def looks_like_challenge(markup: str | None, status: int | None = None) -> bool:
    """True when a response is a bot challenge / block page rather than content."""
    if status in (401, 403, 429, 503) and (markup is None or len(markup) < 200_000):
        # A short 403/503 body is a block page in practice; a long one may still
        # be real content, so fall through to the marker check below.
        if status in (401, 403, 429):
            return True
    if not markup:
        return False
    head = markup[:8000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def page_fingerprint(markup: str | None) -> str | None:
    """Stable hash of a page's *visible* structure.

    Used by the paginator to detect a site that silently serves page 1 again
    for ``?page=2`` (Creati.ai does exactly this).
    """
    if not markup:
        return None
    soup = parse_html(markup)
    if soup is None:
        return None
    hrefs = [a.get("href") or "" for a in soup.find_all("a", href=True)]
    basis = "\n".join(hrefs[:400]) or (soup.get_text(" ", strip=True)[:4000])
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


def absolutize(href: str | None, base_url: str | None) -> str | None:
    """Resolve ``href`` against ``base_url`` and normalize it."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    absolute = urljoin(base_url or "", href) if base_url else href
    if not absolute.startswith(("http://", "https://")):
        return None
    return normalize_url(absolute)


def is_asset_url(url: str | None) -> bool:
    """True for image/script/media URLs that can never be a product website."""
    if not url:
        return True
    parts = urlsplit(url)
    path = parts.path.lower()
    if path.endswith(_ASSET_SUFFIXES):
        return True
    host = parts.netloc.lower()
    return any(host.startswith(hint) for hint in _ASSET_HOST_HINTS)


def is_internal(url: str | None, site_url: str | None) -> bool:
    """True when ``url`` belongs to the same registrable domain as ``site_url``."""
    a, b = extract_registrable_domain(url), extract_registrable_domain(site_url)
    return bool(a and b and a == b)


def node_text(node: Tag | None, *, max_length: int | None = None) -> str | None:
    """Cleaned visible text of ``node`` (``None`` when empty)."""
    if node is None:
        return None
    try:
        raw = node.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return None
    return clean_text(raw, max_length=max_length)


def best_label(node: Tag | None, *, max_length: int | None = None) -> str | None:
    """Best available human label for ``node``.

    Prefers the ``title`` attribute (directories keep the untruncated string
    there), then ``aria-label``/``alt``, then visible text.
    """
    if node is None:
        return None
    for attr in ("title", "aria-label", "alt"):
        value = clean_text(node.get(attr), max_length=max_length)
        if value:
            return value
    return node_text(node, max_length=max_length)


def anchor_targets(scope: Tag, base_url: str | None) -> list[tuple[Tag, str]]:
    """All ``(anchor, absolute_url)`` pairs inside ``scope``, malformed ones dropped."""
    out: list[tuple[Tag, str]] = []
    for anchor in scope.find_all("a", href=True):
        url = absolutize(anchor.get("href"), base_url)
        if url:
            out.append((anchor, url))
    return out


def select_first(scope: Tag, selectors: Iterable[str]) -> Tag | None:
    """First node matching any selector in order; invalid selectors are skipped."""
    for selector in selectors:
        if not selector:
            continue
        try:
            node = scope.select_one(selector)
        except Exception:  # noqa: BLE001 - a bad selector must not kill the run
            logger.debug("invalid selector skipped: %s", selector)
            continue
        if node is not None:
            return node
    return None


def select_all(scope: Tag, selectors: Iterable[str]) -> list[Tag]:
    """All nodes matching the first selector that matches anything."""
    for selector in selectors:
        if not selector:
            continue
        try:
            nodes = scope.select(selector)
        except Exception:  # noqa: BLE001
            logger.debug("invalid selector skipped: %s", selector)
            continue
        if nodes:
            return list(nodes)
    return []


_WS_RE = re.compile(r"\s+")


def compact(value: str | None) -> str | None:
    """Whitespace-collapsed copy of ``value``."""
    if value is None:
        return None
    collapsed = _WS_RE.sub(" ", value).strip()
    return collapsed or None
