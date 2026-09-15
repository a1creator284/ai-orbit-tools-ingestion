"""Deterministic, stable entity IDs.

The technical specification asks for a ``"stable-generated-uuid"``. We use
UUIDv5 (SHA-1 over a fixed namespace + an identity key), which gives:

* **determinism** — rerunning the pipeline never changes an entity's ID;
* **stability** — the key is derived from the entity's strongest identity
  signal (its canonical product URL), so the ID survives re-scraping,
  re-titling and re-categorisation;
* **collision-resistance** across entity types via the ``entity_type`` prefix.

Identity-key precedence for tools (mirrors the deduplication hierarchy in
:mod:`src.deduplication.matcher`):

1. an **explicit canonical URL** asserted by evidence (redirect target,
   ``rel=canonical``, a verified official URL) — authoritative;
2. the **canonical product identity of the official website** — host plus
   product path, so ``company.com/product-a`` and ``company.com/product-b``
   are two products, not one (see :mod:`src.core.product_identity`);
3. the canonical product identity of the directory **listing URL** (used only
   while no official site is known — a listing URL is a stable pointer inside
   one directory, not a cross-directory identity);
4. canonical product name + canonical company name;
5. canonical product name alone (weakest; flagged by ``basis``).

Why not the bare registrable domain
-----------------------------------
It was the key until Run #12 and it silently destroyed real products: every
tool under ``apiframe.ai/models/<vendor>`` collapsed into one ID, as did
``wabot.wadesk.io`` / ``tg.wadesk.io``, and *every* listing-only candidate from
one directory collapsed into a single ID. Domain equality is a *company/site*
signal; it is never on its own proof of product identity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.core.product_identity import product_identity
from src.core.text import canonical_name

__all__ = [
    "AIORBIT_NAMESPACE",
    "IDENTITY_BASES",
    "IdentityKey",
    "build_identity_key",
    "make_entity_id",
    "content_hash",
]

#: Fixed UUIDv5 namespace for the AI Orbit ecosystem. Must never change —
#: changing it would re-key every previously published record.
AIORBIT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://aiorbit.ai/entities")

#: Order of preference, strongest identity signal first.
IDENTITY_BASES = (
    "canonical_url",
    "website_product_url",
    "website_domain",
    "listing_product_url",
    "listing_domain",
    "name+company",
    "name",
)

#: How much a basis can be trusted as a *cross-source* product identity.
IDENTITY_CONFIDENCE = {
    "canonical_url": "high",
    "website_product_url": "high",
    "website_domain": "high",
    # A directory detail URL identifies the product only inside that directory.
    "listing_product_url": "medium",
    "listing_domain": "medium",
    "name+company": "medium",
    "name": "low",
    "explicit": "high",
}

#: Bases derived from a real URL (as opposed to a name).
_URL_BASES = frozenset(
    {
        "canonical_url",
        "website_product_url",
        "website_domain",
        "listing_product_url",
        "listing_domain",
        "explicit",
    }
)

#: Bases derived from the product's **own** site (not a directory listing).
_OFFICIAL_BASES = frozenset({"canonical_url", "website_product_url", "website_domain"})


@dataclass(frozen=True)
class IdentityKey:
    """The string an entity's ID is derived from, plus provenance."""

    value: str
    basis: str

    @property
    def is_url_based(self) -> bool:
        """True when the key came from a URL rather than from a name."""
        return self.basis in _URL_BASES

    #: Backwards-compatible alias — kept so older call sites keep working.
    @property
    def is_domain_based(self) -> bool:
        return self.is_url_based

    @property
    def is_official(self) -> bool:
        """True when the key came from the product's own website."""
        return self.basis in _OFFICIAL_BASES

    @property
    def confidence(self) -> str:
        return IDENTITY_CONFIDENCE.get(self.basis, "low")


def _url_key(url: str | None, *, official: bool) -> IdentityKey | None:
    """Identity key for a product URL, or ``None`` when it is not URL-shaped."""
    identity = product_identity(url)
    if identity is None:
        return None
    if official:
        basis = "website_domain" if identity.is_root else "website_product_url"
    else:
        basis = "listing_domain" if identity.is_root else "listing_product_url"
    return IdentityKey(value=identity.key, basis=basis)


def build_identity_key(
    *,
    name: str | None = None,
    website: str | None = None,
    product_url: str | None = None,
    company: str | None = None,
    canonical_url: str | None = None,
) -> IdentityKey | None:
    """Resolve the strongest available identity key, or ``None`` if there is none.

    Args:
        name: product name.
        website: the product's own official website.
        product_url: directory detail/listing URL (weaker than ``website``).
        company: company/developer name (only used with ``name``).
        canonical_url: a canonical product URL asserted by explicit evidence
            (redirect target, ``rel=canonical``, verified official URL). Wins
            over everything else because it *is* the resolved identity.
    """
    explicit = _url_key(canonical_url, official=True)
    if explicit:
        return IdentityKey(value=explicit.value, basis="canonical_url")

    from_website = _url_key(website, official=True)
    if from_website:
        return from_website

    from_listing = _url_key(product_url, official=False)
    if from_listing:
        return from_listing

    canonical = canonical_name(name)
    if canonical:
        canonical_company = canonical_name(company)
        if canonical_company:
            return IdentityKey(value=f"{canonical}@{canonical_company}", basis="name+company")
        return IdentityKey(value=canonical, basis="name")

    return None


def make_entity_id(
    entity_type: str,
    *,
    name: str | None = None,
    website: str | None = None,
    product_url: str | None = None,
    company: str | None = None,
    canonical_url: str | None = None,
    identity_key: IdentityKey | str | None = None,
) -> tuple[str, IdentityKey]:
    """Return ``(uuid5_string, identity_key)`` for an entity.

    Raises:
        ValueError: if no identity signal at all is available. Callers must not
            invent one — an unidentifiable record is dropped instead.
    """
    if isinstance(identity_key, str):
        key: IdentityKey | None = IdentityKey(value=identity_key, basis="explicit")
    else:
        key = identity_key or build_identity_key(
            name=name,
            website=website,
            product_url=product_url,
            company=company,
            canonical_url=canonical_url,
        )
    if key is None or not key.value:
        raise ValueError(
            "cannot build a deterministic ID: no name, website or product URL available"
        )
    seed = f"{entity_type.strip().lower()}:{key.value.strip().lower()}"
    return str(uuid.uuid5(AIORBIT_NAMESPACE, seed)), key


def content_hash(*parts: object) -> str:
    """Stable short hash of arbitrary content (change detection, cache keys)."""
    import hashlib

    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "ignore"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]
