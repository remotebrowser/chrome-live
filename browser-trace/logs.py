"""Application logging fan-out and JSONL history for ``GET /logs``."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import logfire

_DEFAULT_PATH = Path("/tmp/browser-trace-logs.jsonl")
_HANDLER_MARKER = "browser_trace_handler"
_path = _DEFAULT_PATH


_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _extra_fields(record: logging.LogRecord, *, exclude: frozenset[str] = frozenset()) -> dict:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED and key not in exclude and not key.startswith("_")
    }


class _JsonFormatter(logging.Formatter):
    """Serialize application log records without logging's internal fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    """Render extra fields inline so a generic message (kept short for Logfire's
    span_name cardinality) still reads as a full line on stdout/Fly logs."""

    def format(self, record: logging.LogRecord) -> str:
        extras = _extra_fields(record, exclude=frozenset({"event"}))
        suffix = " ".join(f"{key}={value}" for key, value in extras.items())
        message = record.getMessage()
        return f"{message} {suffix}" if suffix else message


def get_path() -> Path:
    return _path


def read_all() -> list[dict]:
    """Return valid JSONL records in write order, tolerating a torn final line."""
    try:
        with get_path().open() as file:
            lines = file.readlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    records: list[dict] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def configure(
    logger: logging.Logger,
    *,
    path: Path | None,
    logfire_level: int,
    stdout_level: int,
) -> None:
    """Send every application record to JSONL, stdout, and Logfire."""
    global _path
    _path = path or _DEFAULT_PATH
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())
    setattr(file_handler, _HANDLER_MARKER, True)
    logger.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(stdout_level)
    stdout_handler.setFormatter(_TextFormatter())
    setattr(stdout_handler, _HANDLER_MARKER, True)
    logger.addHandler(stdout_handler)

    logfire_handler = logfire.LogfireLoggingHandler(
        level=logfire_level,
        fallback=logging.NullHandler(),
    )
    setattr(logfire_handler, _HANDLER_MARKER, True)
    logger.addHandler(logfire_handler)
