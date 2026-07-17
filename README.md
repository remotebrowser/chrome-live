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

## Automatic CAPTCHA solving

Chrome Live can solve CAPTCHAs end-to-end inside the browser session — detect,
fetch a solution, inject the token, and click the checkbox/verify — with no work
required from the CDP client (mirroring Steel's `solveCaptcha`). This is powered
by the bundled [CapSolver](https://www.capsolver.com) browser extension and is
**off by default**. When disabled, the extension is not loaded at all.

Enable it per session with two environment variables:

```
podman run --name chrome-live -p 7000:80 -p 9222:9222 \
  -e SOLVE_CAPTCHA=1 \
  -e CAPSOLVER_API_KEY=<your-capsolver-key> \
  ghcr.io/remotebrowser/chrome-live
```

- `SOLVE_CAPTCHA=1` loads the extension; anything else (or unset) leaves it out.
- `CAPSOLVER_API_KEY` is your CapSolver API key. It is written into the
  extension's config file at startup, never passed on Chrome's command line.

Supported CAPTCHA types: reCAPTCHA v2, reCAPTCHA v3, Cloudflare Turnstile, and
image-to-text. FunCAPTCHA, enterprise, and custom CAPTCHAs are out of scope.

Notes:

- CapSolver is a **paid, metered** service — each solve consumes your account
  balance. Keeping it opt-in avoids surprise usage.
- The key is read from the extension config on a **fresh Chrome profile**
  (the image ships one). If you persist `/home/user/chrome-profile` across
  restarts and change the key, clear the profile so the new key takes effect.
- The solver reaches `api.capsolver.com` through the built-in proxy; that host
  is already in `allowlist.txt`.

## Daytona backend

A variant for running on Daytona, reached via a Daytona signed preview URL to CDP
on `:9222`. See [daytona/README.md](daytona/README.md).

## Provider startup benchmarks

Measured cold-boot and resume latency for chrome-live on several hosting
providers, with a recommendation for which orchestration approach (direct spawn
vs pre-warmed pool) fits each.

- Aggregated results and ranking: [bench/REPORT.md](bench/REPORT.md)
- Harness layout, credentials, and reproduction steps: [bench/README.md](bench/README.md)
