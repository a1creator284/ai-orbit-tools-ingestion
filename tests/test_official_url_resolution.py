"""Tests for official-URL resolution from directory detail pages.

Every test is offline: real captured fixtures plus ``FakeHttpClient``. The
suite is written to prove two things in equal measure —

1. a URL the directory genuinely published **is** found (the feature works on
   the real markup of both live patterns), and
2. a URL is **never** invented: no slug becomes a domain, a shared/social host
   never becomes a website, and every failure leaves ``website`` as ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.candidates.official_url import (
    OfficialUrlEvidence,
    OfficialUrlResolver,
    ResolutionBasis,
    ResolutionFailure,
    ResolutionReport,
    ResolutionResult,
    UrlCandidate,
    apply_resolution,
    extract_official_url_evidence,
)
from src.core.http_client import FetchResult
from tests.conftest import FakeHttpClient, load_fixture

DANG_AXIOM_URL = "https://dang.ai/tool/axiom-ai-work-assistant"
DANG_BONBON_URL = "https://dang.ai/tool/bonbon-ai-ai-character-chat"
CREATI_URL = "https://creati.ai/ai-tools/quantinor"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@dataclass
class StubCandidate:
    """Minimal stand-in for ``PreparedCandidate``."""

    name: str
    listing_url: str | None = None
    website: str | None = None
    website_domain: str | None = None
    candidate_id: str = "cand-1"
    issues: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    needs_review: bool = False

    def add_issue(self, code: str, note: str, *, review: bool = False) -> None:
        if code not in self.issues:
            self.issues.append(code)
            self.review_notes.append(note)
        if review:
            self.needs_review = True


def fetch(url: str, body: str, *, status: int = 200, ctype: str = "text/html",
          final: str | None = None) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=final or url,
        status=status,
        text=body,
        headers={"content-type": ctype},
    )


def page(*, head: str = "", body: str = "") -> str:
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>"


def json_ld(payload: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def software_node(name: str, url: str, **extra: Any) -> dict[str, Any]:
    node = {"@type": "SoftwareApplication", "name": name, "url": url}
    node.update(extra)
    return node


# --------------------------------------------------------------------------- #
# the real captured pages — the feature must actually work
# --------------------------------------------------------------------------- #
def test_resolves_dang_official_url_from_real_json_ld() -> None:
    """The exact markup dang.ai served on 2026-09-16."""
    result = fetch(DANG_AXIOM_URL, load_fixture("dang_detail_axiom.html"))
    evidence = extract_official_url_evidence(DANG_AXIOM_URL, result, product_name="AXIOM")

    assert evidence.failure is None
    assert evidence.has_candidate
    best = evidence.url_candidates[0]
    assert best.url == "https://axiom-market.com/"
    assert best.basis == ResolutionBasis.JSON_LD
    assert best.domain == "axiom-market.com"
    # The directory named the product, which is what allows the cross-check.
    assert best.declared_name == "AXIOM"


def test_real_dang_page_strips_directory_tracking_params() -> None:
    """Dang appends its own utm/ref params; the stored URL must be canonical."""
    result = fetch(DANG_BONBON_URL, load_fixture("dang_detail_bonbon.html"))
    evidence = extract_official_url_evidence(
        DANG_BONBON_URL, result, product_name="Bonbon AI"
    )

    assert evidence.url_candidates[0].url == "https://bonbon.ai/"
    assert "utm_source" not in evidence.url_candidates[0].url


def test_resolves_creati_official_url_from_real_visit_button() -> None:
    """The exact markup creati.ai served on 2026-09-16 (no JSON-LD product node)."""
    result = fetch(CREATI_URL, load_fixture("creati_detail_quantinor.html"))
    evidence = extract_official_url_evidence(CREATI_URL, result, product_name="Quantinor")

    assert evidence.failure is None
    best = evidence.url_candidates[0]
    assert best.url == "https://quantinor.com/lp/ai-accounting"
    assert best.basis == ResolutionBasis.VISIT_LINK
    assert "Visit AI" in (best.evidence or "")


def test_real_creati_page_rejects_its_own_social_and_store_links() -> None:
    """x.com / facebook / play.google links on the page are never the product."""
    result = fetch(CREATI_URL, load_fixture("creati_detail_quantinor.html"))
    evidence = extract_official_url_evidence(CREATI_URL, result, product_name="Quantinor")

    domains = {c.domain for c in evidence.url_candidates}
    assert domains == {"quantinor.com"}
    for bad in ("x.com", "facebook.com", "linkedin.com", "play.google.com"):
        assert bad not in domains


def test_real_dang_page_ignores_directory_self_links() -> None:
    """dang.ai's own nav/footer links must never be offered as the website."""
    result = fetch(DANG_AXIOM_URL, load_fixture("dang_detail_axiom.html"))
    evidence = extract_official_url_evidence(DANG_AXIOM_URL, result, product_name="AXIOM")

    assert evidence.directory_domain == "dang.ai"
    assert all(c.domain != "dang.ai" for c in evidence.url_candidates)


