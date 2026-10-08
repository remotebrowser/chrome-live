"""Application logging fan-out and JSONL history for ``GET /logs``."""

import atexit
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_DEFAULT_PATH = Path("/tmp/browser-trace-logs.jsonl")
_HANDLER_MARKER = "browser_trace_handler"
_path = _DEFAULT_PATH
_otel_provider: LoggerProvider | None = None


def shutdown() -> None:
    """Flush and close the active OTLP provider at process exit."""
    global _otel_provider
    if _otel_provider is not None:
        _otel_provider.shutdown()
        _otel_provider = None


atexit.register(shutdown)


def attach_traceparent(traceparent: str):
    """Attach a W3C traceparent to the current OpenTelemetry context."""
    extracted = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    return context.attach(extracted)


def detach_context(token) -> None:
    context.detach(token)


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


def configure(
    logger: logging.Logger,
    *,
    path: Path | None,
    otel_endpoint: str,
    otel_headers: str,
    service_name: str,
    otel_log_level: int,
    stdout_level: int,
) -> None:
    """Send application records to JSONL, stdout, and an optional OTLP endpoint."""
    global _path, _otel_provider
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

    if _otel_provider is not None:
        shutdown()

    if otel_endpoint:
        endpoint = otel_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/logs"):
            endpoint += "/v1/logs"
        headers = {
            key.strip(): value.strip()
            for item in otel_headers.split(",")
            if "=" in item
            for key, value in [item.split("=", 1)]
        }
        exporter = OTLPLogExporter(endpoint=endpoint, headers=headers or None)
        _otel_provider = LoggerProvider(
            resource=Resource.create({SERVICE_NAME: service_name})
        )
        _otel_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        otel_handler = LoggingHandler(
            level=otel_log_level, logger_provider=_otel_provider
        )
        setattr(otel_handler, _HANDLER_MARKER, True)
        logger.addHandler(otel_handler)
