# chrome-live startup benchmarks

Reproducible startup-latency benchmarks for running the chrome-live container on
different cloud platforms. Each platform validation produces a working script
that re-runs end-to-end, plus raw result logs. The aggregated comparison is in
[REPORT.md](REPORT.md).

Why this exists: vendor latency claims are not trustworthy for our heavy ~2GB
GUI image (Ubuntu + Chrome + XFCE + VNC + tinyproxy). The image boots to
CDP-ready in ~0.85-1.37s on local NVMe (Apple Silicon, warm image), but ~30x
slower on Fly's HDD-backed rootfs. Disk I/O of the rootfs is the dominant
factor, so every result records the storage type.

## Layout

    bench/
      REPORT.md              aggregated results + recommendation
      README.md              this file
      lib.sh                 shared timing harness (now_ms, percentile, bench_main, bp_* contract)
      <platform>/            one self-contained directory per platform
        <platform>.sh        bp_* hooks + entry point
        lib.sh               copy of the harness used during that run (kept for reproducibility)
        REPORT-<platform>.md per-platform writeup
        run-*.log            raw output captured during the run

Completed: `fly/`, `modal/`, `daytona/`, `e2b/`. Pending: `hetzner-local/`,
`northflank/`, `koyeb/`, `cloudflare/`.

## What each run measures

Wall-clock from the trigger action (start/resume) until CDP is ready:
`curl <cdp-endpoint>/json/version` responds with 200. VNC/noVNC readiness is
out of scope. Reported as p50/p95/min/max over COUNT runs. For platforms with
a pause/suspend/snapshot path, run both `MODE=cold` and `MODE=resume`.

## Common env knobs

    MODE=cold|resume   which start path to measure (default cold)
    COUNT=10           number of timed runs
    READY_TIMEOUT=90   per-run seconds before a run counts as TIMEOUT
    POLL_INTERVAL=0.2  seconds between readiness polls
    PLATFORM=<name>    label in output (set by each platform script)

The harness needs bash 5+ (uses `$EPOCHREALTIME` for sub-second timing, falls
back to perl `Time::HiRes`). macOS users may need `brew install bash` and to
invoke scripts with `/opt/homebrew/bin/bash bench/...`.

## Per-platform credentials

Put platform credentials in `bench/.env` (gitignored). Example:

    # bench/.env
    MODAL_TOKEN_ID=ak-...
    MODAL_TOKEN_SECRET=as-...
    DAYTONA_API_KEY=dtn_...
    E2B_API_KEY=e2b_...
    E2B_TEMPLATE=<template id printed by e2b_build.py>
    HCLOUD_TOKEN=...

The Python-driven benches (modal, daytona, e2b) source `bench/.env` automatically;
the Fly bench relies on `flyctl auth`. Override with `ENV_FILE=/path/to/.env` if
your `.env` lives elsewhere.