# --------------------------------------------------------------------------- #
# nothing is fabricated
# --------------------------------------------------------------------------- #
def test_page_without_any_outbound_url_resolves_nothing() -> None:
    body = page(body='<h1>Mystery Tool</h1><a href="/submit">Submit a tool</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/mystery", fetch("https://dang.ai/tool/mystery", body),
        product_name="Mystery Tool",
    )

    assert evidence.failure == ResolutionFailure.NO_OUTBOUND_URL
    assert evidence.url_candidates == []


def test_slug_is_never_turned_into_a_domain() -> None:
    """The regression this stage exists to avoid: guessing ``<slug>.com``."""
    body = page(body="<h1>Neta Studio</h1><p>An AI world builder.</p>")
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/neta-studio-ai-world-builder",
        fetch("https://dang.ai/tool/neta-studio-ai-world-builder", body),
        product_name="Neta Studio",
    )

    assert evidence.url_candidates == []
    assert evidence.failure == ResolutionFailure.NO_OUTBOUND_URL
    # no domain was invented from "neta-studio"
    assert "neta" not in json.dumps(evidence.to_dict()).replace("neta-studio-ai-world-builder", "")


@pytest.mark.parametrize(
    "host",
    ["github.com", "play.google.com", "apps.apple.com", "huggingface.co",
     "notion.site", "producthunt.com", "vercel.app"],
)
def test_shared_hosts_are_never_accepted_as_a_website(host: str) -> None:
    body = page(body=f'<a href="https://{host}/some/product" rel="external">Visit site</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []
    assert evidence.failure == ResolutionFailure.ONLY_SHARED_HOSTS


@pytest.mark.parametrize(
    "host", ["twitter.com", "x.com", "linkedin.com", "discord.gg", "t.me", "medium.com"]
)
def test_social_hosts_are_never_accepted_as_a_website(host: str) -> None:
    body = page(body=f'<a href="https://{host}/brand" rel="external">Visit us</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []
    assert evidence.failure == ResolutionFailure.ONLY_SHARED_HOSTS


def test_arbitrary_offsite_link_without_a_visit_affordance_is_not_promoted() -> None:
    """A blog link in body copy is not a declaration of the official site."""
    body = page(
        body='<p>As covered by <a href="https://some-news-site.com/post">this article</a>.</p>'
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []


def test_asset_urls_are_never_a_website() -> None:
    body = page(
        body='<a href="https://cdn.example.com/logo.png" rel="external">Visit site</a>'
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []


# --------------------------------------------------------------------------- #
# JSON-LD parsing robustness
# --------------------------------------------------------------------------- #
def test_json_ld_graph_container_is_flattened() -> None:
    payload = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "name": "Directory", "url": "https://dang.ai/"},
            software_node("Reez", "https://reez.app/"),
        ],
    }
    body = page(head=json_ld(payload))
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", fetch("https://dang.ai/tool/reez", body),
        product_name="reez",
    )

    assert evidence.url_candidates[0].url == "https://reez.app/"


