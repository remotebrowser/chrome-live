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
5. Send all events to Logfire if a token is configured

### `tinyproxy` — tinyproxy log shipper

```sh
tinyproxy -d -c /etc/tinyproxy.conf | uv run main.py tinyproxy .env
```

Reads tinyproxy log lines from stdin (one per line), parses the leading log level (`CONNECT`, `ERROR`, `WARNING`, `NOTICE`, `CRITICAL`, `INFO`), tees each line to stdout for container log collectors, and emits to Logfire via the appropriate severity (`logfire.info` / `logfire.warn` / `logfire.error` / `logfire.notice`). Each event carries a `tinyproxy_level` attribute so the level is queryable in Logfire. Configure tinyproxy with `LogFile "/dev/stdout"` so its log writes flow to the pipe.

## Recordings

Recording is always-on in `cdp` mode: a screencast is captured for every tab and finalized when the tab closes. Recordings land in `RECORDING_DIR` as `<id>.mp4` + `<id>.json` sidecar files.

## Building a standalone binary

```sh
uv run --group dev pyinstaller browser-trace.spec
```

The binary will be in `dist/browser-trace`. Build from the spec (not
`--onefile main.py` directly) so `captcha_classifier.js` is bundled as a data
file — `main.py` reads it at runtime from `sys._MEIPASS` when frozen.
