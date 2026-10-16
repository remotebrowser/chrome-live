# Host OpenTelemetry Collector

Run one collector per host. Chrome containers send OTLP to it and hold no Logfire token.

```sh
podman network create chrome-live

podman run -d --name otelcol --network chrome-live \
  -e LOGFIRE_TOKEN=pylf_v1_... \
  -v ./otelcol/config.yaml:/etc/otelcol-contrib/config.yaml:ro,Z \
  otel/opentelemetry-collector-contrib:0.161.0

podman run --network chrome-live \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otelcol:4318 \
  ... chrome-live
```

Do not set `LOGFIRE_TOKEN` on the Chrome containers, or browser-trace also sends directly to Logfire.
