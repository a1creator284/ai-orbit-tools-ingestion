"""Common entity schema shared by every AI Orbit entity type.

Mirrors section 4.1 of the technical specification::

    {
      "id": "stable-generated-uuid",
      "entity_type": "string",
      "name": "string",
      "description": "string",
      "url": "string",
      "categories": ["string"],
      "source": { "name": "string", "url": "string" }
    }

Tool-specific fields are added by :class:`src.models.tool.Tool`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.text import clean_text
from src.core.urls import normalize_url
from src.models.enums import EntityType


class AIOrbitModel(BaseModel):
    """Base model: strict-ish, trims strings, drops empty values."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=True,
        populate_by_name=True,
    )

    def to_dict(self, *, drop_none: bool = False) -> dict[str, Any]:
        """JSON-ready dict. ``drop_none`` omits unverified fields entirely."""
        data = self.model_dump(mode="json", exclude_none=drop_none)
        return data


class SourceRef(AIOrbitModel):
    """Where a piece of information came from.

    Provenance is mandatory: the guideline requires a *discovery source* and a
    *verification source* per tool, and "prefer the official source" can only
    be enforced if we know which source said what.
    """

    name: str = Field(..., description="Human-readable source name, e.g. 'There's An AI For That'")
    url: str | None = Field(None, description="Canonical URL of the listing/page used")
    kind: str | None = Field(
        None, description="directory | official | api | rss | publication | marketplace"
    )
    retrieved_at: datetime | None = Field(
        None, description="UTC timestamp when this source was fetched"
    )

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("source name is required")
        return cleaned

    @field_validator("url", mode="before")
    @classmethod
    def _norm_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @property
    def is_official(self) -> bool:
        return (self.kind or "").lower() == "official"


class FieldProvenance(AIOrbitModel):
    """Per-field provenance so any value can be traced and audited."""

    field: str
    source_name: str
    source_url: str | None = None
    verified: bool = False
    observed_at: datetime | None = None

    @field_validator("source_url", mode="before")
    @classmethod
    def _norm_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None


class BaseEntity(AIOrbitModel):
    """The common entity schema (technical spec 4.1)."""

    id: str = Field(..., description="Deterministic UUIDv5 — see src.core.ids")
    entity_type: EntityType
    name: str
    description: str | None = Field(
        None, description="Short description; None when not verifiable"
    )
    url: str | None = Field(None, description="Primary/official URL, normalized")
    categories: list[str] = Field(default_factory=list)
    source: SourceRef | None = Field(
        None, description="Primary discovery source for this entity"
    )

    # ---- shared operational metadata (not in the minimal spec schema) ----
    additional_sources: list[SourceRef] = Field(
        default_factory=list, description="All other sources this entity was seen in"
    )
    provenance: list[FieldProvenance] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    schema_version: str = Field("1.0.0", description="Schema version of this record")

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> Any:
        cleaned = clean_text(value, max_length=200)
        if not cleaned:
            raise ValueError("entity name is required and must be non-empty")
        return cleaned

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> Any:
        return clean_text(value)

    @field_validator("url", mode="before")
    @classmethod
    def _norm_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @field_validator("categories", mode="before")
    @classmethod
    def _clean_categories(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        seen: dict[str, str] = {}
        for item in value:
            cleaned = clean_text(item, max_length=80)
            if cleaned:
                seen.setdefault(cleaned.lower(), cleaned)
        return list(seen.values())

    @model_validator(mode="after")
    def _stamp(self) -> "BaseEntity":
        now = datetime.now(timezone.utc)
        if self.first_seen_at is None:
            object.__setattr__(self, "first_seen_at", now)
        return self


class Relationship(AIOrbitModel):
    """Edge for ``relationships.json`` (technical spec section 5)."""

    source_id: str
    source_type: EntityType
    relationship: str
    target_id: str | None = None
    target_type: EntityType | None = None
    #: Used when the target entity is not (yet) in our dataset.
    target_name: str | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    evidence_url: str | None = None
    evidence_note: str | None = None
    observed_at: date | None = None

    @field_validator("evidence_url", mode="before")
    @classmethod
    def _norm_url(cls, value: Any) -> Any:
        return normalize_url(value) if value else None

    @model_validator(mode="after")
    def _require_target(self) -> "Relationship":
        if not self.target_id and not self.target_name:
            raise ValueError("relationship requires target_id or target_name")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source_id, str(self.relationship), self.target_id or (self.target_name or ""))
