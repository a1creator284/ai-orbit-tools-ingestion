"""Regression coverage for replaying completed verification artefacts."""

from __future__ import annotations

from datetime import datetime, timezone

from src.candidates.promotion import promote_verified_rows
from src.core.io import write_jsonl
from src.discovery.base import CandidateTool
from src.models.base import SourceRef
from src.verification.store import load_resolved_candidates


def test_promotion_preserves_directory_evidence_and_only_maps_upvotes(tmp_path) -> None:
    source = SourceRef(
        name="Example Directory",
        url="https://directory.example/tool/example",
        kind="directory",
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    candidate = CandidateTool(
        name="Example AI",
        website="https://example.ai/",
        listing_url="https://directory.example/tool/example",
        source_key="example_directory",
        source_name="Example Directory",
        tagline="A sufficiently descriptive directory observation.",
        raw_signals={
            "directory_upvotes_raw": "1,204",
            "directory_like_counter_raw": "40",
            "directory_rating_stars_filled": 5,
        },
    )
    resolved = tmp_path / "resolved.jsonl"
    write_jsonl(resolved, [candidate.to_dict()])
    prepared, _ = load_resolved_candidates(resolved)
    assert len(prepared) == 1
    row = {
        "candidate_id": prepared[0].candidate_id,
        "name": "Example AI",
        "live": True,
        "discovery": {
            "candidate_id": prepared[0].candidate_id,
            "name": "Example AI",
            "website": "https://example.ai/",
            "listing_url": "https://directory.example/tool/example",
            "discovery_sources": [source.to_dict()],
        },
        "verification": {
            "verification_status": "verified",
            "official_url": "https://example.ai/",
            "final_url": "https://example.ai/",
            "checked_at": "2026-09-15T00:00:00+00:00",
            "fetched": True,
            "verification_source": {
                "name": "Official product website",
                "url": "https://example.ai/",
                "kind": "official",
            },
        },
    }

    tools, report = promote_verified_rows([row], resolved_path=resolved)

    assert report.promoted == 1
    assert tools[0].adoption.directory_upvotes == 1204
    assert len(tools[0].adoption.signal_sources) == 1
    assert tools[0].adoption.signal_sources[0].name == "Example Directory"
    assert tools[0].directory_evidence["directory_like_counter_raw"][0]["value"] == "40"
    assert tools[0].directory_evidence["directory_rating_stars_filled"][0]["value"] == 5
