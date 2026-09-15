"""Regression tests for defects found during the 50-site live validation.

Every case below was observed on a *real* official page during
``python run.py verify --live --limit 50``, then frozen here as a literal HTML
fixture so the bug can never come back. These tests are fully offline: no
network, no cache, no live dependency.

Each fix **tightens or corrects** evidence handling; none of them loosens the
verification policy:

1. ``_name_matches`` handled noise tokens asymmetrically. The needle drops
   ``ai``/``free``/``online``… while the haystack kept them, so a page whose
   ``<title>`` spelled the brand out *verbatim* failed identity (observed:
   "Clever AI Detector"). Identity still requires a real page-level brand slot.
2. ``_name_matches`` canonicalized the haystack with tagline trimming, throwing
   away everything after ``" | "`` — exactly where many titles put the brand
   (observed: "… Solver Online | Tenorshare AI Math").
3. A dead-site marker was accepted from anywhere in the body copy, so a live,
   paying product was declared ``unreachable`` because one *feature* was
   labelled "Coming Soon" (observed: "Multiple Angles"). Ambiguous phrases now
   have to be a page-level claim (``<title>``/``h1``/``h2``); unambiguous
   markers such as "this domain may be for sale" are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.http_client import FetchResult
from src.core.text import canonical_name
from src.models.enums import ToolStatus
from src.verification.verifier import (
    VerificationFailure,
    extract_official_evidence,
)

FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _filler(brand: str, chars: int = 900) -> str:
    sentence = (
        f"{brand} turns a single input into a polished result with batch "
        "controls, export options and collaborative review for teams. "
    )
    return sentence * (chars // len(sentence) + 1)


def response(url: str, body: str, *, status: int = 200) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        text=body,
        headers={"content-type": "text/html; charset=utf-8"},
    )


# ======================================== 0. the canonicalization primitive
class TestCanonicalNameTaglineOptOut:
    """``drop_tagline=False`` is what lets a haystack keep its brand half."""

    def test_default_still_trims_a_tagline_for_names(self) -> None:
        assert canonical_name("Jasper AI — AI writing assistant") == "jasper"

    def test_opt_out_keeps_text_after_the_separator(self) -> None:
        kept = canonical_name(
            "Free AI Math Solver Online | Tenorshare AI Math",
            drop_noise=False,
            drop_tagline=False,
        )
        assert kept is not None and "tenorshare" in kept

    def test_opt_out_is_not_the_default(self) -> None:
        trimmed = canonical_name(
            "Free AI Math Solver Online | Tenorshare AI Math", drop_noise=False
        )
        assert trimmed is not None and "tenorshare" not in trimmed


# ============================================ 1. symmetric noise-token handling
#: Real shape of https://cleverhumanizer.ai/ai-detector — the brand is spelled
#: out verbatim in the title, h1 and logo alt, yet identity failed.
CLEVER_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <title>Clever AI Detector: 100% Free AI Checker. Unlimited Use</title>
  <meta property="og:site_name" content="Clever AI Detector" />
  <meta name="description" content="Free AI checker with sentence-level analysis." />
</head>
<body>
  <header><img src="/logo.svg" alt="Clever AI Detector logo" class="logo" /></header>
  <nav><a href="/signup">Sign up</a><a href="/login">Log in</a></nav>
  <h1>Clever AI Detector. Free AI checker with Sentence-Level Analysis</h1>
  <p>{_filler("Clever AI Detector")}</p>
</body>
</html>"""


class TestNoiseTokensAreHandledSymmetrically:
    """A title that names the product verbatim must confirm identity."""

    def test_brand_in_title_confirms_identity(self) -> None:
        evidence = extract_official_evidence(
            "https://cleverhumanizer.ai/ai-detector",
            response("https://cleverhumanizer.ai/ai-detector", CLEVER_PAGE),
            product_name="Clever AI Detector",
            now=FIXED_NOW,
        )

        assert evidence.identity_confirmed is True
        assert "name_in_title" in evidence.identity_signals
        assert VerificationFailure.IDENTITY_NOT_CONFIRMED not in evidence.failures

    def test_identity_does_not_rest_on_body_text_alone(self) -> None:
        """The regression was real evidence being *missed*, not a looser rule."""
        evidence = extract_official_evidence(
            "https://cleverhumanizer.ai/ai-detector",
            response("https://cleverhumanizer.ai/ai-detector", CLEVER_PAGE),
            product_name="Clever AI Detector",
            now=FIXED_NOW,
        )

        page_level = [
            signal
            for signal in evidence.identity_signals
            if signal.startswith("name_in_") and signal != "name_in_body_text"
        ]
        assert page_level, "identity must come from a page-level brand slot"


# ================================================ 2. brand after a title tagline
#: Real shape of https://ai.tenorshare.com/products/ai-math-solver — the brand
#: sits *after* the " | " separator, which tagline trimming discarded.
TENORSHARE_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <title>Free\u00a0AI Math Solver Online | Tenorshare AI Math</title>
  <meta property="og:title" content="Free AI Math Solver Online | Tenorshare AI Math" />
</head>
<body>
  <nav><a href="/pricing">Pricing</a><a href="/demo">Try it free</a></nav>
  <h1>Free AI Math Solver Online</h1>
  <p>{_filler("Tenorshare AI Math")}</p>
