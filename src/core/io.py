"""Artefact persistence helpers.

All pipeline stages read/write JSON or JSON-Lines under ``data/`` so any stage
can be re-run independently from the previous stage's output (reproducibility).
Writes are atomic (temp file + rename) so an interrupted run never leaves a
half-written dataset behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel

from src.core.logging_setup import get_logger

logger = get_logger("io")


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def _to_jsonable(record: Any) -> Any:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    return record


def write_json(path: str | Path, data: Any, *, indent: int = 2) -> Path:
    """Atomically write ``data`` as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_to_jsonable(data), indent=indent, ensure_ascii=False, default=_default)
    _atomic_write(path, payload)
    logger.debug("wrote json", extra={"path": str(path), "bytes": len(payload)})
    return path


def read_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: str | Path, records: Iterable[Any]) -> Path:
    """Atomically write an iterable of records as JSON-Lines."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(_to_jsonable(record), ensure_ascii=False, default=_default)
        for record in records
    ]
    _atomic_write(path, "\n".join(lines) + ("\n" if lines else ""))
    logger.debug("wrote jsonl", extra={"path": str(path), "records": len(lines)})
    return path


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream JSON-Lines records, skipping (and logging) malformed lines."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "skipping malformed jsonl line",
                    extra={"path": str(path), "line": number, "error": str(exc)},
                )


def append_jsonl(path: str | Path, record: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_to_jsonable(record), ensure_ascii=False, default=_default) + "\n")


def _atomic_write(path: Path, payload: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
