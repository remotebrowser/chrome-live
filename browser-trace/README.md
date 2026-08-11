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
| `TIGRIS_BUCKET`         | Bucket for uploaded recordings. Uploading is impossible unless this and both keys are set                                                                                                                                                                                                                                    | No       |
| `TIGRIS_ACCESS_KEY_ID`  | S3 access key id                                                                                                                                                                                                                                                                                                            | No       |
| `TIGRIS_SECRET_ACCESS_KEY` | S3 secret access key                                                                                                                                                                                                                                                                                                     | No       |
| `TIGRIS_ENDPOINT_URL`   | S3 endpoint (default: `https://t3.storage.dev`)                                                                                                                                                                                                                                                                             | No       |
| `TIGRIS_REGION`         | S3 region (default: `auto`)                                                                                                                                                                                                                                                                                                 | No       |

The upload toggle and the client browser id are deliberately absent from this table: they are runtime-only, set over HTTP by `POST /recordings/config`, and never read from or written to the config file.

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

## Recordings

Recording is always-on in `cdp` mode: a screencast is captured for every tab and finalized when the tab closes. Recordings land in `RECORDING_DIR` as `<id>.mp4` + `<id>.json` sidecar files.

### Uploading to object storage

A finalized recording is also uploaded to an S3-compatible bucket (Tigris) when **both** gates are open:

1. Credentials are present (`TIGRIS_BUCKET` + both keys), templated into the config from the container's env.
2. `upload_enabled` is on for this browser.

`upload_enabled` defaults to `false` and is toggled over HTTP:

```sh
curl -X POST localhost:8088/recordings/config \
  -H 'content-type: application/json' \
  -d '{"upload_enabled": true, "browser_id": "xyz123"}'

curl localhost:8088/recordings/config
```

Both values live in memory for the life of the process. A browser-trace restart or a machine stop/start reverts them to `false` / unset, so the caller must re-POST after either. Nothing is written to the config file, and a config reload (say a `LOG_LEVEL` edit) leaves them alone.

The object key is `<browser_id>/<recording_id>.mp4` (flat when no `browser_id` is set) — the container can't derive the client's browser id on its own, so whoever flips the toggle supplies it. Uploads happen in the background at tab close, so a slow bucket never stalls the CDP event loop; shutdown waits up to 30s for in-flight transfers.

Two consequences worth knowing:

- **No backfill.** Only recordings finalized while the toggle is on are uploaded. Turning it on mid-session does not send what's already on disk.
- **The local copy is kept**, so `GET /recordings/{id}/video` keeps working. The sidecar gains `upload_key` once the upload lands. A failed upload is logged and leaves the file in place; nothing retries it.

#### Not finished yet

The container side is complete and wired end to end. What is not:

- **Nothing calls `POST /recordings/config`.** The container cannot derive the client's browser id — `SERVICE_NAME` is the internal fly app name — so the control plane has to supply it. That caller lives in another repo and does not exist yet, so in practice `upload_enabled` is never anything but `false` and no recording is ever uploaded. That caller also has to re-POST after every restart and machine stop/start, since neither value is persisted.
- **The onefile binary has not been built since boto3 was added.** `browser-trace.spec` was not changed. botocore loads its endpoint/service JSON dynamically, which normally needs help from PyInstaller; `pyinstaller-hooks-contrib` ships `hook-boto3` and `hook-botocore`, so it will most likely bundle correctly on its own — but nobody has run the build and started the result. Expect the binary to grow by tens of MB.
- **`GET /recordings/config` reports `upload_enabled` from the toggle alone**, while an upload additionally requires credentials. Deploy without `TIGRIS_*` set, POST `{"upload_enabled": true}`, and the endpoint answers `upload_enabled: true` while nothing is ever uploaded. `storage_configured` in the same response is what actually tells you; the more obvious field is the misleading one.
- **A restart silently drops the toggle and the browser id.** Neither is persisted, so uploads stop and object keys go flat until the control plane POSTs again. If that turns out to be the wrong tradeoff, persistence belongs either back in the config file or in flyfleet templating `BROWSER_ID` into the machine's env at create time.

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