</body>
</html>"""


class TestBrandAfterTitleTagline:
    def test_brand_after_separator_is_still_a_brand_slot(self) -> None:
        url = "https://ai.tenorshare.com/products/ai-math-solver"
        evidence = extract_official_evidence(
            url,
            response(url, TENORSHARE_PAGE),
            product_name="Tenorshare AI Math",
            now=FIXED_NOW,
        )

        assert evidence.identity_confirmed is True
        assert "name_in_title" in evidence.identity_signals

    def test_an_unrelated_brand_after_the_separator_still_fails(self) -> None:
        """Keeping the tagline must not turn the check into a wildcard."""
        url = "https://ai.tenorshare.com/products/ai-math-solver"
        evidence = extract_official_evidence(
            url,
            response(url, TENORSHARE_PAGE),
            product_name="Photoroom",
            now=FIXED_NOW,
        )

        assert evidence.identity_confirmed is False
        assert VerificationFailure.IDENTITY_NOT_CONFIRMED in evidence.failures


# ================================== 3. "Coming Soon" as a feature label, not death
#: Real shape of https://multipleangles.app/ — a live product with pricing,
#: login and signup whose *roadmap row* reads "Batch Processing Coming Soon".
MULTIPLE_ANGLES_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <title>Multiple Angles - Transform Any Image Into Every Angle</title>
  <meta property="og:site_name" content="Multiple Angles" />
</head>
<body>
  <nav>
    <a href="/">Multiple Angles</a>
    <a href="/pricing">Pricing</a>
    <a href="/auth/login">Log in</a>
    <a href="/#generator">Try It Free</a>
  </nav>
  <h1>Generate Multiple Camera Angles from One Photo</h1>
  <h2>Simple, Transparent Pricing</h2>
  <section>
    <h3>Batch Processing <span>Coming Soon</span></h3>
    <p>Generate multiple camera angles at once. Queue perspectives and export
    complete multi-view sets together.</p>
  </section>
  <p>{_filler("Multiple Angles")}</p>
</body>
</html>"""

#: Unambiguous, page-level death — must stay dead.
PARKED_PAGE = f"""<!doctype html>
<html><head><title>multipleangles.app</title></head>
<body><h1>This domain may be for sale</h1><p>{_filler("Domain broker")}</p></body></html>"""


class TestAmbiguousDeadMarkers:
    def test_feature_label_coming_soon_is_not_a_dead_site(self) -> None:
        url = "https://multipleangles.app/"
        evidence = extract_official_evidence(
            url,
            response(url, MULTIPLE_ANGLES_PAGE),
            product_name="Multiple Angles",
            now=FIXED_NOW,
        )

        assert evidence.dead_markers == []
        assert VerificationFailure.DEAD_SITE_MARKER not in evidence.failures
        assert evidence.detected_status == ToolStatus.ACTIVE
        assert evidence.is_accessible is True

    def test_the_mention_is_still_recorded_for_audit(self) -> None:
        """Conservative handling: the phrase is disclosed, not silently dropped."""
        url = "https://multipleangles.app/"
        evidence = extract_official_evidence(
            url,
            response(url, MULTIPLE_ANGLES_PAGE),
            product_name="Multiple Angles",
            now=FIXED_NOW,
        )

        assert "coming soon" in evidence.ambiguous_dead_mentions
        assert "ambiguous_dead_mentions" in evidence.to_dict()

    def test_page_level_coming_soon_is_still_dead(self) -> None:
        """A real placeholder page announces it in the title/heading."""
        url = "https://example-launch.ai/"
        body = """<!doctype html>
<html><head><title>Coming soon</title></head>
<body><h1>Coming Soon</h1><p>We are launching shortly. Sign up for updates and
we will let you know the moment the product is ready for you to try out.</p>
</body></html>"""
        evidence = extract_official_evidence(
            url, response(url, body), product_name="Example Launch", now=FIXED_NOW
        )

        assert "coming soon" in evidence.dead_markers
        assert VerificationFailure.DEAD_SITE_MARKER in evidence.failures

    def test_unambiguous_markers_are_unchanged(self) -> None:
        url = "https://multipleangles.app/"
        evidence = extract_official_evidence(
            url, response(url, PARKED_PAGE), product_name="Multiple Angles", now=FIXED_NOW
        )

        assert "this domain may be for sale" in evidence.dead_markers
        assert VerificationFailure.DEAD_SITE_MARKER in evidence.failures
        assert evidence.is_accessible is False


# ======================================= policy guardrails must remain intact
class TestPolicyStillHolds:
    """The fixes must not have created a path to a fabricated pass."""

    def test_unrelated_page_is_never_verified(self) -> None:
        url = "https://acme-hosting.example/"
        body = f"""<!doctype html>
<html><head><title>Acme Hosting — cheap VPS</title>
<meta property="og:site_name" content="Acme Hosting" /></head>
<body><h1>Acme Hosting plans</h1>
<nav><a href="/pricing">Pricing</a><a href="/login">Log in</a></nav>
<p>{_filler("Acme Hosting")}</p></body></html>"""
        evidence = extract_official_evidence(
            url, response(url, body), product_name="Jasper", now=FIXED_NOW
        )

        assert evidence.identity_confirmed is False
        assert VerificationFailure.IDENTITY_NOT_CONFIRMED in evidence.failures

    def test_challenge_page_still_yields_no_evidence(self) -> None:
        url = "https://ieltswritingchecker.org/"
        body = (
            '<!doctype html><html><head><title>Just a moment...</title></head>'
            '<body><div id="cf-browser-verification">Enable JavaScript and '
            "cookies to continue</div></body></html>"
        )
        evidence = extract_official_evidence(
            url,
            response(url, body, status=403),
            product_name="ielts writing checker",
            now=FIXED_NOW,
        )

        assert evidence.is_challenge is True
        assert VerificationFailure.BOT_CHALLENGE in evidence.failures
        assert evidence.identity_signals == []
        assert evidence.product_signals == []
