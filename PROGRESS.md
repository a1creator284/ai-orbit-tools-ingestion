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

### Run #6 — persist + wire verification (this run)

**Unit 6.1 — `verify_candidates` stage, persistence and CLI**
Connected the already-tested verification layer into the production pipeline.
**No verification decision rule was changed**: `verifier.py`'s policy
(`_decide`, evidence extraction, failure codes) is untouched and reused
verbatim — this unit is wiring, persistence and safety gating only.

* **New stage** `ToolsPipeline.verify_candidates(prepared, *, live, limit,
  persist)` in `src/pipeline.py`, registered in `STAGES` between
  `candidate_preparation` and `normalization` (asserted by a test), and called
  from `run()` in that position. It consumes `PreparedCandidate` records and
  delegates straight to the existing
  `OfficialSiteVerifier.verify_candidates()`; no verification logic is
  duplicated.
* **Network access is opt-in, enforced at the socket, not by a branch.** New
  `src/core/http_client.OfflineHttpClient` is an `HttpClient`-shaped client
  that cannot fetch: `try_fetch` returns `None` (the real client's documented
  graceful-degradation contract) and `fetch` raises `FetchError`.
  `_verifier_for(live=...)` swaps it in whenever `--live` is absent, so
  "offline" is guaranteed in the one place that could open a connection rather
  than re-checked in every stage. It also records the URLs a live run *would*
  have fetched, which is what makes the no-network claim testable.
* **`--limit`** caps how many candidates are processed, so the first live pass
  is a deliberate 10–20 candidate spot check instead of a bulk crawl. The
  stage records `considered` and `skipped_by_limit` for the audit trail.
* **New persistence layer `src/verification/store.py`:**
  * `data/interim/candidates_verified.jsonl` — one row per candidate;
  * `data/interim/candidates_verification_report.json` — auditable pass
    summary (`VerificationReport` plus `live`, `limit`, `input_candidates`,
    `considered`, artefact paths);
  * **discovery provenance and official evidence are separate blocks.** Each
    row carries `discovery` (which directory saw it, listing URL, source keys,
    `SourceRef`s, observation time, identity basis, preparation issues) and
    `verification` (official URL, final URL, HTTP status, identity/product
    signals, evidence notes, failure codes, reason, `SourceRef(kind="official")`).
    Tests assert neither block leaks fields into the other, so "who claimed
    this?" and "what did we confirm?" can never be conflated;
  * every row records `live`, so an offline pass can never be mistaken for a
    real verification;
  * `persist_verification` **refuses to write into `data/final/`** and raises
    `VerificationError` — verified *candidates* are interim data, and the
    published dataset stays a curation decision;
  * `load_prepared_candidates` rehydrates `candidates_prepared.jsonl`
    resiliently: a malformed line or a row missing the guaranteed identity
    fields is skipped, never patched with a placeholder.
* **`python run.py verify`** — new subcommand:
  * `--live` is **required** for any real network verification; without it the
    pass runs through `OfflineHttpClient` and makes zero requests (asserted by
    a test that makes `requests.Session.request` raise);
  * `--limit N` for the spot check, `--no-persist` to skip artefacts;
  * reads the prepared artefact, falling back to preparing the stored
    discovery candidates in memory, and reports 0/0 without fabricating
    anything when there is no input;
  * `run` gained `--verify-limit` so the full pipeline can cap the same stage.
* Fixed a latent bug found while wiring: `_persist` **assigned**
  `report.artefacts`, which would have discarded artefacts registered by
  earlier stages; it now `update`s.
* **Missing official URLs make no substitute request** — inherited from
  `verify_candidate` and re-asserted at the pipeline level: the listing URL is
  never fetched as a stand-in.

---

### Run #7 — first live verification spot check (this run)

**Unit 7.1 — live spot check + robots.txt transport fix**

The first contact with the live web. Command, run exactly twice (before and
after the fix), never widened:

```
python run.py verify --live --limit 10
```

> **Live-web observations below are not test results.** Everything in this
> subsection describes what 10 real websites did on 2026-09-15. Live results are
> inherently non-reproducible: sites change, CDNs behave differently per IP and
> per UA. Deterministic guarantees come only from the offline suite (see
> **Tests**), which is why the fix below is pinned by offline fixtures rather
> than by re-running the live pass.

**Live pass A — before the fix** (10 attempted, **4 actually fetched**):

| | |
|---|---|
| `status_counts` | `verified: 4`, `unreachable: 6` |
| `failure_counts` | `fetch_failed: 6` |

Classification of the 6 failures — **none** were genuine dead, parked or
inaccessible product sites. All 6 answered `HTTP 200` when checked
independently with `curl`, in under 3 s. No `thin_content`, no `bot_challenge`,
no redirect/off-domain issue, and no genuine identity or product-signal failure
was involved. That mismatch — "did not respond" for a site that answers 200 —
is what made this a bug rather than live-web noise.

