"""Official-website verification tests.

Contract under test:

* only the **official** URL is ever fetched — a directory/listing URL is never
  fetched and never grants verification;
* no official URL → no network call at all;
* ``verified`` requires evidence *actually present on the fetched page*
  (identity in a brand slot + a real product affordance);
* redirects, HTTP errors, timeouts, non-HTML bodies, empty/thin bodies and
  Cloudflare-style challenge pages are all handled without ever inventing a
  verified status;
* verification provenance (URL, final URL, timestamp, HTTP status, concise
  evidence/reason) is preserved;
* nothing is invented: launch date, pricing, features, company, usage and
  capabilities are untouched by this stage.

Entirely offline: the fake HTTP client + literal HTML fixtures. No live calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from src.core.http_client import FetchResult
from src.models.enums import RejectionReason, ToolStatus, VerificationStatus
from src.models.tool import Tool
from src.verification.verifier import (
    MIN_CONTENT_CHARS,
    LivenessChecker,
    OfficialSiteVerifier,
    VerificationFailure,
    extract_official_evidence,
    resolve_conflict,
)

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
OFFICIAL = "https://jasper.ai/"  # already normalized (normalize_url drops "www.")
LISTING = "https://theresanaiforthat.com/ai/jasper/"


# ----------------------------------------------------------------- fixtures
def _filler(chars: int = MIN_CONTENT_CHARS + 200) -> str:
    """Realistic body copy so a page clears the thin-content floor."""
    sentence = (
        "Jasper helps marketing teams draft, edit and repurpose campaign copy "
        "with brand voice controls and collaborative review. "
    )
    return sentence * (chars // len(sentence) + 1)


def official_page(
    *,
    title: str = "Jasper — AI copilot for marketing teams",
    heading: str = "Jasper builds on-brand marketing content",
    links: tuple[str, ...] = (
        '<a href="/pricing">Pricing</a>',
        '<a href="/signup">Get started free</a>',
        '<a href="/login">Log in</a>',
        '<a href="/docs">Documentation</a>',
    ),
    body: str | None = None,
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <title>{title}</title>
  <meta name="description" content="Jasper is an AI copilot for marketing teams." />
  <meta property="og:site_name" content="Jasper" />
</head>
<body>
  <header><img src="/jasper-logo.svg" alt="Jasper logo" class="logo" /></header>
  <nav>{''.join(links)}</nav>
  <h1>{heading}</h1>
  <p>{body if body is not None else _filler()}</p>
</body>
</html>"""


CHALLENGE_PAGE = """<!doctype html>
<html><head><title>Just a moment...</title></head>
<body><div id="cf-browser-verification">
Enable JavaScript and cookies to continue
</div></body></html>"""

PARKED_PAGE = f"""<!doctype html>
<html><head><title>jasper.ai</title></head>
<body><h1>This domain may be for sale</h1><p>{_filler()}</p></body></html>"""

#: Body copy that never mentions the product — used for identity negatives.
_GENERIC_FILLER = (
    "Acme Hosting runs managed virtual private servers in twelve regions with "
    "hourly billing, automated snapshots and round-the-clock support. "
) * 6

UNRELATED_PAGE = f"""<!doctype html>
<html><head><title>Acme Hosting — cheap VPS</title>
<meta name="description" content="Managed VPS hosting." />
<meta property="og:site_name" content="Acme Hosting" /></head>
<body><h1>Acme Hosting plans</h1>
<nav><a href="/pricing">Pricing</a><a href="/login">Log in</a></nav>
<p>{_GENERIC_FILLER}</p></body></html>"""


@dataclass
class StubClient:
    """Fake HTTP client with per-URL control over the whole response.

    ``responses`` maps a URL to a :class:`FetchResult`, or to ``None`` to model
    the real client's graceful degradation (timeout / retries exhausted).
    """

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def try_fetch(self, url: str, **_: Any) -> FetchResult | None:
        self.calls.append(url)
        return self.responses.get(url)

    def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        result = self.try_fetch(url, **kwargs)
        if result is None:
            from src.core.errors import FetchError

            raise FetchError(f"no stub for {url}", url=url)
        return result

    def head(self, url: str, **kwargs: Any) -> FetchResult | None:
        return self.try_fetch(url, **kwargs)

    def close(self) -> None:  # pragma: no cover - parity with the real client
        return None


