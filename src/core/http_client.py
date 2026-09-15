"""Resilient HTTP client.

Implements the "Resilience" and "URL Normalization → redirect resolution"
engineering principles:

* per-host rate limiting (politeness);
* retry with exponential backoff on transient errors (429/5xx/timeouts);
* optional robots.txt compliance;
* on-disk response cache so re-runs are cheap and reproducible;
* redirect resolution exposed as ``final_url``.

Failures raise :class:`~src.core.errors.FetchError`; callers decide whether a
field stays ``None`` (they must never substitute a guess).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from src.core.config import HttpConfig, get_settings
from src.core.errors import FetchError
from src.core.logging_setup import get_logger
from src.core.urls import normalize_url

logger = get_logger("http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


@dataclass
class FetchResult:
    """Outcome of a single HTTP fetch."""

    url: str
    final_url: str | None
    status: int | None
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    elapsed_ms: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def redirected(self) -> bool:
        return bool(self.final_url and self.final_url != self.url)

    def json(self) -> Any:
        return json.loads(self.text)


class RateLimiter:
    """Simple per-host token spacing."""

    def __init__(self, default_rps: float = 0.5) -> None:
        self.default_rps = max(default_rps, 0.01)
        self._last: dict[str, float] = {}
        self._overrides: dict[str, float] = {}

    def set_host_rate(self, host: str, rps: float) -> None:
        self._overrides[host] = max(rps, 0.01)

    def wait(self, host: str) -> None:
        rps = self._overrides.get(host, self.default_rps)
        min_interval = 1.0 / rps
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            sleep_for = min_interval - (now - last)
            if sleep_for > 0:
                time.sleep(sleep_for + random.uniform(0, 0.15))
        self._last[host] = time.monotonic()


class ResponseCache:
    """Content-addressed on-disk cache of successful text responses."""

    def __init__(self, directory: Path, ttl_hours: int = 72) -> None:
        self.directory = Path(directory)
        self.ttl = timedelta(hours=ttl_hours)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> FetchResult | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None
        if datetime.now(timezone.utc) - fetched_at > self.ttl:
            return None
        return FetchResult(
            url=payload["url"],
            final_url=payload.get("final_url"),
            status=payload.get("status"),
            text=payload.get("text", ""),
            headers=payload.get("headers", {}),
            from_cache=True,
            fetched_at=fetched_at,
        )

    def put(self, key: str, result: FetchResult) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(
                json.dumps(
                    {
                        "url": result.url,
                        "final_url": result.final_url,
                        "status": result.status,
                        "text": result.text,
                        "headers": result.headers,
                        "fetched_at": result.fetched_at.isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disk issues
            logger.warning("cache write failed: %s", exc)


class OfflineHttpClient:
    """An :class:`HttpClient`-shaped client that **never touches the network**.

    Every stage that can reach out to the internet must be explicitly opted
    into (``--live``). Rather than sprinkling ``if live:`` branches through the
    stages — which is exactly how an accidental crawl happens — the *client* is
    swapped for this one, so "offline" is enforced at the only place that could
    ever open a socket.

    ``try_fetch`` returns ``None``, which is the real client's documented
    graceful-degradation contract: callers already treat it as "nothing was
    learned about this URL", never as evidence. Attempted URLs are recorded so
    a dry run can still prove *which* URLs a live run would have fetched (and,
    just as importantly, which it would not).
    """

    def __init__(self) -> None:
        #: Every URL a live run would have fetched, in order.
        self.attempts: list[str] = []
        self.limiter = RateLimiter(100.0)

    def try_fetch(self, url: str, **_: Any) -> None:
        self.attempts.append(url)
        logger.debug("offline client: fetch suppressed", extra={"url": url})
        return None

    def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.try_fetch(url, **kwargs)
        raise FetchError(f"offline mode: refusing to fetch {url}", url=url)

    def head(self, url: str, **kwargs: Any) -> None:
        return self.try_fetch(url, **kwargs)

    def close(self) -> None:
        return None

    def __enter__(self) -> "OfflineHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class HttpClient:
    """Polite, retrying, caching HTTP client."""

    def __init__(
        self,
        config: HttpConfig | None = None,
        *,
        cache_dir: str | Path | None = None,
        session: requests.Session | None = None,
    ) -> None:
        settings = get_settings()
        self.config = config or settings.http
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.limiter = RateLimiter(self.config.default_rate_limit_rps)
        self.cache = (
            ResponseCache(
                Path(cache_dir) if cache_dir else settings.paths.resolve("cache") / "http",
                self.config.cache_ttl_hours,
            )
            if self.config.cache_enabled
            else None
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ----------------------------------------------------------- robots.txt
    def _robots_allows(self, url: str) -> bool:
        if not self.config.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin, "missing")  # type: ignore[arg-type]
        if parser == "missing":
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                parser.read()
            except Exception:  # noqa: BLE001 - unreachable robots => allow
                parser = None
            self._robots[origin] = parser
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.config.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    # ---------------------------------------------------------------- fetch
    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        use_cache: bool = True,
        allow_redirects: bool = True,
        timeout: float | None = None,
    ) -> FetchResult:
        """Fetch ``url``, raising :class:`FetchError` on unrecoverable failure."""
        normalized = normalize_url(url, force_https=False) or url
        cache_key = f"{method}:{normalized}:{json.dumps(params or {}, sort_keys=True)}"

        if use_cache and self.cache and method.upper() == "GET":
            cached = self.cache.get(cache_key)
            if cached:
                logger.debug("cache hit", extra={"url": normalized})
                return cached

        if not self._robots_allows(normalized):
            raise FetchError(f"blocked by robots.txt: {normalized}", url=normalized)

        host = urlsplit(normalized).netloc
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            self.limiter.wait(host)
            started = time.monotonic()
            try:
                response = self.session.request(
                    method.upper(),
                    normalized,
                    params=dict(params) if params else None,
                    headers=dict(headers) if headers else None,
                    json=json_body,
                    timeout=timeout or self.config.timeout_seconds,
                    allow_redirects=allow_redirects,
                    verify=self.config.verify_tls,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "fetch attempt failed",
                    extra={"url": normalized, "attempt": attempt, "error": str(exc)},
                )
                self._backoff(attempt)
                continue

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if response.status_code in RETRY_STATUS and attempt < self.config.max_retries:
                logger.warning(
                    "retryable status",
                    extra={"url": normalized, "status": response.status_code, "attempt": attempt},
                )
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue

            result = FetchResult(
                url=normalized,
                final_url=normalize_url(response.url, force_https=False),
                status=response.status_code,
                text=response.text or "",
                headers={k.lower(): v for k, v in response.headers.items()},
                elapsed_ms=elapsed_ms,
            )
            if result.ok and self.cache and method.upper() == "GET":
                self.cache.put(cache_key, result)
            return result

        raise FetchError(
            f"all {self.config.max_retries} attempts failed for {normalized}: {last_error}",
            url=normalized,
        )

    def try_fetch(self, url: str, **kwargs: Any) -> FetchResult | None:
        """Fetch without raising — returns ``None`` on failure (graceful degradation)."""
        try:
            return self.fetch(url, **kwargs)
        except FetchError as exc:
            logger.info("fetch failed, degrading gracefully", extra={"url": url, "error": str(exc)})
            return None

    def head(self, url: str, **kwargs: Any) -> FetchResult | None:
        """Liveness probe used by verification; falls back to GET when HEAD is refused."""
        result = self.try_fetch(url, method="HEAD", use_cache=False, **kwargs)
        if result is None or (result.status or 0) in (400, 403, 405, 501):
            result = self.try_fetch(url, method="GET", **kwargs)
        return result

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        time.sleep(min(self.config.backoff_factor**attempt + random.uniform(0, 0.4), 30.0))

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
