#!/usr/bin/env python3
"""AI Orbit — Tools ingestion pipeline entry point.

Usage examples::

    python run.py --help
    python run.py sources                 # show the discovery-source registry
    python run.py selfcheck               # foundation sanity checks (no network)
    python run.py discover --limit 50     # discovery only, stores raw candidates
    python run.py prepare                 # prepare stored candidates (no network)
    python run.py run --dry-run           # full pipeline, no network discovery
    python run.py score --input data/processed/tools.jsonl

Run #1 note: no discovery adapter is enabled yet, so ``run`` executes every
stage over an empty (or injected) candidate set. This is intentional — the
foundation is validated before any bulk collection begins.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.core.config import get_settings
from src.core.io import read_jsonl, write_json
from src.core.logging_setup import setup_logging
from src.discovery.registry import SourceRegistry
from src.discovery.runner import DiscoveryRunner
from src.models.tool import Tool
from src.pipeline import ToolsPipeline
from src.scoring.scorer import QualityScorer, rank_and_select
from src.validation.validator import ToolValidator


def cmd_sources(args: argparse.Namespace) -> int:
    """Print the configured discovery sources and their implementation state."""
    registry = SourceRegistry()
    summary = registry.summary()
    print(json.dumps(summary, indent=2))
    print("\nPrimary (tier-1) sources — reviewed systematically first (guideline §2):")
    for config in registry.primary_sources(enabled_only=False):
        state = "enabled" if config.enabled else "configured (adapter pending)"
        print(f"  - {config.name}: {config.listing_url or config.homepage} [{state}]")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """Foundation sanity checks that require no network access."""
    from src.cleaning.normalizer import ToolNormalizer
    from src.core.ids import make_entity_id
    from src.core.urls import normalize_url
    from src.deduplication.deduplicator import Deduplicator
    from src.discovery.base import CandidateTool

    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []

    total = sum(settings.scoring.weights.values())
    checks.append(("scoring weights sum to 100", total == 100, f"total={total}"))

    url_a = normalize_url("HTTP://WWW.Example.com:80/Path/?utm_source=x&b=2#frag")
    checks.append(
        ("url normalization", url_a == "https://example.com/Path?b=2", f"got {url_a}")
    )

    id_a, key_a = make_entity_id("tool", name="Jasper AI", website="https://www.jasper.ai/pricing")
    id_b, _ = make_entity_id("tool", name="Jasper", website="http://jasper.ai")
    checks.append(("ID determinism across variants", id_a == id_b, f"{id_a} vs {id_b}"))
    checks.append(("identity basis is the official domain", key_a.basis == "website_domain", key_a.basis))

    normalizer = ToolNormalizer(batch_number=settings.batch.batch_number)
    left = normalizer.from_candidate(
        CandidateTool(
            name="Jasper",
            source_key="taaft",
            source_name="There's An AI For That",
            listing_url="https://theresanaiforthat.com/ai/jasper/",
            website="https://www.jasper.ai/",
        )
    )
    right = normalizer.from_candidate(
        CandidateTool(
            name="Jasper AI",
            source_key="futurepedia",
            source_name="Futurepedia",
            listing_url="https://www.futurepedia.io/tool/jasper",
            website="https://jasper.ai/pricing",
        )
    )
    deduped, report = Deduplicator().deduplicate([left, right])
    checks.append(
        (
            "cross-directory duplicate collapses to one record",
            len(deduped) == 1 and deduped[0].dedup.merged_source_count >= 2,
            f"output={len(deduped)} sources={len(deduped[0].discovery_sources) if deduped else 0}",
        )
    )

    scorer = QualityScorer(settings.scoring)
    empty_score = scorer.score(left).total
    checks.append(
        (
            "an unverified/empty record scores below the reject threshold",
            empty_score < settings.scoring.reject_below,
            f"score={empty_score}",
        )
    )

    pipeline = ToolsPipeline(settings)
    checks.append(
        ("pipeline initializes", pipeline.report.batch_number == settings.batch.batch_number, "ok")
    )

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, passed, detail in checks:
        flag = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"[{flag}] {name.ljust(width)}  {detail}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 0 if failures == 0 else 1


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the full pipeline."""
    settings = get_settings(reload=True)
    if args.dry_run:
        settings.dry_run = True
    if args.live:
        settings.dry_run = False
    if args.target:
        settings.batch.target_size = args.target

    pipeline = ToolsPipeline(settings)
    report = pipeline.run(limit_per_source=args.limit)
    print(json.dumps(report.to_dict(), indent=2))
    if report.selected_count == 0:
        print(
            "\nNo tools were selected. Expected in Run #1: no discovery adapter is "
            "enabled yet (see PROGRESS.md for the next step).",
            file=sys.stderr,
        )
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Run the discovery layer only and persist raw candidates.

    Raw candidates land in ``data/raw/discovery/<source>.jsonl`` with full
    provenance; the merged, exact-duplicate-free feed lands in
    ``data/raw/candidates.jsonl`` for the normalization pipeline.
    """
    settings = get_settings(reload=True)
    settings.paths.ensure()

    runner = DiscoveryRunner(settings)
    kwargs: dict[str, object] = {}
    if args.max_pages is not None:
        kwargs["max_pages"] = args.max_pages
    if args.max_categories is not None:
        kwargs["max_categories"] = args.max_categories
    if args.no_categories:
        kwargs["include_categories"] = False
    if args.no_cache:
        kwargs["use_cache"] = False

    candidates, report = runner.run(
        source_keys=args.source or None,
        limit_per_source=args.limit,
        persist=not args.no_persist,
        **kwargs,
    )
    print(json.dumps(report.to_dict(), indent=2))

    if not candidates:
        print(
            "\nNo candidates were discovered. Check the per-source 'outcome' and "
            "'stop_reasons' above — a blocked source reports "
            "'blocked_by_bot_challenge' and never fabricates data.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    """Prepare stored discovery candidates without re-crawling.

    Reads the immutable per-source artefacts in ``data/raw/discovery/``,
    normalizes + identity-validates each candidate, and writes the explicitly
    *unverified* result to ``data/interim/candidates_prepared.jsonl``.

    Prepared candidates are **not** verified tools: official-website
    verification is a separate, later stage.
    """
    from src.candidates.prepare import CandidatePreparer
    from src.candidates.store import load_discovery_dir

    settings = get_settings(reload=True)
    settings.paths.ensure()

    candidates, load_report = load_discovery_dir(
        settings.paths.resolve("raw"), source_keys=args.source or None
    )
    if not candidates:
        print(
            json.dumps({"loaded": load_report.to_dict(), "prepared": 0}, indent=2)
        )
        print(
            "\nNo stored candidates found. Run `python run.py discover` first — "
            "nothing is fabricated when discovery produced no data.",
            file=sys.stderr,
        )
        return 1

    preparer = CandidatePreparer()
    prepared, rejected, report = preparer.prepare_many(candidates)
    if not args.no_persist:
        report = preparer.persist(
            prepared,
            report,
            interim_dir=settings.paths.resolve("interim"),
            rejected=rejected,
        )
    print(json.dumps({"loaded": load_report.to_dict(), **report.to_dict()}, indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Re-score and re-rank an existing processed dataset."""
    settings = get_settings()
    path = Path(args.input)
    tools: list[Tool] = []
    errors = 0
    for payload in read_jsonl(path):
        tool, error = ToolValidator.validate_schema(payload)
        if tool is None:
            errors += 1
            continue
        tools.append(tool)

    scorer = QualityScorer(settings.scoring)
    for tool in tools:
        scorer.apply(tool)
    selected, not_selected = rank_and_select(
        tools,
        target_size=settings.batch.target_size,
        max_per_primary_task=settings.batch.max_per_primary_task,
    )
    output = Path(args.output or settings.paths.resolve("final") / "tools.json")
    write_json(output, [tool.to_dict() for tool in selected])
    print(
        json.dumps(
            {
                "input_records": len(tools),
                "schema_errors": errors,
                "selected": len(selected),
                "not_selected": len(not_selected),
                "output": str(output),
            },
            indent=2,
        )
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a dataset against the schema and business rules."""
    validator = ToolValidator()
    tools: list[Tool] = []
    schema_errors: list[str] = []
    for payload in read_jsonl(Path(args.input)):
        tool, error = ToolValidator.validate_schema(payload)
        if tool is None:
            schema_errors.append(str(error)[:300])
        else:
            tools.append(tool)
    results, summary = validator.validate_many(tools)
    summary["schema_errors"] = len(schema_errors)
    print(json.dumps(summary, indent=2))
    if args.output:
        write_json(args.output, [r.to_dict() for r in results])
    return 0 if not schema_errors and summary["blocked"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="AI Orbit Tools ingestion pipeline"
    )
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="show the discovery-source registry").set_defaults(
        func=cmd_sources
    )
    sub.add_parser("selfcheck", help="run foundation sanity checks (no network)").set_defaults(
        func=cmd_selfcheck
    )

    run_parser = sub.add_parser("run", help="execute the full pipeline")
    run_parser.add_argument("--dry-run", action="store_true", help="skip all network stages")
    run_parser.add_argument("--live", action="store_true", help="allow network verification")
    run_parser.add_argument("--limit", type=int, default=None, help="max candidates per source")
    run_parser.add_argument("--target", type=int, default=None, help="override batch target size")
    run_parser.set_defaults(func=cmd_run)

    discover_parser = sub.add_parser(
        "discover", help="run discovery only and store raw candidates"
    )
    discover_parser.add_argument(
        "--source", action="append", default=None, help="source key (repeatable)"
    )
    discover_parser.add_argument("--limit", type=int, default=None, help="max candidates per source")
    discover_parser.add_argument("--max-pages", type=int, default=None, help="page cap per listing")
    discover_parser.add_argument(
        "--max-categories", type=int, default=None, help="category listings per source"
    )
    discover_parser.add_argument(
        "--no-categories", action="store_true", help="main listing only"
    )
    discover_parser.add_argument("--no-cache", action="store_true", help="bypass the HTTP cache")
    discover_parser.add_argument(
        "--no-persist", action="store_true", help="do not write artefacts"
    )
    discover_parser.set_defaults(func=cmd_discover)

    prepare_parser = sub.add_parser(
        "prepare",
        help="normalize + identity-validate stored candidates (no network)",
    )
    prepare_parser.add_argument(
        "--source", action="append", default=None, help="source key (repeatable)"
    )
    prepare_parser.add_argument(
        "--no-persist", action="store_true", help="do not write artefacts"
    )
    prepare_parser.set_defaults(func=cmd_prepare)

    score_parser = sub.add_parser("score", help="re-score an existing dataset")
    score_parser.add_argument("--input", default="data/processed/tools.jsonl")
    score_parser.add_argument("--output", default=None)
    score_parser.set_defaults(func=cmd_score)

    validate_parser = sub.add_parser("validate", help="validate a dataset")
    validate_parser.add_argument("--input", default="data/processed/tools.jsonl")
    validate_parser.add_argument("--output", default=None)
    validate_parser.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, force=bool(args.log_level))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
