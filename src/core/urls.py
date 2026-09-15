"""URL normalization utilities.

Implements the "URL Normalization: Consistent formatting and redirect
resolution" engineering principle. Redirect *resolution* is network work and
lives in :mod:`src.core.http_client`; this module is pure/offline so it is
deterministic and unit-testable.

Normalization rules applied by :func:`normalize_url`:

* add ``https://`` when the scheme is missing;
* lowercase scheme and host, strip a trailing dot, drop ``www.``;
* strip default ports (80/443);
* decode IDN hosts to punycode-free unicode-safe ASCII (``idna``);
* remove tracking query parameters (utm_*, ref, fbclid, gclid, ...);
* sort remaining query parameters for stability;
* drop the fragment unless it is a SPA hash route (``#/path``);
* collapse duplicate slashes and strip a trailing slash on non-root paths;
* drop common index files (``index.html``, ``index.php``).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

__all__ = [
    "normalize_url",
    "extract_domain",
    "extract_registrable_domain",
    "same_site",
    "is_probably_url",
    "url_slug",
]

TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mc_", "ml_", "vero_", "_hs", "hsa_", "matomo_")
TRACKING_PARAMS = {
    "ref", "ref_src", "referrer", "source", "fbclid", "gclid", "gclsrc", "dclid",
    "msclkid", "twclid", "igshid", "yclid", "wbraid", "gbraid", "si", "spm",
    "mkt_tok", "trk", "trkCampaign", "sc_channel", "sc_campaign", "cmpid",
    "campaignid", "adgroupid", "aff", "affiliate", "partner", "via", "lang_hint",
}
INDEX_FILES = {"index.html", "index.htm", "index.php", "default.aspx", "home.html"}

# Multi-label public suffixes we care about for registrable-domain extraction.
# Not a full PSL (no network dependency); covers the common cases in AI tool
# listings. Extend via config if a source needs more.
_MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp", "co.kr",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "com.ar",
    "co.in", "net.in", "org.in", "com.sg", "com.hk", "com.tw", "com.tr",
    "co.za", "com.cn", "net.cn", "org.cn", "gov.cn", "com.ua", "co.il",
    "com.pl", "com.es", "com.pt", "com.my", "co.id", "com.ph", "com.vn",
    "github.io", "gitlab.io", "pages.dev", "workers.dev", "vercel.app",
    "netlify.app", "web.app", "firebaseapp.com", "herokuapp.com", "streamlit.app",
    "hf.space", "notion.site", "framer.website", "wixsite.com", "bubbleapps.io",
    "repl.co", "replit.app", "glitch.me", "on.fleek.co", "surge.sh",
}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_WS_RE = re.compile(r"\s+")
_SLASHES_RE = re.compile(r"/{2,}")


def is_probably_url(value: str | None) -> bool:
    """Cheap structural check — no network access."""
    if not value or not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or " " in candidate.strip():
        return False
    if _SCHEME_RE.match(candidate):
        host = urlsplit(candidate).netloc
    else:
        host = candidate.split("/")[0]
    host = host.split("@")[-1].split(":")[0]
    return bool(host) and "." in host and not host.startswith(".") and not host.endswith(".")


def normalize_url(
    url: str | None,
    *,
    keep_query: bool = True,
    keep_path: bool = True,
    force_https: bool = True,
) -> str | None:
    """Return a canonical form of ``url``, or ``None`` if unusable.

    Never guesses: an input that is not URL-shaped returns ``None`` so the
    caller can leave the field blank (guideline section 10).
    """
    if not url or not isinstance(url, str):
        return None
    candidate = _WS_RE.sub("", url.strip())
    if not candidate:
        return None
    candidate = candidate.strip("<>\"'")
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if not _SCHEME_RE.match(candidate):
        candidate = "https://" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    scheme = (parts.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        return None
    if force_https and scheme == "http":
        scheme = "https"

    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    port = parts.port
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"

    path = ""
    if keep_path:
        path = unquote(parts.path or "")
        path = _SLASHES_RE.sub("/", path)
        segments = [s for s in path.split("/") if s != ""]
        if segments and segments[-1].lower() in INDEX_FILES:
            segments.pop()
        path = "/" + "/".join(segments) if segments else "/"
        path = quote(path, safe="/-_.~!$&'()*+,;=:@%")

    query = ""
    if keep_query and parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        query = urlencode(sorted(kept), doseq=True) if kept else ""

    fragment = ""
    if parts.fragment.startswith("/") or parts.fragment.startswith("!"):
        fragment = parts.fragment  # SPA hash route carries real identity

    normalized = urlunsplit((scheme, netloc, path or "/", query, fragment))
    if normalized.endswith("/") and (path or "/") != "/":
        normalized = normalized[:-1]
    return normalized


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def extract_domain(url: str | None) -> str | None:
    """Return the full lowercase host without ``www.`` (e.g. ``docs.openai.com``)."""
    normalized = normalize_url(url, keep_query=False, keep_path=False)
    if not normalized:
        return None
    host = urlsplit(normalized).hostname
    return host or None


def extract_registrable_domain(url: str | None) -> str | None:
    """Return the registrable domain (``openai.com`` for ``docs.openai.com``).

    This is the primary deduplication key: the Tools guideline treats the
    official website/domain as the strongest identity signal.
    """
    host = extract_domain(url)
    if not host:
        return None
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    for suffix in _MULTI_LABEL_SUFFIXES:
        if host.endswith("." + suffix):
            depth = suffix.count(".") + 2
            return ".".join(labels[-depth:]) if len(labels) >= depth else host
    return ".".join(labels[-2:])


def same_site(a: str | None, b: str | None) -> bool:
    """True when both URLs share a registrable domain."""
    da, db = extract_registrable_domain(a), extract_registrable_domain(b)
    return bool(da and db and da == db)


def url_slug(url: str | None) -> str | None:
    """Return the final path segment of a URL (useful for directory listings)."""
    normalized = normalize_url(url, keep_query=False)
    if not normalized:
        return None
    segments = [s for s in urlsplit(normalized).path.split("/") if s]
    return segments[-1].lower() if segments else None
