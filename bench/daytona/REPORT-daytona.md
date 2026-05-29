# chrome-live startup latency on Daytona

Run: re-measured 2026-05-28 PDT with the cleaned-up harness (CDP-only result,
noVNC informational). See `run-*.log` headers for exact stamps. Earlier 2026-05-27
numbers in this document are kept where they cover qualitative findings; the
results table below reflects the rerun.

One of 8 parallel platform validations. Numbers are real (live sandbox, $200
credit), measured wall-clock from start-trigger to readiness at Daytona's public
preview endpoints.

## Environment

| Property         | Value                                                                   |
| ---------------- | ----------------------------------------------------------------------- |
| Platform         | Daytona (app.daytona.io, daytonaproxy01.net)                            |
| Arch             | x86_64 / amd64                                                          |
| Tier (requested) | 2 vCPU / 2 GiB / 10 GiB disk                                            |
| Host observed    | 64 vCPU, ~755 GiB RAM shared host; sandbox capped to the requested tier |
| Rootfs storage   | overlayfs on `/var/lib/docker/overlay2`, backed by NVMe SSD (md RAID; `nvme0-3`, `rotational=0`) |
| Image            | `ghcr.io/remotebrowser/chrome-live@sha256:d977214a...` (amd64 digest; `:latest` is rejected by Daytona) |
| Chrome           | Chrome/148.0.7778.167, CDP protocol 1.3                                 |