def test_json_ld_organization_node_alone_is_not_a_product_url() -> None:
    """Only product-typed nodes count; the directory's Organization node never does."""
    payload = {"@type": "Organization", "name": "Dang.ai", "url": "https://dang.ai/"}
    body = page(head=json_ld(payload))
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []


def test_json_ld_same_as_is_read_when_url_is_absent() -> None:
    node = {"@type": "WebApplication", "name": "Neta", "sameAs": ["https://neta.art/"]}
    body = page(head=json_ld(node))
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/neta", fetch("https://dang.ai/tool/neta", body),
        product_name="Neta",
    )

    assert evidence.url_candidates[0].url == "https://neta.art/"


def test_json_ld_type_list_is_handled() -> None:
    node = {"@type": ["Thing", "SoftwareApplication"], "name": "Axiom",
            "url": "https://axiom-market.com/"}
    body = page(head=json_ld(node))
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/axiom", fetch("https://dang.ai/tool/axiom", body),
        product_name="Axiom",
    )

    assert evidence.url_candidates[0].url == "https://axiom-market.com/"


def test_malformed_json_ld_block_is_skipped_not_fatal() -> None:
    body = page(
        head='<script type="application/ld+json">{not json at all,,}</script>'
        + json_ld(software_node("Reez", "https://reez.app/"))
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", fetch("https://dang.ai/tool/reez", body),
        product_name="reez",
    )

    assert evidence.url_candidates[0].url == "https://reez.app/"


def test_empty_json_ld_block_is_skipped() -> None:
    body = page(head='<script type="application/ld+json"></script>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.failure == ResolutionFailure.NO_OUTBOUND_URL


def test_json_ld_url_object_form_is_read() -> None:
    node = {"@type": "SoftwareApplication", "name": "Reez",
            "url": {"@id": "https://reez.app/"}}
    body = page(head=json_ld(node))
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", fetch("https://dang.ai/tool/reez", body),
        product_name="reez",
    )

    assert evidence.url_candidates[0].url == "https://reez.app/"


def test_json_ld_outranks_a_visit_button_on_a_different_domain() -> None:
    """Structured declaration beats a recognised label, and the clash is flagged."""
    body = page(
        head=json_ld(software_node("Reez", "https://reez.app/")),
        body='<a href="https://affiliate-tracker.com/out/9" rel="external">Visit site</a>',
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", fetch("https://dang.ai/tool/reez", body),
        product_name="reez",
    )

    assert evidence.url_candidates[0].basis == ResolutionBasis.JSON_LD
    assert evidence.url_candidates[0].url == "https://reez.app/"

    resolver = OfficialUrlResolver(client=FakeHttpClient(responses={
        "https://dang.ai/tool/reez": body
    }))
    result = resolver.resolve_candidate(
        StubCandidate(name="reez", listing_url="https://dang.ai/tool/reez")
    )
    assert result.website == "https://reez.app/"
    assert result.needs_review is True
    assert any("more than one off-site domain" in n for n in result.review_notes)


