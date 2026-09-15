"""Discovery-stage candidate identity.

One function, used by both the per-source de-duplicator
(:meth:`src.discovery.html_source.HtmlListingSource.dedup_key`) and the
cross-source merger (:func:`src.discovery.runner.candidate_identity`), so the
two can never drift apart.

Design rule — **exact identity only**
-------------------------------------
Discovery must never *lose* a real product. Fuzzy entity resolution (name
similarity, shared-domain review, company matching) is owned by
:mod:`src.deduplication`, which runs later and is explainable/reviewable.
Therefore this key collapses two candidates only when they are the *same
observed thing*, and otherwise keeps them separate.

Why the key is not the bare registrable domain
----------------------------------------------
Verified on live ``creati.ai/ai-tools/`` (2026-09-15), a bare-domain key lost
real products in two different ways:

* **shared vendor path** — ``apiframe.ai/models/suno`` (Suno AI API),
  ``apiframe.ai/models/midjourney`` (Midjourney AI API), ``apiframe.ai/``
  (Apiframe AI) and 10 more collapsed into one candidate, discarding 12 tools;
* **shared vendor host** — ``wabot.wadesk.io``, ``tg.wadesk.io``,
  ``link.wadesk.io`` and ``warmer.wadesk.io`` are four separate products that
  collapsed into one.

So the key is ``host + product path``:

* the **full host** is kept (``www.`` already removed by ``normalize_url``), so
  distinct subdomain products stay distinct;
* the **path** is stripped of parts that never identify a different product —
  a leading locale segment (``/en/pricing`` → ``/pricing``) and trailing
  generic marketing/account segments (``/pricing``, ``/login``, …) — so
  ``jasper.ai/pricing`` and ``jasper.ai/`` remain one candidate;
* the query string and fragment are already removed by ``normalize_url``.

The deliberate consequence is that discovery *under*-merges rather than
over-merges: ``docs.openai.com`` and ``openai.com`` stay two candidates here
and are folded together later by :mod:`src.deduplication`, whose registrable
-domain signal and review queue make that decision explainable. Losing a real
tool is unrecoverable; an extra candidate is not.

Candidates with no outbound website fall back to the directory's own detail
URL, then to the name — in that order of reliability.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from src.core.urls import extract_domain, normalize_url

__all__ = ["candidate_key", "website_identity", "GENERIC_PATH_SEGMENTS"]

#: Path segments that address a *page of a product*, not a different product.
GENERIC_PATH_SEGMENTS = {
    "pricing", "price", "prices", "plans", "plan", "billing",
    "home", "homepage", "index", "main", "welcome", "start", "landing",
    "about", "about-us", "aboutus", "contact", "contact-us",
    "login", "log-in", "signin", "sign-in", "signup", "sign-up", "register",
    "get-started", "getting-started", "getstarted", "try", "try-free", "free",
    "download", "downloads", "install",
    "en-us", "en-gb", "default",
}

#: Leading locale segment, e.g. ``/en/`` or ``/pt-br/``.
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


def website_identity(url: str | None) -> str | None:
    """Identity key for an official/product URL, or ``None`` when unusable.

    Returns ``"domain:<host>"`` for a product at the host root (after generic
    segments are stripped) and ``"url:<host>/<path>"`` for a product that lives
    at a specific path on a shared host.
    """
    domain = extract_domain(url)
    if not domain:
        return None

    normalized = normalize_url(url, keep_query=False)
    path = urlsplit(normalized).path if normalized else ""
    segments = [segment for segment in path.split("/") if segment]

    if segments and _LOCALE_RE.match(segments[0].lower()):
        segments = segments[1:]
    while segments and segments[-1].lower() in GENERIC_PATH_SEGMENTS:
        segments.pop()

    if not segments:
        return f"domain:{domain}"
    return "url:{}/{}".format(domain, "/".join(s.lower() for s in segments))


def candidate_key(
    *,
    website: str | None,
    listing_url: str | None,
    name: str | None,
) -> str:
    """Exact-identity key for a discovery candidate.

    Precedence mirrors how trustworthy each pointer is:

    1. the official/product URL (the guideline's strongest identity signal);
    2. the directory's own detail URL (stable within a directory);
    3. the candidate name (last resort; only reached when a card has no links,
       which :attr:`~src.discovery.base.CandidateTool.is_usable` rejects anyway).
    """
    from_website = website_identity(website)
    if from_website:
        return from_website
    normalized_listing = normalize_url(listing_url) if listing_url else None
    if normalized_listing:
        return f"listing:{normalized_listing}"
    return f"name:{(name or '').strip().lower()}"
