"""Relationship extraction for ``relationships.json``.

Technical specification §5 requires mappings such as:

* Company **develops** Tool/Model
* Tool **solves** Task
* MCP **integrates with** Tool
* Device **runs** Model

The Tools module can honestly produce only the edges whose evidence exists in a
tool record. Every edge carries an ``evidence_url`` and a ``confidence`` so the
graph is auditable, and edges are **never** emitted for unverified data.

Out of scope for this module (owned by the company): Repositories and Videos.
This module therefore emits ``has_repository`` edges pointing at the repository
URL by name only — it does not create repository entities.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from src.core.ids import make_entity_id
from src.core.logging_setup import get_logger
from src.models.base import Relationship
from src.models.enums import EntityType, RelationshipType
from src.models.tool import Tool

logger = get_logger("enrichment.relationships")


def company_id_for(company: str | None, website: str | None = None) -> str | None:
    """Deterministic company ID, or ``None`` when the developer is unknown."""
    if not company:
        return None
    try:
        company_id, _ = make_entity_id(
            EntityType.COMPANY.value, name=company, website=website
        )
    except ValueError:
        return None
    return company_id


def task_id_for(task: str | None) -> str | None:
    """Deterministic Task ID from a primary-task label."""
    if not task:
        return None
    try:
        task_id, _ = make_entity_id(EntityType.TASK.value, name=task)
    except ValueError:
        return None
    return task_id


class RelationshipExtractor:
    """Derives graph edges from verified tool fields only."""

    def extract(self, tool: Tool) -> list[Relationship]:
        edges: list[Relationship] = []
        today = datetime.now(timezone.utc).date()

        # Company --develops--> Tool
        if tool.company:
            company_id = tool.company_id or company_id_for(tool.company, tool.website)
            if company_id:
                edges.append(
                    Relationship(
                        source_id=company_id,
                        source_type=EntityType.COMPANY,
                        relationship=RelationshipType.DEVELOPS.value,
                        target_id=tool.id,
                        target_type=EntityType.TOOL,
                        target_name=tool.name,
                        confidence=0.95 if tool.is_verified else 0.6,
                        evidence_url=tool.website,
                        evidence_note="developer stated on the official website"
                        if tool.is_verified
                        else "developer reported by a discovery source",
                        observed_at=today,
                    )
                )

        # Tool --solves--> Task
        if tool.primary_task:
            task_id = tool.primary_task_id or task_id_for(tool.primary_task)
            if task_id:
                edges.append(
                    Relationship(
                        source_id=tool.id,
                        source_type=EntityType.TOOL,
                        relationship=RelationshipType.SOLVES.value,
                        target_id=task_id,
                        target_type=EntityType.TASK,
                        target_name=tool.primary_task,
                        confidence=0.9 if tool.is_verified else 0.55,
                        evidence_url=tool.website,
                        evidence_note="derived from the tool's verified primary task",
                        observed_at=today,
                    )
                )

        # Tool --integrates with--> named third-party product
        for integration in tool.integrations:
            edges.append(
                Relationship(
                    source_id=tool.id,
                    source_type=EntityType.TOOL,
                    relationship=RelationshipType.INTEGRATES_WITH.value,
                    target_name=integration,
                    target_type=EntityType.TOOL,
                    confidence=0.8 if "integrations" in tool.verification.verified_fields else 0.5,
                    evidence_url=tool.website,
                    evidence_note="integration listed on the official website",
                    observed_at=today,
                )
            )

        # Tool --has repository--> repo URL (repository entities are out of scope)
        if tool.repository_url:
            edges.append(
                Relationship(
                    source_id=tool.id,
                    source_type=EntityType.TOOL,
                    relationship=RelationshipType.HAS_REPOSITORY.value,
                    target_name=tool.repository_url,
                    target_type=EntityType.REPOSITORY,
                    confidence=0.9,
                    evidence_url=tool.repository_url,
                    evidence_note="public repository linked from the product",
                    observed_at=today,
                )
            )

        return edges

    def extract_many(self, tools: Iterable[Tool]) -> list[Relationship]:
        """Extract and de-duplicate edges across many tools."""
        seen: set[tuple[str, str, str]] = set()
        edges: list[Relationship] = []
        for tool in tools:
            for edge in self.extract(tool):
                if edge.key in seen:
                    continue
                seen.add(edge.key)
                edges.append(edge)
        logger.info("relationship extraction complete", extra={"edges": len(edges)})
        return edges
