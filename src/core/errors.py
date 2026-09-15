"""Exception hierarchy for the AI Orbit Tools ingestion pipeline.

Every stage raises a subclass of :class:`PipelineError` so the orchestrator can
degrade gracefully (spec: "Resilience: Graceful degradation and logging for
missing fields") instead of aborting a whole batch on one bad record.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors."""

    #: When True the orchestrator may skip the offending record and continue.
    recoverable: bool = True


class ConfigError(PipelineError):
    """Invalid or missing configuration."""

    recoverable = False


class DiscoveryError(PipelineError):
    """A discovery source failed to produce candidates."""


class ExtractionError(PipelineError):
    """A raw payload could not be parsed into a candidate record."""


class NormalizationError(PipelineError):
    """A record could not be normalized into the canonical schema."""


class VerificationError(PipelineError):
    """Official-website verification could not be completed."""


class ValidationError(PipelineError):
    """A record failed schema/business validation."""


class ScoringError(PipelineError):
    """Quality scoring could not be computed."""


class FetchError(PipelineError):
    """Network-level failure while fetching a URL."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status