| Platform                                            | Required env vars                              | Notes |
| --------------------------------------------------- | ---------------------------------------------- | ----- |
| [fly][fly]                                          | `flyctl` authenticated                          | Pass `FLY_APP=<non-production-app>` to the script. |
| [modal][modal]                                      | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`          | Stock s6 image does not boot on Modal; the bench uses an adapted launch. |
| [daytona][daytona]                                  | `DAYTONA_API_KEY`                               | Image needs an entrypoint override (`unshare --pid --fork --mount-proc /init`) baked in. |
| [e2b][e2b]                                          | `E2B_API_KEY`                                   | Template is built via `e2b_build.py` (SDK Build System 2.0). `E2B_ACCESS_TOKEN` is needed only for `e2b template delete`. |
| [northflank][northflank]                            | Northflank API token / CLI                     | Pending; free-tier tokens cannot provision (HTTP 409). |
| [koyeb][koyeb]                                      | Koyeb API token                                 | Pending. |
| [cloudflare][cloudflare-containers]                 | Cloudflare account, Workers Paid + `wrangler`  | Pending. |
| [hetzner-local][hetzner]                            | `HCLOUD_TOKEN`                              | Pending. Provisions and destroys a Cloud VM per run. |

[fly]: https://fly.io
[modal]: https://modal.com
[daytona]: https://www.daytona.io
[e2b]: https://e2b.dev
[hetzner]: https://www.hetzner.com/cloud/
[northflank]: https://northflank.com
[koyeb]: https://www.koyeb.com
[cloudflare-containers]: https://developers.cloudflare.com/containers/

## Reproducing the committed results

All commands assume the repo root and bash 5+. Set up `.env` first (see above).

### [Fly.io][fly]

    FLY_APP=chrome-live-bench MODE=cold   COUNT=10 bash bench/fly/fly.sh
    FLY_APP=chrome-live-bench MODE=resume COUNT=10 bash bench/fly/fly.sh

The script creates one machine on `FLY_APP`, drives stop/start (cold) or
suspend/start (resume) between runs, and destroys it on teardown. Set `FLY_APP`
to a non-production app; the script refuses to touch any image whose name
contains `keep-chrome-live`. Raw output is captured to
`bench/fly/cold-results.txt` and `bench/fly/resume-results.txt`.

### [Modal][modal]

    uv venv bench/modal/.venv
    uv pip install --python bench/modal/.venv/bin/python modal
    MODE=cold   COUNT=10 bash bench/modal/modal.sh
    MODE=resume COUNT=10 bash bench/modal/modal.sh

`bench/modal/modal.sh` loads `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` from `.env`
and drives a long-lived Python helper (`modal_bench.py`) that creates the
sandbox, exposes tunnels, snapshots/restores, and tears down. See
`bench/modal/REPORT-modal.md` for the s6 PID-1 issue and how the adapted boot is
wired.

### [Daytona][daytona]

    uv venv bench/daytona/.venv
    uv pip install --python bench/daytona/.venv/bin/python daytona
    MODE=resume COUNT=10 bash bench/daytona/daytona.sh
    MODE=cold   COUNT=10 bash bench/daytona/daytona.sh

The script reads `DAYTONA_API_KEY` from `bench/.env` by default; override with
`ENV_FILE=/path/to/.env` if your `.env` lives elsewhere.
`daytona_ctl.py` is a coprocess driver to avoid per-trigger interpreter boot in
the timed path. `MODE=resume` is stop -> start; `MODE=cold` is stop + archive ->
start. See `bench/daytona/REPORT-daytona.md` for the entrypoint override
(`unshare`).

### [E2B][e2b]

    uv venv bench/e2b/.venv
    uv pip install --python bench/e2b/.venv/bin/python e2b-code-interpreter
    # Put E2B_API_KEY (and later E2B_TEMPLATE) in bench/.env; the script auto-loads it.
    bench/e2b/.venv/bin/python bench/e2b/e2b_build.py    # prints TEMPLATE_ID

Then:

    E2B_TEMPLATE=<id> MODE=cold   COUNT=10 READY_TIMEOUT=180 bash bench/e2b/e2b.sh
    E2B_TEMPLATE=<id> MODE=resume COUNT=10 READY_TIMEOUT=180 bash bench/e2b/e2b.sh

E2B's template build is the slow step (one-time, image is baked into a
Firecracker snapshot). Subsequent sandbox creates are ~0.4s of restore + boot.
CDP is measured intra-sandbox because E2B's public proxy rewrites the `Host`
header in a way Chrome's DevTools endpoint rejects.

## Adding a new platform

1. Create `bench/<platform>/` with `<platform>.sh` defining the `bp_*` hooks
   (see `bench/lib.sh` for the contract) and calling `bench_main`. Copy
   `bench/fly/fly.sh` as a template.
2. Implement `bp_setup` to provision and wait for the first full boot. Tear
   everything down in `bp_teardown`.
3. Record the rootfs storage type in the script's header comment, and run the
   full suite (`MODE=cold` and `MODE=resume`) with `COUNT>=10`.
4. Write `REPORT-<platform>.md` to the same shape as the existing four, then
   add a row to `REPORT.md`.
