"""Tests for the HTML extraction primitives."""

from __future__ import annotations

from src.extraction.html import (
    absolutize,
    best_label,
    is_asset_url,
    is_internal,
    looks_like_challenge,
    node_text,
    page_fingerprint,
    parse_html,
    select_all,
)
from tests.conftest import load_fixture


class TestParsing:
    def test_empty_markup_returns_none(self) -> None:
        assert parse_html("") is None
        assert parse_html(None) is None
        assert parse_html("   ") is None

    def test_malformed_markup_still_parses(self) -> None:
        soup = parse_html("<div class='a'><p>unclosed<div>x")
        assert soup is not None
        assert soup.select_one("div.a") is not None

    def test_select_all_falls_back_to_next_selector(self) -> None:
        soup = parse_html("<ul><li class='b'>1</li><li class='b'>2</li></ul>")
        assert soup is not None
        assert len(select_all(soup, ("div.missing", "li.b"))) == 2

    def test_invalid_selector_is_skipped(self) -> None:
        soup = parse_html("<div class='x'>y</div>")
        assert soup is not None
        assert select_all(soup, ("<<<bad", "div.x"))


class TestChallengeDetection:
    def test_cloudflare_interstitial_detected(self) -> None:
        assert looks_like_challenge(load_fixture("cloudflare_challenge.html"), 403)

    def test_status_403_is_treated_as_blocked(self) -> None:
        assert looks_like_challenge("<html><body>nope</body></html>", 403)

    def test_real_content_not_flagged(self) -> None:
        assert not looks_like_challenge(load_fixture("creati_listing_page1.html"), 200)

    def test_none_markup_is_not_a_challenge(self) -> None:
        assert not looks_like_challenge(None, 200)


class TestFingerprint:
    def test_same_page_same_fingerprint(self) -> None:
        html = load_fixture("creati_listing_page1.html")
        assert page_fingerprint(html) == page_fingerprint(html)

    def test_different_pages_differ(self) -> None:
        a = page_fingerprint(load_fixture("taaft_listing_page1.html"))
        b = page_fingerprint(load_fixture("taaft_listing_page2.html"))
        assert a and b and a != b

    def test_empty_input(self) -> None:
        assert page_fingerprint("") is None


class TestUrlHelpers:
    def test_absolutize_resolves_and_normalizes(self) -> None:
        got = absolutize("/ai-tools/x/?utm_source=creati.ai", "https://creati.ai/ai-tools/")
        assert got == "https://creati.ai/ai-tools/x"

    def test_absolutize_rejects_non_http(self) -> None:
        for href in ("#frag", "javascript:void(0)", "mailto:a@b.co", "tel:+1", "data:x"):
            assert absolutize(href, "https://creati.ai/") is None

    def test_absolutize_handles_none_and_blank(self) -> None:
        assert absolutize(None, "https://creati.ai/") is None
        assert absolutize("   ", "https://creati.ai/") is None

    def test_asset_urls_rejected(self) -> None:
        assert is_asset_url("https://cdn-image.creati.ai/image/x.webp")
        assert is_asset_url("https://example.com/logo.png")
        assert is_asset_url(None)
        assert not is_asset_url("https://example.com/product")

    def test_internal_detection_ignores_www(self) -> None:
        assert is_internal("https://www.creati.ai/x", "https://creati.ai/ai-tools/")
        assert not is_internal("https://other.example/x", "https://creati.ai/ai-tools/")


class TestLabels:
    def test_title_attribute_preferred_over_clamped_text(self) -> None:
        soup = parse_html('<a title="Full Product Name">Full Produ…</a>')
        assert soup is not None
        assert best_label(soup.a) == "Full Product Name"

    def test_falls_back_to_text(self) -> None:
        soup = parse_html("<a>Visible Name</a>")
        assert soup is not None
        assert best_label(soup.a) == "Visible Name"

    def test_none_node(self) -> None:
        assert best_label(None) is None
        assert node_text(None) is None
