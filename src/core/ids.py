"""Deterministic, stable entity IDs.

The technical specification asks for a ``"stable-generated-uuid"``. We use
UUIDv5 (SHA-1 over a fixed namespace + an identity key), which gives:

* **determinism** — rerunning the pipeline never changes an entity's ID;
* **stability** — the key is derived from the entity's strongest identity
  signal (its official registrable domain), so ID survives re-scraping,
  re-titling and re-categorisation;
* **collision-resistance** across entity types via the ``entity_type`` prefix.

Identity-key precedence for tools (mirrors the deduplication signals in the
Tools guideline section 7):

1. registrable domain of the official website — authoritative;
2. registrable domain of the product URL;
3. canonical product name + canonical company name;
4. canonical product name alone (weakest; flagged by ``basis``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.core.text import canonical_name
from src.core.urls import extract_registrable_domain

__all__ = ["AIORBIT_NAMESPACE", "IdentityKey", "build_identity_key", "make_entity_id", "content_hash"]

#: Fixed UUIDv5 namespace for the AI Orbit ecosystem. Must never change —
#: changing it would re-key every previously published record.
AIORBIT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://aiorbit.ai/entities")

#: Order of preference, strongest identity signal first.
IDENTITY_BASES = ("website_domain", "product_url_domain", "name+company", "name")


@dataclass(frozen=True)
class IdentityKey:
    """The string an entity's ID is derived from, plus provenance."""

    value: str
    basis: str

    @property
    def is_domain_based(self) -> bool:
        return self.basis in {"website_domain", "product_url_domain"}

    @property
    def confidence(self) -> str:
        return {
            "website_domain": "high",
            "product_url_domain": "high",
            "name+company": "medium",
            "name": "low",
        }.get(self.basis, "low")


def build_identity_key(
    *,
    name: str | None = None,
    website: str | None = None,
    product_url: str | None = None,
    company: str | None = None,
) -> IdentityKey | None:
    """Resolve the strongest available identity key, or ``None`` if there is none."""
    domain = extract_registrable_domain(website)
    if domain:
        return IdentityKey(value=domain, basis="website_domain")

    domain = extract_registrable_domain(product_url)
    if domain:
        return IdentityKey(value=domain, basis="product_url_domain")

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
            name=name, website=website, product_url=product_url, company=company
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
