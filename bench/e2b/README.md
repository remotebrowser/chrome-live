# chrome-live startup benchmarks

Reproducible startup-latency benchmarks for running the chrome-live container on
different cloud platforms. Goal: per-session instance ready (CDP responding +
noVNC frame) in under ~1s, scaling to zero with no fixed base fee.

Why this exists: vendor latency claims are not trustworthy for our heavy ~2GB
GUI image (Ubuntu + Chrome + XFCE + VNC + tinyproxy). The image boots to
CDP-ready in ~0.85-1.37s on local NVMe (Apple Silicon, warm image), but ~30x
slower on Fly's HDD-backed rootfs. Disk I/O of the rootfs is the dominant
factor, so every result must record the storage type.

## Layout

- `lib.sh` — shared, platform-agnostic timing harness. Defines the `bp_*` hook
  contract and `bench_main`. Do not duplicate timing logic in platform scripts.
- `<platform>.sh` — one per platform (`fly`, `northflank`, `daytona`, `modal`,
  `e2b`, `koyeb`, `cloudflare`, `hetzner-local`). Each defines the `bp_*` hooks
  for that platform and calls `bench_main`. Idempotent; cleans up what it creates.
- `fly.sh` is the reference implementation; copy its shape for new platforms.

## Running

Common env knobs (see `lib.sh`):

    MODE=cold|resume   # which start path to measure (default cold)
    COUNT=10           # number of timed runs
    READY_TIMEOUT=90   # per-run seconds before a run counts as TIMEOUT
    PLATFORM=<name>    # label in output (set by each platform script)

Each platform script documents its own required credentials/env at the top.
Example:

    FLY_APP=chrome-live-bench MODE=cold   COUNT=10 bench/fly.sh
    FLY_APP=chrome-live-bench MODE=resume COUNT=10 bench/fly.sh

## What each run measures

Wall-clock from the trigger action (start/resume) until:
- CDP ready: `curl <cdp-endpoint>/json/version` responds (primary number), and
- first noVNC frame served on `:80` (user-perceived).

Reported as p50/p95/min/max over COUNT runs. For platforms with a
pause/suspend/snapshot path, run both `MODE=cold` and `MODE=resume`.

## Per-platform required credentials

| Platform      | Needs                                              |
| ------------- | -------------------------------------------------- |
| fly           | `flyctl` authenticated; a non-production `FLY_APP` |
| northflank    | Northflank account + API token / CLI               |
| daytona       | Daytona account + API key                          |
| modal         | Modal account + token                              |
| e2b           | E2B account + API key (Hobby tier OK for a trial)  |
| koyeb         | Koyeb account + API token                          |
| cloudflare    | Cloudflare account, Workers Paid ($5/mo) + wrangler|
| hetzner-local | Hetzner Cloud API token (provisions a NVMe VM)     |

## E2B notes

E2B needs only `E2B_API_KEY` (the SDK and Build System 2.0 build templates with
just the key; the `e2b` CLI's separate access token is not required). Build the
template with `bench/e2b_build.py` (pulls the public `chrome-live` image, 2 vCPU
/ 2 GiB), then run `bench/e2b.sh` with `E2B_TEMPLATE=<id>`.

Two things about this GUI image don't fit E2B's model and shape the harness:

- The image's s6 entrypoint must be PID 1, which it gets via `unshare --pid`.
  E2B's *start-command* context lacks `CAP_SYS_ADMIN`, so `unshare` is denied
  there (the start command can't boot the stack). `commands.run` as root has
  full caps, so the template's start command is just `sleep infinity` and the
  driver launches the real init with `commands.run(background=True)` right after
  create/resume. The init-launch RPC is therefore inside the timed cold window.
- Chrome's DevTools HTTP endpoint rejects a non-localhost `Host` header, and
  E2B's host-based proxy must forward `<port>-<id>.e2b.app` as `Host`, so CDP is
  unreachable through the public proxy (500; no `Host` value satisfies both the
  edge router and Chrome). CDP readiness is therefore measured intra-sandbox
  (`curl 127.0.0.1:9222`). noVNC :80 has no such check and is polled via the
  public `get_host(80)` URL. Exposing CDP publicly on E2B would need a
  Host-rewriting sidecar.

Chrome's cold boot here is disk-I/O/CPU bound on the 2-vCPU microVM and highly
variable (~9–33s observed), so `READY_TIMEOUT=180` is the default; the microVM
spawn itself is sub-second (NVMe).