def response(
    url: str,
    text: str,
    *,
    status: int = 200,
    final_url: str | None = None,
    content_type: str = "text/html; charset=utf-8",
) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        text=text,
        headers={"content-type": content_type},
    )


@dataclass
class FakeCandidate:
    """Minimal :class:`PreparedCandidate`-shaped record."""

    candidate_id: str = "cand-1"
    name: str = "Jasper"
    website: str | None = OFFICIAL
    listing_url: str | None = LISTING


def make_tool(**overrides: Any) -> Tool:
    """A ``Tool`` with the deterministic id the schema requires."""
    kwargs: dict[str, Any] = {
        "id": "11111111-1111-5111-8111-111111111111",
        "name": "Jasper",
        "website": OFFICIAL,
    }
    kwargs.update(overrides)
    return Tool(**kwargs)


def verifier(responses: dict[str, Any]) -> tuple[OfficialSiteVerifier, StubClient]:
    client = StubClient(responses=responses)
    return OfficialSiteVerifier(client=client, now=FIXED_NOW), client  # type: ignore[arg-type]


# ================================================================= success
class TestSuccessfulVerification:
    def test_verified_from_official_page_evidence(self) -> None:
        verify, client = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.VERIFIED
        assert result.verified is True
        assert result.identity_confirmed is True
        assert "pricing_link" in result.product_signals
        assert "signup_link" in result.product_signals
        assert client.calls == [OFFICIAL]

    def test_only_the_official_url_is_fetched(self) -> None:
        """A directory listing is discovery, never verification."""
        verify, client = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        verify.verify_candidate(FakeCandidate())

        assert LISTING not in client.calls
        assert all("theresanaiforthat" not in call for call in client.calls)

    def test_provenance_is_preserved(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        result = verify.verify_candidate(FakeCandidate())

        assert result.official_url == OFFICIAL
        assert result.final_url == OFFICIAL
        assert result.http_status == 200
        assert result.checked_at == FIXED_NOW.isoformat()
        assert result.content_type is not None
        assert result.reason
        assert any(note.startswith("HTTP 200") for note in result.evidence_notes)

        source = result.verification_source
        assert source is not None
        assert source.kind == "official"
        assert source.is_official is True
        assert source.retrieved_at == FIXED_NOW

    def test_evidence_records_only_what_is_on_the_page(self) -> None:
        evidence = extract_official_evidence(
            OFFICIAL,
            response(OFFICIAL, official_page()),
            product_name="Jasper",
            now=FIXED_NOW,
        )
        assert evidence.title is not None and "Jasper" in evidence.title
        assert evidence.site_name == "Jasper"
        assert evidence.headings[0].startswith("Jasper builds")
        assert "name_in_title" in evidence.identity_signals
        assert evidence.detected_status == ToolStatus.ACTIVE
        # Nothing about pricing amounts, launch dates or user counts is invented.
        assert not hasattr(evidence, "pricing")
        assert not hasattr(evidence, "launch_date")

    def test_tool_verification_records_the_outcome(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        subject = make_tool()

        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.VERIFIED
        assert subject.verification.http_status == 200
        assert subject.verification.official_url_checked == OFFICIAL
        assert subject.verification.checked_at == FIXED_NOW
        assert subject.verification.is_accessible is True
        assert subject.last_verified_date == FIXED_NOW.date()
        assert any(s.is_official for s in subject.verification.verification_sources)
        assert subject.rejected is False

    def test_tool_verification_invents_nothing(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        subject = make_tool()

        verify.verify(subject)

        assert subject.launch_date is None
        assert subject.pricing.model is None
        assert subject.key_features == []
        assert subject.company is None
        assert subject.integrations == []
        assert subject.ai_capabilities == []
        # ...and the blanks are declared rather than silently dropped.
        assert "launch_date" in subject.verification.unverifiable_fields
        assert "pricing.model" in subject.verification.unverifiable_fields

    def test_extractors_receive_the_verified_response_only(self) -> None:
        seen: list[str] = []

        def extractor(tool: Tool, fetched: FetchResult) -> dict[str, Any]:
            seen.append(fetched.url)
            return {"short_description": "AI copilot for marketing teams"}

        verify, client = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        subject = make_tool()
        verify.verify(subject, extractors=[extractor])

        assert seen == [OFFICIAL]
        assert client.calls == [OFFICIAL], "the official page must be fetched once"
        assert subject.short_description == "AI copilot for marketing teams"
        assert "short_description" in subject.verification.verified_fields


# ================================================================ redirects
class TestRedirects:
    def test_same_site_redirect_still_verifies(self) -> None:
        final = "https://jasper.ai/home"
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, official_page(), final_url=final)}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.VERIFIED
        assert result.final_url == final
        assert any("redirected to" in note for note in result.evidence_notes)

    def test_off_domain_redirect_is_flagged_not_verified(self) -> None:
        final = "https://brandnew.io/"
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, official_page(), final_url=final)}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        assert VerificationFailure.OFF_DOMAIN_REDIRECT in result.failures
        assert result.needs_review is True
        assert result.final_url == final

    def test_off_domain_redirect_flags_the_tool_for_review(self) -> None:
        verify, _ = verifier(
            {
                OFFICIAL: response(
                    OFFICIAL, official_page(), final_url="https://brandnew.io/"
                )
            }
        )
        subject = make_tool()
        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.PARTIALLY_VERIFIED
        assert subject.needs_human_review is True
        assert subject.rejected is False


