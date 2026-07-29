# Chrome Live

<img width="800" src="screenshot.jpg" alt="Screenshot" />

Run a containerized Google Chrome on Linux, accessible from any web browser.

Try using Docker:

```
docker run --name chrome-live -p 7000:80 ghcr.io/remotebrowser/chrome-live
```

or Podman:

```
podman run --name chrome-live -p 7000:80 ghcr.io/remotebrowser/chrome-live
```

Then open `localhost:7000` in your browser.

To enable remote control of Chrome via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/), map port 9222 as well:

```
podman run --name chrome-live -p 7000:80 -p 9222:9222 ghcr.io/remotebrowser/chrome-live
```

Configure Chrome's proxy connection using [Tinyproxy](https://tinyproxy.github.io) (refer to the sample `tinyproxy.conf`).

To allow specific domains, add them to `allowlist.txt` (one domain per line).

To test the CDP connection:

```
curl http://127.0.0.1:9222/json/list
```

To build and run locally:

```
docker build -t chrome-live .
docker run -p 7000:80 chrome-live
```

To deploy to [Fly.io](https://fly.io)

```
fly apps create test-chrome-live
fly deploy --ha=false -a test-chrome-live

# test CDP connection
FLY_IP=$(fly ips list -a test-chrome-live --json | jq -r '.[] | select(.Type=="v4") | .Address')
curl http://$FLY_IP:9222/json/list
```

## Stealth browser (CloakBrowser)

The image ships [CloakBrowser](https://pypi.org/project/cloakbrowser/) — a stealth
Chromium build — alongside Google Chrome. Chrome is the default. Both serve CDP on the
same internal port (`:9221`), so browser-trace and the `:9222` proxy work identically
regardless of which is active.

> The image is **amd64 only**. Google Chrome has no arm64 Linux package and CloakBrowser
> publishes no arm64 binary, so the base image builds `linux/amd64` exclusively.

Switch at runtime (e.g. over ssh into the container):

```
switch-browser cloak    # kill Chrome, launch CloakBrowser
switch-browser chrome   # switch back
```

The choice persists in `/home/user/.active-browser` and the `chromium` service restarts on
the same CDP endpoint.

### CloakBrowser Pro

Pass a [CloakBrowser Pro](https://github.com/CloakHQ/cloakbrowser) license key to download
the latest Pro binary (Chromium 150+) instead of the free v146 build.

**Build time** (bakes Pro into the image):

```
docker build --build-arg CLOAKBROWSER_LICENSE_KEY=cb_xxxxxxxx -t chrome-live .
```

**Runtime** (re-downloads Pro on container start if not baked in):

```
docker run -e CLOAKBROWSER_LICENSE_KEY=cb_xxxxxxxx -p 7000:80 chrome-live
```

On Fly.io: `fly secrets set CLOAKBROWSER_LICENSE_KEY=cb_xxxxxxxx`

CI publish reads the same key from the `CLOAKBROWSER_LICENSE_KEY` repository secret.

Daytona publishes a dedicated snapshot: `chrome-live-cloakbrowser-pro-daytona`.

## Daytona backend

The same image also runs on Daytona (a sandbox reached via a Daytona signed preview URL to
CDP on `:9222`); `start-init.sh` auto-detects Daytona/Fly vs. a plain Docker host at boot.
See [daytona/README.md](daytona/README.md).

## Provider startup benchmarks

Measured cold-boot and resume latency for chrome-live on several hosting
providers, with a recommendation for which orchestration approach (direct spawn
vs pre-warmed pool) fits each.

- Aggregated results and ranking: [bench/REPORT.md](bench/REPORT.md)
- Harness layout, credentials, and reproduction steps: [bench/README.md](bench/README.md)
