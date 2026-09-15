"""Centralised logging configuration.

Two handlers are installed:

* a human readable console handler (level from ``LOG_LEVEL``);
* a JSON-lines file handler under ``logs/`` so a pipeline run can be audited
  after the fact (required for the "resilience / logging" engineering
  principle in the technical specification).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonLinesFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    level: str | int | None = None,
    log_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """Configure the root logger once and return the pipeline logger."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED and not force:
        return logging.getLogger("aiorbit")

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    level = level or os.getenv("LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    root.addHandler(console)

    log_dir = Path(log_dir or os.getenv("LOG_DIR", "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "pipeline.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonLinesFormatter())
        root.addHandler(file_handler)
    except OSError:  # read-only FS: console logging is still enough
        root.warning("could not create log directory %s; file logging disabled", log_dir)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    _CONFIGURED = True
    return logging.getLogger("aiorbit")


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger, configuring logging on first use."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"aiorbit.{name}" if not name.startswith("aiorbit") else name)
