#!/usr/bin/env python3
"""AI Orbit — Tools ingestion pipeline entry point.

Usage examples::

    python run.py --help
    python run.py sources                 # show the discovery-source registry
    python run.py selfcheck               # foundation sanity checks (no network)
    python run.py discover --limit 50     # discovery only, stores raw candidates
    python run.py prepare                 # prepare stored candidates (no network)
    python run.py verify                  # wiring/offline check, makes NO network calls
    python run.py verify --live --limit 10  # small real official-site spot check
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
from src.core.io import read_jsonl, write_json, write_jsonl
from src.core.logging_setup import setup_logging
from src.discovery.registry import SourceRegistry
from src.discovery.runner import DiscoveryRunner
from src.models.tool import Tool
from src.pipeline import ToolsPipeline
from src.scoring.filter import Outcome, QualityFilter
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
    breakdown = scorer.score(left)
    empty_score = breakdown.total
    checks.append(
        (
            "an unverified/empty record scores below the reject threshold",
            empty_score < settings.scoring.reject_below,
            f"score={empty_score}",
        )
    )
    checks.append(
        (
            "score is bounded to 0-100",
            0.0 <= empty_score <= 100.0,
            f"score={empty_score}",
        )
    )
    from src.scoring.rubric import COMPONENTS as RUBRIC_COMPONENTS

    checks.append(
        (
            "every rubric component is scored",
            set(breakdown.component_points()) == set(RUBRIC_COMPONENTS),
            f"components={len(breakdown.component_points())}",
        )
    )
    checks.append(
        (
            "scoring is deterministic",
            scorer.score(left).total == empty_score,
            f"repeat={scorer.score(left).total}",
        )
    )
    # Two distinct refusals, both of which must carry a recorded reason.
    # ``QualityScorer.score`` is deliberately pure, so the record above is
    # still *unscored*: filtering it must produce ``unscored``, not ``reject``.
    # A record that was actually scored and fell short must produce ``reject``.
    quality_filter = QualityFilter(settings.scoring)
    unscored_decision = quality_filter.decide(left.model_copy(deep=True))
    checks.append(
        (
            "an unscored record is refused with a recorded reason",
            unscored_decision.outcome == Outcome.UNSCORED
            and bool(unscored_decision.rejection_reason),
            f"outcome={unscored_decision.outcome} "
            f"reason={unscored_decision.rejection_reason}",
        )
    )
    low_scoring = scorer.apply(left.model_copy(deep=True))
    decision = quality_filter.decide(low_scoring)
    checks.append(
        (
            "a record below the reject threshold is rejected with a reason",
            decision.outcome == Outcome.REJECT and bool(decision.rejection_reason),
            f"outcome={decision.outcome} score={decision.score} "
            f"reason={decision.rejection_reason}",
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
    report = pipeline.run(
        limit_per_source=args.limit, verify_limit=args.verify_limit
    )
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

    Batches accumulate by default: a run adds to what is already stored and
    checkpoints to disk as it goes, so production discovery can be executed in
    small resumable slices. Pass ``--replace`` for a clean re-crawl.
    """
    settings = get_settings(reload=True)
    settings.paths.ensure()

    runner = DiscoveryRunner(
        settings,
        accumulate=not args.replace,
        checkpoint_every=args.checkpoint_every,
    )
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


