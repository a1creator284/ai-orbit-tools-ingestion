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

### Run #4 — candidate processing

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

### Run #5 — official-website verification (this run)

**Unit 5.1 — official-site verification (`src/verification/verifier.py`)**
Completed and hardened the only stage allowed to promote a record out of
`unverified`, and only from evidence fetched from the product's **own** site.

* `extract_official_evidence(url, FetchResult, product_name=...)` — a **pure**
  function (no network) producing an `OfficialPageEvidence` record: transport
  facts (status, final URL, content type, redirect/off-domain flags) plus
  values literally read off the page (title, `og:site_name`, meta description,
  `h1`/`h2`, logo `alt`, same-site product links, dead-site markers). Nothing
  is inferred or defaulted.
* **Only the official URL is ever fetched.** A TAAFT/Creati listing URL is a
  discovery pointer and is never fetched as a substitute, never counted as
  verification. No official URL → *no HTTP call at all* (asserted in tests).
* Safe handling of every failure mode, each with a stable `VerificationFailure`
  code: `fetch_failed` (timeout/retries exhausted via the existing
  `HttpClient.try_fetch` graceful-degradation contract), `http_error`,
  `bot_challenge` (reuses `extraction.html.looks_like_challenge`, so challenge
  markup can never become evidence), `non_html_response`, `empty_response`,
  `unparseable_html`, `thin_content`, `dead_site_marker`,
  `off_domain_redirect`, `identity_not_confirmed`, `no_product_signal`.
* **Identity** must be confirmed from a page-level brand slot (title,
  `og:title`/`og:site_name`, main heading, logo `alt`, meta description, or a
  domain named after the product *plus* the name in body text) — conservative
  canonicalized matching, whole-token for very short names.
* **Product existence/functionality** is evidenced only by *same-site*
  affordances actually on the page (pricing, signup, login, docs/API, download,
  demo/playground, app/dashboard, or an email/password form). Off-site links
  (e.g. a Twitter profile) are explicitly not evidence.
* Status is **granted, never assumed** (`_decide`, one readable policy
  function): `verified` needs identity **and** a product affordance;
  identity-only or an off-domain redirect gives `partially_verified` + review;
  unreadable pages (challenge, non-HTML, empty, thin, unparseable) stay
  `unverified` + review — unreadable ≠ non-existent, so those are *not*
  rejected; dead/parked/HTTP-error sites become `unreachable` and reject the
  Tool (`website_broken` / `dead_or_shutdown`).
* **Provenance preserved**: official URL, final URL, HTTP status, content type,
  `checked_at`, ordered concise evidence notes, failure codes and a single
  concise `reason` per outcome; a `SourceRef(kind="official")` is attached
  *only* when a status was actually earned.
* **Discovery provenance stays separate**: `verify_candidate()` returns an
  immutable `VerificationResult` and never mutates the candidate, so "which
  directory saw it" and "what its own site proves" can never be conflated.
* `verify_candidates()` + `VerificationReport` give an auditable batch pass;
  one hostile record (even a property that raises) is reported, not fatal.
* Deterministic: injectable clock (`now=`), injectable HTTP client; identical
  input produces byte-identical `to_dict()` output (asserted).
* Tool path (`verify()`) rewritten on the same evidence engine: it now fetches
  the official page **once** and hands that exact response to injected
  extractors, so no field can be sourced from a page that was not verified.
  Nothing is invented — launch date, pricing, features, company, integrations,
  capabilities and usage stay blank and are declared in `unverifiable_fields`.
* `src/verification/__init__.py` now exports the stage's public API.

---

## Tests

| Suite | Count |
|---|---|
| Before Run #4 | 75 passed |
| After Unit 4.1 (`tests/test_candidate_store.py`, +18) | 93 passed |
| After Units 4.2 + 4.3 (`test_candidate_prepare.py` +28, `test_logging_setup.py` +6) | 127 passed |
| After Unit 5.1 (`tests/test_verification_official_site.py`, +41) | **168 passed** |

Offline smoke check against the existing `data/raw/discovery/` artefacts
(200 real candidates): 200 loaded / 0 skipped, 200 prepared / 0 rejected,
186 with an official URL, 14 identified by listing URL only, 18 flagged for
review. No bulk run performed.

Run #5 offline smoke check over the same 200 prepared candidates with a
deliberately offline client: **exactly 186 fetch attempts** — one per candidate
that has an official URL, and zero for the 14 that do not. All 186 became
`unreachable` (`fetch_failed`), the 14 became `failed` (`no_official_url`), and
**0** were verified. Verification cannot be earned without a real response.

New verification tests cover: success, same-site redirect, off-domain redirect,
timeout/network failure, HTTP 404/410/500/502, parked domain, non-HTML body,
empty body, thin page, synthetic *and* real captured Cloudflare challenge
pages, HTTP 403 block page, missing official URL (with the assertion that the
listing URL is not fetched), unrelated page (identity not confirmed),
identity-without-affordance, off-site links not counted as evidence, batch
reporting, JSON round-trip, determinism and hostile-record resilience.

All tests are offline: HTML fixtures + `FakeHttpClient`, and `tmp_path` JSONL
fixtures for the candidate store. No network access, no live directory calls.

---

## GitHub

* Run #3 checkpoint: `d29da83`
* Run #4 Unit 4.1: `79857d7` — candidate store
* Run #4 Units 4.2 + 4.3: pushed to `origin/main` (candidate preparation +
  logging fix)
* Run #4 checkpoint: `96361eb`
* Run #5 Unit 5.1: official-website verification (this commit)

---

## Known limitations

* Official-website verification is implemented and tested offline, but has
  **not** been run against live websites yet: `ToolsPipeline.verify` still
  skips the stage while `dry_run: true`. No verification artefact is persisted
  under `data/` yet either (see next step).
* Verification currently reads only the official **landing** page. Pricing,
  feature and launch-date extraction from deeper official pages is not
  implemented — those fields therefore stay blank by design.
* No LLM enrichment (deliberate — editorial descriptions come later, built only
  from verified facts).
* `taaft` is bot-protected in practice; it reports
  `blocked_by_bot_challenge` and emits zero candidates rather than fabricating
  any.
* No bulk 1,000-tool run has been performed, by design.

---

## Exact next recommended step

**Persist and wire the verification stage.** Specifically:

1. add `verify_candidates` to `src/pipeline.py` between
   `prepare_candidates` and `normalize`, running
   `OfficialSiteVerifier.verify_candidates` over the prepared candidates and
   recording `VerificationReport` in the run report;
2. persist the outcome to `data/interim/candidates_verified.jsonl` (+
   `candidates_verification_report.json`) via `write_jsonl`/`write_json`,
   keeping verified records out of `data/final/`;
3. add a `python run.py verify` subcommand (with an explicit `--live` flag, so
   network access is always opt-in) plus a small `--limit` so the first real
   run can be a 10–20 candidate spot check, not a bulk crawl;
4. only then consider official-page field extraction (pricing/features), which
   must feed the existing extractor injection point — still with no LLM and no
   guessing.