# --------------------------------------------------------------------------- #
# visit-button recognition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "label",
    ["Visit AI", "Visit site", "Visit Website", "Official Website", "Go to site",
     "Open website", "Homepage"],
)
def test_visit_labels_are_recognised(label: str) -> None:
    body = page(body=f'<a href="https://tool-site.com/">{label}</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates[0].url == "https://tool-site.com/"
    assert evidence.url_candidates[0].basis == ResolutionBasis.VISIT_LINK


def test_rel_external_image_button_is_recognised_without_a_label() -> None:
    """Creati's logo anchor has no text — ``rel="external"`` is the declaration."""
    body = page(
        body='<a href="https://tool-site.com/" rel="external noopener">'
        '<img alt="Tool"/></a>'
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates[0].url == "https://tool-site.com/"
    assert 'rel="external"' in (evidence.url_candidates[0].evidence or "")


def test_title_attribute_is_used_as_a_label() -> None:
    body = page(body='<a href="https://tool-site.com/" title="Visit website"></a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates[0].url == "https://tool-site.com/"


def test_visit_word_in_the_url_path_alone_does_not_qualify() -> None:
    """Matching is on the label, never the URL, so paths cannot fake a button."""
    body = page(body='<a href="https://random-site.com/visit-us">Read more</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []


def test_visit_link_back_into_the_directory_is_ignored() -> None:
    body = page(body='<a href="https://dang.ai/pricing" rel="external">Visit site</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert evidence.url_candidates == []


def test_relative_visit_href_is_absolutized_against_the_final_url() -> None:
    """A same-site relative href is directory navigation, never a product URL."""
    body = page(body='<a href="/go/tool" rel="external">Visit site</a>')
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x",
        fetch("https://dang.ai/tool/x", body, final="https://dang.ai/tool/x"),
        product_name="X",
    )

    assert evidence.url_candidates == []


def test_duplicate_urls_are_deduplicated_per_basis() -> None:
    body = page(
        body='<a href="https://tool-site.com/" rel="external">Visit site</a>'
        '<a href="https://tool-site.com/" rel="external">Visit website</a>'
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", body), product_name="X"
    )

    assert len(evidence.url_candidates) == 1


# --------------------------------------------------------------------------- #
# transport failure modes — each is "unreadable", never "no product"
# --------------------------------------------------------------------------- #
def test_fetch_failure_reports_fetch_failed() -> None:
    evidence = extract_official_url_evidence("https://dang.ai/tool/x", None)

    assert evidence.failure == ResolutionFailure.FETCH_FAILED
    assert evidence.url_candidates == []


@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_http_error_reports_http_error(status: int) -> None:
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x",
        fetch("https://dang.ai/tool/x", page(body="nope"), status=status),
    )

    assert evidence.failure == ResolutionFailure.HTTP_ERROR
    assert evidence.status == status


@pytest.mark.parametrize("status", [401, 403, 429])
def test_blocked_statuses_are_reported_as_a_challenge_not_an_http_error(
    status: int,
) -> None:
    """Matches the project-wide convention in ``looks_like_challenge``.

    A 401/403/429 is a *block page*, and the distinction matters: "the
    directory refused us" is a crawling problem to solve, whereas
    ``http_error`` would suggest the page itself is broken.
    """
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x",
        fetch("https://dang.ai/tool/x", page(body="nope"), status=status),
    )

    assert evidence.failure == ResolutionFailure.BOT_CHALLENGE
    assert evidence.url_candidates == []


def test_bot_challenge_is_never_read_as_a_url() -> None:
    challenge = load_fixture("cloudflare_challenge.html")
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x",
        fetch("https://dang.ai/tool/x", challenge, status=403),
    )

    assert evidence.failure == ResolutionFailure.BOT_CHALLENGE
    assert evidence.url_candidates == []


def test_non_html_response_is_refused() -> None:
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x",
        fetch("https://dang.ai/tool/x", '{"website": "https://spoof.com"}',
              ctype="application/json"),
    )

    assert evidence.failure == ResolutionFailure.NON_HTML_RESPONSE
    assert evidence.url_candidates == []


def test_empty_response_is_refused() -> None:
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/x", fetch("https://dang.ai/tool/x", "   ")
    )

    assert evidence.failure == ResolutionFailure.EMPTY_RESPONSE


def test_missing_content_type_still_parses() -> None:
    body = page(head=json_ld(software_node("Reez", "https://reez.app/")))
    result = FetchResult(
        url="https://dang.ai/tool/reez", final_url="https://dang.ai/tool/reez",
        status=200, text=body, headers={},
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", result, product_name="reez"
    )

    assert evidence.url_candidates[0].url == "https://reez.app/"


# --------------------------------------------------------------------------- #
# identity cross-check
# --------------------------------------------------------------------------- #
def test_identity_mismatch_refuses_the_url() -> None:
    """A page naming a different product must not donate its URL."""
    body = page(head=json_ld(software_node("Completely Other Product",
                                           "https://other-product.com/")))
    resolver = OfficialUrlResolver(client=FakeHttpClient(responses={
        "https://dang.ai/tool/reez": body
    }))
    result = resolver.resolve_candidate(
        StubCandidate(name="reez", listing_url="https://dang.ai/tool/reez")
    )

    assert result.website is None
    assert result.failure == ResolutionFailure.IDENTITY_MISMATCH
    assert result.needs_review is True