def cmd_resolve_urls(args: argparse.Namespace) -> int:
    """Resolve official URLs for stored raw candidates from their detail pages.

    Reads ``data/raw/candidates.jsonl`` (never rewriting it) and writes the
    enriched dataset plus a report to ``data/interim/``. Offline unless
    ``--live`` is passed, resumable by default, and it fetches *directory
    detail pages only* — never the products' own sites.

    A resolved URL is a directory claim, not verification.
    """
    from src.candidates.resolution import OfficialUrlResolutionRunner
    from src.discovery.registry import load_source_configs

    settings = get_settings(reload=True)
    settings.paths.ensure()

    input_path = Path(args.input) if args.input else settings.paths.resolve("raw") / "candidates.jsonl"
    if not input_path.exists():
        print(
            f"No candidate feed at {input_path}. Run `python run.py discover` first — "
            "nothing is fabricated when discovery produced no data.",
            file=sys.stderr,
        )
        return 1

    # Reuse the politeness already declared per source rather than inventing a
    # new rate here.
    host_rates: dict[str, float] = {}
    try:
        from urllib.parse import urlsplit

        for config in load_source_configs().values():
            target = config.homepage or config.listing_url
            host = urlsplit(target or "").netloc
            if host:
                host_rates[host] = config.rate_limit_rps
    except Exception:  # noqa: BLE001 - politeness config is best-effort
        host_rates = {}

    runner = OfficialUrlResolutionRunner(
        live=bool(args.live),
        checkpoint_every=args.checkpoint_every,
        host_rates=host_rates,
    )
    report = runner.run(
        input_path,
        interim_dir=settings.paths.resolve("interim"),
        limit=args.limit,
        resume=not args.restart,
        sources=set(args.source) if args.source else None,
    )
    print(json.dumps(report.to_dict(), indent=2))
    if not args.live:
        print(
            "\nOffline pass: no network calls were made. Pass --live to read the "
            "directory detail pages for real.",
            file=sys.stderr,
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify prepared candidates against their official websites.

    Reads ``data/interim/candidates_prepared.jsonl`` (falling back to
    preparing the stored discovery artefacts when it is absent), runs the
    official-site verifier, and writes
    ``data/interim/candidates_verified.jsonl`` plus a verification report.

    Network access is **opt-in**: without ``--live`` the stage runs through an
    offline HTTP client and makes no network calls at all, which is how the
    wiring is exercised safely. Use ``--limit`` to keep the first live pass a
    small spot check (10–20 candidates) rather than a bulk crawl.
    """
    from src.candidates.prepare import INTERIM_FILENAME, CandidatePreparer
    from src.candidates.store import load_discovery_dir
    from src.verification.store import load_prepared_candidates

    settings = get_settings(reload=True)
    settings.paths.ensure()

    interim = settings.paths.resolve("interim")
    prepared, source = load_prepared_candidates(interim / INTERIM_FILENAME)
    if not prepared:
        # No prepared artefact yet: prepare the stored discovery candidates in
        # memory rather than asking the user to re-run a previous stage.
        candidates, _ = load_discovery_dir(settings.paths.resolve("raw"))
        prepared, _rejected, _report = CandidatePreparer().prepare_many(candidates)
        source = "data/raw/discovery/ (prepared in memory)"

    if not prepared:
        print(json.dumps({"prepared_candidates": 0, "verified": 0}, indent=2))
        print(
            "\nNo prepared candidates found. Run `python run.py discover` and "
            "`python run.py prepare` first — nothing is fabricated when there "
            "is no input.",
            file=sys.stderr,
        )
        return 1

    pipeline = ToolsPipeline(settings)
    _results, report = pipeline.verify_candidates(
        prepared,
        live=args.live,
        limit=args.limit,
        persist=not args.no_persist,
    )

    stage = pipeline.report.stage("verify_candidates")
    print(
        json.dumps(
            {
                "input_source": source,
                "prepared_candidates": len(prepared),
                "live": args.live,
                "limit": args.limit,
                "considered": stage.details.get("considered"),
                "with_official_url": stage.details.get("with_official_url"),
                "report": report.to_dict(),
                "artefacts": stage.details.get("artefacts", {}),
            },
            indent=2,
        )
    )
    if not args.live:
        print(
            "\nOffline pass: no network calls were made, so nothing could be "
            "verified from real page evidence. Re-run with `--live --limit 10` "
            "for a small real spot check.",
            file=sys.stderr,
        )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Re-score, threshold-filter and re-rank an existing processed dataset.

    Runs the same three stages the pipeline does, in the same order:
    score (100-point rubric) → threshold filter (§5 bands) → select best N.
    Every dropped record keeps its score, its band and its reason, written to
    ``score_decisions.jsonl`` next to the output, so nothing disappears
    silently and the batch is never padded to reach the target.
    """
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

    kept, decisions, filter_report = QualityFilter(settings.scoring).filter(tools)
    selected, not_selected = rank_and_select(
        kept,
        target_size=settings.batch.target_size,
        max_per_primary_task=settings.batch.max_per_primary_task,
    )
    output = Path(args.output or settings.paths.resolve("final") / "tools.json")
    write_json(output, [tool.to_dict() for tool in selected])
    decisions_path = output.parent / "score_decisions.jsonl"
    write_jsonl(decisions_path, [decision.to_dict() for decision in decisions])
    print(
        json.dumps(
            {
                "input_records": len(tools),
                "schema_errors": errors,
                "threshold_filter": filter_report.to_dict(),
                "above_threshold": len(kept),
                "selected": len(selected),
                "not_selected": len(not_selected),
                "output": str(output),
                "decisions": str(decisions_path),
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
    run_parser.add_argument(
        "--verify-limit",
        type=int,
        default=None,
        help="max candidates sent to official-site verification",
    )
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
    discover_parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "overwrite the stored artefact for each source instead of "
            "accumulating into it (default is accumulate + resume)"
        ),
    )
    discover_parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="flush the source artefact to disk every N new candidates (0=off)",
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

    resolve_parser = sub.add_parser(
        "resolve-urls",
        help="resolve official URLs for stored candidates from directory detail pages",
        description=(
            "Reads data/raw/candidates.jsonl (never modifies it) and writes "
            "data/interim/candidates_resolved.jsonl plus a resolution report. "
            "Offline by default: pass --live to read the directory detail "
            "pages. Resumable — rerun to continue where the last pass stopped. "
            "A resolved URL is a directory claim, not verification."
        ),
    )
    resolve_parser.add_argument(
        "--input", default=None, help="candidate feed (default data/raw/candidates.jsonl)"
    )
    resolve_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED to fetch detail pages; without it the stage makes no "
            "network calls and only records what it would have read"
        ),
    )
    resolve_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max detail pages to fetch in this pass (the rest stay pending)",
    )
    resolve_parser.add_argument(
        "--source", action="append", default=None, help="source key (repeatable)"
    )
    resolve_parser.add_argument(
        "--restart",
        action="store_true",
        help="discard the existing enriched artefact instead of resuming it",
    )
    resolve_parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="refresh the state file every N written records",
    )
    resolve_parser.set_defaults(func=cmd_resolve_urls)

    verify_parser = sub.add_parser(
        "verify",
        help="verify prepared candidates against their official websites",
        description=(
            "Official-website verification. Offline by default: pass --live to "
            "allow real network calls, and keep the first live pass small with "
            "--limit 10."
        ),
    )
    verify_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED for real network verification; without it the stage runs "
            "fully offline and makes no network calls"
        ),
    )
    verify_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max candidates to verify (use 10-20 for the first live spot check)",
    )
    verify_parser.add_argument(
        "--no-persist", action="store_true", help="do not write artefacts"
    )
    verify_parser.set_defaults(func=cmd_verify)

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