# ======================================================== errors / timeouts
class TestTransportFailures:
    def test_timeout_is_unreachable_not_verified(self) -> None:
        verify, client = verifier({OFFICIAL: None})  # try_fetch degrades to None
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNREACHABLE
        assert result.verified is False
        assert result.fetched is False
        assert VerificationFailure.FETCH_FAILED in result.failures
        assert result.needs_review is True
        assert client.calls == [OFFICIAL]

    @pytest.mark.parametrize("status", [404, 410, 500, 502])
    def test_http_errors_are_unreachable(self, status: int) -> None:
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, official_page(), status=status)}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNREACHABLE
        assert result.http_status == status
        assert VerificationFailure.HTTP_ERROR in result.failures
        assert f"HTTP {status}" in result.reason

    def test_http_error_rejects_the_tool_as_broken(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, "gone", status=404)})
        subject = make_tool()
        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.UNREACHABLE
        assert subject.rejected is True
        assert RejectionReason.WEBSITE_BROKEN in subject.rejection_reasons

    def test_parked_domain_is_rejected_as_dead(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, PARKED_PAGE)})
        subject = make_tool()
        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.UNREACHABLE
        assert subject.rejected is True
        assert VerificationFailure.DEAD_SITE_MARKER in [
            *extract_official_evidence(
                OFFICIAL, response(OFFICIAL, PARKED_PAGE), product_name="Jasper"
            ).failures
        ]

    def test_non_html_response_cannot_verify(self) -> None:
        verify, _ = verifier(
            {
                OFFICIAL: response(
                    OFFICIAL, "%PDF-1.7 binary junk", content_type="application/pdf"
                )
            }
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert VerificationFailure.NON_HTML_RESPONSE in result.failures
        assert result.needs_review is True

    def test_empty_body_cannot_verify(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, "   ")})
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert VerificationFailure.EMPTY_RESPONSE in result.failures


