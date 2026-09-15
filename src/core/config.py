"""Configuration management.

Layered resolution (later layers win):

1. dataclass defaults in this module;
2. ``config/settings.yaml`` (committed, non-secret);
3. environment variables / ``.env`` (secrets and per-run overrides).

Discovery-source definitions live in ``config/sources.yaml`` and are loaded by
:mod:`src.discovery.registry`, keeping code free of hard-coded URLs.
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.core.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency); does not overwrite real env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class PathsConfig:
    """Filesystem layout for pipeline artefacts."""

    root: str = str(PROJECT_ROOT)
    raw: str = "data/raw"
    interim: str = "data/interim"
    processed: str = "data/processed"
    final: str = "data/final"
    cache: str = ".cache"
    logs: str = "logs"

    def resolve(self, name: str) -> Path:
        value = getattr(self, name, None)
        if value is None:
            raise ConfigError(f"unknown path key: {name}")
        path = Path(value)
        return path if path.is_absolute() else Path(self.root) / path

    def ensure(self) -> None:
        for name in ("raw", "interim", "processed", "final", "cache", "logs"):
            self.resolve(name).mkdir(parents=True, exist_ok=True)


@dataclass
class HttpConfig:
    """Network behaviour: polite crawling + resilience."""

    user_agent: str = "AIOrbitBot/0.1 (+https://aiorbit.ai; data-ingestion)"
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_factor: float = 1.5
    default_rate_limit_rps: float = 0.5
    respect_robots_txt: bool = True
    cache_enabled: bool = True
    cache_ttl_hours: int = 72
    verify_tls: bool = True


@dataclass
class ScoringConfig:
    """Weights and thresholds for the 100-point framework.

    Weights are defined verbatim in the Tools guideline (section 5) and are
    validated to sum to 100 at load time.
    """

    weights: dict[str, int] = field(
        default_factory=lambda: {
            "product_quality_capability": 25,
            "real_user_value": 20,
            "current_usage_adoption": 15,
            "activity_maintenance": 15,
            "product_maturity_reliability": 10,
            "recency_momentum": 5,
            "differentiation": 5,
            "information_quality_verifiability": 5,
        }
    )
    #: Lower bound of each ranking band, per guideline section 5.
    bands: dict[str, int] = field(
        default_factory=lambda: {
            "exceptional": 90,
            "excellent": 80,
            "good": 70,
            "average": 60,
        }
    )
    #: Scores strictly below this are rejected ("Below 60 → Reject").
    reject_below: int = 60
    #: 60-69 is "average — usually skip": below this a record is skipped.
    skip_below: int = 70
    #: 80+ is "include"; 70-79 is included only selectively, on evidence.
    include_at_or_above: int = 80

    def __post_init__(self) -> None:
        # The rubric module owns the arithmetic (weights sum to 100 and no
        # component can overflow its weight), so there is exactly one
        # definition of "valid rubric" in the codebase.
        from src.scoring.rubric import validate_rubric

        validate_rubric(self.weights)
        if not self.reject_below <= self.skip_below <= self.include_at_or_above:
            raise ConfigError(
                "scoring thresholds must be ordered reject_below <= skip_below "
                f"<= include_at_or_above, got {self.reject_below}/"
                f"{self.skip_below}/{self.include_at_or_above}"
            )


@dataclass
class DedupConfig:
    """Entity-resolution thresholds."""

    name_similarity_threshold: float = 0.88
    #: Similarity at or above this, but below the auto-merge threshold, is
    #: queued for human review rather than merged silently.
    review_similarity_threshold: float = 0.78
    domain_match_is_authoritative: bool = True
    ignore_domains: list[str] = field(
        default_factory=lambda: [
            "github.com", "gitlab.com", "huggingface.co", "notion.so", "notion.site",
            "vercel.app", "netlify.app", "streamlit.app", "gumroad.com", "carrd.co",
            "framer.app", "webflow.io", "wixsite.com", "replit.app", "glitch.me",
            "herokuapp.com", "pages.dev", "web.app", "firebaseapp.com", "bubbleapps.io",
            "softr.app", "canva.site", "apps.apple.com", "play.google.com",
            "chromewebstore.google.com", "chrome.google.com", "producthunt.com",
        ]
    )


@dataclass
class BatchConfig:
    """Batch/curation targets from the Tools guideline (sections 1 and 11)."""

    batch_number: int = 1
    target_size: int = 1000
    #: "If only 800 tools genuinely meet the standard, submit 800." — never pad.
    allow_under_target: bool = True
    long_term_target: int = 50000
    subsequent_batch_size: int = 5000
    #: Max records kept per primary_task so one category cannot dominate batch 1.
    max_per_primary_task: int | None = 60


@dataclass
class LLMConfig:
    """Optional LLM usage — descriptions only, never facts."""

    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"
    max_output_tokens: int = 700
    temperature: float = 0.2

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None

    @property
    def base_url(self) -> str | None:
        return os.getenv(self.base_url_env) or None


@dataclass
class Settings:
    """Root configuration object."""

    environment: str = "development"
    dry_run: bool = True
    log_level: str = "INFO"
    paths: PathsConfig = field(default_factory=PathsConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sources_path: str = str(DEFAULT_SOURCES_PATH)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(
        cls,
        settings_path: str | Path | None = None,
        *,
        env_file: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        _load_dotenv(Path(env_file) if env_file else PROJECT_ROOT / ".env")

        raw: dict[str, Any] = {}
        path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{path} must contain a YAML mapping")
            raw = loaded

        if overrides:
            raw = _deep_merge(raw, dict(overrides))

        settings = _build(cls, raw)
        settings._apply_env()
        return settings

    def _apply_env(self) -> None:
        env = os.environ
        self.environment = env.get("AIORBIT_ENV", self.environment)
        self.log_level = env.get("LOG_LEVEL", self.log_level)
        if "DRY_RUN" in env:
            self.dry_run = env["DRY_RUN"].strip().lower() in {"1", "true", "yes", "on"}
        if "HTTP_USER_AGENT" in env:
            self.http.user_agent = env["HTTP_USER_AGENT"]
        if "HTTP_TIMEOUT_SECONDS" in env:
            self.http.timeout_seconds = float(env["HTTP_TIMEOUT_SECONDS"])
        if "HTTP_RATE_LIMIT_RPS" in env:
            self.http.default_rate_limit_rps = float(env["HTTP_RATE_LIMIT_RPS"])
        if "BATCH_TARGET_SIZE" in env:
            self.batch.target_size = int(env["BATCH_TARGET_SIZE"])
        if "LLM_ENABLED" in env:
            self.llm.enabled = env["LLM_ENABLED"].strip().lower() in {"1", "true", "yes", "on"}
        if "DATA_DIR" in env:
            base = env["DATA_DIR"].rstrip("/")
            self.paths.raw = f"{base}/raw"
            self.paths.interim = f"{base}/interim"
            self.paths.processed = f"{base}/processed"
            self.paths.final = f"{base}/final"

    # ------------------------------------------------------------------ misc
    def to_dict(self) -> dict[str, Any]:
        return _as_dict(self)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _build(cls: type, data: Mapping[str, Any]) -> Any:
    """Instantiate a (possibly nested) dataclass from a mapping."""
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            continue  # forward/backward-compatible: ignore unknown keys
        field_type = known[key].type
        target = field_type if is_dataclass(field_type) else None
        if target is None:
            # With ``from __future__ import annotations`` the annotation is a
            # string, so probe the default_factory to discover nested types.
            factory = known[key].default_factory
            if factory is not MISSING:
                try:
                    probe = factory()
                except TypeError:
                    probe = None
                if is_dataclass(probe):
                    target = type(probe)
        if target is not None and isinstance(value, Mapping):
            kwargs[key] = _build(target, value)
        else:
            kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid configuration for {cls.__name__}: {exc}") from exc


def _as_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _as_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_as_dict(v) for v in obj]
    return obj


_SETTINGS: Settings | None = None


def get_settings(reload: bool = False, **kwargs: Any) -> Settings:
    """Return the process-wide settings singleton."""
    global _SETTINGS
    if _SETTINGS is None or reload:
        _SETTINGS = Settings.load(**kwargs)
    return _SETTINGS
