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

---

## Tests

| Suite | Count |
|---|---|
| Before Run #4 | 75 passed |
| After Unit 4.1 (`tests/test_candidate_store.py`, 18 new) | 93 passed |

All tests are offline: HTML fixtures + `FakeHttpClient`, and `tmp_path` JSONL
fixtures for the candidate store. No network access, no live directory calls.

---

## GitHub

* Run #3 checkpoint: `d29da83`
* Run #4 Unit 4.1 checkpoint: see the commit that added
  `src/candidates/store.py` (pushed to `origin/main`).

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

Add `src/candidates/prepare.py`: a `CandidatePreparer` that takes loaded
candidates and produces explicit, auditable `PreparedCandidate` records
(normalized name/URL/categories/description, required-identity validation,
`verification_status = "unverified"`, preserved provenance), persisted to
`data/interim/candidates_prepared.jsonl` — keeping discovery candidates clearly
separated from verified final tools in `data/final/tools.json`.