# ========================================================== challenge pages
class TestChallengePages:
    def test_cloudflare_challenge_is_never_verification(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, CHALLENGE_PAGE)})
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert result.verified is False
        assert VerificationFailure.BOT_CHALLENGE in result.failures
        assert result.needs_review is True
        assert "challenge" in (result.reason or "")

    def test_challenge_markup_yields_no_evidence(self) -> None:
        evidence = extract_official_evidence(
            OFFICIAL,
            response(OFFICIAL, CHALLENGE_PAGE),
            product_name="Jasper",
            now=FIXED_NOW,
        )
        assert evidence.is_challenge is True
        assert evidence.identity_signals == []
        assert evidence.product_signals == []
        assert evidence.title is None
        assert evidence.is_accessible is False

    def test_real_cloudflare_fixture_is_detected(self) -> None:
        """The interstitial captured from a live source must never verify."""
        from tests.conftest import load_fixture

        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, load_fixture("cloudflare_challenge.html"))}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert VerificationFailure.BOT_CHALLENGE in result.failures

    def test_403_block_page_is_a_challenge(self) -> None:
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, "<html>Access denied</html>", status=403)}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert VerificationFailure.BOT_CHALLENGE in result.failures

    def test_challenged_tool_is_flagged_not_rejected(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, CHALLENGE_PAGE)})
        subject = make_tool()
        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.UNVERIFIED
        assert subject.is_verified is False
        assert subject.rejected is False, "unreadable ≠ non-existent"
        assert subject.needs_human_review is True
        assert subject.last_verified_date is None


# ===================================================== no official url path
class TestMissingOfficialUrl:
    def test_no_official_url_means_no_fetch(self) -> None:
        verify, client = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        result = verify.verify_candidate(FakeCandidate(website=None))

        assert client.calls == [], "nothing may be fetched without an official URL"
        assert result.status == VerificationStatus.FAILED
        assert result.verified is False
        assert VerificationFailure.NO_OFFICIAL_URL in result.failures
        assert result.needs_review is True

    def test_listing_url_is_not_a_substitute(self) -> None:
        verify, client = verifier({LISTING: response(LISTING, official_page())})
        result = verify.verify_candidate(FakeCandidate(website=None))

        assert LISTING not in client.calls
        assert result.verified is False

    def test_tool_without_a_website_is_unverifiable(self) -> None:
        verify, client = verifier({})
        subject = make_tool(website=None)
        verify.verify(subject)

        assert client.calls == []
        assert subject.verification.status == VerificationStatus.FAILED
        assert RejectionReason.FAKE_OR_UNVERIFIABLE in subject.rejection_reasons


# ================================================== insufficient evidence
class TestInsufficientEvidence:
    def test_thin_page_cannot_verify(self) -> None:
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, official_page(body="Jasper."))}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert VerificationFailure.THIN_CONTENT in result.failures
        assert result.needs_review is True

    def test_unrelated_page_fails_identity(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, UNRELATED_PAGE)})
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.UNVERIFIED
        assert result.identity_confirmed is False
        assert VerificationFailure.IDENTITY_NOT_CONFIRMED in result.failures
        assert "identity" in (result.reason or "")

    def test_identity_without_product_affordance_is_partial(self) -> None:
        verify, _ = verifier(
            {OFFICIAL: response(OFFICIAL, official_page(links=('<a href="/blog">Blog</a>',)))}
        )
        result = verify.verify_candidate(FakeCandidate())

        assert result.status == VerificationStatus.PARTIALLY_VERIFIED
        assert result.identity_confirmed is True
        assert VerificationFailure.NO_PRODUCT_SIGNAL in result.failures
        assert result.needs_review is True

    def test_offsite_links_are_not_product_evidence(self) -> None:
        evidence = extract_official_evidence(
            OFFICIAL,
            response(
                OFFICIAL,
                official_page(
                    links=(
                        '<a href="https://twitter.com/jasper">Twitter</a>',
                        '<a href="https://other.example.com/pricing">Pricing</a>',
                    )
                ),
            ),
            product_name="Jasper",
            now=FIXED_NOW,
        )
        assert evidence.product_signals == []

    def test_unverified_tool_keeps_fields_blank(self) -> None:
        verify, _ = verifier({OFFICIAL: response(OFFICIAL, UNRELATED_PAGE)})
        subject = make_tool()
        verify.verify(subject)

        assert subject.verification.status == VerificationStatus.UNVERIFIED
        assert subject.status is None
        assert subject.last_verified_date is None
        assert subject.verification.verification_sources == []