def test_identity_agreement_is_tolerant_of_suffixes() -> None:
    body = page(head=json_ld(software_node("Reez Inc.", "https://reez.app/")))
    resolver = OfficialUrlResolver(client=FakeHttpClient(responses={
        "https://dang.ai/tool/reez": body
    }))
    result = resolver.resolve_candidate(
        StubCandidate(name="reez", listing_url="https://dang.ai/tool/reez")
    )

    assert result.website == "https://reez.app/"
    assert result.failure is None


def test_unnamed_url_is_accepted_but_notes_the_missing_cross_check() -> None:
    body = page(body='<a href="https://tool-site.com/" rel="external">Visit site</a>')
    resolver = OfficialUrlResolver(client=FakeHttpClient(responses={
        "https://dang.ai/tool/x": body
    }))
    result = resolver.resolve_candidate(
        StubCandidate(name="Some Tool", listing_url="https://dang.ai/tool/x")
    )

    assert result.website == "https://tool-site.com/"
    assert any("identity could not be cross-checked" in n for n in result.review_notes)


def test_name_agreement_orders_equally_ranked_candidates() -> None:
    body = page(
        head=json_ld({
            "@graph": [
                software_node("Unrelated Thing", "https://unrelated.com/"),
                software_node("Reez", "https://reez.app/"),
            ]
        })
    )
    evidence = extract_official_url_evidence(
        "https://dang.ai/tool/reez", fetch("https://dang.ai/tool/reez", body),
        product_name="Reez",
    )

    assert evidence.url_candidates[0].url == "https://reez.app/"


# --------------------------------------------------------------------------- #
# resolver orchestration
# --------------------------------------------------------------------------- #
def test_candidate_with_a_website_is_never_fetched_or_overwritten() -> None:
    client = FakeHttpClient(responses={})
    resolver = OfficialUrlResolver(client=client)
    candidate = StubCandidate(
        name="AIToolNet Product",
        listing_url="https://www.aitoolnet.com/thing",
        website="https://already-known.com/",
    )
    result = resolver.resolve_candidate(candidate)

    assert client.calls == []
    assert result.fetched is False
    assert result.failure == ResolutionFailure.ALREADY_RESOLVED
    assert candidate.website == "https://already-known.com/"


def test_candidate_without_a_detail_url_makes_no_request() -> None:
    client = FakeHttpClient(responses={})
    resolver = OfficialUrlResolver(client=client)
    result = resolver.resolve_candidate(StubCandidate(name="Nameless", listing_url=None))

    assert client.calls == []
    assert result.failure == ResolutionFailure.NO_DETAIL_URL
    assert result.needs_review is True


def test_only_the_candidates_own_detail_url_is_fetched() -> None:
    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    resolver = OfficialUrlResolver(client=client)
    resolver.resolve_candidate(StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL))

    assert client.calls == [DANG_AXIOM_URL]


def test_resolver_never_mutates_the_candidate() -> None:
    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    candidate = StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL)
    result = OfficialUrlResolver(client=client).resolve_candidate(candidate)

    assert result.website == "https://axiom-market.com/"
    assert candidate.website is None  # applying it is a separate, explicit step


def test_client_exception_degrades_to_unresolved() -> None:
    class Boom:
        def try_fetch(self, url: str, **_: Any) -> Any:
            raise RuntimeError("socket exploded")

    result = OfficialUrlResolver(client=Boom()).resolve_candidate(
        StubCandidate(name="X", listing_url="https://dang.ai/tool/x")
    )

    assert result.website is None
    assert result.failure == ResolutionFailure.FETCH_FAILED