**Genuine reproducible bug found (transport layer, not verification policy).**
Root cause in `src/core/http_client.HttpClient._robots_allows`: it used
`urllib.robotparser.RobotFileParser.read()`, which fetches `robots.txt` with
the default `Python-urllib/x.y` user agent. Those hosts' CDNs answer that UA
with **403**; `read()` swallows the error and sets `disallow_all = True`. So:

* the client refused to send the request at all (0 entries parsed,
  `can_fetch() == False`), even though each site's real `robots.txt` contains
  `Allow: /` — confirmed by re-fetching it with our own UA, where
  `can_fetch()` returns `True` for every one of the 6;
* `fetch()` raised `blocked by robots.txt`, `try_fetch()` degraded to `None`,
  and the verifier — which cannot distinguish *why* a fetch returned nothing —
  recorded `fetch_failed` with the reason *"official website did not respond
  (timeout/network failure after retries)"*. **That reason was factually
  false**: no network failure and no timeout ever happened, and the ~50 ms
  per-candidate failure time proved it (three retries with backoff cannot
  complete that fast);
* it also contradicted the function's own documented intent,
  `unreachable robots => allow`.

Fix — the smallest one that addresses the cause, in the transport layer only:
new `HttpClient._fetch_robots()` reads `robots.txt` through the client's **own
session** (same UA the rules are then evaluated against) and treats a response
that carries no usable rules (4xx/5xx, network error, unparseable) as *no
restriction found* rather than as a site-wide prohibition — 4xx meaning
unrestricted access is also what RFC 9309 specifies.

**No verification decision rule was changed.** `src/verification/verifier.py`
is untouched: `_decide`, the evidence extraction, the identity and
product-affordance rules and every `VerificationFailure` code are byte-for-byte
as they were. `thin_content`, `bot_challenge`, CDN behaviour and temporary
network problems were explicitly *not* treated as evidence that the verifier is
wrong — and in fact none of them occurred in this pass.

**Live pass B — after the fix** (10 attempted, **10 actually fetched**):

| | |
|---|---|
| `status_counts` | `verified: 9`, `partially_verified: 1` |
| `failure_counts` | `no_product_signal: 1` |

`unreachable`, `unverified` and `failed` all dropped to 0; `needs_review` fell
from 6 to 1. The 9 verified records each earned identity evidence from a
page-level brand slot plus at least one same-site affordance actually present
on the page (`pricing_link`, `signup_link`, `login_link`, `docs_link`,
`app_link`, `download_link`).

The single remaining failure was **inspected and judged correct, not a bug**:
`imagetoprompt.org` returns `HTTP 200` with strong identity (7 signals) but its
landing page's only affordance-shaped link points **off-site**
(`minmail.app`) — verified by hand against the live HTML. Off-site links are
deliberately not evidence, so identity-without-affordance correctly yields
`partially_verified` + review instead of `verified`. Conservative under-claiming
is the intended behaviour.

Not done, by design: no wider run (`--limit` stayed at 10), no TAAFT/Creati
crawling, no directory URL used as a substitute verification URL, and no
relaxation of a verification rule to make a difficult live page pass.

---

## Tests

| Suite | Count |
|---|---|
| Before Run #4 | 75 passed |
| After Unit 4.1 (`tests/test_candidate_store.py`, +18) | 93 passed |
| After Units 4.2 + 4.3 (`test_candidate_prepare.py` +28, `test_logging_setup.py` +6) | 127 passed |
| After Unit 5.1 (`tests/test_verification_official_site.py`, +41) | 168 passed |
| After Unit 6.1 (`tests/test_verification_pipeline_wiring.py`, +38) | 206 passed |
| After Unit 7.1 (`tests/test_http_robots.py`, +15) | **221 passed** |

**Run #7 deterministic results** (as opposed to the live observations above):
the robots.txt bug is pinned by 15 new offline tests in
`tests/test_http_robots.py`, driven by a `FakeSession` and the captured
`tests/fixtures/robots_allow_root.txt` (a real-world `Allow: /` file from one
of the sites that was wrongly skipped). No sockets, fully reproducible.

They cover the bug itself (a 403 on `robots.txt` no longer blocks the page
request; `robots.txt` is fetched with the configured UA, asserted not to be a
`urllib` UA; `Allow: /` permits the landing page) and — just as important —
that compliance was **not** weakened: a real `Disallow: /api/` rule still
blocks and sends no request, a site-wide `Disallow: /` still blocks,
`respect_robots_txt: false` skips the lookup entirely, rules are fetched once
per origin, 401/404/410/500/503, network errors, empty and unparseable
`robots.txt` all mean "no restriction found", and a blocked URL still degrades
to `None` rather than to a fabricated response — so a genuinely disallowed page
can never reach the verifier as evidence.

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

