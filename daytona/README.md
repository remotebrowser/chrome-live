# chrome-live on Daytona

This directory builds a Daytona variant of chrome-live. A consumer launches a sandbox from
the snapshot and reaches Chrome's CDP over a Daytona signed preview URL to port 9222.

The variant is the base chrome-live image plus two Daytona-specific things:

- An ENTRYPOINT that boots s6 in a nested PID namespace. Daytona overrides the image
  ENTRYPOINT with its own daemon, and s6-overlay refuses to run unless it is PID 1, so the
  snapshot must declare `["/usr/bin/unshare","--pid","--fork","--mount-proc","/init"]` (the
  sandbox has CAP_SYS_ADMIN). Sandboxes created from a snapshot can't override the entrypoint
  at create time, so this has to live in the snapshot.
- An s6 `novnc-fwd` longrun forwarding `:8080 -> :80`, because noVNC's `:80` is outside
  Daytona's previewable 3000-9999 range. Optional; it just lets you eyeball the desktop via a
  Daytona preview.

CDP itself needs no customization: the base image already serves it on `0.0.0.0:9222`, which
is exactly what a signed preview URL fronts.

Build file: [../Dockerfile.daytona](../Dockerfile.daytona). It layers on the published base
image rather than duplicating the ~110-line Fly Dockerfile, so it stays in lockstep with the
base.

## No WireGuard

An earlier version of this variant ran a WireGuard interface to reach CDP over a private
network. That was dropped: Daytona sandboxes block outbound UDP, so WireGuard never completes
a handshake. The supported path is a Daytona signed preview URL instead, a public,
self-authenticating HTTPS reverse proxy to port 9222. The embedded token is the only
credential, so the preview URL is a bearer secret. No `wireguard-tools`, no `wg0.conf`, no
private-network setup is in the image.

## How a consumer uses it

Create a sandbox from the snapshot, then get a signed preview URL for port 9222 and connect
to it:

```python
from daytona import Daytona, CreateSandboxFromSnapshotParams
daytona = Daytona()
sandbox = daytona.create(CreateSandboxFromSnapshotParams(
    snapshot="chrome-live-cloakbrowser-pro-daytona",
    public=False,
    auto_stop_interval=15,
))
preview = sandbox.create_signed_preview_url(9222)
# connect a CDP client to preview.url
```

Chrome reports `webSocketDebuggerUrl` as `ws://localhost:9222/...`, which is unreachable
through a reverse proxy. A consumer that fronts CDP behind the preview URL must rewrite that
field to the preview URL's scheme+host before using it. This is a client concern; the image
does nothing about it.

## Publishing (CI)

`.github/workflows/publish-daytona.yml` runs after the base image workflow ("Publish
Container Image") completes, so the variant builds from a fresh base:

- `ensure-image` builds and pushes the image when the workflow is dispatched manually
  (feature branches are not published on push); on `workflow_run` it verifies the image
  the base workflow just pushed exists.
- `publish-daytona` pushes snapshot `chrome-live-cloakbrowser-pro-daytona` to Daytona Cloud.
- `publish-daytona-lambda` registers the same snapshot on a self-hosted
  Daytona instance reached over the tailnet, via `daytona snapshot create --image
  ghcr.io/<repo>-daytona:<commit-sha>`. It authenticates by setting both `DAYTONA_API_URL` and
  `DAYTONA_API_KEY` (the CLI then uses an implicit env profile; do not run `daytona login` in
  that mode, it conflicts with "profile with id env not found").
- `publish-daytona` registers the same snapshot on Daytona Cloud via `snapshot push` (the
  cloud has a transient registry, so push works there). Cloud auth uses `daytona login
  --api-key "$DAYTONA_API_KEY"` (the env key alone gives "no profiles found" with no URL set).

Why create-by-reference, not push: the self-hosted instance has no transient registry, so
`snapshot push` (local-image upload) returns a 500. `snapshot create --image` instead has the
instance pull the published image. The ref must be `name:tag` with a single colon, the CLI
rejects `:latest` and also rejects `@digest` ("must contain exactly one colon"), hence the
commit-SHA tag. The image's baked ENTRYPOINT (the unshare wrapper) is preserved by create, the
sandbox's PID 1 runs `daytona <entrypoint>`, so s6 boots. `snapshot create` refuses an existing
name and `snapshot delete` is asynchronous, so the job deletes the old snapshot then retries
create until the delete propagates (~5-10s), failing fast on any other error. Deleting the
snapshot is safe while sandboxes from it are running: a sandbox copies the snapshot at create
time, so the delete/republish doesn't affect live sandboxes (they keep the old image; new
sandboxes get the new one).

