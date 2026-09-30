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


class _JsonFormatter(logging.Formatter):
    """Serialize application log records without logging's internal fields."""

    _reserved = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in self._reserved and not key.startswith("_")
            }
        )
        return json.dumps(payload, default=str)


def get_path() -> Path:
    return _path


def read_log(*, limit: int, before: int | None = None) -> tuple[list[dict], int]:
    """Return up to `limit` records newest-first, and the total record count.

    `before` keeps only records earlier than that `line`, so feeding back the
    oldest `line` of a page walks into the past. Line numbers stay valid across
    calls because the file is only ever appended to.
    """
    records = _read_all()
    total = len(records)
    if before is not None:
        records = [record for record in records if record["line"] < before]
    records.reverse()
    return records[:limit], total


def _read_all() -> list[dict]:
    """Return valid JSONL records in write order, tolerating a torn final line.

    Each record carries its 1-based line number as ``line``; blank and torn
    lines are skipped but still consume a number, so it names a real file line.
    """
    try:
        with get_path().open() as file:
            lines = file.readlines()
    except OSError:
        return []

    records: list[dict] = []
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record["line"] = number
        records.append(record)
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
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(stdout_handler, _HANDLER_MARKER, True)
    logger.addHandler(stdout_handler)

    logfire_handler = logfire.LogfireLoggingHandler(
        level=logfire_level,
        fallback=logging.NullHandler(),
    )
    setattr(logfire_handler, _HANDLER_MARKER, True)
    logger.addHandler(logfire_handler)
