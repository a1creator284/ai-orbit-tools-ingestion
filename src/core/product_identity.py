"""Canonical *product* identity derived from a URL.

Why this module exists
----------------------
The registrable domain alone is **not** a product identity. Verified on live
directory data (see :mod:`src.discovery.identity`), a bare-domain key silently
destroys real products:

* ``apiframe.ai/`` (Apiframe AI), ``apiframe.ai/models/suno`` (Suno AI API) and
  ``apiframe.ai/models/midjourney`` (Midjourney AI API) are three products;
* ``wabot.wadesk.io``, ``tg.wadesk.io`` and ``link.wadesk.io`` are three
  products;
* ``theresanaiforthat.com/ai/<slug>`` is a *different* product per slug.

So the canonical identity of a product URL is **host + product path**:

* the full host is kept (``www.`` already removed by
  :func:`src.core.urls.normalize_url`), so distinct subdomain products stay
  distinct;
* the path keeps only segments that can identify a different product — a
  leading locale segment (``/en/pricing`` → ``/pricing``) and trailing generic
  marketing/account segments (``/pricing``, ``/login``, …) are dropped, so
  ``jasper.ai/pricing`` and ``jasper.ai/`` are one product;
* query strings, tracking parameters and fragments are already removed by
  :func:`src.core.urls.normalize_url`.

This is the single source of truth used by

* :mod:`src.core.ids` — to build deterministic entity IDs;
* :mod:`src.deduplication.matcher` — to compare product identity;
* :mod:`src.discovery.identity` — to key discovery candidates.

Keeping one implementation is deliberate: three copies of "what makes two URLs
the same product" would drift, and a drift here either loses real tools or
merges distinct ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.core.urls import extract_domain, extract_registrable_domain, normalize_url

__all__ = [
    "GENERIC_PATH_SEGMENTS",
    "ProductIdentity",
    "product_identity",
    "product_identity_key",
]

#: Path segments that address a *page of a product*, not a different product.
GENERIC_PATH_SEGMENTS = frozenset(
    {
        "pricing", "price", "prices", "plans", "plan", "billing",
        "home", "homepage", "index", "main", "welcome", "start", "landing",
        "about", "about-us", "aboutus", "contact", "contact-us",
        "login", "log-in", "signin", "sign-in", "signup", "sign-up", "register",
        "get-started", "getting-started", "getstarted", "try", "try-free", "free",
        "download", "downloads", "install",
        "en-us", "en-gb", "default",
    }
)

#: Leading locale segment, e.g. ``/en/`` or ``/pt-br/``.
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


@dataclass(frozen=True)
class ProductIdentity:
    """The identity a URL asserts about a product.

    Attributes:
        host: full lowercase host (``www.`` stripped), e.g. ``tg.wadesk.io``.
        registrable_domain: e.g. ``wadesk.io`` — the *company/site* level.
        path_segments: significant, product-identifying path segments.
        key: ``host`` for a product at the host root, ``host/a/b`` otherwise.
    """

    host: str
    registrable_domain: str
    path_segments: tuple[str, ...]
    key: str

    @property
    def is_root(self) -> bool:
        """True when the product lives at the host root (no product path)."""
        return not self.path_segments

    @property
    def path(self) -> str | None:
        """``/a/b`` or ``None`` for a root product."""
        return "/" + "/".join(self.path_segments) if self.path_segments else None

    @property
    def is_subdomain(self) -> bool:
        """True when the host is a subdomain of its registrable domain."""
        return self.host != self.registrable_domain

    @property
    def slug(self) -> str | None:
        return self.path_segments[-1] if self.path_segments else None

    def shares_registrable_domain(self, other: "ProductIdentity") -> bool:
        return self.registrable_domain == other.registrable_domain

    def path_conflicts_with(self, other: "ProductIdentity") -> bool:
        """True when two identities on the **same host** name different products.

        ``company.com/product-a`` vs ``company.com/product-b`` conflicts.
        ``company.com/product-a`` vs ``company.com/product-a/docs`` does not —
        one path is a prefix of the other, i.e. the same product's sub-page.
        """
        if self.host != other.host:
            return False
        if self.is_root or other.is_root:
            return False
        shorter, longer = sorted((self.path_segments, other.path_segments), key=len)
        return longer[: len(shorter)] != shorter


def product_identity(url: str | None) -> ProductIdentity | None:
    """Canonical product identity of ``url``, or ``None`` when unusable.

    Pure and offline, so it is deterministic and unit-testable. Handles
    http/https, ``www``/non-``www``, trailing slashes, fragments, tracking
    parameters, index files, default ports and locale prefixes by delegating to
    :func:`src.core.urls.normalize_url`.
    """
    host = extract_domain(url)
    if not host:
        return None
    registrable = extract_registrable_domain(url) or host

    normalized = normalize_url(url, keep_query=False)
    raw_path = urlsplit(normalized).path if normalized else ""
    segments = [segment for segment in raw_path.split("/") if segment]

    if segments and _LOCALE_RE.match(segments[0].lower()):
        segments = segments[1:]
    while segments and segments[-1].lower() in GENERIC_PATH_SEGMENTS:
        segments.pop()

    significant = tuple(segment.lower() for segment in segments)
    key = host if not significant else "{}/{}".format(host, "/".join(significant))
    return ProductIdentity(
        host=host,
        registrable_domain=registrable,
        path_segments=significant,
        key=key,
    )


def product_identity_key(url: str | None) -> str | None:
    """Convenience wrapper returning only :attr:`ProductIdentity.key`."""
    identity = product_identity(url)
    return identity.key if identity else None