The critical storage datapoint: rootfs is local NVMe, not network storage. So disk I/O is
NOT the latency bottleneck here (contrast with Fly's HDD rootfs).

Latency breakdown (measured, 3 resume cycles, in `bench/decompose` evidence below):

| Component                                                | Time      |
| -------------------------------------------------------- | --------- |
| `start()` infra (schedule + container start -> `started`) | ~1.1s     |
| App boot in-container (s6 + Chrome -> CDP on localhost)   | ~0.5-0.9s (in-sandbox probe, includes some exec overhead) |
| Daytona public preview proxy reattach/routing            | ~3.0-4.0s |
| Total: trigger -> CDP 200 at the public preview URL      | ~5.2-6.0s |

The dominant cost is NOT disk, app boot, or container scheduling. The app is reachable
internally (`localhost:9222`) at ~2s, but the public preview endpoint
(`https://9222-<id>.daytonaproxy01.net`) does not serve 200 until ~5.3s. The extra ~3s is
the public preview proxy re-establishing a route to the freshly started sandbox. A client
that reaches the sandbox without the public proxy (SSH tunnel, private network, or the SDK
transport) sees effective readiness ~2s; the ~5s figure is specific to the preview endpoint
the methodology specified.

## Results (n=10 per mode)

PRIMARY signal is CDP (`curl <preview:9222>/json/version` == 200). noVNC is secondary
(HTTP 200 on the `:8080` forwarder, since `:80` is outside Daytona's previewable 3000-9999
range). All times are milliseconds, trigger -> ready, measured at the preview endpoint.

Resume mode (stop -> start; filesystem stays on fast local storage):

| Signal           | p50  | p95  | min  | max  |
| ---------------- | ---- | ---- | ---- | ---- |
| CDP (result)     | 4371 | 4849 | 4265 | 4849 |
| noVNC (informational) | per-run ~4.8s, see run-resume.log |

Cold mode (stop + archive -> start; filesystem restored from object storage):

| Signal           | p50  | p95  | min  | max  |
| ---------------- | ---- | ---- | ---- | ---- |
| CDP (result)     | 4550 | 5310 | 4293 | 5310 |
| noVNC (informational) | per-run ~5.0s, see run-cold.log |

Observations:
- Cold (archive restore) is barely slower than resume. The archived filesystem is small and
  restored to NVMe; archive/restore is not the dominant cost.
- The one noVNC 11.3s outlier in resume mode was a single slow forwarder/proxy warm-up; CDP
  on that same run was normal (~5.5s).
- First-ever create (image pull + declarative build layer): ~30-33s, one-time.

## Acceptance

Target: ready < ~1s, acceptance p50 <= ~1.5s. Local reference 0.85-1.37s (warm Docker, NVMe).

FAIL at the preview endpoint. CDP p50 is 5.0s (resume) / 5.3s (cold), ~3.5-4x over the gate.
Per the breakdown above, ~3s of that is the public preview proxy, not compute/disk/app:
internal CDP readiness is ~2s. Daytona stop/start is a full container restart, not a
memory-snapshot resume, so there is no sub-second path for this workload; but if the consumer
avoids the public preview proxy, ~2s is achievable.

## Pricing

Confirmed rates: $0.0504/vCPU-hr + $0.0162/GiB-hr, $0 base fee, $200 signup credit.
Storage $0.000108/GiB-hr after 5 free GiB.

| Item                         | Value                                             |
| ---------------------------- | ------------------------------------------------- |
| Compute, 2 vCPU / 2 GiB      | 2*0.0504 + 2*0.0162 = $0.1332 / container-hr      |
| Base fee                     | $0                                                |
| Monthly @ 100h running       | $13.32 compute                                    |
| Monthly @ 1000h running      | $133.20 compute                                   |
| Storage (10 GiB disk)        | (10 - 5 free) * 0.000108 = $0.00054/hr ~= $0.39/mo if kept 720h |
| Scales to zero               | Yes for compute: $0 compute while stopped/archived. Storage still billed (disk persists; archived state in object storage). |

Net: cheap and genuinely scale-to-zero on compute, with no base fee. The cost question is
not money, it is the ~5s cold/resume latency.

## Blockers and integration notes

1. Entrypoint override (REQUIRED workaround). Daytona replaces the image ENTRYPOINT with its
   own daemon; a plain create-from-image runs `daytona sleep infinity` and chrome-live never
   boots (s6/Chrome/noVNC absent). Fix: declare the entrypoint on the sandbox image. s6-overlay
   refuses to run unless PID 1, so it must be given a nested PID namespace:
   `["/usr/bin/unshare","--pid","--fork","--mount-proc","/init"]` (the sandbox has
   CAP_SYS_ADMIN). This mirrors chrome-live's own Fly.io path in `start-init.sh`. With this,
   PID 1 becomes `daytona /usr/bin/unshare --pid --fork --mount-proc /init` and the stack boots
   natively on every start/resume.
2. noVNC port not previewable. `:80` is outside Daytona's 3000-9999 preview range. Baked a
   socat `:8080 -> :80` s6 longrun into the image (compiled by s6 at every boot, survives
   resume) and preview `:8080`. CDP `:9222` is in range and needs no workaround.
3. `:latest` tag rejected by Daytona for images/snapshots; must pin a tag or digest.
4. No API key blocker hit: key was provided in `.env`; all numbers above are live.

## Reproduce

Files (left unstaged):
- `bench/lib.sh`         generic harness (now_ms, percentile, _wait_ready, bench_main, hook contract)
- `bench/daytona.sh`     Daytona bp_* hooks (entry point to run)
- `bench/daytona_ctl.py` persistent SDK controller driven as a coprocess (avoids per-trigger
                         interpreter boot polluting the timed path)
- `bench/run-resume.log`, `bench/run-cold.log`, `bench/storage-info.txt` raw output

Run (needs `DAYTONA_API_KEY` in `/Users/bin/dev/chrome-live/.env` and the `.venv-bench` venv
with the `daytona` package):

```
MODE=resume COUNT=10 bash bench/daytona.sh
MODE=cold   COUNT=10 bash bench/daytona.sh
```

Hook -> Daytona mapping: bp_make_paused = stop; bp_make_cold = stop + archive;
bp_trigger = start (timed); bp_ready_cdp/novnc = curl preview :9222 / :8080;
bp_teardown = delete (+ EXIT trap). Sandbox is public, auto_stop disabled during the run.
