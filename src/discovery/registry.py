"""Discovery-source registry.

Loads ``config/sources.yaml`` and resolves each entry to an adapter class.
Sources without an implemented adapter are reported as *configured but not
implemented* rather than silently ignored — that keeps the roadmap honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from src.core.config import DEFAULT_SOURCES_PATH, get_settings
from src.core.errors import ConfigError
from src.core.http_client import HttpClient
from src.core.logging_setup import get_logger
from src.discovery.base import DiscoverySource, SourceConfig

logger = get_logger("discovery.registry")

_ADAPTERS: dict[str, type[DiscoverySource]] = {}

_KNOWN_FIELDS = {
    "key", "name", "homepage", "listing_url", "tier", "roles", "access", "trust",
    "rate_limit_rps", "respect_robots_txt", "enabled", "api_key_env", "notes",
}


_adapters_loaded = False


def load_adapters() -> dict[str, type[DiscoverySource]]:
    """Import :mod:`src.discovery.sources` so every adapter self-registers.

    Imported lazily (not at module scope) because the adapter modules import
    :func:`register_source` from here.
    """
    global _adapters_loaded
    if not _adapters_loaded:
        _adapters_loaded = True
        try:
            import src.discovery.sources  # noqa: F401  (import side effect: registration)
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not kill the run
            logger.error("failed to import discovery adapters: %s", exc)
    return dict(_ADAPTERS)


def register_source(key: str) -> Callable[[type[DiscoverySource]], type[DiscoverySource]]:
    """Class decorator registering an adapter under a ``sources.yaml`` key."""

    def wrapper(cls: type[DiscoverySource]) -> type[DiscoverySource]:
        if key in _ADAPTERS and _ADAPTERS[key] is not cls:
            raise ConfigError(f"duplicate discovery adapter for key: {key}")
        _ADAPTERS[key] = cls
        return cls

    return wrapper


def load_source_configs(path: str | Path | None = None) -> dict[str, SourceConfig]:
    """Parse the source registry file into :class:`SourceConfig` objects."""
    path = Path(path or get_settings().sources_path or DEFAULT_SOURCES_PATH)
    if not path.exists():
        raise ConfigError(f"sources file not found: {path}")

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")

    defaults: dict[str, Any] = document.get("defaults") or {}
    entries = document.get("sources") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: 'sources' must be a list")

    configs: dict[str, SourceConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("key"):
            logger.warning("skipping malformed source entry", extra={"entry": repr(entry)[:200]})
            continue
        merged = {**defaults, **entry}
        kwargs = {k: v for k, v in merged.items() if k in _KNOWN_FIELDS}
        extra = {k: v for k, v in merged.items() if k not in _KNOWN_FIELDS}
        key = str(kwargs["key"])
        if key in configs:
            raise ConfigError(f"duplicate source key in {path}: {key}")
        if "name" not in kwargs:
            raise ConfigError(f"source '{key}' is missing a name")
        configs[key] = SourceConfig(**kwargs, extra=extra)

    logger.info("loaded source registry", extra={"path": str(path), "count": len(configs)})
    return configs


class SourceRegistry:
    """Resolves configured sources to live adapter instances."""

    def __init__(
        self,
        configs: dict[str, SourceConfig] | None = None,
        client: HttpClient | None = None,
    ) -> None:
        load_adapters()
        self.configs = configs if configs is not None else load_source_configs()
        self._client = client
        self._instances: dict[str, DiscoverySource] = {}

    # ------------------------------------------------------------- queries
    def keys(self) -> list[str]:
        return list(self.configs)

    def get_config(self, key: str) -> SourceConfig:
        if key not in self.configs:
            raise ConfigError(f"unknown source key: {key}")
        return self.configs[key]

    def by_role(self, role: str, *, enabled_only: bool = True) -> list[SourceConfig]:
        return [
            config
            for config in self.configs.values()
            if role in config.roles and (config.enabled or not enabled_only)
        ]

    def primary_sources(self, *, enabled_only: bool = True) -> list[SourceConfig]:
        """Tier-1 sources, reviewed first per guideline §2."""
        return sorted(
            (
                config
                for config in self.configs.values()
                if config.is_primary and (config.enabled or not enabled_only)
            ),
            key=lambda config: config.key,
        )

    def implemented(self) -> list[str]:
        return sorted(k for k in self.configs if k in _ADAPTERS)

    def not_implemented(self) -> list[str]:
        return sorted(k for k in self.configs if k not in _ADAPTERS)

    # ---------------------------------------------------------- resolution
    @property
    def client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient()
        return self._client

    def get(self, key: str) -> DiscoverySource:
        """Instantiate (and memoise) the adapter for ``key``."""
        if key in self._instances:
            return self._instances[key]
        config = self.get_config(key)
        adapter = _ADAPTERS.get(key)
        if adapter is None:
            raise ConfigError(
                f"source '{key}' is configured but no adapter is implemented yet"
            )
        instance = adapter(config, self.client)
        self._instances[key] = instance
        return instance

    def active(self, *, role: str = "discovery") -> Iterator[DiscoverySource]:
        """Yield adapters that are enabled, in-role and implemented (tier order)."""
        candidates = sorted(
            (c for c in self.configs.values() if c.enabled and role in c.roles),
            key=lambda config: (config.tier, config.key),
        )
        for config in candidates:
            if config.key not in _ADAPTERS:
                logger.warning(
                    "source enabled but adapter missing; skipping",
                    extra={"source": config.key},
                )
                continue
            yield self.get(config.key)

    def summary(self) -> dict[str, Any]:
        return {
            "total_configured": len(self.configs),
            "implemented": self.implemented(),
            "not_implemented": self.not_implemented(),
            "enabled": sorted(k for k, c in self.configs.items() if c.enabled),
            "primary": [c.key for c in self.primary_sources(enabled_only=False)],
            "by_role": {
                role: sorted(c.key for c in self.by_role(role, enabled_only=False))
                for role in ("discovery", "cross_check", "enrichment", "verification")
            },
        }