# ============================================================ batch + misc
class TestBatchAndReport:
    def test_batch_report_counts_every_outcome(self) -> None:
        verify, _ = verifier(
            {
                OFFICIAL: response(OFFICIAL, official_page()),
                "https://dead.example.com/": response(
                    "https://dead.example.com/", "gone", status=404
                ),
                "https://blocked.example.com/": response(
                    "https://blocked.example.com/", CHALLENGE_PAGE
                ),
            }
        )
        results, report = verify.verify_candidates(
            [
                FakeCandidate(),
                FakeCandidate(candidate_id="c2", name="Dead", website="https://dead.example.com/"),
                FakeCandidate(
                    candidate_id="c3", name="Blocked", website="https://blocked.example.com/"
                ),
                FakeCandidate(candidate_id="c4", name="NoSite", website=None),
            ]
        )

        assert [r.candidate_id for r in results] == ["cand-1", "c2", "c3", "c4"]
        payload = report.to_dict()
        assert payload["input"] == 4
        assert payload["verified"] == 1
        assert payload["unreachable"] == 1
        assert payload["unverified"] == 1
        assert payload["failed"] == 1
        assert payload["fetched"] == 3, "the candidate without an official URL is not fetched"
        assert payload["started_at"] == FIXED_NOW.isoformat()

    def test_results_are_json_serialisable(self) -> None:
        import json

        verify, _ = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        result = verify.verify_candidate(FakeCandidate())
        payload = json.loads(json.dumps(result.to_dict()))

        assert payload["verification_status"] == "verified"
        assert payload["official_url"] == OFFICIAL
        assert payload["verification_source"]["kind"] == "official"

    def test_verification_is_deterministic(self) -> None:
        page = official_page()
        first, _ = verifier({OFFICIAL: response(OFFICIAL, page)})
        second, _ = verifier({OFFICIAL: response(OFFICIAL, page)})

        assert first.verify_candidate(FakeCandidate()).to_dict() == (
            second.verify_candidate(FakeCandidate()).to_dict()
        )

    def test_broken_candidate_does_not_abort_the_batch(self) -> None:
        class Exploding:
            candidate_id = "boom"

            @property
            def name(self) -> str:
                raise RuntimeError("bad record")

            website = OFFICIAL

        verify, _ = verifier({OFFICIAL: response(OFFICIAL, official_page())})
        results, report = verify.verify_candidates([Exploding(), FakeCandidate()])

        assert len(results) == 2
        assert results[0].verified is False
        assert results[1].verified is True
        assert report.input_count == 2


class TestLivenessChecker:
    def test_no_url_returns_none(self) -> None:
        assert LivenessChecker(client=StubClient()).check(None) is None  # type: ignore[arg-type]

    def test_evaluate_is_pure(self) -> None:
        checker = LivenessChecker(client=StubClient())  # type: ignore[arg-type]
        live = checker.evaluate(
            OFFICIAL, response(OFFICIAL, official_page()), product_name="Jasper"
        )

        assert live.is_accessible is True
        assert live.is_html is True
        assert live.is_challenge is False
        assert live.content_length >= MIN_CONTENT_CHARS
        assert live.looks_dead is False

    def test_dead_site_looks_dead(self) -> None:
        checker = LivenessChecker(client=StubClient())  # type: ignore[arg-type]
        live = checker.evaluate(OFFICIAL, response(OFFICIAL, PARKED_PAGE))

        assert live.is_accessible is False
        assert live.looks_dead is True
        assert live.detected_status == ToolStatus.INACCESSIBLE


class TestConflictResolution:
    def test_official_value_wins(self) -> None:
        value, notes = resolve_conflict("company", "Jasper AI Inc", {"taaft": "Jasper Labs"})
        assert value == "Jasper AI Inc"
        assert notes and "official value kept" in notes[0]

    def test_disagreeing_non_official_sources_leave_it_blank(self) -> None:
        value, notes = resolve_conflict("company", None, {"a": "X Corp", "b": "Y Ltd"})
        assert value is None
        assert notes and "left blank" in notes[0]
