"""Discovery source interface.

A *discovery source* finds candidate tools; it does **not** verify them.
Per guideline §2/§10: "Directories are for discovery. The official product
website is the primary source for verification."

To add a source, subclass :class:`DiscoverySource`, implement
:meth:`DiscoverySource.discover`, and register it with
``@register_source("<key>")``. No other pipeline stage changes.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from src.core.http_client import HttpClient
from src.core.logging_setup import get_logger
from src.core.text import clean_text
from src.core.urls import normalize_url
from src.models.base import SourceRef


@dataclass
class SourceConfig:
    """One entry from ``config/sources.yaml``."""

    key: str
    name: str
    homepage: str | None = None
    listing_url: str | None = None
    tier: int = 2
    roles: list[str] = field(default_factory=lambda: ["discovery"])
    access: str = "html"
    #: Prior trust used when sources disagree (official site always wins).
    trust: float = 0.5
    rate_limit_rps: float = 0.5
    respect_robots_txt: bool = True
    enabled: bool = False
    api_key_env: str | None = None
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_primary(self) -> bool:
        """Tier-1 sources must be reviewed systematically first (guideline §2)."""
        return self.tier == 1

    @property
    def is_official(self) -> bool:
        return "verification" in self.roles

    def to_source_ref(self, url: str | None = None) -> SourceRef:
        kind = "official" if self.is_official else (
            "api" if self.access == "api" else "rss" if self.access == "rss" else "directory"
        )
        return SourceRef(
            name=self.name,
            url=normalize_url(url or self.listing_url or self.homepage),
            kind=kind,
            retrieved_at=datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateTool:
    """Minimal, *unverified* discovery output.

    Intentionally thin: a directory listing is only allowed to contribute
    identity + pointers + raw observed signals. Every richer field is filled in
    later from the official website, or left blank.
    """

    name: str
    source_key: str
    source_name: str
    listing_url: str | None = None
    website: str | None = None
    tagline: str | None = None
    categories: list[str] = field(default_factory=list)
    #: Raw, unnormalized values exactly as observed (audit trail).
    raw_signals: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.name = clean_text(self.name, max_length=200) or ""
        self.tagline = clean_text(self.tagline, max_length=320)
        self.website = normalize_url(self.website) if self.website else None
        self.listing_url = normalize_url(self.listing_url) if self.listing_url else None
        self.categories = [c for c in (clean_text(x, max_length=80) for x in self.categories) if c]

    @property
    def is_usable(self) -> bool:
        """A candidate needs a name plus at least one resolvable pointer."""
        return bool(self.name) and bool(self.website or self.listing_url)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["discovered_at"] = self.discovered_at.isoformat()
        return data


class DiscoverySource(abc.ABC):
    """Base class for all discovery adapters."""

    def __init__(self, config: SourceConfig, client: HttpClient | None = None) -> None:
        self.config = config
        self.client = client or HttpClient()
        self.logger = get_logger(f"discovery.{config.key}")
        if config.homepage:
            from urllib.parse import urlsplit

            host = urlsplit(normalize_url(config.homepage) or "").netloc
            if host:
                self.client.limiter.set_host_rate(host, config.rate_limit_rps)

    @property
    def key(self) -> str:
        return self.config.key

    @abc.abstractmethod
    def discover(self, *, limit: int | None = None, **kwargs: Any) -> Iterator[CandidateTool]:
        """Yield candidate tools. Must not raise for a single bad listing item."""

    def health_check(self) -> bool:
        """Cheap reachability probe; a dead source is skipped, not fatal."""
        target = self.config.listing_url or self.config.homepage
        if not target:
            return False
        result = self.client.head(target)
        return bool(result and result.ok)

    def source_ref(self, url: str | None = None) -> SourceRef:
        return self.config.to_source_ref(url)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} key={self.key} tier={self.config.tier}>"