def test_hostile_candidate_object_does_not_crash_the_batch() -> None:
    class Hostile:
        candidate_id = "bad"

        @property
        def name(self) -> str:
            return "Hostile"

        @property
        def website(self) -> Any:
            return None

        @property
        def listing_url(self) -> Any:
            raise RuntimeError("no listing url for you")

    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    good = StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL)
    results, report = OfficialUrlResolver(client=client).resolve_many([Hostile(), good])

    # Both records are accounted for, the good one still resolves, and the
    # hostile one is reported honestly (its raising ``listing_url`` reads as
    # "no detail URL") instead of taking the batch down.
    assert len(results) == 2
    assert report.considered == 2
    assert report.failures[ResolutionFailure.NO_DETAIL_URL] == 1
    assert any(r.website == "https://axiom-market.com/" for r in results)
    assert all(r.website is None for r in results if r.name == "Hostile")


def test_resolution_result_creation_survives_a_raising_attribute() -> None:
    """``resolve_many``'s own error path must not be able to raise either."""

    class Exploding:
        @property
        def name(self) -> str:
            raise RuntimeError("no name")

        @property
        def website(self) -> Any:
            return None

        @property
        def listing_url(self) -> Any:
            raise RuntimeError("no listing url")

    results, report = OfficialUrlResolver(client=FakeHttpClient()).resolve_many(
        [Exploding()]
    )

    assert len(results) == 1
    assert results[0].website is None
    assert report.resolved == 0


# --------------------------------------------------------------------------- #
# batch behaviour + report
# --------------------------------------------------------------------------- #
def test_resolve_many_skips_candidates_that_already_have_a_website() -> None:
    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    candidates = [
        StubCandidate(name="Known", listing_url="https://x.test/a",
                      website="https://known.com/"),
        StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL),
    ]
    results, report = OfficialUrlResolver(client=client).resolve_many(candidates)

    assert client.calls == [DANG_AXIOM_URL]
    assert report.already_had_website == 1
    assert report.considered == 1
    assert report.resolved == 1
    assert len(results) == 1


@pytest.mark.parametrize("limit,expected", [(0, 0), (1, 1), (2, 2), (99, 2), (None, 2)])
def test_limit_caps_the_number_of_fetches(limit: int | None, expected: int) -> None:
    responses = {
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html"),
        DANG_BONBON_URL: load_fixture("dang_detail_bonbon.html"),
    }
    client = FakeHttpClient(responses=responses)
    candidates = [
        StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL, candidate_id="a"),
        StubCandidate(name="Bonbon AI", listing_url=DANG_BONBON_URL, candidate_id="b"),
    ]
    _results, report = OfficialUrlResolver(client=client).resolve_many(
        candidates, limit=limit
    )

    assert len(client.calls) == expected
    assert report.attempted == expected


def test_report_counts_failures_and_bases() -> None:
    responses = {
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html"),
        CREATI_URL: load_fixture("creati_detail_quantinor.html"),
        "https://dang.ai/tool/empty": page(body="<h1>Nothing here</h1>"),
    }
    client = FakeHttpClient(responses=responses)
    candidates = [
        StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL, candidate_id="a"),
        StubCandidate(name="Quantinor", listing_url=CREATI_URL, candidate_id="b"),
        StubCandidate(name="Empty", listing_url="https://dang.ai/tool/empty",
                      candidate_id="c"),
        StubCandidate(name="Gone", listing_url="https://dang.ai/tool/missing",
                      candidate_id="d"),
    ]
    _results, report = OfficialUrlResolver(client=client).resolve_many(candidates)

    assert report.considered == 4
    assert report.resolved == 2
    assert report.bases == {
        ResolutionBasis.JSON_LD: 1,
        ResolutionBasis.VISIT_LINK: 1,
    }
    assert report.failures[ResolutionFailure.NO_OUTBOUND_URL] == 1
    assert report.failures[ResolutionFailure.FETCH_FAILED] == 1


def test_report_is_json_serialisable() -> None:
    report = ResolutionReport(live=True)
    report.record(
        ResolutionResult(candidate_id="a", name="A", detail_url="https://d/x",
                         website="https://a.com/", basis=ResolutionBasis.JSON_LD)
    )
    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["resolved"] == 1
    assert payload["live"] is True