The `chrome-live-daytona` ghcr package must be public (or the self-hosted instance configured
with ghcr pull credentials) so the instance can pull the image.

Consumers pin the stable snapshot name `chrome-live-cloakbrowser-pro-daytona` on whichever instance they use.

Required CI configuration:

| Kind   | Name                      | Used by                | Purpose                                                    |
| ------ | ------------------------- | ---------------------- | ---------------------------------------------------------- |
| secret | `DAYTONA_LAMBDA_API_URL`  | publish-daytona-lambda | Self-hosted API URL, e.g. `http://<tailscale-ip>:3000/api` |
| secret | `DAYTONA_LAMBDA_API_KEY`  | publish-daytona-lambda | Self-hosted Daytona API key                                |
| secret | `TS_OAUTH_CLIENT_ID`      | publish-daytona-lambda | Tailscale OAuth client id                                  |
| secret | `TS_OAUTH_SECRET`         | publish-daytona-lambda | Tailscale OAuth client secret                              |
| secret | `DAYTONA_API_KEY`         | publish-daytona (cloud) | Daytona Cloud API key                                     |

The runner must be on the tailnet to reach `DAYTONA_LAMBDA_API_URL`. The "Connect to Tailscale"
step handles that on GitHub-hosted runners; its OAuth client must be authorized for the `tag:ci`
ACL tag (or change the `tags:` input). When running where the host already has tailnet access
(e.g. local `act` on a machine on the tailnet), comment that step out.

One-time ghcr package step (required): make the `chrome-live-daytona` package public (Package
settings -> Change visibility), the same as `chrome-live`. The package is created private on
the first push; visibility is sticky, so this is done once, and there is no REST API for it.
This is required, not cosmetic: `snapshot create --image` has the self-hosted instance pull the
image, which fails on a private package unless the instance has ghcr credentials.

## Build and snapshot manually

```sh
# build (from the repo root). Daytona's host is x86_64, so build linux/amd64.
docker build --platform linux/amd64 -f Dockerfile.daytona -t chrome-live-daytona:latest .

# push the local image to Daytona as a named snapshot. The Dockerfile ENTRYPOINT is
# preserved, so no --entrypoint flag is needed.
daytona snapshot push chrome-live-daytona:latest \
  --name chrome-live-cloakbrowser-pro-daytona --cpu 2 --memory 2 --disk 10
```

Pin `BASE_IMAGE` to a digest for reproducible builds:

```sh
docker build --platform linux/amd64 -f Dockerfile.daytona \
  --build-arg BASE_IMAGE=ghcr.io/remotebrowser/chrome-live@sha256:<digest> \
  -t chrome-live-daytona:latest .
```

Daytona rejects `latest`/`lts`/`stable` tags when pulling a base image for a snapshot, so pin
a specific tag or digest there.

## Verify the snapshot boots

Create a sandbox and confirm s6 came up (PID 1 is the unshare wrapper, not Daytona's
`sleep infinity` daemon) and CDP answers:

```sh
daytona create --snapshot <snapshot-name> --auto-stop 0   # add --public only for manual poking
daytona exec <sandbox-id> -- sh -c 'ps -e | grep -E "s6-svscan|chrome|socat" | head; \
  curl -s -o /dev/null -w "CDP %{http_code}\n" http://127.0.0.1:9222/json/version'
```

`s6-svscan` running and `CDP 200` means the entrypoint baked correctly and the stack is up.
If PID 1 is `daytona ... sleep infinity` with no s6, the snapshot did not carry the
entrypoint, re-push it with an explicit `--entrypoint '/usr/bin/unshare --pid --fork --mount-proc /init'`.

## VNC / desktop access (use noVNC on :8080, not Daytona Computer Use)

To watch the desktop, open the preview link for port `8080` (`sandbox.get_preview_link(8080)`).
That is the image's own noVNC, the one Chrome actually renders to.

Do not use Daytona's built-in VNC (the Computer Use toolbox / dashboard VNC button) with this
image. It does not work here, by design:

- This image runs its own `Xvnc` on `DISPLAY :99` / `rfbport 5900`, with XFCE and Chrome on
  `:99` and `websockify` serving noVNC on `:80` (re-exposed on `:8080` by `novnc-fwd`).
- Daytona Computer Use starts its own Xvfb + x11vnc + XFCE + noVNC on a separate display. Its
  `x11vnc` collides with our `Xvnc` on `:5900`, and even if it bound a free port it would
  render its own empty desktop, not our Chrome on `:99`. The two are separate display servers.

So a black Daytona VNC view is expected, not a bug in our stack; ours is the viewer with the
browser.