New Run #6 wiring tests (38) cover: stage position in `STAGES` and in an actual
run, consumption of prepared candidates through the existing verifier, the
listing URL never being fetched as a substitute, run-report stage statistics,
`--live` gating (offline client selected by default, real client only when
live, injected verifier always winning, zero fetches and zero verifications
offline, `OfflineHttpClient` recording intent and refusing `fetch`), `--limit`
(cap, `None`, `0`, over-large), persistence (JSONL + report under `interim`,
nothing in `data/final/`, `VerificationError` when asked to write there,
discovery/verification block separation in both directions, `live` flag on
every row, `--no-persist`, JSON round-trip, hostile-record resilience),
prepared-candidate reload (round-trip, missing file, malformed rows skipped)
and the CLI (subcommand registration, `--live` default false and opt-in,
`--limit` parsing, `run --verify-limit`, an offline CLI run asserted to open no
socket, limit applied end to end, interim-only persistence, missing input
reported honestly).

Run #6 offline CLI smoke check (`python run.py verify --limit 15`) over the
real stored artefacts: 200 prepared, 15 considered, 15 with an official URL,
**0 fetched, 0 verified**, 15 `unreachable` (`fetch_failed`) — and `data/final/`
untouched. No live verification run has been performed.

All tests are offline: HTML fixtures + `FakeHttpClient`/`RecordingClient`/
`OfflineHttpClient`, and `tmp_path` JSONL fixtures for the stores. The CLI
offline test additionally makes `HttpClient` construction fatal, so a network
leak fails loudly instead of passing silently; the CLI smoke check was
re-executed with `socket.socket.connect` patched to raise and completed
cleanly. No network access, no live directory calls.

---

## GitHub

* Run #3 checkpoint: `d29da83`
* Run #4 Unit 4.1: `79857d7` — candidate store
* Run #4 Units 4.2 + 4.3: pushed to `origin/main` (candidate preparation +
  logging fix)
* Run #4 checkpoint: `96361eb`
* Run #5 Unit 5.1: official-website verification
* Run #5 checkpoint: `74cd440`
* Run #6 Unit 6.1: persist + wire official-site verification
* Run #6 checkpoint: `16cb0af`
* Run #7 Unit 7.1: first live verification spot check + robots.txt transport
  fix (this commit). `data/interim/candidates_verified.jsonl` and
  `candidates_verification_report.json` are committed as the preserved
  evidence of live pass B (`live: true` on every row).

---

## Known limitations

* Official-website verification has now been run live **exactly once, over 10
  candidates** (Run #7). The stored artefacts are that 10-row spot check, not a
  full dataset: 190 of the 200 prepared candidates have never been verified.
  Widening (`--limit 50`, then `200`) is the next calibration step.
* A 10-site sample is too small to conclude the decision rules are correctly
  calibrated. Notably **zero** `thin_content`, `bot_challenge`,
  `off_domain_redirect` and `dead_site_marker` outcomes occurred, so those
  branches still have no live evidence behind them — only offline fixtures.
  Expect JS-rendered landing pages and CDN challenges to appear at larger
  limits; per the rule above, those are not by themselves proof that the
  verifier is wrong.
* The robots.txt fix means the crawler now reaches sites it previously skipped,
  so **live pass A's 6 `fetch_failed` results should not be cited as evidence
  about those products** — they were an artefact of our own client.
* The `Tool`-level `ToolsPipeline.verify` stage still skips while
  `dry_run: true`; candidate-level verification (`verify_candidates`) is the
  wired stage. Feeding verified candidate evidence into the normalized `Tool`
  records is the next integration step, not part of this unit.
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

**Widen the live verification pass to `--limit 50`, then calibrate.** The
10-candidate spot check is done (Run #7) and the transport bug it exposed is
fixed, so the next pass is about *volume of evidence*, not new machinery:

1. run `python run.py verify --live --limit 50` and read the report the same
   way: classify each failure as genuine (dead/parked/no product) or as an
   artefact of meeting the live web (JS-rendered `thin_content`, CDN
   `bot_challenge`). Only a failure reproduced on a real page, with evidence
   that the *rule* is wrong, justifies touching `_decide`;
2. pay particular attention to the branches the 10-site sample never exercised
   (`thin_content`, `bot_challenge`, `off_domain_redirect`,
   `dead_site_marker`), and to whether `partially_verified` stays a small
   minority — a large share would suggest the product-affordance rule is too
   narrow for JS-rendered landing pages, which *would* be a real calibration
   finding;
3. then widen to `--limit 200`, keeping every pass under an explicit limit —
   still no bulk crawl;
4. then feed verified candidate evidence into the normalized `Tool` records, so
   `verification_status`, `last_verified_date` and the official `SourceRef`
   reach the records that scoring and validation consume;
5. only after that, official-page field extraction (pricing/features) through
   the existing extractor injection point — still no LLM, no guessing.
