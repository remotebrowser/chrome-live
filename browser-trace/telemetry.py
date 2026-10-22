"""OpenTelemetry log export over OTLP/HTTP."""

import logging
from collections.abc import Generator
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.re import parse_env_headers

_propagator = TraceContextTextMapPropagator()


class _OwningHandler(LoggingHandler):
    """Shuts down its provider on close, so a config reload flushes and frees it."""

    def __init__(self, level: int, provider: LoggerProvider) -> None:
        super().__init__(level=level, logger_provider=provider)
        self._provider = provider

    def close(self) -> None:
        super().close()
        self._provider.shutdown()


def create_handler(
    *,
    service_name: str,
    environment: str,
    endpoint: str,
    headers: str,
    level: int,
) -> logging.Handler | None:
    """Build an OTLP log handler, or return None when no endpoint is configured."""
    if not endpoint:
        return None

    provider = LoggerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment.name": environment}
        )
    )
    exporter = OTLPLogExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/logs",
        headers=dict(parse_env_headers(headers, liberal=True)),
    )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return _OwningHandler(level, provider)


@contextmanager
def traceparent_context(traceparent: str | None) -> Generator[None]:
    """Link log records emitted inside this block to a W3C traceparent."""
    if not traceparent:
        yield
        return
    token = otel_context.attach(_propagator.extract({"traceparent": traceparent}))
    try:
        yield
    finally:
        otel_context.detach(token)
