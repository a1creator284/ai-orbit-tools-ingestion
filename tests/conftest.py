"""Shared test fixtures.

All discovery tests run against local HTML fixtures through a fake HTTP client,
so the suite never touches an unstable (or bot-protected) live website.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES = Path(__file__).parent / "fixtures"

from src.core.http_client import FetchResult, RateLimiter  # noqa: E402
from src.discovery.base import SourceConfig  # noqa: E402


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class FakeHttpClient:
    """Deterministic stand-in for :class:`src.core.http_client.HttpClient`.

    ``responses`` maps a URL to either an HTML string, a ``(status, html)``
    tuple, or an ``Exception`` to raise. Unmapped URLs return ``None`` (the
    real client's graceful-degradation contract).
    """

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(100.0))

    def try_fetch(self, url: str, **_: Any) -> FetchResult | None:
        self.calls.append(url)
        entry = self.responses.get(url)
        if entry is None:
            return None
        if isinstance(entry, Exception):
            return None
        status, text = entry if isinstance(entry, tuple) else (200, entry)
        return FetchResult(
            url=url,
            final_url=url,
            status=status,
            text=text,
            headers={"content-type": "text/html"},
        )

    def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        result = self.try_fetch(url, **kwargs)
        if result is None:
            from src.core.errors import FetchError

            raise FetchError(f"no fixture for {url}", url=url)
        return result

    def head(self, url: str, **kwargs: Any) -> FetchResult | None:
        return self.try_fetch(url, **kwargs)

    def close(self) -> None:  # pragma: no cover - parity with the real client
        return None


@pytest.fixture
def fake_client() -> FakeHttpClient:
    return FakeHttpClient()


@pytest.fixture
def creati_config() -> SourceConfig:
    return SourceConfig(
        key="creati",
        name="Creati.ai",
        homepage="https://creati.ai/",
        listing_url="https://creati.ai/ai-tools/",
        tier=1,
        roles=["discovery", "enrichment"],
        trust=0.7,
        enabled=True,
    )


@pytest.fixture
def taaft_config() -> SourceConfig:
    return SourceConfig(
        key="taaft",
        name="There's An AI For That",
        homepage="https://theresanaiforthat.com/",
        listing_url="https://theresanaiforthat.com/tools/",
        tier=1,
        roles=["discovery", "enrichment"],
        trust=0.8,
        enabled=True,
    )
