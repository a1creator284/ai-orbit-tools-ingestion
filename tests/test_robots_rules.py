"""Regression tests for :mod:`src.core.robots`.

These pin the two defects that Run #9 traced in ``urllib.robotparser``, both
triggered by ``tests/fixtures/robots_allow_root.txt`` — a completely ordinary
real-world robots.txt:

1. a blank line inside a group made Python <= 3.12 discard every rule after it;
2. first-match (<= 3.12) instead of RFC 9309 longest-match precedence made
   ``Allow: /`` written above ``Disallow: /api/`` win for *every* path.

Either defect alone silently converted a genuine ``Disallow`` into
"crawlable", so the project matches robots rules itself and these tests keep
the answers identical on every interpreter.

The complementary rule — that a robots.txt which cannot be read is *not* a
prohibition — is a transport concern and lives in ``test_http_robots.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.robots import RobotsRules

FIXTURES = Path(__file__).parent / "fixtures"
ALLOW_ROOT = (FIXTURES / "robots_allow_root.txt").read_text(encoding="utf-8")

UA = "AIOrbitBot/0.1 (+https://aiorbit.ai)"


def _can(text: str, path: str, user_agent: str = UA) -> bool:
    return RobotsRules.parse(text).can_fetch(user_agent, f"https://ezaudio.io{path}")


# ------------------------------------------------- defect 1: blank lines
def test_blank_line_inside_a_group_does_not_discard_later_rules() -> None:
    """A blank line is whitespace, not a group terminator (RFC 9309 §2.2).

    ``urllib.robotparser`` on Python <= 3.12 ended the group at the blank line,
    dropping all four ``Disallow`` rules and reporting ``/api/private`` as
    crawlable. This is the precise regression that must never come back.
    """
    assert "Allow: /\n\nDisallow: /api/" in ALLOW_ROOT, "fixture must keep its blank line"

    assert _can(ALLOW_ROOT, "/api/private") is False
    assert _can(ALLOW_ROOT, "/_nuxt/entry.js") is False
    assert _can(ALLOW_ROOT, "/__sitemap__/index.xml") is False
    assert _can(ALLOW_ROOT, "/cdn-cgi/trace") is False


def test_allow_root_still_permits_public_pages() -> None:
    """The private Disallows must not spill over onto the public site."""
    assert _can(ALLOW_ROOT, "/") is True
    assert _can(ALLOW_ROOT, "/pricing") is True
    # "/api/" is a prefix rule: a path merely *starting* with "api" is free.
    assert _can(ALLOW_ROOT, "/apixyz") is True


# --------------------------------------- defect 2: longest-match precedence
def test_longest_match_wins_regardless_of_rule_order() -> None:
    """``Disallow: /api/`` beats a broader ``Allow: /`` written above it."""
    allow_first = "User-agent: *\nAllow: /\nDisallow: /api/\n"
    disallow_first = "User-agent: *\nDisallow: /api/\nAllow: /\n"

    for text in (allow_first, disallow_first):
        assert _can(text, "/api/private") is False
        assert _can(text, "/") is True


def test_more_specific_allow_overrides_a_broader_disallow() -> None:
    """Longest-match cuts both ways: a deeper Allow re-opens a subtree."""
    text = "User-agent: *\nDisallow: /api/\nAllow: /api/public/\n"

    assert _can(text, "/api/public/docs") is True
    assert _can(text, "/api/private") is False


def test_equal_length_conflict_favours_access() -> None:
    """On an exact-specificity tie, ``Allow`` wins (RFC 9309 §2.2.2)."""
    assert _can("User-agent: *\nDisallow: /x\nAllow: /x\n", "/x") is True


# --------------------------------------------------------- absence of rules
def test_empty_robots_forbids_nothing() -> None:
    assert _can("", "/anything") is True


def test_empty_disallow_value_is_not_a_prohibition() -> None:
    """``Disallow:`` with no value grants full access (RFC 9309 §2.2.2)."""
    assert _can("User-agent: *\nDisallow:\n", "/anything") is True


def test_rules_without_a_user_agent_line_are_ignored() -> None:
    """Stray rules belong to no group, so they cannot ban anything."""
    assert _can("Disallow: /\n", "/") is True


def test_comments_and_odd_whitespace_are_tolerated() -> None:
    text = "# banner\nUser-Agent:   *   \n  Disallow: /api/   # private\n"
    assert _can(text, "/api/x") is False
    assert _can(text, "/") is True


# ----------------------------------------------------------- global ban
def test_global_disallow_blocks_everything() -> None:
    assert _can("User-agent: *\nDisallow: /\n", "/") is False
    assert _can("User-agent: *\nDisallow: /\n", "/deep/page") is False


# ------------------------------------------------------- group selection
def test_named_group_takes_precedence_over_the_catch_all() -> None:
    """A group naming our token wins over ``*`` even if it is more permissive."""
    text = "User-agent: *\nDisallow: /\n\nUser-agent: AIOrbitBot\nAllow: /\nDisallow: /secret/\n"

    assert _can(text, "/") is True
    assert _can(text, "/secret/x") is False


def test_catch_all_applies_to_unnamed_agents() -> None:
    text = "User-agent: *\nDisallow: /private/\n\nUser-agent: Googlebot\nDisallow: /\n"

    assert _can(text, "/private/x") is False
    assert _can(text, "/public") is True
    # The Googlebot group must not leak onto us.
    assert _can(text, "/") is True


def test_consecutive_user_agent_lines_share_one_rule_set() -> None:
    """Stacked agent lines form a single group (RFC 9309 §2.2.1)."""
    text = "User-agent: AIOrbitBot\nUser-agent: OtherBot\nDisallow: /api/\n"

    assert _can(text, "/api/x") is False
    assert _can(text, "/api/x", user_agent="OtherBot/2.0") is False


# ------------------------------------------------------------- wildcards
@pytest.mark.parametrize(
    ("path", "expected"),
    [("/a/private/x", False), ("/b/private/y", False), ("/a/public/x", True)],
)
def test_star_wildcard_matches_any_segment(path: str, expected: bool) -> None:
    assert _can("User-agent: *\nDisallow: /*/private/\n", path) is expected


def test_dollar_anchors_the_end_of_the_path() -> None:
    text = "User-agent: *\nDisallow: /*.pdf$\n"

    assert _can(text, "/manual.pdf") is False
    assert _can(text, "/manual.pdf.html") is True


def test_patterns_are_literal_outside_wildcards() -> None:
    """A regex metacharacter in a pattern must not behave like a regex."""
    assert _can("User-agent: *\nDisallow: /a.b/\n", "/axb/") is True
    assert _can("User-agent: *\nDisallow: /a.b/\n", "/a.b/") is False


def test_percent_encoded_paths_match_their_decoded_rule() -> None:
    assert _can("User-agent: *\nDisallow: /private files/\n", "/private%20files/x") is False
