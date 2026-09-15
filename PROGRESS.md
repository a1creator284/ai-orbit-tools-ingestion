# AI Orbit — Tools ingestion: progress log

Scope: **Tools module only**. Repositories and Videos are out of scope.

---

## Completed

### Run #1 — foundation
`src/core` (config, logging, http client, ids, io, text, urls), `src/models`
(Tool schema, enums, base entity), and the stage skeletons for cleaning,
verification, deduplication, scoring, enrichment, validation plus
`src/pipeline.py` and the `run.py` CLI.

### Run #2 — discovery + extraction
HTML extraction helpers (`src/extraction/html.py`), the shared paginated
listing base (`src/discovery/html_source.py`), `creati` and `taaft` adapters,
the source registry, and `DiscoveryRunner` with immutable raw artefacts under
`data/raw/discovery/`.

### Run #3 — hardened discovery identity + pagination
`src/discovery/identity.py` (host + product-path identity, deliberately
under-merging) and `src/discovery/pagination.py` guards (bot-challenge
detection, repeat-page and empty-page stop reasons).

### Run #4 — candidate processing (this run)

**Unit 4.1 — candidate store (`src/candidates/store.py`)**
Resilient rehydration of persisted discovery candidates so any later stage can
resume from `data/raw/discovery/` without re-crawling.

* `candidate_from_dict` — rebuilds one `CandidateTool`; skips (never patches)
  records with no name, no pointer, or a non-mapping shape.
* `load_candidate_file` / `load_discovery_dir` — per-source loading; a missing
  file or directory is reported, not fatal; malformed JSONL lines are skipped.
* `CandidateLoadReport` — machine-readable, JSON-serialisable audit trail
  (`records_seen`, `candidates_loaded`, `skip_reasons`, `skipped_examples`,
  `field_repairs`).
* Provenance (`raw_signals`, `raw_payload`, `discovered_at`) is preserved
  verbatim; an unparseable timestamp becomes `None` and is reported rather than
  silently replaced with "now".
* Per-source artefacts are read instead of the merged feed, so every sighting
  survives and cross-source merging stays explicit in
  `src.discovery.runner.merge_candidates`.

**Unit 4.2 — candidate preparation (`src/candidates/prepare.py`)**
Turns loaded candidates into stable, explicitly **unverified**
`PreparedCandidate` records. 1-in → 1-out: preparation never merges (exact
merging stays in `discovery.runner`, fuzzy resolution in `src.deduplication`).

* deterministic `candidate_id` from the strongest identity signal available
  (official domain > product-URL domain > name+company > name), with
  `identity_basis` + `identity_confidence` recorded;
* normalizes only what a directory may legitimately supply: display name,
  `name_key`, slug, website, `website_domain`, listing URL, categories
  (case-insensitive de-duplication, stable order) and the observed tagline
  (kept verbatim, never rewritten);
* validates required identity — name plus at least one resolvable pointer —
  rejecting with an explainable `RejectReason` rather than patching;
* a weak identity or a missing official URL is **flagged for review**, not
  dropped (discovery prefers under-merging over losing a real product);
* `CandidateIssue` codes record what is missing/uncertain without repairing it
  (`no_description_observed`, `no_categories_observed`,
  `weak_identity_name_only`, `name_may_contain_a_tagline`,
  `directory_sponsored_placement`, …), each with a human-readable note;
* provenance preserved: every contributing source becomes a `SourceRef`,
  `source_keys` lists all contributors, `raw_signals` pass through untouched;
* `verification_status` is always `"unverified"`; no pricing, launch-date,
  status or feature fields exist at this stage at all;
* deterministic (injectable clock) and resilient — one broken candidate is
  rejected with `preparation_failed`, the batch continues;
* persisted to `data/interim/candidates_prepared.jsonl` (+ rejected feed and
  report) — deliberately **never** to `data/final/`.

**Unit 4.3 — logging bug fix (`src/core/logging_setup.py`)**
Found a real pre-existing bug while testing resilience: `logger.warning(...,
extra={"name": ...})` raises `KeyError: "Attempt to overwrite 'name' in
LogRecord"`. Five call sites were affected (`cleaning/normalizer.py` ×2,
`pipeline.py`, `candidates/store.py`, `candidates/prepare.py`) — all on
error-handling paths, so a *handled* per-record failure became an unhandled
crash. Fixed centrally with `SafeExtraLogger`, which renames a colliding
`extra` key to `ctx_<key>` instead of raising, preserving the context.

---

## Tests

| Suite | Count |
|---|---|
| Before Run #4 | 75 passed |
| After Unit 4.1 (`tests/test_candidate_store.py`, +18) | 93 passed |
| After Units 4.2 + 4.3 (`test_candidate_prepare.py` +28, `test_logging_setup.py` +6) | **127 passed** |

Offline smoke check against the existing `data/raw/discovery/` artefacts
(200 real candidates): 200 loaded / 0 skipped, 200 prepared / 0 rejected,
186 with an official URL, 14 identified by listing URL only, 18 flagged for
review. No bulk run performed.

All tests are offline: HTML fixtures + `FakeHttpClient`, and `tmp_path` JSONL
fixtures for the candidate store. No network access, no live directory calls.

---

## GitHub

* Run #3 checkpoint: `d29da83`
* Run #4 Unit 4.1: `79857d7` — candidate store
* Run #4 Units 4.2 + 4.3: pushed to `origin/main` (candidate preparation +
  logging fix)

---

## Known limitations

* Official-website verification is still not implemented for real: the
  `verification` stage is skipped while `dry_run: true`. A directory listing is
  **not** verification.
* No LLM enrichment (deliberate — editorial descriptions come later, built only
  from verified facts).
* `taaft` is bot-protected in practice; it reports
  `blocked_by_bot_challenge` and emits zero candidates rather than fabricating
  any.
* No bulk 1,000-tool run has been performed, by design.

---

## Exact next recommended step

Wire the candidate stage into the orchestrator: add a `prepare_candidates`
stage to `src/pipeline.py` (between `discovery` and `normalization`) plus a
`python run.py prepare` subcommand that runs
`load_discovery_dir` → `CandidatePreparer.prepare_many` → `persist`, records
`PreparationReport` in the run report, and lets `normalize` consume
`data/interim/candidates_prepared.jsonl`. After that, the next stage is
real official-website verification (`src/verification/verifier.py`) driven by
`PreparedCandidate.website`, promoting `verification_status` only on evidence
actually fetched from the official site.
