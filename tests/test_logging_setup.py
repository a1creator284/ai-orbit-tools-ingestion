"""Logging regression tests.

Every stage logs structured context via ``extra={...}`` and identifies the
offending record with ``extra={"name": ...}``. The stdlib raises ``KeyError``
for such a key, which would convert a *handled* per-record failure into an
unhandled crash and break the pipeline's graceful-degradation contract.
"""

from __future__ import annotations

import logging

from src.core.logging_setup import SafeExtraLogger, get_logger


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestSafeExtra:
    def test_pipeline_loggers_use_the_safe_logger_class(self) -> None:
        assert isinstance(get_logger("test.safe"), SafeExtraLogger)

    def test_reserved_extra_key_does_not_raise(self) -> None:
        logger = get_logger("test.reserved")
        # would raise KeyError: "Attempt to overwrite 'name' in LogRecord"
        logger.warning("record dropped", extra={"name": "Jasper", "error": "boom"})

    def test_reserved_key_context_is_preserved_under_a_prefix(self) -> None:
        logger = get_logger("test.prefix")
        handler = CapturingHandler()
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning("record dropped", extra={"name": "Jasper", "error": "boom"})
        finally:
            logger.removeHandler(handler)
            logger.propagate = True

        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.ctx_name == "Jasper"
        assert record.error == "boom"
        # the logger's own identity is untouched
        assert record.name == "aiorbit.test.prefix"

    def test_non_reserved_keys_are_passed_through_unchanged(self) -> None:
        logger = get_logger("test.passthrough")
        handler = CapturingHandler()
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.info("ok", extra={"tool": "Jasper", "source": "taaft"})
        finally:
            logger.removeHandler(handler)
            logger.propagate = True

        record = handler.records[0]
        assert record.tool == "Jasper"
        assert record.source == "taaft"


class TestCallSites:
    """The concrete call sites that used to crash."""

    def test_normalizer_logs_an_unusable_candidate_safely(self) -> None:
        from src.cleaning.normalizer import ToolNormalizer
        from src.discovery.base import CandidateTool

        unusable = CandidateTool(name="Ghost", source_key="taaft", source_name="TAAFT")
        assert ToolNormalizer().from_candidate(unusable) is None

    def test_candidate_store_logs_a_failed_rehydration_safely(self) -> None:
        from src.candidates.store import CandidateLoadReport, candidate_from_dict

        report = CandidateLoadReport()
        # a name that survives cleaning but a payload that cannot build
        assert candidate_from_dict({"name": "X", "source_key": "k"}, report=report) is None
        assert report.skipped == 1
