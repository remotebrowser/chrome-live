"""Application logging fan-out and JSONL history for ``GET /logs``."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

_DEFAULT_PATH = Path("/tmp/browser-trace-logs.jsonl")
_HANDLER_MARKER = "browser_trace_handler"
_path = _DEFAULT_PATH
_provider: LoggerProvider | None = None
_provider_key: tuple[str, str, str] | None = None


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


def _logger_provider(endpoint: str, service_name: str, environment: str) -> LoggerProvider | None:
    """Reuse the provider until its resource or endpoint changes, flushing the old one."""
    global _provider, _provider_key
    key = (endpoint, service_name, environment)
    if key == _provider_key:
        return _provider
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _provider_key = key
    if endpoint:
        resource = Resource.create(
            {"service.name": service_name, "deployment.environment.name": environment}
        )
        _provider = LoggerProvider(resource=resource)
        _provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint.rstrip('/')}/v1/logs"))
        )
    return _provider


def configure(
    logger: logging.Logger,
    *,
    path: Path | None,
    otlp_endpoint: str,
    service_name: str,
    environment: str,
    otlp_level: int,
    stdout_level: int,
) -> None:
    """Send every application record to JSONL, stdout, and OTLP."""
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

    provider = _logger_provider(otlp_endpoint, service_name, environment)
    if provider is not None:
        otlp_handler = LoggingHandler(level=otlp_level, logger_provider=provider)
        setattr(otlp_handler, _HANDLER_MARKER, True)
        logger.addHandler(otlp_handler)
