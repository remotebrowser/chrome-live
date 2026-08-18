# browser-trace

Monitors browser tab opens and page navigations via the Chrome DevTools Protocol (CDP) and reports them to [Pydantic Logfire](https://logfire.pydantic.dev/) as telemetry events. Also ships tinyproxy log lines from stdin to Logfire when invoked in `tinyproxy` mode — used by chrome-live to surface upstream residential-proxy 407s and connect failures.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A Chromium-based browser running with remote debugging enabled:
  ```sh
  google-chrome --remote-debugging-port=9222
  # or
  chromium --remote-debugging-port=9222
  ```

## Installation

```sh
git clone <repo-url> && cd browser-trace
uv sync
```

## Configuration

Create a config file (e.g. `.env`) with key=value pairs:

```
SERVICE_NAME=browser-trace
LOGFIRE_TOKEN=your-logfire-write-token
LOGFIRE_TRACEPARENT=00-abc123...-01
CDP_HOST=127.0.0.1
CDP_PORT=9222
```

| Key                     | Description                                                                                                                                                                                                                                                                                                                 | Required |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| `SERVICE_NAME`          | Service name reported to Logfire (default: `browser-trace`)                                                                                                                                                                                                                                                                 | No       |
| `LOGFIRE_TOKEN`         | Logfire write token for sending telemetry                                                                                                                                                                                                                                                                                   | No       |
| `LOGFIRE_TRACEPARENT`   | W3C traceparent to attach events to a parent trace                                                                                                                                                                                                                                                                          | No       |
| `LOG_LEVEL`             | Minimum severity gate for both the Fly-logs tee **and** Logfire emission (passed through to `logfire.configure(min_level=...)`). Default `INFO` drops tinyproxy `CONNECT` / `INFO` (mapped to `logfire.debug`) from both sinks; set `DEBUG` to surface them. Accepted: `DEBUG`, `INFO`, `NOTICE`, `WARN`, `ERROR`, `FATAL`. | No       |
| `CDP_HOST`              | Chrome DevTools Protocol host (default: `127.0.0.1`)                                                                                                                                                                                                                                                                        | No       |
| `CDP_PORT`              | Chrome DevTools Protocol port (default: `9222`)                                                                                                                                                                                                                                                                             | No       |
| `RECORDING_DIR`         | Directory for recordings (default: `/tmp/recordings`)                                                                                                                                                                                                                                                                       | No       |

The config file is watched for changes every 2 seconds, so `LOGFIRE_TRACEPARENT` can be updated at runtime without restarting the service.

## Usage

Two modes, selected by subcommand. The legacy positional form (`uv run main.py <config>`) is preserved as sugar for `cdp`.

### `cdp` — browser navigation tracing

```sh
uv run main.py cdp .env
```

1. Connect to the browser's CDP websocket at `127.0.0.1:9222`
2. Auto-attach to all page targets
3. Emit `tab_opened` events when new tabs are created
4. Emit `navigation` events (with HTTP status codes) for top-frame document navigations
5. Emit `tab_traffic` events with per-tab / per-host byte totals (see [Traffic accounting](#traffic-accounting))
6. Send all events to Logfire if a token is configured

### `tinyproxy` — tinyproxy log shipper

```sh
tinyproxy -d -c /etc/tinyproxy.conf | uv run main.py tinyproxy .env
```

Reads tinyproxy log lines from stdin (one per line), parses the leading log level (`CONNECT`, `ERROR`, `WARNING`, `NOTICE`, `CRITICAL`, `INFO`), tees each line to stdout for container log collectors, and emits to Logfire via the appropriate severity (`logfire.info` / `logfire.warn` / `logfire.error` / `logfire.notice`). Each event carries a `tinyproxy_level` attribute so the level is queryable in Logfire. Configure tinyproxy with `LogFile "/dev/stdout"` so its log writes flow to the pipe.

### `record` — send a recording to a pre-signed URL

```sh
uv run main.py record --url="<presigned_put_url>" [--recording-id <id>]
```

PUTs a finalized recording at a URL someone else signed. This container holds no bucket
credentials and never names a key — the control plane signs for the key it wants and passes only
the URL. Without `--recording-id` the newest recording is sent; `--file` sends an exact path, and
`--config` is read for `RECORDING_DIR`.

The `Content-Type` sent is always `video/mp4` and must match what the URL was signed with, or S3
answers `SignatureDoesNotMatch`. Transient failures (5xx, timeouts, connection errors) are retried
with backoff; a 4xx is not, because a mis-signed or expired URL will not start working. Retry
notices go to stdout and only a fatal error goes to stderr, so a caller that treats stderr as
failure sees nothing from a run that recovered. Exit status is 0 on success, 1 otherwise.

Uploading changes nothing on disk: the local MP4 stays and `GET /recordings/{id}/video` keeps
serving it, so a repeat is harmless.

## Recordings

Recording is always-on in `cdp` mode: a screencast is captured for every tab and finalized when the tab closes. Recordings land in `RECORDING_DIR` as `<id>.mp4` + `<id>.json` sidecar files.

## Traffic accounting

`cdp` mode sums `Network.dataReceived` / `Network.loadingFinished` `encodedDataLength` per tab and per host, so proxy data usage can be attributed to the pages that caused it. Cache hits report zero bytes, so they cost nothing here, matching what a proxy would bill.

Reporting:

- `GET /traffic` on the HTTP server returns live totals: process-wide bytes and request counts, a host ranking, and a per-tab breakdown for open tabs plus the last 50 closed ones. `?hosts=N` caps the host ranking (default 20).
- A `tab_traffic` event goes to Logfire once a minute per active tab and once more when the tab closes. It carries `bytes_received` (the tab's running total), `bytes_delta` (increase since that tab's previous event, so deltas sum over a session), `requests`, `host_count`, and a `hosts` map of the top 10 hosts by bytes. Tabs that pulled nothing since the last rollup are skipped.

These totals are a floor on the real cost, not the bill. Measured against a byte-counting proxy placed under Chrome, one Wikipedia article in a fresh profile came to 573,305 bytes here against 697,683 on the wire, so 82%. The gap is TLS handshakes and certificate chains (per-connection, so per-host coverage ran from 97% on the main document down to 19% on a host contacted once for 1.5 KB), request/upload bytes and TCP overhead, which CDP does not report at all, and Chrome's own background traffic, which belongs to no tab. Calibrate against the proxy provider's usage API before using these numbers for billing.

Known blind spots: WebSocket payloads (frames emit no `dataReceived`), and fetches issued by service-worker or worker targets, since only `type == "page"` targets are attached.

## Building a standalone binary

```sh
uv run --group dev pyinstaller browser-trace.spec
```

The binary will be in `dist/browser-trace`. Build from the spec (not
`--onefile main.py` directly) so `captcha_classifier.js` is bundled as a data
file — `main.py` reads it at runtime from `sys._MEIPASS` when frozen.
