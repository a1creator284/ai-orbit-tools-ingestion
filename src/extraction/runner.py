"""Resumable official-facts enrichment for already verified products.

This module intentionally does not decide verification.  It fetches only the
official URL on a promoted ``verified``/``partially_verified`` Tool, extracts
facts with the existing official-page extractor, and writes an independent
append-only checkpoint artefact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.core.ids import content_hash
from src.core.io import append_jsonl, read_jsonl, write_json
from src.core.urls import normalize_url
from src.extraction.official_page import OfficialFacts, OfficialFactsExtractor
from src.models.base import SourceRef
from src.models.enums import VerificationStatus
from src.models.tool import Tool

OFFICIAL_FACTS_FILENAME = "official_facts.jsonl"
OFFICIAL_FACTS_REPORT_FILENAME = "official_facts_report.json"
OFFICIAL_FACTS_STATE_FILENAME = "official_facts_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def facts_key(url: str | None) -> str | None:
    """Deterministic identity for one official URL; no candidate-id collapsing."""
    normalized = normalize_url(url) if url else None
    return content_hash("official_facts", normalized) if normalized else None


def eligible(tool: Tool) -> bool:
    return bool(tool.website) and tool.verification.status in {
        VerificationStatus.VERIFIED, VerificationStatus.PARTIALLY_VERIFIED,
        VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value,
    }


def load_facts(path: str | Path) -> dict[str, dict[str, Any]]:
    """Latest valid completed fact rows keyed by their normalized official URL."""
    rows: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        if not isinstance(row, Mapping) or row.get("outcome") not in {"success", "empty"}:
            continue
        url = normalize_url(row.get("official_url"))
        facts = row.get("facts")
        if url and isinstance(facts, Mapping):
            rows[url] = dict(facts)
    return rows


def apply_facts(tool: Tool, payload: Mapping[str, Any]) -> bool:
    """Apply persisted official facts additively, with the extractor's mapping."""
    try:
        facts = OfficialFacts(**dict(payload))
    except (TypeError, ValueError):
        return False
    values = OfficialFactsExtractor(fetch_pricing_page=False).to_field_values(tool, facts)
    applied: set[str] = set()
    for name, value in values.items():
        if value in (None, "", [], {}) or not hasattr(tool, name):
            continue
        setattr(tool, name, value)
        applied.add(name)
    tool.verification.verified_fields = sorted({*tool.verification.verified_fields, *applied})
    tool.official_unverified_fields = list(facts.unverified_fields)
    return bool(applied)


class OfficialFactsRunner:
    def __init__(self, extractor: OfficialFactsExtractor, *, checkpoint_every: int = 25) -> None:
        self.extractor = extractor
        self.client = extractor.client
        if self.client is None:
            raise ValueError("OfficialFactsRunner requires an HTTP-backed OfficialFactsExtractor")
        self.checkpoint_every = max(1, int(checkpoint_every))

    def run(self, tools: Iterable[Tool], *, interim_dir: str | Path, limit: int | None = None) -> dict[str, Any]:
        interim = Path(interim_dir)
        interim.mkdir(parents=True, exist_ok=True)
        output = interim / OFFICIAL_FACTS_FILENAME
        report_path = interim / OFFICIAL_FACTS_REPORT_FILENAME
        state_path = interim / OFFICIAL_FACTS_STATE_FILENAME
        done = {str(row.get("key")) for row in read_jsonl(output) if isinstance(row, Mapping) and row.get("key")}
        candidates = [tool for tool in tools if eligible(tool)]
        processed = 0
        for tool in candidates:
            key = facts_key(tool.website)
            if not key or key in done:
                continue
            if limit is not None and processed >= max(0, limit):
                break
            row = self._extract(tool, key)
            append_jsonl(output, row)
            done.add(key)
            processed += 1
            if processed % self.checkpoint_every == 0:
                self._checkpoint(output, report_path, state_path, candidates, processed, finished=False)
        return self._checkpoint(output, report_path, state_path, candidates, processed, finished=True)

    def _extract(self, tool: Tool, key: str) -> dict[str, Any]:
        url = normalize_url(tool.website) or tool.website
        base = {"key": key, "official_url": url, "tool_id": tool.id, "tool_name": tool.name,
                "attempted_at": _now(), "provenance": SourceRef(name="Official product website", url=url, kind="official").to_dict()}
        try:
            fetched = self.client.try_fetch(url, use_cache=True)
        except Exception as exc:  # client failures must be checkpointed, not fatal
            return {**base, "outcome": "http_failure", "error": str(exc)[:500]}
        if fetched is None:
            return {**base, "outcome": "http_failure", "error": "request failed or blocked"}
        if not fetched.ok:
            return {**base, "outcome": "http_failure", "http_status": fetched.status, "final_url": fetched.final_url}
        if not (fetched.text or "").strip():
            return {**base, "outcome": "empty", "http_status": fetched.status, "final_url": fetched.final_url,
                    "facts": OfficialFacts(official_url=url, unverified_fields=["all"]).to_dict()}
        try:
            values = self.extractor(tool, fetched)
            facts = self.extractor.facts_by_tool.get(tool.id)
            if facts is None:
                return {**base, "outcome": "parsing_failure", "http_status": fetched.status, "final_url": fetched.final_url,
                        "error": "extractor returned no facts record"}
            return {**base, "outcome": "success" if facts.has_any_fact else "empty", "partial": bool(facts.unverified_fields),
                    "http_status": fetched.status, "final_url": fetched.final_url, "applied_fields": sorted(values), "facts": facts.to_dict()}
        except Exception as exc:
            return {**base, "outcome": "parsing_failure", "http_status": fetched.status, "final_url": fetched.final_url, "error": str(exc)[:500]}

    @staticmethod
    def _checkpoint(output: Path, report_path: Path, state_path: Path, candidates: list[Tool], processed: int, *, finished: bool) -> dict[str, Any]:
        rows = [row for row in read_jsonl(output) if isinstance(row, Mapping)]
        outcomes: dict[str, int] = {}
        partial = 0
        for row in rows:
            outcome = str(row.get("outcome") or "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            partial += bool(row.get("partial"))
        payload = {"eligible": len(candidates), "attempted": len(rows), "succeeded": outcomes.get("success", 0),
                   "partial": partial, "failed": sum(v for k, v in outcomes.items() if k in {"http_failure", "parsing_failure"}),
                   "empty": outcomes.get("empty", 0), "outcomes": dict(sorted(outcomes.items())), "processed_this_pass": processed,
                   "pending": max(0, len({facts_key(t.website) for t in candidates if facts_key(t.website)}) - len(rows)), "complete": finished,
                   "artefacts": {"official_facts": str(output), "state": str(state_path)}}
        write_json(report_path, payload)
        write_json(state_path, {"version": 1, "updated_at": _now(), **payload})
        return payload
