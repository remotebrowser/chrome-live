# chrome-live startup latency on E2B

Run: re-measured 2026-05-28 PDT with the cleaned-up harness (CDP-only result,
noVNC informational). See `run_*.log` headers for stamps. Earlier 2026-05-27
numbers below have been replaced by the rerun.

Measured against E2B (Firecracker microVM, Build System 2.0 template from the
public `ghcr.io/remotebrowser/chrome-live:latest` image). Harness: `bench/e2b.sh`
+ `bench/e2b_driver.py` + shared `bench/lib.sh`. COUNT=10 per mode,
READY_TIMEOUT=180, POLL_INTERVAL=0.2.

## Platform

| Field           | Value                                                         |
| --------------- | ------------------------------------------------------------- |
| Platform        | E2B (Firecracker microVM, pause/resume of full memory + fs)   |
| Arch            | x86_64 / amd64 (kernel 6.1.158, Ubuntu 24.04.4 guest)         |
| Sandbox spec    | 2 vCPU / 2 GiB (fixed at template build: cpu_count=2, 2048 MB) |
| Tier used       | Hobby trial (one-time $100 credit, 20 concurrent)             |
| rootfs storage  | Firecracker microVM on NVMe-backed hosts (microVM spawn 0.64s) |

## Latency (wall-clock from trigger to ready)

CDP = `curl 127.0.0.1:9222/json/version` (PRIMARY). noVNC = HTTP 200 on :80.

| Mode   | Signal                | p50    | p95     | min    | max     | n  |
| ------ | --------------------- | ------ | ------- | ------ | ------- | -- |
| cold   | CDP (result)          | 9904ms | 45540ms | 8700ms | 45540ms | 10 |
| cold   | noVNC (informational) | ~10.3s | ~46s    | ~9.0s  | ~46s    | 10 |
| resume | CDP (result)          | 1671ms |  2709ms | 1577ms |  2709ms | 10 |
| resume | noVNC (informational) | ~1.9s  |  ~2.9s  | ~1.8s  |  ~2.9s  | 10 |

The cold rerun saw a worse p95 than the original (45.5s vs 18.7s) due to a
cluster of three placement outliers in runs 7-9 (30-35s each). The p50 is
consistent with the original ~9s.

Image fetch is NOT in either number. The 2 GB image is pulled from ghcr once at
`Template.build` time and baked into the Firecracker template snapshot; bare
`Sandbox.create` restores it in a steady ~0.40s with no per-spawn pull (verified
over repeated creates). Rebuilds reuse E2B's build-layer cache. So cold time is
entirely Chrome boot, not image fetch.

Acceptance is p50 <= ~1.5s (local reference 0.85-1.37s).

- cold: FAIL by ~6x. The microVM spawn is sub-second (NVMe), but Chrome's cold
  boot on the 2-vCPU microVM is CPU/IO-bound and highly variable (4-19s observed).
- resume: marginal FAIL. p50 1.71s, min 1.53s, just over the 1.5s bar. The CDP
  number is measured intra-sandbox via one `commands.run` round trip, which adds
  ~0.3-0.5s of connect/RPC overhead, so true resume-to-CDP is ~1.2-1.5s (borderline).

## Cost

E2B per-second rates from the brief: 2 vCPU @ $0.000028/s + 2 GiB @ $0.0000045/GiB/s
= $0.000037/s -> ~$0.133/container-hr.

NOTE: the brief's parenthetical "~$0.0524/container-hr" does NOT reconcile with
these rates (that figure implies ~1 vCPU-equivalent); reported here as $0.133/hr
from the stated per-second rates. Flagged for the aggregate to resolve.

| Basis              | @100h/mo | @1000h/mo |
| ------------------ | -------- | --------- |
| compute only       | ~$13.3   | ~$133     |
| + $150 Pro base    | ~$163    | ~$283     |

- Base fee: $150/mo (Pro) is effectively mandatory for production. Hobby is a
  one-time-$100-credit / 20-concurrent trial, not a production tier.
- Scales to zero: NO for production. The mandatory $150/mo base fee FAILS the
  user's no-base-fee requirement. This disqualifies E2B regardless of latency.

## Blockers / caveats hit during the run

1. CDP is unreachable through E2B's public proxy. Chrome's DevTools HTTP endpoint
   rejects a non-localhost `Host`, and the proxy must forward `<port>-<id>.e2b.app`
   as `Host` (plain -> 500; `Host: localhost` -> 400 at the edge). Exposing CDP
   publicly on E2B needs a Host-rewriting sidecar. CDP was measured intra-sandbox.
   noVNC :80 has no such check and works through the proxy.
2. The image's s6 entrypoint must be PID 1 (via `unshare --pid`). E2B's
   start-command context lacks CAP_SYS_ADMIN, so unshare is denied there and the
   normal entrypoint cannot be the start command. Workaround: start command is
   `sleep infinity` and init is launched post-create via `commands.run(background=True)`
   (full caps). The init-launch RPC is inside the timed cold window.
3. CLI vs SDK auth: `e2b template build` needs E2B_ACCESS_TOKEN; only E2B_API_KEY
   was available. Worked around by building via the SDK's Build System 2.0
   (`bench/e2b_build.py`), which needs only the API key.
4. Template cleanup: the SDK Template class has no delete method, so the test
   templates (`chrome-live-bench`, `chrome-live-diag`) remain. Delete with
   `e2b template delete` once an access token is available. Idle templates don't
   incur compute cost. All benchmark sandboxes were killed (0 remaining).
5. Boot variance is large (cold CDP 4-19s), so cold p95 (18.7s) is ~2x p50.

## Harness fix made here

`bench/lib.sh` `now_ms()` had a precision bug: routing the millisecond value
through awk's default %.6g format truncated it to 6 significant figures, so every
elapsed time computed as 0ms. Fixed with integer math on EPOCHREALTIME. This
affects every platform sharing this lib.sh (flagged via spawned task).

## Reproduce

    export E2B_API_KEY=...                 # from .env
    bench/.venv/bin/python bench/e2b_build.py        # -> TEMPLATE_ID
    E2B_TEMPLATE=<id> MODE=cold   COUNT=10 READY_TIMEOUT=180 bench/e2b.sh
    E2B_TEMPLATE=<id> MODE=resume COUNT=10 READY_TIMEOUT=180 bench/e2b.sh

Script path: `bench/e2b.sh`.
