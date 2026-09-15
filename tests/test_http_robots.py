"""Regression tests for robots.txt handling in :class:`HttpClient`.

These tests exist because of a bug found by the first **live** official-site
verification spot check (``python run.py verify --live --limit 10``). Six of
ten candidates were reported as ``unreachable`` / ``fetch_failed`` with the
reason "official website did not respond (timeout/network failure after
retries)", yet every one of those six sites answered ``HTTP 200`` and publishes
a robots.txt containing ``Allow: /``.

Root cause: ``_robots_allows`` used ``RobotFileParser.read()``, which fetches
robots.txt via ``urllib`` using the default ``Python-urllib/x.y`` user agent.
CDNs commonly answer that UA with ``403``; ``read()`` swallows the error and
sets ``disallow_all = True``. The client therefore refused to send the request
at all, and the verifier recorded a *network failure that never happened*.

The verifier's decision rules are deliberately **not** involved: the fix is in
the transport layer, and these tests pin the transport behaviour. Everything
here is offline — a fake session, no sockets.

Run #9 found a second, opposite defect in the same code path: the stdlib
parser was *under*-enforcing. For this very fixture it dropped every rule
following a blank line (Python <= 3.12) and applied first-match rather than
RFC 9309 longest-match precedence, so real ``Disallow`` rules silently became
"crawlable" — and the answers changed between interpreter versions. Robots
matching therefore moved to :mod:`src.core.robots`; the rule-semantics tests
live in ``test_robots_rules.py`` and the end-to-end enforcement tests here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from src.core.config import HttpConfig
from src.core.errors import FetchError
from src.core.http_client import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"

#: A real-world robots.txt shape: explicitly allows ``/``, blocks only private
#: paths. Captured from one of the sites the live spot check wrongly skipped.
ROBOTS_ALLOW_ROOT = (FIXTURES / "robots_allow_root.txt").read_text(encoding="utf-8")


class FakeResponse:
    """Minimal ``requests.Response`` stand-in."""

    def __init__(self, status: int, text: str = "", url: str = "https://ezaudio.io/") -> None:
        self.status_code = status
        self.text = text
        self.url = url
        self.headers: dict[str, str] = {"content-type": "text/html"}


class FakeSession:
    """Records every request and replays scripted responses.

    ``robots`` is the response returned for ``/robots.txt`` — either a
    ``FakeResponse`` or an exception to raise, which is how a CDN 403 or a
    network error is reproduced without a socket.
    """

    def __init__(self, robots: Any, page: Any = None) -> None:
        self._robots = robots
        self._page = page or FakeResponse(200, "<html><title>EZAudio</title></html>")
        self.headers: dict[str, str] = {}
        self.robots_requests: list[tuple[str, str]] = []
        self.page_requests: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/robots.txt"):
            self.robots_requests.append((url, self.headers.get("User-Agent", "")))
            if isinstance(self._robots, Exception):
                raise self._robots
            return self._robots
        raise AssertionError(f"unexpected session.get for {url}")

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.page_requests.append(url)
        if isinstance(self._page, Exception):
            raise self._page
        return self._page

    def close(self) -> None:
        return None


def _client(session: FakeSession, **overrides: Any) -> HttpClient:
    options: dict[str, Any] = {
        "respect_robots_txt": True,
        "cache_enabled": False,
        "max_retries": 1,
        "backoff_factor": 0.0,
        "default_rate_limit_rps": 100.0,
    }
    options.update(overrides)
    config = HttpConfig(**options)
    return HttpClient(config=config, session=session)  # type: ignore[arg-type]


# --------------------------------------------------------------- the live bug
def test_cdn_403_on_robots_does_not_block_the_fetch() -> None:
    """A 403 on robots.txt must not be read as "crawling forbidden".

    This is the exact live failure: robots.txt is refused for the *robots*
    request, so no rules can be established. RFC 9309 treats 4xx as
    unrestricted access, and inventing a prohibition made the verifier report
    a network failure for a site that was perfectly reachable.
    """
    session = FakeSession(robots=FakeResponse(403, "<html>Forbidden</html>"))
    client = _client(session)

    result = client.fetch("https://ezaudio.io/")

    assert result.status == 200
    assert session.page_requests == ["https://ezaudio.io/"], (
        "the page request must actually be sent; a 403 on robots.txt is not a "
        "disallow rule"
    )


def test_robots_is_fetched_with_the_configured_user_agent() -> None:
    """robots.txt must be requested with the UA the rules are evaluated for.

    ``RobotFileParser.read()`` used ``Python-urllib``, which is what triggered
    the CDN 403 in the first place.
    """
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session, user_agent="AIOrbitBot/0.1 (+https://aiorbit.ai)")

    client.fetch("https://ezaudio.io/")

    assert len(session.robots_requests) == 1
    url, user_agent = session.robots_requests[0]
    assert url == "https://ezaudio.io/robots.txt"
    assert user_agent == "AIOrbitBot/0.1 (+https://aiorbit.ai)"
    assert "urllib" not in user_agent.lower()


def test_allow_root_robots_permits_the_landing_page() -> None:
    """``Allow: /`` plus private Disallows must permit the landing page."""
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.page_requests == ["https://ezaudio.io/"]


# ------------------------------------------------- rules are still obeyed
def test_real_disallow_rule_is_still_enforced() -> None:
    """The fix must not weaken robots compliance: a real Disallow still blocks.

    Politeness is a project principle — the bug was a *false* prohibition, and
    removing it must not remove genuine ones.
    """
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session)

    with pytest.raises(FetchError, match="robots.txt"):
        client.fetch("https://ezaudio.io/api/private")

    assert session.page_requests == [], "a disallowed URL must never be requested"


@pytest.mark.parametrize(
    "path",
    ["/api/private", "/_nuxt/entry.js", "/__sitemap__/index.xml", "/cdn-cgi/trace"],
)
def test_every_disallowed_prefix_in_the_fixture_is_enforced(path: str) -> None:
    """All four ``Disallow`` rules must bind, not just the first one.

    In the fixture a blank line separates ``Allow: /`` from the ``Disallow``
    block. ``urllib.robotparser`` on Python <= 3.12 treated that blank line as
    a group terminator and discarded all four rules, so the client happily
    requested private paths. Run #9 replaced the parser; this asserts the
    whole block is honoured end-to-end through the transport.
    """
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session)

    with pytest.raises(FetchError, match="robots.txt"):
        client.fetch(f"https://ezaudio.io{path}")

    assert session.page_requests == []


def test_public_paths_stay_fetchable_under_the_fixture_rules() -> None:
    """Enforcing the private prefixes must not over-block the public site."""
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/pricing").status == 200
    assert client.fetch("https://ezaudio.io/apixyz").status == 200
    assert session.page_requests == [
        "https://ezaudio.io/pricing",
        "https://ezaudio.io/apixyz",
    ]


def test_global_disallow_all_is_respected() -> None:
    """An explicit site-wide ``Disallow: /`` still blocks every request."""
    session = FakeSession(robots=FakeResponse(200, "User-agent: *\nDisallow: /\n"))
    client = _client(session)

    with pytest.raises(FetchError, match="robots.txt"):
        client.fetch("https://ezaudio.io/")

    assert session.page_requests == []


def test_robots_rules_are_fetched_once_per_origin() -> None:
    """Rules are cached per origin — politeness must not mean re-fetching."""
    session = FakeSession(robots=FakeResponse(200, ROBOTS_ALLOW_ROOT))
    client = _client(session)

    client.fetch("https://ezaudio.io/")
    client.fetch("https://ezaudio.io/pricing")

    assert len(session.robots_requests) == 1
    assert session.page_requests == [
        "https://ezaudio.io/",
        "https://ezaudio.io/pricing",
    ]


# ------------------------------------------------- other unusable responses
@pytest.mark.parametrize("status", [401, 404, 410, 500, 503])
def test_unusable_robots_status_allows_the_fetch(status: int) -> None:
    """No usable rules (4xx/5xx) must mean "no restriction found", not a block.

    The client documents "unreachable robots => allow"; this pins that intent
    so a transport hiccup can never masquerade as a site prohibition — or,
    worse, as a product's website being dead.
    """
    session = FakeSession(robots=FakeResponse(status, ""))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.page_requests == ["https://ezaudio.io/"]


def test_network_error_on_robots_allows_the_fetch() -> None:
    """A network error fetching robots.txt must not block the real request."""
    session = FakeSession(robots=requests.ConnectionError("dns failure"))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.page_requests == ["https://ezaudio.io/"]


def test_empty_robots_allows_the_fetch() -> None:
    """An empty robots.txt expresses no rules, so nothing is disallowed."""
    session = FakeSession(robots=FakeResponse(200, ""))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.page_requests == ["https://ezaudio.io/"]


def test_unparseable_robots_allows_the_fetch() -> None:
    """A body that yields no usable rules is not a prohibition either.

    Some origins answer /robots.txt with an HTML error page or a login wall.
    That expresses no rules, so Run #7's intent holds: only explicitly
    published rules may block a request.
    """
    session = FakeSession(robots=FakeResponse(200, "<html><body>Login required</body></html>"))
    client = _client(session)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.page_requests == ["https://ezaudio.io/"]


def test_robots_disabled_skips_the_robots_request_entirely() -> None:
    """``respect_robots_txt: false`` must not even look robots.txt up."""
    session = FakeSession(robots=FakeResponse(200, "User-agent: *\nDisallow: /\n"))
    client = _client(session, respect_robots_txt=False)

    assert client.fetch("https://ezaudio.io/").status == 200
    assert session.robots_requests == []


# ------------------------------------------------------ verifier consequence
def test_blocked_url_degrades_to_none_not_to_a_fake_response() -> None:
    """``try_fetch`` keeps its graceful-degradation contract when blocked.

    The verifier turns ``None`` into a failure code rather than evidence, so a
    genuinely disallowed page can never be scored as a verified product page.
    """
    session = FakeSession(robots=FakeResponse(200, "User-agent: *\nDisallow: /\n"))
    client = _client(session)

    assert client.try_fetch("https://ezaudio.io/") is None