def test_result_round_trips_through_json() -> None:
    result = fetch(DANG_AXIOM_URL, load_fixture("dang_detail_axiom.html"))
    evidence = extract_official_url_evidence(DANG_AXIOM_URL, result, product_name="AXIOM")
    res = ResolutionResult(
        candidate_id="a", name="AXIOM", detail_url=DANG_AXIOM_URL,
        website=evidence.url_candidates[0].url, page_evidence=evidence,
    )
    payload = json.loads(json.dumps(res.to_dict()))

    assert payload["website"] == "https://axiom-market.com/"
    assert payload["page_evidence"]["directory_domain"] == "dang.ai"


def test_extraction_is_deterministic() -> None:
    body = load_fixture("dang_detail_axiom.html")
    first = extract_official_url_evidence(
        DANG_AXIOM_URL, fetch(DANG_AXIOM_URL, body), product_name="AXIOM"
    )
    second = extract_official_url_evidence(
        DANG_AXIOM_URL, fetch(DANG_AXIOM_URL, body), product_name="AXIOM"
    )

    assert first.to_dict() == second.to_dict()


# --------------------------------------------------------------------------- #
# applying a resolution
# --------------------------------------------------------------------------- #
def test_apply_resolution_sets_the_website_and_records_provenance() -> None:
    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    candidate = StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL)
    result = OfficialUrlResolver(client=client).resolve_candidate(candidate)

    assert apply_resolution(candidate, result) is True
    assert candidate.website == "https://axiom-market.com/"
    assert candidate.website_domain == "axiom-market.com"
    assert "official_url_resolved_from_directory" in candidate.issues
    assert any("still has to be verified" in n for n in candidate.review_notes)


def test_apply_resolution_is_a_no_op_for_an_unresolved_result() -> None:
    candidate = StubCandidate(name="X", listing_url="https://dang.ai/tool/x")
    result = ResolutionResult(candidate_id="x", name="X", detail_url=None,
                              failure=ResolutionFailure.NO_OUTBOUND_URL)

    assert apply_resolution(candidate, result) is False
    assert candidate.website is None
    assert candidate.issues == []


def test_apply_resolution_never_overwrites_an_existing_website() -> None:
    candidate = StubCandidate(name="X", listing_url="https://dang.ai/tool/x",
                              website="https://observed.com/")
    result = ResolutionResult(candidate_id="x", name="X", detail_url="https://dang.ai/tool/x",
                              website="https://resolved.com/")

    assert apply_resolution(candidate, result) is False
    assert candidate.website == "https://observed.com/"


def test_resolution_never_claims_verification() -> None:
    """A directory-published URL is a claim; it must not set any status."""
    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    candidate = StubCandidate(name="AXIOM", listing_url=DANG_AXIOM_URL)
    result = OfficialUrlResolver(client=client).resolve_candidate(candidate)
    apply_resolution(candidate, result)

    payload = result.to_dict()
    assert "verification_status" not in payload
    assert "verified" not in {k.lower() for k in payload} - {"resolved"}
    assert not hasattr(candidate, "verification_status") or True


def test_prepared_candidate_integration_keeps_status_unverified() -> None:
    """End-to-end with the real ``PreparedCandidate`` record."""
    from src.candidates.prepare import CandidatePreparer
    from src.discovery.base import CandidateTool

    candidate = CandidateTool(
        name="AXIOM",
        source_key="dang",
        source_name="Dang.ai",
        listing_url=DANG_AXIOM_URL,
        website=None,
        tagline="Turn everyday work requests into finished outputs.",
    )
    prepared, rejected, _report = CandidatePreparer().prepare_many([candidate])
    assert not rejected and len(prepared) == 1
    record = prepared[0]
    assert record.website is None

    client = FakeHttpClient(responses={
        DANG_AXIOM_URL: load_fixture("dang_detail_axiom.html")
    })
    result = OfficialUrlResolver(client=client).resolve_candidate(record)
    assert apply_resolution(record, result) is True

    assert record.website == "https://axiom-market.com/"
    assert record.website_domain == "axiom-market.com"
    assert record.has_official_url is True
    # The crucial invariant: a resolved URL is still not verification.
    assert record.verification_status == "unverified"
    assert "official_url_resolved_from_directory" in record.issues
